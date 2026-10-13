# Phase 3 — Code Map: modules-clés du pipeline d'analyse

> Cartographie produite par l'agent Explore (read-only).
> Toutes les références de ligne sont à confirmer en Phase 5 par lecture directe
> avant d'écrire toute modification.

---

## 1. Benchmark Driver (`scripts/benchmark_analysis.py:22-315`)

- **Entrée** : `run_benchmark(article_ref, base_url, output_path)` appelle
  `/cgi/{article_ref}/analysis/stream?force_refresh=true` (ligne 29).
- **Appelle le LLM** : OUI (indirectement via l'API). Mesure le coût en tokens
  par phase (lignes 227-231).
- **CLI** : `--article` (default "150-0 B ter"), `--output`, `--compare`, `--url`.

---

## 2. Search Service (`src/indexing/search.py`)

### 2.1 Keyword Expansion

**HARDCODED PLUS-VALUES (lignes 275-298)** — UNIQUEMENT pour "150-0 B ter" :
```python
"150-0 B ter": ["apport-cession", "report d'imposition", "plus-value en report",
                "plus-value d'apport", "holding", "reinvestissement", ...]
```
- **973** et **39 C** : ZÉRO expansion curatée. Fallback uniquement sur les
  keywords de l'index sparse.
- **Impact** : 150-0 B ter bénéficie d'un vocabulaire métier ; les autres non.

**Variantes d'identifiant article** (lignes 457-492) :
- Refs courtes (973, 39 C) : variantes scopées CGI ("article 973 du c")
  pour éviter les faux-positifs.
- Refs longues (150-0 B ter) : variantes larges (minuscule, sans tirets).
- Discriminator : `_is_short_article_ref()` ligne 576 — True si ≤3 chiffres + pas
  de tiret.

**Best ILIKE keywords** (ligne 382, `top_k=7`) :
- Filtre >5 chars, ≤40 chars, pas de virgules, pas de chiffres seuls.
- Priorise multiword non-générique de la liste curatée puis longest-first.

### 2.2 Pipeline & Calibrations

**Hybrid search** (lignes 994-1149) :
- `keyword_boost = 0.25` (ligne 1001)
- `min_similarity = 0.30` (config.py:130)
- Speaker boosts : Rapporteur général +0.08, Rapporteur +0.06, Ministre +0.05,
  Pdt Commission +0.04.

**Poids depuis settings** (config.py:170-173) :
- `signal_weight_semantic: 0.20`
- `signal_weight_article_ref: 0.20`
- `signal_weight_keywords: 0.15`
- `signal_weight_speaker: 0.15`

### 2.3 Reranker Rescue (commit 4cdcef0)

`search.py:1865-1935`
- Sauve les chunks avec ref CGI explicite OU mentionnant l'article modificateur
  de la loi (ligne 1908).
- Ré-injecte les chunks coupés par le reranker s'ils mentionnent l'article-LF
  (ligne 1926, limite = `top_k`).
- **Critique pour les articles courts** où la mention de l'article-LF est le
  seul signal explicite.
- **Max content** : 1500 chars (reranker.py:12).

### 2.4 Chamber-Aware Search Pipeline

`search_chamber()` (lignes 1663-1943) — Triple-anchor :
1. `article_in_law` dans `articles_mentionnes` (n° d'article LF)
2. Ref CGI dans `articles_mentionnes` OU `contenu` (ILIKE)
3. Keyword fallback (AND pairs d'abord, puis OR) — déclenché uniquement si
   `<3 debates` trouvés (ligne 2059).

**Filtre temporel dur** (lignes 1834-1857) :
- `mod_date - 540 days ≤ chunk_date ≤ mod_date + 30 days` (lookback 18 mois).
- Risque : pour vieux articles modifiés par retouches mineures, la "date de
  modification" du sous-article peut être très éloignée du débat substantiel.

---

## 3. Article Analyzer (`src/analysis/article_analyzer.py`)

### 3.1 Dynamic Keyword Tiering — `_compute_keyword_tiers()` (lignes 794-849)

Classification mise en cache, data-driven :
- **always_generic** : nombres, %, mots-uniques ≥ 0.5% fréquence index.
- **downweight** : termes juridiques multiword ≥ 2% fréquence index.
- **keep** : tout le reste (spécifique, poids max).

Exemples :
- "titres", "capital" → always_generic
- "impôt sur les sociétés" → downweight
- "apport-cession", "report d'imposition" → keep (spécifique 150-0 B ter)

**Risque de biais** : articles avec <5 modifications (973 : 3, 39 C : 6) sont
sous-représentés dans le calcul de fréquence.

### 3.2 Three-Tier Article Relevance Filter

**Phase 1 (strict, lignes 488-502)** : Mention article explicite OU aucun autre
article CGI cité. Supprime les chunks discutant d'articles voisins.

**Phase 2 (family-aware, lignes 503-513)** — **UNIQUEMENT si Phase 1 a yield ≥1 chunk** :
- Accepte les chunks mentionnant l'article cible OU citant uniquement des
  articles de la famille cross-reference.
- Famille cross-reference définie ligne 481-484.
- ⚠️ **CRITIQUE** : ligne 503 condition `elif debate_chunks:` — Tier 2 ne
  s'active QUE si Phase 1 avait du contenu pour démarrer.

**Phase 3 (last-resort, lignes 514-520)** : Garde non-filtré si les deux
échouent. Préserve le recall ; log un WARNING (ligne 515).

**Diagnostic** : Articles peu documentés (973, 39 C) peuvent **sauter Tier 2
entièrement** si Phase 1 échoue.

### 3.3 Topical Relevance Cleanup — `_topical_relevance_cleanup()` (lignes 1088-1147)

Scoring spécificité-pondéré :
- `specificity = 1 / log2(1 + article_count_in_index)`
- Exemple : "apport-cession" unique à 150-0 B ter → score ≈ 0.63.
- Keep si : `weighted_score ≥ 0.6` OU `hit_count ≥ 3`.

**Calibration-critique** : Articles à peu de keywords ET peu de modifs
scorent plus haut par-keyword → cleanup agressif pour 973 et 39 C.

---

## 4. Fichiers de données

### `article_modifications.json` (14.2 MB)
- 150-0 B ter : **55 keywords, 17 modifications** ✓
- 973 : **10 keywords, 3 modifications** (82% moins de keywords que la réf)
- 39 C : **16 keywords, 6 modifications** (71% moins)

> Sparsity du fichier suggère une indexation Légifrance incomplète pour
> 973 et 39 C.

### `legal_term_expansions.json` (9.1 KB)
- Curated MANUELLEMENT : "150-0 B ter", "150-0 B", "150-0 A" UNIQUEMENT.
- Tous les autres : auto-generated depuis l'index.

---

## 3 Findings calibration les plus suspects

### 1. Vocabulaire plus-values hardcodé exclusivement pour 150-0 B ter
**`search.py:275-298`** — Les expansions curatées de fallback existent
UNIQUEMENT pour "150-0 B ter". Articles 973 et 39 C ont zéro expansion curatée.
→ Confirmation directe des hypothèses E.1.2 et I.2 de l'arbre.

### 2. Tier 2 family-aware ne se déclenche QUE si Tier 1 a déjà yield
**`article_analyzer.py:503`** — Condition `elif debate_chunks:`. Pour articles
peu documentés (973 : 3 mods, 10 keywords ; 39 C : 6 mods, 16 keywords),
Phase 1 strict est plus susceptible d'être vide → Tier 2 est sauté → Tier 3
(non-filtré) prend la main, sacrifiant la précision.
→ Confirmation directe de l'hypothèse F.1.2 de l'arbre.

### 3. Cleanup topical biaisé par la rareté de l'article
**`article_analyzer.py:1135-1137`** — `if weighted_score >= 0.6 or hit_count >= 3`.
La spécificité varie à l'inverse de la fréquence : articles rares scorent plus
haut par-keyword → seuil 0.6 plus facile à passer individuellement, mais
combiné avec la perte de Tier 2 → cleanup agressif.

---

**Note** : ces findings sont à confirmer par lecture directe en Phase 5 avant
toute proposition de patch.
