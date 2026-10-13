"""TEMPORARY exploration script - examine LIENS structure in LEGI XML dump.

Goal: understand if we can extract "law X article Y modifies CGI article Z"
from the XML metadata even when article text is a stub like
"a modifié les dispositions suivantes".
"""

import json
import tarfile
import xml.etree.ElementTree as ET
from pathlib import Path

DATA_DIR = Path("data")
LEGI_ARCHIVE = DATA_DIR / "raw/legi/Freemium_legi_global.tar.gz"

# Load reference to get JORF IDs of finance laws
REFERENCE_FILE = DATA_DIR / "lois_cgi_1958_2026.json"

with open(REFERENCE_FILE) as f:
    ref_data = json.load(f)

# Build set of JORF IDs for finance laws (focus on pre-2002)
target_jorfs = {}
for loi in ref_data["lois"]:
    jorf = loi.get("jorf")
    if jorf:
        target_jorfs[jorf] = loi

print(f"Loaded {len(target_jorfs)} finance law JORF IDs")

# We'll collect articles that have LIENS and stub content
found_with_liens = []
articles_scanned = 0
MAX_EXAMPLES = 10  # Stop after finding enough examples

print(f"Opening {LEGI_ARCHIVE} (1.1 GB)...")

with tarfile.open(LEGI_ARCHIVE, "r:gz") as tf:
    for member in tf:
        if not member.name.endswith(".xml"):
            continue
        if "/article/" not in member.name:
            continue

        # Check if this belongs to one of our finance laws
        matched_jorf = None
        for jorf_id in target_jorfs:
            if jorf_id in member.name:
                matched_jorf = jorf_id
                break
        if not matched_jorf:
            continue

        f = tf.extractfile(member)
        if not f:
            continue

        xml_bytes = f.read()
        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError:
            continue

        articles_scanned += 1

        # Check for LIENS elements
        liens = root.findall(".//LIENS/LIEN")
        if not liens:
            continue

        # Check content - look for stub articles
        bloc = root.find(".//BLOC_TEXTUEL/CONTENU")
        content_text = ""
        if bloc is not None:
            content_text = ET.tostring(bloc, encoding="unicode", method="text").strip()

        is_stub = any(phrase in content_text.lower() for phrase in [
            "a modifié les dispositions suivantes",
            "modifie les dispositions suivantes",
            "a créé les dispositions suivantes",
            "a abrogé les dispositions suivantes",
            "modifications non reproduites",
        ])

        # Check if any LIEN references CGI (Code Général des Impôts)
        has_cgi_link = False
        for lien in liens:
            # Check typelien, nref, cidtexte attributes and text
            text = (lien.text or "").strip()
            attrs = dict(lien.attrib)
            if any("impôt" in str(v).lower() or "impot" in str(v).lower()
                   or "CGI" in str(v) or "LEGITEXT000006069577" in str(v)
                   for v in list(attrs.values()) + [text]):
                has_cgi_link = True
                break

        if is_stub and len(liens) > 0:
            loi_info = target_jorfs[matched_jorf]

            # Get article number
            num_el = root.find(".//META_ARTICLE/NUM")
            num = num_el.text.strip() if num_el is not None and num_el.text else "?"

            # Get article ID
            id_el = root.find(".//META_COMMUN/ID")
            art_id = id_el.text.strip() if id_el is not None and id_el.text else "?"

            entry = {
                "path": member.name,
                "jorf": matched_jorf,
                "loi": f"{loi_info['numero']} ({loi_info['type']})",
                "article_num": num,
                "article_id": art_id,
                "content_stub": content_text[:200],
                "has_cgi_link": has_cgi_link,
                "liens_count": len(liens),
                "liens": [],
            }

            for lien in liens:
                entry["liens"].append({
                    "attribs": dict(lien.attrib),
                    "text": (lien.text or "").strip(),
                })

            found_with_liens.append(entry)

            if len(found_with_liens) >= MAX_EXAMPLES:
                break

        if articles_scanned % 500 == 0:
            print(f"  Scanned {articles_scanned} articles, found {len(found_with_liens)} with LIENS+stub...")

    if len(found_with_liens) >= MAX_EXAMPLES:
        pass  # already broke out

print(f"\n{'='*80}")
print(f"Scanned {articles_scanned} articles from finance laws")
print(f"Found {len(found_with_liens)} stub articles with LIENS")
print(f"{'='*80}\n")

for i, entry in enumerate(found_with_liens):
    print(f"\n{'='*80}")
    print(f"EXAMPLE {i+1}: {entry['loi']} - Article {entry['article_num']}")
    print(f"  ID: {entry['article_id']}")
    print(f"  Path: {entry['path']}")
    print(f"  Content stub: {entry['content_stub']}")
    print(f"  Has CGI link: {entry['has_cgi_link']}")
    print(f"  Number of LIENS: {entry['liens_count']}")
    print(f"  LIENS details:")
    for j, lien in enumerate(entry["liens"]):
        print(f"    LIEN {j+1}:")
        for k, v in lien["attribs"].items():
            print(f"      @{k} = {v}")
        if lien["text"]:
            print(f"      text = {lien['text']}")
    print()

# Summary: check what types of IDs appear in liens
print(f"\n{'='*80}")
print("SUMMARY OF LIEN ID PATTERNS:")
print(f"{'='*80}")

id_patterns = {}
for entry in found_with_liens:
    for lien in entry["liens"]:
        for attr in ["cidtexte", "id"]:
            val = lien["attribs"].get(attr, "")
            if val:
                prefix = val[:10] if len(val) > 10 else val
                id_patterns.setdefault(attr, {}).setdefault(prefix, []).append(val)

for attr, patterns in id_patterns.items():
    print(f"\n  @{attr} prefixes:")
    for prefix, vals in sorted(patterns.items()):
        print(f"    {prefix}... ({len(vals)} occurrences, e.g. {vals[0]})")

# Check typelien values
print("\n  @typelien values:")
typelien_counts = {}
for entry in found_with_liens:
    for lien in entry["liens"]:
        tl = lien["attribs"].get("typelien", "?")
        typelien_counts[tl] = typelien_counts.get(tl, 0) + 1
for tl, count in sorted(typelien_counts.items()):
    print(f"    {tl}: {count}")

# Check sens values
print("\n  @sens values:")
sens_counts = {}
for entry in found_with_liens:
    for lien in entry["liens"]:
        s = lien["attribs"].get("sens", "?")
        sens_counts[s] = sens_counts.get(s, 0) + 1
for s, count in sorted(sens_counts.items()):
    print(f"    {s}: {count}")

# Check nature values
print("\n  @naturetexte values:")
nature_counts = {}
for entry in found_with_liens:
    for lien in entry["liens"]:
        n = lien["attribs"].get("naturetexte", "?")
        nature_counts[n] = nature_counts.get(n, 0) + 1
for n, count in sorted(nature_counts.items()):
    print(f"    {n}: {count}")
