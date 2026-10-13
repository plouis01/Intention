#!/usr/bin/env python3
"""Conservative local batch enrichment for 4 priority fields.

Fields handled:
- curated_quotes (missing only)
- jurisprudence (missing/hollow only; strict verified references)
- modifications.objet (only when source law text explicitly mentions target article)
- rapports_parlementaires (missing only; direct dossier URLs)

No external API calls.
"""

from __future__ import annotations

import argparse
import html as html_mod
import json
import re
import sqlite3
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "data" / "article_modifications.json"
MENTION_PATH = ROOT / "data" / "cgi_mention_index.json"
DB_PATH = ROOT / "data" / "legislateur.db"

PROCEDURAL_PATTERNS = (
    "la parole est",
    "je mets aux voix",
    "scrutin public",
    "ordre du jour",
    "seance est levee",
    "seance est suspendue",
    "la seance est reprise",
    "l'amendement est adopte",
    "l'amendement n'est pas adopte",
    "amendement n°",
    "amendement n ",
    "vote est reserve",
    "article additionnel",
)

QUOTE_INTENT_MARKERS = (
    "vise",
    "objectif",
    "permet",
    "clarif",
    "securis",
    "evit",
    "anti-abus",
    "renforc",
    "simplif",
    "releve",
    "abaisse",
    "seuil",
    "taux",
    "exoner",
    "credit d'impot",
    "abrog",
    "reecri",
    "complete",
    "modifie",
    "corrige",
)

JURIS_CASE_MARKERS = (
    "jurisprudence",
    "decision",
    "arret",
    "pourvoi",
    "qpc",
    "a juge",
    "a estime",
    "a confirme",
)

JURIS_QPC_RE = re.compile(r"\b([12][0-9]{3}-[0-9]{2,4}\s*QPC)\b", re.IGNORECASE)
JURIS_NUM_RE = re.compile(r"\bn[°ºo]\s*([0-9][0-9A-Za-z./-]{2,})", re.IGNORECASE)


@dataclass
class Stats:
    objets_filled: int = 0
    objets_refined: int = 0
    rapports_filled: int = 0
    juris_replaced: int = 0
    juris_resume_backfilled: int = 0
    quotes_filled: int = 0


def normalize(text: str) -> str:
    text = html_mod.unescape(text or "").lower()
    text = "".join(
        c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn"
    )
    return re.sub(r"\s+", " ", text).strip()


