# Complete CGI Laws Before 2002 — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the `data/lois_cgi_1958_2026.json` file with missing laws that modified the CGI before 2002, by crawling all historical versions of CGI articles and adding known missing LFR.

**Architecture:** Two complementary approaches: (A) Enhance `extract_laws_from_article()` to iterate over all `articleVersions` of each CGI article (currently only the current version's `lienModifications` is used — tested: 14 laws found vs 2 for article 54 septies); (C) Complete the `_KNOWN_FINANCE_LAWS` list with missing LFR from 1959-1981.

**Tech Stack:** Python, httpx, Légifrance PISTE API (`/consult/getArticle`)

---

## Context

### Problem
The current collection script (`scripts/collect_cgi_laws.py`) calls `getArticle(article_id)` for each of ~3700 CGI articles and extracts `lienModifications`. But this only returns modifications for the **current in-force version**. Historical versions (accessible via `articleVersions` in the response) each have their own `lienModifications` pointing to the law that created that version.

Result: ~743 laws found, but significantly incomplete before ~1982 (e.g., no LFR before 1982, only 7 laws/year avg before 2002 vs 18/year after).

### Validated Approach
Test on article 54 septies (13 versions): `getArticle` on each version ID yields 14 unique modifying laws vs 2 from the current version alone.

### API Constraints
- `getArticleWithAncestorsAndChildren` returns 401 (not available on current API subscription)
- `getArticle(version_id)` works for all version IDs returned in `articleVersions`
- Rate: 0.05s delay between requests, 8 parallel workers
- Estimate: ~3700 articles × ~5 extra versions avg = ~18,500 additional API calls (~25 min with current settings)

---

## Chunk 1: Deep Version Crawling

### Task 1: Add `--deep` flag and modify `extract_laws_from_article()`

**Files:**
- Modify: `scripts/collect_cgi_laws.py:237-273` (extract_laws_from_article)
- Modify: `scripts/collect_cgi_laws.py:760-810` (main, argparse + phase 2)

- [ ] **Step 1: Add `deep` parameter to `extract_laws_from_article()`**

In `scripts/collect_cgi_laws.py`, modify the function signature and add version iteration after the existing extraction logic:

```python
def extract_laws_from_article(client: LegifranceCodeClient,
                              article: dict,
                              deep: bool = False) -> list[dict]:
    """Extract all modifying laws from an article via getArticle endpoint.

    getArticle returns fields: lienModifications, lienCitations, lienAutres
    with keys: linkType, textTitle, textCid, numTexte, natureText, datePubli

    If deep=True, also fetches all historical versions of the article
    and extracts their lienModifications (captures older modifications
    not linked from the current version).
    """
    article_id = article["id"]
    laws = []

    try:
        data = client.get_article(article_id)
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (404, 400, 403):
            logger.debug(f"Article {article['num']}: {e.response.status_code}")
            return []
        raise

    art_data = data.get("article")
    if not art_data or not isinstance(art_data, dict):
        return []

    # Extract from lienModifications (CREATION, MODIFIE, etc.)
    for lien in (art_data.get("lienModifications") or []):
        law_info = _parse_law_from_lien(lien)
        if law_info:
            laws.append(law_info)

    # Also scan lienCitations for LOI/ORDONNANCE references
    for lien in (art_data.get("lienCitations") or []):
        nature = (lien.get("natureText") or "").upper()
        if nature in ("LOI", "ORDONNANCE"):
            law_info = _parse_law_from_lien(lien)
            if law_info:
                laws.append(law_info)

    # Deep mode: fetch all historical versions' modifications
    if deep:
        versions = art_data.get("articleVersions", [])
        seen_ids = {article_id}
        for version in versions:
            vid = version.get("id")
            if not vid or vid in seen_ids:
                continue
            seen_ids.add(vid)
            try:
                vdata = client.get_article(vid)
            except httpx.HTTPStatusError:
                continue
            vart = vdata.get("article")
            if not vart or not isinstance(vart, dict):
                continue
            for lien in (vart.get("lienModifications") or []):
                law_info = _parse_law_from_lien(lien)
                if law_info:
                    laws.append(law_info)

    return laws
```

- [ ] **Step 2: Add `--deep` CLI argument in `main()`**

After the existing `--workers` argument (line ~773):

```python
parser.add_argument("--deep", action="store_true",
                    help="Fetch all historical versions of each article "
                         "(slower but finds older modifications)")
```

- [ ] **Step 3: Pass `deep` to `extract_laws_from_article` in the ThreadPoolExecutor**

In `main()`, modify the `executor.submit` call (line ~813):

```python
futures = {
    executor.submit(extract_laws_from_article, client, art, args.deep): art
    for art in remaining
}
```

- [ ] **Step 4: Commit**

```bash
git add scripts/collect_cgi_laws.py
git commit -m "feat(collect): add --deep flag to crawl all article versions"
```

---

## Chunk 2: Complete Known Finance Laws

### Task 2: Add missing LFR 1959-1981 to injection lists

**Files:**
- Modify: `scripts/collect_cgi_laws.py:439-461` (historical_lfr list)
- Modify: `scripts/collect_cgi_laws.py:625-701` (_KNOWN_FINANCE_LAWS)

- [ ] **Step 1: Add missing LFR to `historical_lfr` list**

Insert before the existing first entry `("82-540", 1982)` in the `historical_lfr` list (line ~440):

```python
    historical_lfr = [
        # Pre-1982 LFR (not discovered by article crawl)
        ("60-706", 1960), ("60-859", 1960),
        ("61-825", 1961),
        ("62-873", 1962),
        ("63-628", 1963), ("63-1293", 1963),
        ("64-1278", 1964),
        ("65-1154", 1965),
        ("66-948", 1966),
        ("69-1160", 1969),
        ("71-1025", 1971),
        ("72-1147", 1972),
        ("74-1114", 1974),
        ("75-408", 1975),
        ("76-978", 1976),
        ("78-653", 1978),
        ("80-1039", 1980),
        ("81-734", 1981), ("81-1179", 1981),
        # Post-1982 (existing)
        ("82-540", 1982), ("85-1404", 1985), ("86-824", 1986),
        ...  # rest unchanged
    ]
```

- [ ] **Step 2: Add corresponding entries to `_KNOWN_FINANCE_LAWS`**

Add entries with full metadata (titre, date, JORF) to `_KNOWN_FINANCE_LAWS` (line ~625). These will be injected if the deep crawl still misses them:

```python
    # Pre-1982 LFR
    ("60-706", "LFR", 1960,
     "Loi n° 60-706 du 21 juillet 1960 de finances rectificative pour 1960",
     "1960-07-21", "JORFTEXT000000512208"),
    ("61-825", "LFR", 1961,
     "Loi n° 61-825 du 29 juillet 1961 de finances rectificative pour 1961",
     "1961-07-29", ""),
    ("63-1293", "LFR", 1963,
     "Loi n° 63-1293 du 21 décembre 1963 de finances rectificative pour 1963",
     "1963-12-21", ""),
    ("64-1278", "LFR", 1964,
     "Loi n° 64-1278 du 23 décembre 1964 de finances rectificative pour 1964",
     "1964-12-23", ""),
    ("65-1154", "LFR", 1965,
     "Loi n° 65-1154 du 30 décembre 1965 de finances rectificative pour 1965",
     "1965-12-30", ""),
    ("66-948", "LFR", 1966,
     "Loi n° 66-948 du 22 décembre 1966 de finances rectificative pour 1966",
     "1966-12-22", ""),
    ("69-1160", "LFR", 1969,
     "Loi n° 69-1160 du 24 décembre 1969 de finances rectificative pour 1969",
     "1969-12-24", ""),
    ("72-1147", "LFR", 1972,
     "Loi n° 72-1147 du 23 décembre 1972 de finances rectificative pour 1972",
     "1972-12-23", ""),
    ("74-1114", "LFR", 1974,
     "Loi n° 74-1114 du 27 décembre 1974 de finances rectificative pour 1974",
     "1974-12-27", ""),
    ("76-978", "LFR", 1976,
     "Loi n° 76-978 du 29 octobre 1976 de finances rectificative pour 1976",
     "1976-10-29", ""),
    ("78-653", "LFR", 1978,
     "Loi n° 78-653 du 22 juin 1978 de finances rectificative pour 1978",
     "1978-06-22", ""),
    ("80-1039", "LFR", 1980,
     "Loi n° 80-1039 du 23 décembre 1980 de finances rectificative pour 1980",
     "1980-12-23", "JORFTEXT000000339354"),
    ("81-734", "LFR", 1981,
     "Loi n° 81-734 du 3 août 1981 de finances rectificative pour 1981",
     "1981-08-03", ""),
```

Note: JORF IDs left empty for entries where not confirmed — the deep crawl should fill most of these.

- [ ] **Step 3: Commit**

```bash
git add scripts/collect_cgi_laws.py
git commit -m "feat(collect): add known LFR 1959-1981 to injection lists"
```

---

## Chunk 3: Run and Verify

### Task 3: Run enhanced collection

- [ ] **Step 1: Run the collection with `--deep` flag**

```bash
python scripts/collect_cgi_laws.py --deep --workers 8
```

Expected: ~25-40 minutes. Uses checkpoint system so can be resumed with `--resume --deep` if interrupted.

- [ ] **Step 2: Compare before/after**

```bash
python3 -c "
import json
with open('data/lois_cgi_1958_2026.json') as f:
    data = json.load(f)
lois = data['lois']
from collections import Counter
by_year = Counter(l['annee_budgetaire'] for l in lois)
print(f'Total: {len(lois)}')
for y in range(1958, 2002):
    print(f'{y}: {by_year.get(y, 0)}')
# Check LFR coverage
lfr = [l for l in lois if l['type'] == 'LFR']
print(f'LFR total: {len(lfr)}')
print(f'Earliest LFR: {min(l[\"annee_budgetaire\"] for l in lfr)}')
"
```

Expected: significantly more laws before 2002, LFR starting from 1960s.

- [ ] **Step 3: Verify no regressions post-2002**

```bash
python3 -c "
import json
with open('data/lois_cgi_1958_2026.json') as f:
    new = json.load(f)
# Ensure post-2002 data is unchanged or only grew
print(f'Total: {new[\"stats\"][\"total_laws\"]}')
print(f'By type: {json.dumps(new[\"stats\"][\"by_type\"], indent=2)}')
"
```

- [ ] **Step 4: Commit updated data**

```bash
git add data/lois_cgi_1958_2026.json
git commit -m "data: update CGI laws with deep version crawl results"
```
