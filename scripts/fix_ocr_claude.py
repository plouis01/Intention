#!/usr/bin/env python3
"""
Fix OCR errors in reocr/ text files using Claude Code CLI with Sonnet model.
Verify speaker names with a 4-pass pipeline for 98%+ accuracy.

Workflows:

1. OCR TEXT CORRECTION (default):
   Sends each .txt file to claude --print --model sonnet to fix OCR errors.

2. SPEAKER NAME PIPELINE:
   Passe 1: --extract-speakers     Extract ALL speaker names → speakers_all.tsv
   Passe 2: --build-reference      Scrape official deputy list from AN Sycomore → deputies_reference.json
   Passe 3: --match-speakers       Fuzzy match extracted names against reference (no LLM)
   Passe 4: --verify-speakers      Verify ONLY unresolved cases with Sonnet + enriched context
   Post:    --post-validate        Coherence checks on final output

Usage:
    python scripts/fix_ocr_claude.py --extract-speakers
    python scripts/fix_ocr_claude.py --build-reference
    python scripts/fix_ocr_claude.py --match-speakers
    python scripts/fix_ocr_claude.py --verify-speakers --workers 5
    python scripts/fix_ocr_claude.py --post-validate
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock
from urllib.request import Request, urlopen

PROJECT_DIR = Path(__file__).resolve().parent.parent
REOCR_DIR = PROJECT_DIR / "data" / "reocr"
LOG_DIR = PROJECT_DIR / "data" / "reocr_fix_logs"

REFERENCE_FILE = LOG_DIR / "deputies_reference.json"
SPEAKERS_ALL_TSV = LOG_DIR / "speakers_all.tsv"
SPEAKERS_MATCHED_TSV = LOG_DIR / "speakers_matched.tsv"
SPEAKERS_VERIFIED_TSV = LOG_DIR / "speakers_verified.tsv"

# Sycomore legislature IDs: legislature number -> sycomore ID
SYCOMORE_LEGISLATURES = {
    1: 48, 2: 49, 3: 50, 4: 51, 5: 52, 6: 53,
    7: 54, 8: 55, 9: 56, 10: 57, 11: 58,
}

# ---------------------------------------------------------------------------
# OCR correction prompts
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """Tu es un correcteur spécialisé dans la correction de textes OCR de débats parlementaires français (1958-1970).

RÈGLES STRICTES:
1. Corrige UNIQUEMENT les erreurs évidentes d'OCR (caractères mal reconnus, mots tronqués, lettres confondues)
2. NE MODIFIE PAS la structure du texte (sauts de ligne, espaces, tirets de mise en page)
3. NE MODIFIE PAS le style ou la grammaire intentionnelle du texte d'époque
4. NE SUPPRIME PAS de contenu
5. NE RAJOUTE PAS de contenu
6. Conserve les noms propres tels quels sauf erreur OCR évidente
7. Les séquences de caractères aléatoires comme "0.00...00", "eee", "ooo", "sss" etc. sont des artefacts OCR de lignes de points/tirets - laisse-les tels quels

Exemples de corrections typiques:
- "PRIÈRZ" → "PRIÈRE"
- "Picrro" → "Pierre"
- "sertslin" → "scrutin"
- "résuliat" → "résultat"
- "Chaban-Deimnas" → "Chaban-Delmas"
- "dz" → "de" (quand erreur évidente)
- "oux" → "aux"
- "besolns" → "besoins"

Renvoie LE TEXTE CORRIGÉ EN ENTIER, sans commentaire, sans explication, sans balise markdown."""

USER_PROMPT = "Corrige les erreurs OCR dans ce texte de débat parlementaire français. Renvoie uniquement le texte corrigé, rien d'autre:\n\n"

# ---------------------------------------------------------------------------
# Speaker verification prompt (Sonnet, enriched context)
# ---------------------------------------------------------------------------

SPEAKER_VERIFY_PROMPT = """Tu es un expert en histoire parlementaire française de la Ve République (1958-2002).

On te donne un lot de noms de speakers extraits de textes OCR de débats parlementaires qui n'ont PAS pu être résolus automatiquement par fuzzy matching contre la liste officielle des députés.

Pour chaque nom, tu disposes de:
- Le nom tel qu'extrait de l'OCR
- Son nombre d'occurrences dans le corpus
- Les candidats les plus proches trouvés dans la liste officielle des députés (avec score de similarité)