def clean_space(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def is_session_chair(fonction: str) -> bool:
    f = normalize(fonction)
    if "president" not in f:
        return False
    if "commission" in f or "rapporteur" in f:
        return False
    return True


def is_chair_orateur(orateur: str) -> bool:
    o = normalize(orateur)
    return o.startswith("m. le president") or o.startswith("mme la presidente")


def is_procedural(snippet: str) -> bool:
    n = normalize(snippet)
    if len(n) < 90:
        return True
    return any(p in n for p in PROCEDURAL_PATTERNS)


def ref_pattern(ref: str) -> re.Pattern[str]:
    parts: list[str] = []
    for token in (ref or "").split():
        token_pat = re.escape(token).replace(r"\-", r"\s*[-\u2011]?\s*")
        parts.append(token_pat)
    expr = r"\s+".join(parts) if parts else re.escape(ref)
    return re.compile(rf"(?:l['’]\s*)?articles?\s+{expr}\b", re.IGNORECASE)


def has_explicit_cgi_context(ref: str, snippet: str) -> bool:
    n = normalize(snippet)
    rp = ref_pattern(ref)
    if not rp.search(n):
        return False

    parts: list[str] = []
    for token in (ref or "").split():
        token_pat = re.escape(token).replace(r"\-", r"\s*[-\u2011]?\s*")
        parts.append(token_pat)
    expr = r"\s+".join(parts)
    pat_cgi = re.compile(
        rf"(?:l['’]\s*)?articles?\s+{expr}\b[^.\n]{{0,70}}\bdu\s+code\s+general\s+des\s+impots\b",
        re.IGNORECASE,
    )
    pat_cgi_short = re.compile(
        rf"(?:l['’]\s*)?articles?\s+{expr}\b[^.\n]{{0,70}}\bcgi\b",
        re.IGNORECASE,
    )
    cgi_modified = "code general des impots est ainsi modifie" in n
    return bool(pat_cgi.search(n) or pat_cgi_short.search(n) or cgi_modified)


def has_ref_cgi_phrase(ref: str, snippet: str) -> bool:
    n = normalize(snippet)
    parts: list[str] = []
    for token in (ref or "").split():
        token_pat = re.escape(token).replace(r"\-", r"\s*[-\u2011]?\s*")
        parts.append(token_pat)
    expr = r"\s+".join(parts)
    pat_cgi = re.compile(
        rf"(?:l['’]\s*)?articles?\s+{expr}\b[^.\n]{{0,70}}\bdu\s+code\s+general\s+des\s+impots\b",
        re.IGNORECASE,
    )
    pat_cgi_short = re.compile(
        rf"(?:l['’]\s*)?articles?\s+{expr}\b[^.\n]{{0,70}}\bcgi\b",
        re.IGNORECASE,
    )
    return bool(pat_cgi.search(n) or pat_cgi_short.search(n))


def quote_score(snippet: str) -> int:
    n = normalize(snippet)
    return sum(1 for k in QUOTE_INTENT_MARKERS if k in n)


def extract_clause(text: str, match: re.Match[str]) -> str:
    """Extract the nearest legal clause around a matched article reference."""
    s = clean_space(text)
    if not s:
        return ""

    matched = clean_space(match.group(0))
    idx = s.lower().find(matched.lower())
    if idx < 0:
        idx = max(0, min(len(s) - 1, match.start()))

    prev = max(s.rfind(".", 0, idx), s.rfind(";", 0, idx), s.rfind(":", 0, idx))
    start = 0 if prev < 0 else prev + 1

    end = -1
    for pos in range(idx, len(s)):
        ch = s[pos]
        if ch == ";":
            end = pos + 1
            break
        if ch == ".":
            prev = s[max(0, pos - 6) : pos].lower()
            # Do not cut on abbreviations like "Art."
            if prev.rstrip().endswith("art"):
                continue
            end = pos + 1
            break
    if end == -1:
        end = min(len(s), idx + 320)

    clause = s[start:end].strip(" -")
    if len(clause) < 45:
        next2 = s.find(".", end)
        if next2 != -1 and next2 - end < 220:
            clause = s[start : next2 + 1].strip(" -")

    if len(clause) > 420:
        clause = clause[:417].rstrip() + "..."
    return clause


def detect_jurisdiction(snippet: str) -> str | None:
    n = normalize(snippet)
    if " qpc" in n:
        return "CC"
    if "conseil d'etat" in n or "conseil d etat" in n or " ce," in n:
        return "CE"
    if "cour de cassation" in n or "cass." in n or "cassation" in n:
        return "Cass."
    if "conseil constitutionnel" in n:
        return "CC"
    if "cjue" in n or "cour de justice de l'union europeenne" in n:
        return "CJUE"
    if "cedh" in n or "cour europeenne des droits de l'homme" in n:
        return "CEDH"
    return None


def extract_verified_juris_ref(snippet: str, jurisdiction: str | None) -> str | None:
    n = normalize(snippet)

    # Require litigation context to avoid law numbers (e.g. n° 82-1153 law references).
    if not any(m in n for m in JURIS_CASE_MARKERS):
        return None

    qpc = JURIS_QPC_RE.search(snippet)
    if qpc:
        return qpc.group(1).upper().replace("  ", " ")

    # For Conseil constitutionnel we only keep QPC-style references.
    if jurisdiction == "CC":
        return None

    m = JURIS_NUM_RE.search(snippet)
    if not m:
        return None

    raw = m.group(1)
    raw_clean = raw.strip()

    # Filter likely law numbers unless QPC.
    if "-" in raw_clean and "/" not in raw_clean:
        return None

    digits = re.sub(r"\D", "", raw_clean)
    if jurisdiction in {"CE", "Cass.", "CJUE", "CEDH"} and len(digits) < 5:
        return None

    return f"n° {raw_clean}"


def has_verified_juris(juris: list[dict]) -> bool:
    if not juris:
        return False
    for entry in juris:
        if not isinstance(entry, dict):
            continue
        ref = (entry.get("reference") or "").strip().lower()
        titre = (entry.get("titre") or "").strip().lower()
        if (
            ref
            and ref not in {"n/d", "nd", "reference non precisee", "reference non précisée en débat"}
            and "aucune jurisprudence" not in titre
            and "reference jurisprudentielle citee en debat parlementaire" not in titre
        ):
            return True
    return False


def run(apply_changes: bool) -> None:
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    mention_index = json.loads(MENTION_PATH.read_text(encoding="utf-8"))

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    law_by_num = {r["numero"]: r["id"] for r in conn.execute("SELECT id, numero FROM lois")}
    article_text = {
        (r["loi_id"], r["numero_article"]): r["contenu_texte"]
        for r in conn.execute("SELECT loi_id, numero_article, contenu_texte FROM articles")
    }

    dossiers_by_law: dict[str, list[dict]] = defaultdict(list)
    for row in conn.execute(
        """
        SELECT l.numero AS law_numero, d.chambre, d.url_source
        FROM dossiers_legislatifs d
        JOIN lois l ON l.id = d.loi_id
        WHERE d.url_source IS NOT NULL AND trim(d.url_source) <> ''
        """
    ):
        dossiers_by_law[row["law_numero"]].append(
            {"chambre": row["chambre"], "url": row["url_source"]}
        )

    stats = Stats()
    obj_samples: list[tuple[str, str, str, str]] = []
    rap_samples: list[tuple[str, int, dict]] = []
    juris_samples: list[tuple[str, dict]] = []
    quote_samples: list[tuple[str, str, str, str, str]] = []

    for ref, article in data.get("articles", {}).items():
        # 1) Fill modifications.objet (strict)
        rp = ref_pattern(ref)
        for mod in article.get("modifications", []):
            law_numero = mod.get("law_numero")
            law_id = law_by_num.get(law_numero)
            if not law_id:
                continue
            law_article = (mod.get("article_in_law") or "").replace("Art.", "").strip()
            text = article_text.get((law_id, law_article))
            if not text:
                continue
            match = rp.search(text)
            if not match:
                continue
            current_objet = clean_space(mod.get("objet"))
            clause = extract_clause(text, match)
            if len(clause) < 25:
                continue

            if not current_objet:
                mod["objet"] = clause
                stats.objets_filled += 1
                if len(obj_samples) < 8:
                    obj_samples.append((ref, law_numero, law_article, clause))
                continue

            # Refine truncated placeholders ending with "Art."
            if (
                current_objet.endswith("Art.")
                or current_objet.endswith("« Art.")
                or current_objet.endswith("« Art")
            ):
                if not (clause.endswith("Art.") or clause.endswith("« Art.")) and len(clause) > len(current_objet) + 10:
                    mod["objet"] = clause
                    stats.objets_refined += 1

        # 2) Fill rapports_parlementaires (missing only)
        if not article.get("rapports_parlementaires"):
            entries: list[dict] = []
            seen: set[tuple[str, int | None, str]] = set()
            for mod in sorted(
                article.get("modifications", []),
                key=lambda m: (m.get("law_year") or 0, m.get("law_numero") or ""),
            ):
                law_numero = mod.get("law_numero")
                if law_numero not in dossiers_by_law:
                    continue
                for drow in dossiers_by_law[law_numero]:
                    chamber = drow["chambre"]
                    type_label = "AN" if chamber == "AN" else ("Sénat" if chamber == "SENAT" else chamber)
                    key = (type_label, mod.get("law_year"), drow["url"])
                    if key in seen:
                        continue
                    seen.add(key)
                    law_title = (mod.get("law_title") or "").strip() or f"Loi {law_numero}"
                    entries.append(
                        {
                            "type": type_label,
                            "year": mod.get("law_year"),
                            "title": f"{law_title} (dossier {type_label})",
                            "url": drow["url"],
                            "rapporteur": "",
                        }
                    )
            if entries:
                entries.sort(key=lambda e: ((e.get("year") or 0), e.get("type") or ""))
                article["rapports_parlementaires"] = entries
                stats.rapports_filled += 1
                if len(rap_samples) < 8:
                    rap_samples.append((ref, len(entries), entries[0]))

        # 3) Replace missing/hollow jurisprudence with strict verified refs
        current_juris = article.get("jurisprudence") or []
        needs_juris = (not current_juris) or (not has_verified_juris(current_juris))
        if needs_juris:
            entries: list[dict] = []
            seen: set[tuple[str, str]] = set()
            rp = ref_pattern(ref)
            for mention in sorted(
                mention_index.get(ref, []),
                key=lambda m: m.get("debat_date", ""),
                reverse=True,
            ):
                snippet = clean_space(mention.get("snippet") or "")
                if len(snippet) < 80:
                    continue
                if is_session_chair(mention.get("fonction", "")):
                    continue
                if not rp.search(normalize(snippet)):
                    continue
                jurisdiction = detect_jurisdiction(snippet)
                if not jurisdiction:
                    continue
                reference = extract_verified_juris_ref(snippet, jurisdiction)
                if not reference:
                    continue
                key = (jurisdiction, reference)
                if key in seen:
                    continue
                seen.add(key)

                title = "Décision juridictionnelle citée en débat parlementaire"
                if jurisdiction == "CC" and "QPC" in reference.upper():
                    title = "Décision QPC citée en débat parlementaire"

                entries.append(
                    {
                        "juridiction": jurisdiction,
                        "date": mention.get("debat_date", "") or "",
                        "reference": reference,
                        "titre": title,
                        "resume": snippet[:300],
                        "faits": snippet[:320],
                        "solution": "La référence est mobilisée dans le débat pour éclairer l'interprétation de l'article.",
                        "portee": "Vérification de la décision source recommandée avant usage contentieux.",
                    }
                )
                if len(entries) >= 2:
                    break

            if entries:
                article["jurisprudence"] = entries
                stats.juris_replaced += 1
                if len(juris_samples) < 8:
                    juris_samples.append((ref, entries[0]))

        # 4) Fill curated_quotes (missing only; strict context)
        existing_quotes = article.get("curated_quotes") or {}
        has_quotes = False
        if isinstance(existing_quotes, dict):
            has_quotes = any(isinstance(v, list) and v for v in existing_quotes.values())
        elif isinstance(existing_quotes, list):
            has_quotes = bool(existing_quotes)

        if not has_quotes:
            best = None
            best_score = -1
            best_date = ""
            for mention in sorted(
                mention_index.get(ref, []),
                key=lambda m: m.get("debat_date", ""),
                reverse=True,
            ):
                snippet = clean_space(mention.get("snippet") or "")
                if not snippet:
                    continue
                if is_procedural(snippet):
                    continue
                if is_session_chair(mention.get("fonction", "")):
                    continue
                if is_chair_orateur(mention.get("orateur", "")):
                    continue
                if not has_explicit_cgi_context(ref, snippet):
                    continue

                score = quote_score(snippet)
                explicit_ref_cgi = has_ref_cgi_phrase(ref, snippet)
                fonction_norm = normalize(mention.get("fonction", ""))
                high_signal_speaker = (
                    ("rapporteur" in fonction_norm)
                    or ("ministre" in fonction_norm)
                    or ("secretaire d" in fonction_norm)
                )
                if score <= 0 and not explicit_ref_cgi and not high_signal_speaker:
                    continue

                date = mention.get("debat_date", "") or ""
                if score > best_score or (score == best_score and date > best_date):
                    best_score = score
                    best_date = date
                    best = mention

            if best:
                year = (best.get("debat_date") or "")[:4] or "N/D"
                quote = {
                    "citation": clean_space(best.get("snippet", ""))[:420],
                    "orateur": best.get("orateur", "") or "",
                    "fonction": best.get("fonction", "") or "",
                    "date": best.get("debat_date", "") or "",
                    "chambre": best.get("chambre", "") or "",
                }
                article["curated_quotes"] = {year: [quote]}
                stats.quotes_filled += 1
                if len(quote_samples) < 8:
                    quote_samples.append(
                        (
                            ref,
                            year,
                            quote["orateur"],
                            quote["fonction"],
                            quote["citation"][:140],
                        )
                    )

        # 5) Backfill jurisprudence.resume for verified entries when absent.
        for entry in article.get("jurisprudence") or []:
            if not isinstance(entry, dict):
                continue
            ref_val = (entry.get("reference") or "").strip().lower()
            title_val = (entry.get("titre") or "").strip().lower()
            if (
                not ref_val
                or ref_val in {"n/d", "nd", "reference non precisee", "reference non précisée en débat"}
                or "aucune jurisprudence" in title_val
                or "reference jurisprudentielle citee en debat parlementaire" in title_val
            ):
                continue
            if clean_space(entry.get("resume")):
                continue

            resume = clean_space(entry.get("solution") or "")
            if not resume:
                resume = clean_space(entry.get("faits") or "")
            if not resume:
                resume = clean_space(entry.get("titre") or "")
            if resume:
                entry["resume"] = resume[:320]
                stats.juris_resume_backfilled += 1

    print(f"APPLY={apply_changes}")
    print(f"objets_filled={stats.objets_filled}")
    print(f"objets_refined={stats.objets_refined}")
    print(f"rapports_filled={stats.rapports_filled}")
    print(f"juris_replaced={stats.juris_replaced}")
    print(f"juris_resume_backfilled={stats.juris_resume_backfilled}")
    print(f"quotes_filled={stats.quotes_filled}")

    print("\nobj_samples:")
    for s in obj_samples:
        print(" -", s)

    print("\nrap_samples:")
    for s in rap_samples:
        print(" -", s)

    print("\njuris_samples:")
    for s in juris_samples:
        print(" -", s[0], s[1]["juridiction"], s[1]["reference"], s[1]["date"])

    print("\nquote_samples:")
    for s in quote_samples:
        print(" -", s)

    if apply_changes:
        DATA_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwritten={DATA_PATH}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Conservative local batch enrichment for 4 fields.")
    parser.add_argument("--apply", action="store_true", help="Write changes to data/article_modifications.json")
    args = parser.parse_args()
    run(apply_changes=args.apply)
