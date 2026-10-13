"""TEMPORARY - broader exploration of LIENS across many finance laws.

Questions:
1. How many total stub articles with LIENS exist across all finance laws?
2. What typelien values exist beyond MODIFICATION/CREATION?
3. What cidtexte values appear (CGI vs other codes)?
4. Do any LIENS reference non-CODE targets (e.g. other laws)?
5. Can we count total CGI article modifications extractable this way?
"""

import json
import tarfile
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

DATA_DIR = Path("data")
LEGI_ARCHIVE = DATA_DIR / "raw/legi/Freemium_legi_global.tar.gz"
REFERENCE_FILE = DATA_DIR / "lois_cgi_1958_2026.json"

CGI_CIDTEXTE = "LEGITEXT000006069577"

with open(REFERENCE_FILE) as f:
    ref_data = json.load(f)

target_jorfs = {}
for loi in ref_data["lois"]:
    jorf = loi.get("jorf")
    if jorf:
        target_jorfs[jorf] = loi

print(f"Loaded {len(target_jorfs)} finance law JORF IDs")

# Counters
articles_scanned = 0
stub_articles = 0
stub_with_liens = 0
stub_with_cgi_liens = 0
total_liens = 0
total_cgi_liens = 0

typelien_counter = Counter()
sens_counter = Counter()
nature_counter = Counter()
cidtexte_counter = Counter()
loi_stub_count = Counter()  # how many stubs per law
loi_cgi_links = Counter()  # how many CGI links per law
cgi_articles_modified = set()  # unique CGI articles referenced

# Examples of non-CGI liens
non_cgi_examples = []

print(f"Opening {LEGI_ARCHIVE}...")

with tarfile.open(LEGI_ARCHIVE, "r:gz") as tf:
    for member in tf:
        if not member.name.endswith(".xml"):
            continue
        if "/article/" not in member.name:
            continue

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

        # Check content
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
            "a transféré les dispositions suivantes",
        ])

        if not is_stub:
            continue

        stub_articles += 1
        loi_info = target_jorfs[matched_jorf]
        loi_key = f"{loi_info['numero']} ({loi_info['type']})"

        liens = root.findall(".//LIENS/LIEN")
        if not liens:
            continue

        stub_with_liens += 1
        loi_stub_count[loi_key] += 1

        num_el = root.find(".//META_ARTICLE/NUM")
        num = num_el.text.strip() if num_el is not None and num_el.text else "?"

        has_cgi = False
        for lien in liens:
            attrs = dict(lien.attrib)
            total_liens += 1
            typelien_counter[attrs.get("typelien", "?")] += 1
            sens_counter[attrs.get("sens", "?")] += 1
            nature_counter[attrs.get("naturetexte", "?")] += 1

            cid = attrs.get("cidtexte", "")
            if cid:
                # Shorten for display
                cidtexte_counter[cid] += 1

            if cid == CGI_CIDTEXTE:
                total_cgi_liens += 1
                has_cgi = True
                cgi_art_num = attrs.get("num", "?")
                cgi_art_id = attrs.get("id", "?")
                cgi_articles_modified.add(cgi_art_num)
                loi_cgi_links[loi_key] += 1
            elif len(non_cgi_examples) < 5:
                non_cgi_examples.append({
                    "loi": loi_key,
                    "article": num,
                    "lien_text": (lien.text or "").strip(),
                    "cidtexte": cid,
                    "naturetexte": attrs.get("naturetexte", "?"),
                    "typelien": attrs.get("typelien", "?"),
                })

        if has_cgi:
            stub_with_cgi_liens += 1

        if articles_scanned % 1000 == 0:
            print(f"  Scanned {articles_scanned} articles, {stub_with_liens} stubs with liens...")

print(f"\n{'='*80}")
print(f"RESULTS")
print(f"{'='*80}")
print(f"\nTotal articles scanned from finance laws: {articles_scanned}")
print(f"Stub articles (no real content):          {stub_articles}")
print(f"Stub articles WITH LIENS:                 {stub_with_liens}")
print(f"Stub articles with CGI LIENS:             {stub_with_cgi_liens}")
print(f"Total LIEN elements:                      {total_liens}")
print(f"Total CGI LIEN elements:                  {total_cgi_liens}")
print(f"Unique CGI articles referenced:           {len(cgi_articles_modified)}")

print(f"\n--- @typelien distribution ---")
for val, count in typelien_counter.most_common():
    print(f"  {val}: {count}")

print(f"\n--- @sens distribution ---")
for val, count in sens_counter.most_common():
    print(f"  {val}: {count}")

print(f"\n--- @naturetexte distribution ---")
for val, count in nature_counter.most_common():
    print(f"  {val}: {count}")

print(f"\n--- @cidtexte distribution (top 20) ---")
for cid, count in cidtexte_counter.most_common(20):
    label = "CGI" if cid == CGI_CIDTEXTE else cid
    print(f"  {label}: {count}")

print(f"\n--- Laws with most CGI modification links (top 20) ---")
for loi, count in loi_cgi_links.most_common(20):
    print(f"  {loi}: {count} CGI links")

print(f"\n--- Non-CGI LIEN examples ---")
for ex in non_cgi_examples:
    print(f"  {ex['loi']} art. {ex['article']}: {ex['lien_text']}")
    print(f"    cidtexte={ex['cidtexte']} nature={ex['naturetexte']} type={ex['typelien']}")

print(f"\n--- Sample CGI articles modified (first 30) ---")
for art in sorted(cgi_articles_modified)[:30]:
    print(f"  CGI art. {art}")