Pour CHAQUE nom, détermine:
1. "status": "matched" → tu es confiant que c'est un des candidats proposés. Indique lequel dans "correct_name".
2. "status": "ok" → le nom est correct tel quel (c'est un ministre, président, sénateur, ou autre intervenant non-député)
3. "status": "ocr_error" → c'est clairement une erreur OCR mais aucun candidat ne correspond. Propose le nom correct dans "correct_name" si possible.
4. "status": "uncertain" → tu n'es pas sûr.

Renvoie UNIQUEMENT un tableau JSON valide. Chaque entrée:
{
  "name": "le nom tel qu'extrait",
  "status": "matched" | "ok" | "ocr_error" | "uncertain",
  "correct_name": "le vrai nom ou null",
  "confidence": "high" | "medium" | "low"
}

IMPORTANT:
- Les intervenants ne sont pas tous des députés: il y a des ministres, des présidents de commissions, des sénateurs, des personnalités auditionnées
- Un nom avec beaucoup d'occurrences (>20) sans match est probablement correct (ministre, sénateur, etc.)
- Regarde attentivement les candidats proposés: un score >0.80 est souvent le bon match
- Sois rigoureux: préfère "uncertain" plutôt que de deviner

Renvoie le JSON sans commentaire ni markdown."""

# ---------------------------------------------------------------------------
# Speaker name extraction regex
# ---------------------------------------------------------------------------

_WORD = r"[A-ZÀ-Ü][a-zà-ü']*(?:-[A-ZÀ-Ü]?[a-zà-ü']+)*"
SPEAKER_RE = re.compile(
    r"^(M\.|Mme|Mlle)\s+"
    r"(?!le |la |les |l'|du |de |des |un |une |Je |Ie |te |ia |lo )"
    rf"((?:{_WORD})(?:\s+{_WORD}){{0,3}})"
    r"\s*[.,]",
    re.MULTILINE,
)

# Thread-safe log writing
log_lock = Lock()


def log_append(filepath: Path, line: str):
    with log_lock:
        with open(filepath, "a") as f:
            f.write(line + "\n")


def is_done(rel_path: str, done_log: Path) -> bool:
    if not done_log.exists():
        return False
    with open(done_log) as f:
        return rel_path in {line.strip() for line in f}


# ---------------------------------------------------------------------------
# String normalization & similarity
# ---------------------------------------------------------------------------


def normalize(s: str) -> str:
    """Normalize a name for comparison: lowercase, strip accents, remove punctuation."""
    s = s.strip().lower()
    # Decompose unicode and remove combining marks (accents)
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    # Remove apostrophes, hyphens, commas → spaces
    s = s.replace("'", "").replace("'", "").replace("-", " ").replace(",", " ")
    # Collapse whitespace
    s = " ".join(s.split())
    return s


def simplify_name(deputy: dict) -> str:
    """Build 'FirstName LastName' using only the first (usage) first name."""
    first = deputy.get("first_name", "")
    last = deputy.get("last_name", "")
    # Sycomore stores "Robert, Victor, Marcel, François" — take only the first
    if "," in first:
        first = first.split(",")[0].strip()
    # Strip "dit ..." / "née ..." suffixes from last name
    last = re.split(r"\s+(?:dit|née|nee)\s+", last, flags=re.IGNORECASE)[0].strip()
    return f"{first} {last}".strip()


def name_variants(deputy: dict) -> list[str]:
    """Generate all plausible name variants for a deputy."""
    variants = []
    first = deputy.get("first_name", "")
    last = deputy.get("last_name", "")

    # Usage first names (before comma)
    first_parts = [p.strip() for p in first.split(",")]
    usage_first = first_parts[0] if first_parts else first

    # Last name variants
    last_variants = [last]
    # "Petit dit Claudius-Petit" → "Petit", "Claudius-Petit"
    if re.search(r"\s+dit\s+", last, re.IGNORECASE):
        parts = re.split(r"\s+dit\s+", last, flags=re.IGNORECASE)
        last_variants = parts
    # "Vaillant-Couturier Née Vogel" → "Vaillant-Couturier"
    if re.search(r"\s+n[ée]+e?\s+", last, re.IGNORECASE):
        last_variants.append(re.split(r"\s+n[ée]+e?\s+", last, flags=re.IGNORECASE)[0])
    # "Villon-Ginsburger" → also "Villon"
    if "-" in last and " " not in last:
        last_variants.append(last.split("-")[0])

    for lv in last_variants:
        # Full usage name
        variants.append(f"{usage_first} {lv}")
        # Hyphenated compound first names: "René-Georges" → also just "René"
        if "-" in usage_first:
            variants.append(f"{usage_first.split('-')[0]} {lv}")

    return variants


def levenshtein(s1: str, s2: str) -> int:
    """Compute Levenshtein distance between two strings."""
    if len(s1) < len(s2):
        return levenshtein(s2, s1)
    if len(s2) == 0:
        return len(s1)
    prev = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr = [i + 1]
        for j, c2 in enumerate(s2):
            cost = 0 if c1 == c2 else 1
            curr.append(min(curr[j] + 1, prev[j + 1] + 1, prev[j] + cost))
        prev = curr
    return prev[-1]


def similarity(s1: str, s2: str) -> float:
    """Normalized similarity between 0.0 and 1.0."""
    n1, n2 = normalize(s1), normalize(s2)
    if n1 == n2:
        return 1.0
    max_len = max(len(n1), len(n2))
    if max_len == 0:
        return 1.0
    dist = levenshtein(n1, n2)
    return 1.0 - dist / max_len


# ---------------------------------------------------------------------------
# Claude CLI helper
# ---------------------------------------------------------------------------


def call_claude(
    prompt: str, system_prompt: str, timeout: int = 300, model: str = "haiku"
) -> str | None:
    """Call claude CLI, return stdout or None on failure."""
    try:
        result = subprocess.run(
            [
                "claude",
                "--print",
                "--model", model,
                "--system-prompt", system_prompt,
                "--allowedTools", "",
                "--no-session-persistence",
                "-p",
                prompt,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
        return None
    except subprocess.TimeoutExpired:
        return None
    except Exception as e:
        print(f"  [ERROR] claude call failed: {e}", file=sys.stderr)
        return None


# ===================================================================
# PASSE 1: Extract all speakers
# ===================================================================


def extract_all_speakers(reocr_dir: Path, filter_pattern: str | None = None):
    """Scan all _body.txt files, extract every speaker name."""
    name_counts: Counter = Counter()
    name_files: dict[str, list[str]] = defaultdict(list)

    files = sorted(reocr_dir.rglob("*_body.txt"))
    if filter_pattern:
        files = [f for f in files if filter_pattern in str(f)]

    for i, fpath in enumerate(files):
        rel = str(fpath.relative_to(reocr_dir))
        try:
            text = fpath.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        file_names: Counter = Counter()
        for match in SPEAKER_RE.finditer(text):
            name = match.group(2).strip()
            name = re.sub(r"\s*\(.*$", "", name)
            name = name.rstrip(" ,;:")
            if len(name) > 2:
                file_names[name] += 1

        for name, count in file_names.items():
            name_counts[name] += count
            name_files[name].append(rel)

        if (i + 1) % 100 == 0:
            print(f"  Scanned {i+1}/{len(files)} files...", flush=True)

    print(f"  Scanned {len(files)} files total.")
    return name_counts, name_files


def write_speakers_tsv(
    output_path: Path,
    name_counts: Counter,
    name_files: dict[str, list[str]],
):
    """Write all extracted speakers to a TSV file."""
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("name\tcount\tfiles\n")
        for name in sorted(name_counts.keys()):
            count = name_counts[name]
            files_str = ";".join(name_files[name][:10])
            if len(name_files[name]) > 10:
                files_str += f";...({len(name_files[name])} total)"
            f.write(f"{name}\t{count}\t{files_str}\n")
    print(f"Wrote {len(name_counts)} names to {output_path}")


# ===================================================================
# PASSE 2: Build reference list from Sycomore
# ===================================================================


def scrape_sycomore_legislature(leg_num: int, sycomore_id: int) -> list[dict]:
    """Scrape deputy names from one Sycomore legislature page."""
    url = f"https://www2.assemblee-nationale.fr/sycomore/liste/{sycomore_id}?alpha=true"
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 (legal-project/research)"})

    try:
        with urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  [ERROR] Failed to fetch legislature {leg_num}: {e}", file=sys.stderr)
        return []

    # Pattern: <a href="/sycomore/fiche/{ID}?legislature={LEG_ID}">
    #            Prénom <strong>NOM</strong>
    #          </a>
    pattern = re.compile(
        r'<a\s+href="/sycomore/fiche/(\d+)\?legislature=\d+">\s*'
        r"(.*?)<strong>(.*?)</strong>(.*?)\s*</a>",
        re.DOTALL,
    )

    deputies = []
    for match in pattern.finditer(html):
        sycomore_fiche_id = match.group(1)
        before_strong = match.group(2).strip()  # first name part
        in_strong = match.group(3).strip()  # last name (in bold)
        after_strong = match.group(4).strip()  # sometimes extra

        # Clean HTML entities
        for entity, char in [("&amp;", "&"), ("&#039;", "'"), ("&quot;", '"')]:
            before_strong = before_strong.replace(entity, char)
            in_strong = in_strong.replace(entity, char)
            after_strong = after_strong.replace(entity, char)

        first_name = before_strong.strip()
        last_name = in_strong.strip()
        if after_strong:
            last_name += " " + after_strong.strip()

        full_name = f"{first_name} {last_name}".strip()
        # Normalize whitespace
        full_name = " ".join(full_name.split())

        if full_name:
            deputies.append({
                "full_name": full_name,
                "first_name": first_name,
                "last_name": last_name,
                "sycomore_id": int(sycomore_fiche_id),
                "legislature": leg_num,
            })

    return deputies


def build_reference():
    """Scrape all 11 legislatures from Sycomore and build reference JSON."""
    all_deputies = []
    seen_names: set[str] = set()

    for leg_num, syc_id in sorted(SYCOMORE_LEGISLATURES.items()):
        print(f"  Scraping legislature {leg_num} (Sycomore ID {syc_id})...")
        deputies = scrape_sycomore_legislature(leg_num, syc_id)
        print(f"    Found {len(deputies)} deputies.")

        for d in deputies:
            all_deputies.append(d)
            seen_names.add(d["full_name"])

        # Polite delay
        time.sleep(1)

    # Deduplicate: build a canonical list of unique names across all legislatures
    canonical: dict[str, dict] = {}
    for d in all_deputies:
        name = d["full_name"]
        if name not in canonical:
            canonical[name] = {
                "full_name": name,
                "first_name": d["first_name"],
                "last_name": d["last_name"],
                "sycomore_id": d["sycomore_id"],
                "legislatures": [d["legislature"]],
            }
        else:
            if d["legislature"] not in canonical[name]["legislatures"]:
                canonical[name]["legislatures"].append(d["legislature"])

    reference = {
        "source": "Assemblée Nationale - Base Sycomore",
        "url": "https://www2.assemblee-nationale.fr/sycomore/liste/",
        "legislatures": list(SYCOMORE_LEGISLATURES.keys()),
        "total_unique_names": len(canonical),
        "total_entries": len(all_deputies),
        "deputies": list(canonical.values()),
    }

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(REFERENCE_FILE, "w", encoding="utf-8") as f:
        json.dump(reference, f, ensure_ascii=False, indent=2)

    print(f"\nWrote {len(canonical)} unique deputies to {REFERENCE_FILE}")
    print(f"Total entries across all legislatures: {len(all_deputies)}")
    return reference


# ===================================================================
# PASSE 3: Fuzzy matching (algorithmic, no LLM)
# ===================================================================


def load_reference() -> list[dict]:
    """Load the reference deputy list."""
    with open(REFERENCE_FILE, encoding="utf-8") as f:
        data = json.load(f)
    return data["deputies"]


def load_speakers_tsv(tsv_path: Path) -> list[tuple[str, int, str]]:
    """Load speakers TSV, return list of (name, count, files_str)."""
    entries = []
    with open(tsv_path, encoding="utf-8") as f:
        f.readline()  # skip header
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 3:
                entries.append((parts[0], int(parts[1]), parts[2]))
    return entries


def build_normalized_index(deputies: list[dict]) -> dict[str, list[dict]]:
    """Build a lookup: normalized_name -> list of deputy dicts."""
    index: dict[str, list[dict]] = defaultdict(list)
    for d in deputies:
        # Full Sycomore name
        norm = normalize(d["full_name"])
        index[norm].append(d)
        # All plausible name variants
        for variant in name_variants(d):
            nv = normalize(variant)
            if nv and nv != norm and d not in index.get(nv, []):
                index[nv].append(d)
        # Last name only (for single-word OCR names)
        norm_last = normalize(d["last_name"])
        if d not in index.get(norm_last, []):
            index[norm_last].append(d)
    return index


def find_best_matches(
    name: str, deputies: list[dict], norm_index: dict[str, list[dict]], top_n: int = 5
) -> list[tuple[dict, float]]:
    """Find the top_n best matching deputies for a given name."""
    norm_name = normalize(name)

    # 1. Exact normalized match (checks full_name, simplified name, and last_name)
    if norm_name in norm_index:
        return [(d, 1.0) for d in norm_index[norm_name]]

    # 2. Compute similarity against all reference names and their variants
    scores: list[tuple[dict, float]] = []
    seen_names: set[str] = set()
    for d in deputies:
        ref_name = d["full_name"]
        if ref_name in seen_names:
            continue
        seen_names.add(ref_name)
        # Score against full name, simplified name, and all variants
        best_sim = similarity(name, ref_name)
        for variant in name_variants(d):
            best_sim = max(best_sim, similarity(name, variant))
        if best_sim > 0.55:  # minimum threshold to consider
            scores.append((d, best_sim))

    scores.sort(key=lambda x: x[1], reverse=True)
    return scores[:top_n]


def match_speakers(speakers_tsv: Path, reference_file: Path, output_tsv: Path):
    """Match extracted speaker names against reference list using fuzzy matching."""
    deputies = load_reference()
    norm_index = build_normalized_index(deputies)
    entries = load_speakers_tsv(speakers_tsv)

    print(f"Loaded {len(deputies)} reference deputies")
    print(f"Loaded {len(entries)} extracted names")
    print(f"Matching...\n")

    results = []
    exact_count = 0
    fuzzy_count = 0
    unresolved_count = 0

    for i, (name, count, files_str) in enumerate(entries):
        matches = find_best_matches(name, deputies, norm_index)

        if matches and matches[0][1] == 1.0:
            # Exact normalized match
            results.append({
                "name": name,
                "status": "exact_match",
                "correct_name": matches[0][0]["full_name"],
                "score": 1.0,
                "confidence": "high",
                "count": count,
                "files": files_str,
                "candidates": "",
            })
            exact_count += 1

        elif matches and matches[0][1] >= 0.85:
            # High fuzzy match
            results.append({
                "name": name,
                "status": "fuzzy_match",
                "correct_name": matches[0][0]["full_name"],
                "score": matches[0][1],
                "confidence": "high" if matches[0][1] >= 0.92 else "medium",
                "count": count,
                "files": files_str,
                "candidates": "|".join(
                    f"{d['full_name']}({s:.2f})" for d, s in matches[:3]
                ),
            })
            fuzzy_count += 1

        else:
            # Unresolved - will go to LLM verification
            candidates_str = "|".join(
                f"{d['full_name']}({s:.2f})" for d, s in matches[:5]
            ) if matches else ""
            results.append({
                "name": name,
                "status": "unresolved",
                "correct_name": "",
                "score": matches[0][1] if matches else 0.0,
                "confidence": "",
                "count": count,
                "files": files_str,
                "candidates": candidates_str,
            })
            unresolved_count += 1

        if (i + 1) % 1000 == 0:
            print(f"  Processed {i+1}/{len(entries)}...", flush=True)

    # Write results
    with open(output_tsv, "w", encoding="utf-8") as f:
        f.write("name\tstatus\tcorrect_name\tscore\tconfidence\tcount\tfiles\tcandidates\n")
        for r in results:
            f.write(
                f"{r['name']}\t{r['status']}\t{r['correct_name']}\t{r['score']:.3f}\t"
                f"{r['confidence']}\t{r['count']}\t{r['files']}\t{r['candidates']}\n"
            )

    print(f"\n{'='*50}")
    print(f"Fuzzy Matching Complete")
    print(f"{'='*50}")
    print(f"Total names:       {len(entries)}")
    print(f"Exact matches:     {exact_count} ({exact_count*100//len(entries)}%)")
    print(f"Fuzzy matches:     {fuzzy_count} ({fuzzy_count*100//len(entries)}%)")
    print(f"Unresolved:        {unresolved_count} ({unresolved_count*100//len(entries)}%)")
    print(f"Output:            {output_tsv}")
    print(f"{'='*50}")
    print(f"\nNext step: python scripts/fix_ocr_claude.py --verify-speakers")


# ===================================================================
# PASSE 4: Verify unresolved with Sonnet + enriched context
# ===================================================================


def verify_speaker_batch(
    batch: list[dict], batch_index: int, total_batches: int
) -> list[dict]:
    """Send a batch of unresolved names to Sonnet for verification."""
    lines = []
    for entry in batch:
        line = f"- {entry['name']} ({entry['count']}x)"
        if entry.get("candidates"):
            line += f"  |  Candidats proches: {entry['candidates']}"
        lines.append(line)
    names_text = "\n".join(lines)

    prompt = (
        f"Lot {batch_index + 1}/{total_batches}.\n\n"
        f"Voici les noms de speakers non résolus par fuzzy matching, "
        f"avec leurs candidats les plus proches dans la liste officielle des députés:\n\n"
        f"{names_text}\n\n"
        f"Pour chaque nom, détermine son statut. Renvoie un tableau JSON."
    )

    output = call_claude(prompt, SPEAKER_VERIFY_PROMPT, timeout=300, model="sonnet")
    if not output:
        return []

    try:
        bracket_start = output.find("[")
        bracket_end = output.rfind("]")
        if bracket_start == -1 or bracket_end == -1:
            return []
        results = json.loads(output[bracket_start : bracket_end + 1])
        return results if isinstance(results, list) else []
    except (json.JSONDecodeError, Exception):
        return []


def run_speaker_verification(matched_tsv: Path, output_tsv: Path, workers: int, batch_size: int):
    """Verify unresolved names with Sonnet, merge with matched results."""
    # Load matched results
    all_entries = []
    unresolved = []
    with open(matched_tsv, encoding="utf-8") as f:
        header = f.readline().rstrip("\n")
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 8:
                entry = {
                    "name": parts[0],
                    "status": parts[1],
                    "correct_name": parts[2],
                    "score": float(parts[3]),
                    "confidence": parts[4],
                    "count": int(parts[5]),
                    "files": parts[6],
                    "candidates": parts[7] if len(parts) > 7 else "",
                }
                all_entries.append(entry)
                if entry["status"] == "unresolved":
                    unresolved.append(entry)

    print(f"Loaded {len(all_entries)} total entries, {len(unresolved)} unresolved")

    if not unresolved:
        print("No unresolved entries to verify!")
        return

    # Build batches
    batches = []
    for i in range(0, len(unresolved), batch_size):
        batches.append(unresolved[i : i + batch_size])

    print(f"Created {len(batches)} batches of ~{batch_size} names each")
    print(f"Using Sonnet with {workers} parallel workers...\n")

    llm_results: list[dict] = []
    errors = 0
    start_time = time.time()

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(verify_speaker_batch, batch, idx, len(batches)): idx
            for idx, batch in enumerate(batches)
        }

        for i, future in enumerate(as_completed(futures), 1):
            try:
                results = future.result()
                llm_results.extend(results)
            except Exception as e:
                print(f"  [ERROR] Batch: {e}", file=sys.stderr)
                errors += 1

            if i % 5 == 0 or i == len(batches):
                elapsed = time.time() - start_time
                rate = i / elapsed if elapsed > 0 else 0
                eta = (len(batches) - i) / rate if rate > 0 else 0
                print(
                    f"  [{i}/{len(batches)}] Verified: {len(llm_results)} | "
                    f"Errors: {errors} | ETA: {eta/60:.0f}min",
                    flush=True,
                )

    # Merge LLM results into entries
    llm_map = {}
    for r in llm_results:
        n = r.get("name", "")
        if n:
            llm_map[n] = r

    for entry in all_entries:
        if entry["status"] == "unresolved" and entry["name"] in llm_map:
            r = llm_map[entry["name"]]
            entry["status"] = r.get("status", "uncertain")
            entry["correct_name"] = r.get("correct_name") or ""
            entry["confidence"] = r.get("confidence") or ""

    # Write final output
    with open(output_tsv, "w", encoding="utf-8") as f:
        f.write("name\tstatus\tcorrect_name\tscore\tconfidence\tcount\tfiles\tcandidates\n")
        for e in all_entries:
            f.write(
                f"{e['name']}\t{e['status']}\t{e['correct_name']}\t{e['score']:.3f}\t"
                f"{e['confidence']}\t{e['count']}\t{e['files']}\t{e.get('candidates', '')}\n"
            )

    elapsed = time.time() - start_time
    statuses = Counter(e["status"] for e in all_entries)

    print(f"\n{'='*50}")
    print(f"Speaker Verification Complete")
    print(f"{'='*50}")
    print(f"Total names:       {len(all_entries)}")
    for status, cnt in statuses.most_common():
        print(f"  {status:18s} {cnt:5d} ({cnt*100//len(all_entries)}%)")
    print(f"LLM batch errors:  {errors}")
    print(f"Time:              {elapsed/60:.1f} min")
    print(f"Output:            {output_tsv}")
    print(f"{'='*50}")


# ===================================================================
# POST-VALIDATION: Coherence checks
# ===================================================================


def post_validate(verified_tsv: Path):
    """Run coherence checks on the verified speakers file."""
    entries = []
    with open(verified_tsv, encoding="utf-8") as f:
        f.readline()  # skip header
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 6:
                entries.append({
                    "name": parts[0],
                    "status": parts[1],
                    "correct_name": parts[2],
                    "score": float(parts[3]) if parts[3] else 0,
                    "confidence": parts[4],
                    "count": int(parts[5]),
                })

    issues = []

    # Load reference names for validation
    ref_names: set[str] = set()
    ref_norm: set[str] = set()
    if REFERENCE_FILE.exists():
        deputies = load_reference()
        ref_names = {d["full_name"] for d in deputies}
        ref_norm = {normalize(d["full_name"]) for d in deputies}
        # Also add simplified name variants
        for d in deputies:
            ref_norm.add(normalize(simplify_name(d)))
            for v in name_variants(d):
                ref_norm.add(normalize(v))

    # Build sets
    ok_names = {e["name"] for e in entries if e["status"] in ("exact_match", "ok")}
    all_entry_names = {e["name"] for e in entries}
    all_known_norm = {normalize(n) for n in ok_names} | ref_norm

    # Check 1: Corrections pointing to names not in reference or dataset
    for e in entries:
        if e["correct_name"] and e["status"] in ("fuzzy_match", "matched", "ocr_error"):
            target_norm = normalize(e["correct_name"])
            if target_norm not in all_known_norm:
                issues.append(
                    f"[ORPHAN_CORRECTION] '{e['name']}' -> '{e['correct_name']}' "
                    f"(target not in reference or ok names)"
                )

    # Check 2: Self-corrections (name normalized == correct_name normalized, for non-exact)
    for e in entries:
        if e["correct_name"] and e["status"] != "exact_match":
            if normalize(e["correct_name"]) == normalize(e["name"]):
                issues.append(
                    f"[SELF_CORRECTION] '{e['name']}' corrected to '{e['correct_name']}' (same normalized)"
                )

    # Check 3: OK names with very low count that are similar to high-count names
    high_count_names = [(e["name"], e["count"]) for e in entries if e["count"] >= 20]
    for e in entries:
        if e["status"] in ("exact_match", "ok") and e["count"] <= 2:
            for hc_name, hc_count in high_count_names:
                if hc_name != e["name"]:
                    sim = similarity(e["name"], hc_name)
                    if sim >= 0.85:
                        issues.append(
                            f"[SUSPECT_OK] '{e['name']}' (count={e['count']}) similar to "
                            f"'{hc_name}' (count={hc_count}, sim={sim:.2f})"
                        )
                        break

    # Check 4: Duplicate ok names (same normalized form)
    norm_to_ok: dict[str, list[str]] = defaultdict(list)
    for e in entries:
        if e["status"] in ("exact_match", "ok"):
            norm_to_ok[normalize(e["name"])].append(e["name"])
    for norm, names in norm_to_ok.items():
        if len(names) > 1:
            issues.append(
                f"[DUPLICATE_OK] Same normalized form: {', '.join(names)}"
            )

    # Check 5: Remaining unresolved
    unresolved = [e for e in entries if e["status"] == "unresolved"]
    if unresolved:
        issues.append(f"[UNRESOLVED] {len(unresolved)} names still unresolved")

    # Write issues report
    report_path = LOG_DIR / "post_validation_report.txt"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(f"Post-Validation Report\n{'='*50}\n\n")
        f.write(f"Total entries: {len(entries)}\n")
        f.write(f"Issues found:  {len(issues)}\n\n")
        for issue in sorted(issues):
            f.write(f"{issue}\n")

    # Print summary
    issue_types = Counter(i.split("]")[0] + "]" for i in issues)
    print(f"\n{'='*50}")
    print(f"Post-Validation Report")
    print(f"{'='*50}")
    print(f"Total entries:     {len(entries)}")
    print(f"Issues found:      {len(issues)}")
    for itype, cnt in issue_types.most_common():
        print(f"  {itype:25s} {cnt}")
    print(f"Report:            {report_path}")
    print(f"{'='*50}")

    return issues


# ===================================================================
# OCR text correction (unchanged from before)
# ===================================================================


def process_chunk(text: str) -> str | None:
    result = call_claude(USER_PROMPT + text, SYSTEM_PROMPT, timeout=300, model="sonnet")
    if result is None:
        return None
    in_len = len(text)
    out_len = len(result)
    ratio = out_len / in_len if in_len > 0 else 0
    if ratio < 0.5 or ratio > 1.6:
        return None
    return result


def split_into_chunks(text: str, chunk_size: int) -> list[str]:
    if len(text) <= chunk_size:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        if end < len(text):
            newline_pos = text.rfind("\n", start + chunk_size // 2, end)
            if newline_pos > start:
                end = newline_pos + 1
        chunks.append(text[start:end])
        start = end
    return chunks


def process_file(
    file_path: Path,
    reocr_dir: Path,
    chunk_size: int,
    backup: bool,
    resume: bool,
    done_log: Path,
    error_log: Path,
    progress_log: Path,
) -> tuple[str, bool]:
    rel_path = str(file_path.relative_to(reocr_dir))
    if resume and is_done(rel_path, done_log):
        return rel_path, True
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        log_append(error_log, f"[READ_ERROR] {rel_path}: {e}")
        return rel_path, False
    if not text.strip():
        log_append(done_log, rel_path)
        return rel_path, True
    original_size = len(text)
    if backup:
        bak_path = file_path.with_suffix(file_path.suffix + ".bak")
        if not bak_path.exists():
            shutil.copy2(file_path, bak_path)
    chunks = split_into_chunks(text, chunk_size)
    num_chunks = len(chunks)
    corrected_chunks = []
    for i, chunk in enumerate(chunks):
        result = process_chunk(chunk)
        if result is None:
            log_append(error_log, f"[CHUNK_FAIL] {rel_path} chunk {i+1}/{num_chunks}")
            corrected_chunks.append(chunk)
        else:
            corrected_chunks.append(result)
    corrected_text = "".join(corrected_chunks)
    corrected_size = len(corrected_text)
    ratio = corrected_size / original_size if original_size > 0 else 0
    if ratio < 0.5 or ratio > 1.6:
        log_append(
            error_log,
            f"[SIZE_MISMATCH] {rel_path} (original: {original_size}, output: {corrected_size}, ratio: {ratio:.2f})",
        )
        return rel_path, False
    try:
        file_path.write_text(corrected_text, encoding="utf-8")
    except Exception as e:
        log_append(error_log, f"[WRITE_ERROR] {rel_path}: {e}")
        return rel_path, False
    log_append(done_log, rel_path)
    chunks_info = f", {num_chunks} chunks" if num_chunks > 1 else ""
    log_append(
        progress_log,
        f"[OK] {rel_path} ({original_size} -> {corrected_size} bytes{chunks_info})",
    )
    return rel_path, True


# ===================================================================
# Main
# ===================================================================


def main():
    parser = argparse.ArgumentParser(
        description="Fix OCR errors and verify speaker names"
    )
    # OCR correction args
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--chunk-size", type=int, default=60000)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--backup", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dir", type=str, default=None)
    parser.add_argument("--filter", type=str, default=None)
    parser.add_argument("--test", type=str, default=None)
    parser.add_argument("--max-files", type=int, default=None)

    # Speaker pipeline
    parser.add_argument("--extract-speakers", action="store_true",
                        help="Passe 1: Extract all speaker names")
    parser.add_argument("--build-reference", action="store_true",
                        help="Passe 2: Scrape Sycomore for official deputy list")
    parser.add_argument("--match-speakers", action="store_true",
                        help="Passe 3: Fuzzy match against reference")
    parser.add_argument("--verify-speakers", action="store_true",
                        help="Passe 4: Verify unresolved with Sonnet")
    parser.add_argument("--post-validate", action="store_true",
                        help="Post: Coherence checks")
    parser.add_argument("--batch-size", type=int, default=50,
                        help="Names per batch for LLM verification (default: 50)")

    args = parser.parse_args()
    reocr_dir = Path(args.dir) if args.dir else REOCR_DIR
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # --- Passe 1: Extract ---
    if args.extract_speakers:
        print("=== PASSE 1: Extracting all speaker names ===\n")
        name_counts, name_files = extract_all_speakers(reocr_dir, args.filter)
        write_speakers_tsv(SPEAKERS_ALL_TSV, name_counts, name_files)
        total_unique = len(name_counts)
        singletons = sum(1 for c in name_counts.values() if c == 1)
        print(f"\nUnique names:    {total_unique}")
        print(f"Total mentions:  {sum(name_counts.values())}")
        print(f"Singletons:      {singletons} ({singletons*100//total_unique}%)")
        print(f"\nNext: python scripts/fix_ocr_claude.py --build-reference")
        return

    # --- Passe 2: Build reference ---
    if args.build_reference:
        print("=== PASSE 2: Building reference deputy list from Sycomore ===\n")
        build_reference()
        print(f"\nNext: python scripts/fix_ocr_claude.py --match-speakers")
        return

    # --- Passe 3: Fuzzy match ---
    if args.match_speakers:
        print("=== PASSE 3: Fuzzy matching against reference ===\n")
        if not SPEAKERS_ALL_TSV.exists():
            print(f"Error: {SPEAKERS_ALL_TSV} not found. Run --extract-speakers first.", file=sys.stderr)
            sys.exit(1)
        if not REFERENCE_FILE.exists():
            print(f"Error: {REFERENCE_FILE} not found. Run --build-reference first.", file=sys.stderr)
            sys.exit(1)
        match_speakers(SPEAKERS_ALL_TSV, REFERENCE_FILE, SPEAKERS_MATCHED_TSV)
        return

    # --- Passe 4: Verify with Sonnet ---
    if args.verify_speakers:
        print("=== PASSE 4: Verifying unresolved names with Sonnet ===\n")
        if not SPEAKERS_MATCHED_TSV.exists():
            print(f"Error: {SPEAKERS_MATCHED_TSV} not found. Run --match-speakers first.", file=sys.stderr)
            sys.exit(1)
        run_speaker_verification(SPEAKERS_MATCHED_TSV, SPEAKERS_VERIFIED_TSV, args.workers, args.batch_size)
        return

    # --- Post-validation ---
    if args.post_validate:
        print("=== POST-VALIDATION: Coherence checks ===\n")
        if not SPEAKERS_VERIFIED_TSV.exists():
            print(f"Error: {SPEAKERS_VERIFIED_TSV} not found. Run --verify-speakers first.", file=sys.stderr)
            sys.exit(1)
        post_validate(SPEAKERS_VERIFIED_TSV)
        return

    # --- Test mode ---
    if args.test:
        test_path = Path(args.test)
        if not test_path.exists():
            print(f"File not found: {args.test}", file=sys.stderr)
            sys.exit(1)
        text = test_path.read_text(encoding="utf-8", errors="replace")
        print(f"Input size: {len(text)} chars", file=sys.stderr)
        chunks = split_into_chunks(text, args.chunk_size)
        print(f"Chunks: {len(chunks)}", file=sys.stderr)
        for i, chunk in enumerate(chunks):
            print(f"Processing chunk {i+1}/{len(chunks)}...", file=sys.stderr)
            result = process_chunk(chunk)
            if result:
                print(result, end="")
            else:
                print(f"[FAILED chunk {i+1}]", file=sys.stderr)
                print(chunk, end="")
        return

    # --- Default: OCR text correction ---
    print(f"Scanning files in {reocr_dir}...")
    files = sorted(reocr_dir.rglob("*.txt"))
    if args.filter:
        files = [f for f in files if args.filter in str(f)]
    total = len(files)
    print(f"Found {total} files to process.")
    if args.max_files:
        files = files[: args.max_files]
        print(f"Limited to {len(files)} files (--max-files).")
    if args.dry_run:
        print(f"\n[DRY RUN] Would process {len(files)} files with {args.workers} workers.\n")
        for f in files[:20]:
            size = f.stat().st_size
            print(f"  {f.relative_to(reocr_dir)} ({size:,} bytes)")
        if len(files) > 20:
            print(f"  ... and {len(files) - 20} more")
        return

    done_log = LOG_DIR / "done.log"
    error_log = LOG_DIR / "errors.log"
    progress_log = LOG_DIR / "progress.log"
    for log_file in [done_log, error_log, progress_log]:
        log_file.touch()
    if args.resume:
        done_count = sum(1 for _ in open(done_log)) if done_log.exists() else 0
        print(f"Resume mode: {done_count} files already processed.")

    print(f"\nStarting OCR correction with {args.workers} parallel workers...")
    print(f"Logs: {LOG_DIR}/")
    print(f"Progress: tail -f {progress_log}")
    print(f"Errors: tail -f {error_log}\n")

    ok_count = 0
    fail_count = 0
    start_time = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                process_file, f, reocr_dir, args.chunk_size,
                args.backup, args.resume, done_log, error_log, progress_log,
            ): f
            for f in files
        }
        for i, future in enumerate(as_completed(futures), 1):
            rel_path, success = future.result()
            if success:
                ok_count += 1
            else:
                fail_count += 1
            if i % 10 == 0 or i == len(files):
                elapsed = time.time() - start_time
                rate = i / elapsed if elapsed > 0 else 0
                eta = (len(files) - i) / rate if rate > 0 else 0
                print(
                    f"  [{i}/{len(files)}] OK: {ok_count} | Fail: {fail_count} | "
                    f"Rate: {rate:.1f} files/s | ETA: {eta/60:.0f}min",
                    flush=True,
                )

    elapsed = time.time() - start_time
    print(f"\n{'='*40}")
    print(f"OCR Correction Complete")
    print(f"{'='*40}")
    print(f"Total files:     {len(files)}")
    print(f"Processed OK:    {ok_count}")
    print(f"Errors:          {fail_count}")
    print(f"Time:            {elapsed/60:.1f} min")
    print(f"Log directory:   {LOG_DIR}")
    print(f"{'='*40}")


if __name__ == "__main__":
    main()
