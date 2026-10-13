#!/usr/bin/env python3
"""Local (no external API) filler for missing curated article fields.

This script fills missing:
- synthese
- jurisprudence
- forward_looking
- bofip_url (fallback search URL)

It also audits curated quote removals against HEAD to flag potential
high-signal removals.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import unicodedata
from collections import Counter
from pathlib import Path
from urllib.parse import quote_plus


ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "data" / "article_modifications.json"
MENTION_PATH = ROOT / "data" / "cgi_mention_index.json"

PROCEDURAL_PATTERNS = (
    "la parole est",
    "je mets aux voix",
    "scrutin public",
    "ordre du jour",
    "prochaine seance",
    "l'amendement est adopte",
    "l'amendement est rejete",
)

# Keep this list short and high-signal: only terms that are too generic.
GENERIC_WORDS = {
    "article",
    "articles",
    "fiscal",
    "fiscale",
    "fiscaux",
    "fiscales",
    "impot",
    "impots",
    "taxe",
    "taxes",
    "revenu",
    "revenus",
    "regime",
    "regimes",
    "dispositions",
    "disposition",
    "mesure",
    "mesures",
    "code",
    "general",
    "france",
}

JURIS_REF_RE = re.compile(r"\b(?:n[°º]|no)\s*[0-9][0-9A-Za-z./-]*\b", re.IGNORECASE)
CASE_MARKER_RE = re.compile(r"\b(jurisprudence|decision(?:s)?|arret(?:s)?|pourvoi|qpc)\b")


def normalize_text(text: str) -> str:
    if not text:
        return ""
    text = text.lower()
    text = "".join(
        c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn"
    )
    return text


def clean_text(text: str, max_len: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rstrip() + "..."


def is_procedural(text: str) -> bool:
    norm = normalize_text(text)
    if len(norm.strip()) < 50:
        return True
    return any(p in norm for p in PROCEDURAL_PATTERNS)


def normalize_chamber(chambre: str) -> str:
    norm = normalize_text(chambre)
    if norm == "an":
        return "AN"
    if norm == "senat":
        return "Senat"
    return chambre or "?"


def is_session_chair(fonction: str) -> bool:
    """Filter low-signal session chair interventions."""
    f = normalize_text(fonction)
    if "president" not in f:
        return False
    if "commission" in f or "rapporteur" in f:
        return False
    return True


def build_specific_keywords(article: dict) -> list[str]:
    kws: set[str] = set()

    for kw in article.get("keywords", []):
        if not isinstance(kw, str):
            continue
        n = normalize_text(kw).strip()
        if len(n) < 6 or n.isdigit():
            continue
        if n.startswith("article ") or n.startswith("l'article "):
            continue
        if n in GENERIC_WORDS:
            continue
        kws.add(n)

    title = normalize_text(article.get("titre", ""))
    for token in re.findall(r"[a-z0-9]+", title):
        if len(token) >= 6 and token not in GENERIC_WORDS and not token.isdigit():
            kws.add(token)

    return sorted(kws)


def generate_synthese(ref: str, article: dict) -> dict:
    titre = (article.get("titre") or f"l'article {ref}").strip()
    mods = article.get("modifications", [])
    years = sorted(
        {
            m.get("law_year")
            for m in mods
            if isinstance(m.get("law_year"), int)
        }
    )
    nature_counts = Counter(
        (m.get("nature") or "Modification") for m in mods if isinstance(m, dict)
    )
    dominant_nature = (nature_counts.most_common(1)[0][0] if nature_counts else "adaptation").lower()

    if years:
        objectif = (
            f"L'article {ref} encadre {titre.lower()} et a ete ajuste entre {years[0]} et {years[-1]} "
            f"par {len(mods)} modifications (dominante: {dominant_nature}) afin de maintenir un cadre fiscal "
            "coherent, applicable et juridiquement stable."
        )
    else:
        objectif = (
            f"L'article {ref} encadre {titre.lower()} avec un objectif de securisation des regles fiscales "
            "et d'harmonisation des conditions d'application."
        )

    keywords = build_specific_keywords(article)
    concerns: list[str] = []
    for kw in keywords[:3]:
        concerns.append(f"Clarifier les conditions d'application relatives a {kw}.")

    defaults = [
        "Limiter les effets d'aubaine et les contournements.",
        "Assurer la lisibilite des obligations declaratives et de controle.",
        "Maintenir la coherence avec les autres mecanismes du CGI.",
    ]
    for d in defaults:
        if len(concerns) >= 3:
            break
        concerns.append(d)

    return {
        "objectif_constant": objectif,
        "preoccupations_recurrentes": concerns,
    }


def detect_jurisdiction(snippet: str) -> str | None:
    s = normalize_text(snippet)
    if "conseil constitutionnel" in s or "qpc" in s:
        return "CC"
    if "conseil d'etat" in s:
        return "CE"
    if "cour de cassation" in s:
        return "Cass."
    if "cour administrative d'appel" in s:
        return "CAA"
    if "cjue" in s or "cour de justice de l'union europeenne" in s:
        return "CJUE"
    return None


def generate_jurisprudence(ref: str, mentions: list[dict]) -> list[dict]:
    entries: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for m in mentions:
        snippet = m.get("snippet", "") or ""
        if len(snippet) < 40:
            continue
        if is_session_chair(m.get("fonction", "")):
            continue

        juridiction = detect_jurisdiction(snippet)
        if not juridiction:
            continue
        norm_snippet = normalize_text(snippet)
        has_case_marker = bool(CASE_MARKER_RE.search(norm_snippet))
        if not has_case_marker:
            continue

        ref_match = JURIS_REF_RE.search(snippet)
        if ref_match:
            reference_candidate = ref_match.group(0)
            digits = re.sub(r"\D", "", reference_candidate)
            reference = reference_candidate if len(digits) >= 5 else "reference non precisee"
        else:
            reference = "reference non precisee"
        key = (juridiction, reference)
        if key in seen:
            continue
        seen.add(key)

        debate_date = m.get("debat_date", "") or ""
        extrait = clean_text(snippet, 220)
        entry = {
            "juridiction": juridiction,
            "date": debate_date,
            "reference": reference,
            "titre": "Reference jurisprudentielle citee en debat parlementaire",
            "faits": extrait,
            "solution": "La discussion parlementaire mobilise cette reference pour appuyer l'interpretation de l'article.",
            "portee": "Verification juridique complementaire recommandee avant reutilisation contentieuse.",
        }
        entries.append(entry)
        if len(entries) >= 3:
            break

    if entries:
        return entries

    return [
        {
            "juridiction": "N/D",
            "date": "",
            "reference": "N/D",
            "titre": "Aucune jurisprudence explicite identifiee dans les sources locales",
            "faits": (
                f"Les extraits parlementaires indexes localement ne citent pas de decision juridictionnelle "
                f"identifiable pour l'article {ref}."
            ),
            "solution": "Aucune solution jurisprudentielle verifiable n'a ete extraite automatiquement.",
            "portee": "Un enrichissement manuel cible est necessaire pour des references juridictionnelles completes.",
        }
    ]


def snippet_to_proposition(snippet: str, ref: str) -> str:
    text = clean_text(snippet, 260)
    if not text:
        return f"Suivi des evolutions du regime de l'article {ref}."
    for part in re.split(r"[.;:!?]", text):
        part = part.strip()
        if len(part) >= 60:
            return clean_text(part, 200)
    return clean_text(text, 200)


def generate_forward_looking(ref: str, mentions: list[dict]) -> list[dict]:
    recent = [
        m
        for m in mentions
        if (m.get("debat_date") or "") >= "2025-01-01" and not is_procedural(m.get("snippet", ""))
    ]

    citations: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for m in sorted(recent, key=lambda x: x.get("debat_date", ""), reverse=True):
        if is_session_chair(m.get("fonction", "")):
            continue
        citation_text = clean_text(m.get("snippet", ""), 250)
        if not citation_text:
            continue
        key = (m.get("orateur", ""), m.get("debat_date", ""), citation_text)
        if key in seen:
            continue
        seen.add(key)
        citations.append(
            {
                "citation": citation_text,
                "orateur": m.get("orateur", ""),
                "fonction": m.get("fonction", ""),
                "date": m.get("debat_date", ""),
                "chambre": normalize_chamber(m.get("chambre", "")),
            }
        )
        if len(citations) >= 3:
            break

    if citations:
        propositions: list[str] = []
        for c in citations:
            prop = snippet_to_proposition(c["citation"], ref)
            if prop not in propositions:
                propositions.append(prop)
            if len(propositions) >= 3:
                break
        return [
            {
                "plf_year": 2026,
                "status": "en cours",
                "propositions": propositions,
                "citations": citations,
                "niveau_certitude": "moyen",
            }
        ]

    return [
        {
            "plf_year": 2026,
            "status": "projet",
            "propositions": [
                f"Aucun extrait explicite sur l'article {ref} n'a ete identifie dans les debats PLF 2025/2026 indexes localement."
            ],
            "citations": [],
            "niveau_certitude": "faible",
        }
    ]


def bofip_fallback_url(ref: str) -> str:
    query = quote_plus(f"article {ref} CGI")
    return f"https://bofip.impots.gouv.fr/bofip_search_advance/form/search_advance?query={query}"


def audit_quote_removals(current_data: dict) -> dict:
    """Compare current staged file vs HEAD and detect suspicious quote removals."""
    try:
        head_raw = subprocess.check_output(
            ["git", "show", "HEAD:data/article_modifications.json"], cwd=ROOT
        )
        head_data = json.loads(head_raw)
    except Exception:
        return {"available": False}

    old_articles = head_data.get("articles", {})
    new_articles = current_data.get("articles", {})

    removed_total = 0
    suspicious = []

    for ref in set(old_articles) | set(new_articles):
        old_q = old_articles.get(ref, {}).get("curated_quotes") or {}
        new_q = new_articles.get(ref, {}).get("curated_quotes") or {}

        old_set = {
            (
                str(y),
                q.get("citation", "").strip(),
                q.get("orateur", "").strip(),
                q.get("fonction", "").strip(),
                q.get("date", "").strip(),
                q.get("chambre", "").strip(),
            )
            for y, arr in old_q.items()
            for q in arr
        }
        new_set = {
            (
                str(y),
                q.get("citation", "").strip(),
                q.get("orateur", "").strip(),
                q.get("fonction", "").strip(),
                q.get("date", "").strip(),
                q.get("chambre", "").strip(),
            )
            for y, arr in new_q.items()
            for q in arr
        }

        removed = old_set - new_set
        removed_total += len(removed)

        # Suspicious only if removed quote mentions the exact article ref
        # and CGI context, while not being procedural.
        ref_pat = re.escape(normalize_text(ref)).replace(r"\-", r"[-\s]?")
        article_re = re.compile(rf"(?:l[' ]?)?articles?\s+{ref_pat}\b")
        cgi_re = re.compile(r"(code\s+general\s+des\s+impots|\bcgi\b|du\s+meme\s+code)")

        for item in removed:
            citation = item[1]
            norm = normalize_text(citation)
            if is_procedural(norm):
                continue
            if article_re.search(norm) and cgi_re.search(norm):
                suspicious.append(
                    {
                        "ref": ref,
                        "date": item[4],
                        "orateur": item[2],
                        "citation": clean_text(citation, 220),
                    }
                )

    return {
        "available": True,
        "removed_total": removed_total,
        "suspicious_count": len(suspicious),
        "suspicious_samples": suspicious[:20],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Fill missing fields locally (no external API).")
    parser.add_argument("--dry-run", action="store_true", help="Do not write changes to disk.")
    parser.add_argument(
        "--skip-audit",
        action="store_true",
        help="Skip curated quote removal audit against HEAD.",
    )
    args = parser.parse_args()

    with open(DATA_PATH, encoding="utf-8") as f:
        data = json.load(f)

    with open(MENTION_PATH, encoding="utf-8") as f:
        mention_index = json.load(f)

    if not args.skip_audit:
        audit = audit_quote_removals(data)
        if audit.get("available"):
            print(
                f"Quote audit: removed_total={audit['removed_total']}, "
                f"suspicious={audit['suspicious_count']}"
            )
            for s in audit["suspicious_samples"]:
                print(f"  - {s['ref']} {s['date']} {s['orateur']}: {s['citation']}")
        else:
            print("Quote audit: unavailable (could not read HEAD snapshot)")

    stats = Counter()
    articles = data.get("articles", {})

    for ref, article in articles.items():
        mentions = mention_index.get(ref, [])

        if not article.get("synthese"):
            article["synthese"] = generate_synthese(ref, article)
            stats["synthese_filled"] += 1

        if not article.get("jurisprudence"):
            article["jurisprudence"] = generate_jurisprudence(ref, mentions)
            stats["jurisprudence_filled"] += 1

        if not article.get("forward_looking"):
            article["forward_looking"] = generate_forward_looking(ref, mentions)
            stats["forward_looking_filled"] += 1

        if not article.get("bofip_url"):
            article["bofip_url"] = bofip_fallback_url(ref)
            stats["bofip_filled"] += 1

    print("Fill stats:")
    for key in ("synthese_filled", "jurisprudence_filled", "forward_looking_filled", "bofip_filled"):
        print(f"  {key}: {stats[key]}")

    if args.dry_run:
        print("Dry run: no file written")
        return

    backup = DATA_PATH.with_suffix(".json.localfill.bak")
    if not backup.exists():
        backup.write_text(DATA_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"Backup created: {backup}")

    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"Updated: {DATA_PATH}")


if __name__ == "__main__":
    main()
