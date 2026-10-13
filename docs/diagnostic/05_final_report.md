# Phase 6 — Rapport final priorisé

> ⚠️ **Errata** : ce rapport cite des statistiques (keyword counts, modification
> counts, titre 973) tirées de `data/article_modifications.json`. Le code de
> production utilise `data/modifications_cgi_enriched.json`. Les chiffres
> exacts sont dans [`08_final_validation.md`](08_final_validation.md#catalogue-corrigé-depuis-le-bon-fichier).
> Les conclusions architecturales restent valides. **La CR-7 (« métadonnée 973
> obsolète ») est à retirer** : le titre est correct dans le fichier de
> production.


> Diagnostic différentiel de la dégradation d'analyse d'intention sur articles
> CGI peu documentés / anciens (cibles : **973**, **39 C** ; référence positive :
> **150-0 B ter**).
>
> Source des preuves : `01_cause_tree.md`, `02_manual_oracle.md`,
> `03_code_map.md`, `04_differential_diagnosis.md`, et les trois benchmarks
> v1 baseline (`data/benchmarks/*-v1-baseline.json`) + logs `[DIAG:*]`
> capturés dans `/tmp/diag-api.log`.

---

## Résumé exécutif

| Article | Sources parl. (oracle SQL) | Sources parl. (bench) | Avg confidence | Sections "faible" |
|---|---|---|---|---|
| 150-0 B ter | 119 int. + 56 amend. | **17** | 2.0/3 | 2/8 |
| 973 | 32 int. + 24 amend. | **1** | 1.0/3 | 8/8 |
| 39 C | 53 int. + 12 amend. | **0** | 1.0/3 | 14/14 |

→ Le pipeline ne remonte que 5-15 % de ce que l'oracle SQL prouve disponible, et le
ratio s'effondre sur articles à vocabulaire sparse. Le LLM produit malgré tout
8-14 sections en remplissage avec confidence universellement "faible".

---

## 1 — Causes racines identifiées (par criticité)

### CR-1 🔴 — `_topical_relevance_cleanup` est l'EXÉCUTEUR principal de la dégradation
**Localisation** : `src/analysis/article_analyzer.py:1089-1148`
**Preuve** :
- `[DIAG:TOPICAL] art='973' input=N kept=0` répété 8/8 fois (toutes modifications).
- `[DIAG:TOPICAL] art='39 C' input=N kept=0 ou 1` sur 8/8 modifications.
- Pour 150-0 B ter le filtre supprime aussi 30-100 % par modification, mais
  d'autres modifications gardent assez (13/18, 9/23) pour produire ~17 sources finales.

**Mécanisme** :
1. Le filtre exige `weighted_score ≥ 0.6 OR hit_count ≥ 3` OR substring du ref.
2. `scoreable_kw` est l'intersection (article keywords ∩ multiword non-générique).
3. Pour 973/39 C : `scoreable_kw_count=9` → atteindre 0.6 demande 1 keyword
   très spécifique OU plusieurs keywords génériques rares — peu probable.
4. La branche substring (`ref_lower in content_lower`) ne retient que les chunks
   nommant explicitement l'article — rares pour les articles peu connus.

### CR-2 🔴 — `article_modifications.json` est déséquilibré (et contient des cross-références parasites)
**Localisation** : `data/article_modifications.json`
**Preuve** :
- 150-0 B ter : 55 keywords ; 973 : 10 ; 39 C : 16.
- Premier scoreable keyword pour 973 : **`"2° du iii de l'article 150-0 b ter"`** (cross-référence vers la *référence* qui pollue son propre filtre).
- Premier scoreable keyword pour 39 C : **`"articles 239 quater"`** (autre article CGI).
- Métadonnée titre 973 : **`"Droit de timbre - permis de conduire"`** (ancien usage avant IFI 2018).

**Impact** : alimente `_topical_relevance_cleanup`, `get_keywords_for_article`,
et toute la couche de pondération. Le déséquilibre se propage à tout le
pipeline aval.

### CR-3 🔴 — Substring `in` au lieu de regex contextualisé pour la détection de référence
**Localisation** :
- `src/analysis/article_analyzer.py:1123` (topical cleanup)
- `src/analysis/article_analyzer.py:492` (analyze_article — chemin mort mais à corriger)
- `src/indexing/search.py:1900-1902` (rescue pool)

**Preuve** :
- Pour "39 C", `"39 c" in "article 39 c bis"` → True (faux-positif).
- Pour "973", `"973" in "1973"` → True.
- La fonction correcte existe : `_build_contextualized_patterns()` (search.py:595-619)
  avec negative lookahead distinguant 39 C de 39 C bis/39 CA, mais elle est
  utilisée seulement à `search.py:3040`, pas dans les filtres aval.

### CR-4 🔴 — Expansions de vocabulaire hardcodées exclusivement pour 150-0
**Localisation** : `src/indexing/search.py:272-298` (`_get_fallback_expansions`)
**Preuve** : seules `150-0 B ter`, `150-0 B`, `150-0 A` ont des expansions
curatées. 973 (IFI) et 39 C (amortissement) tombent sur l'auto-génération
depuis `article_modifications.json`.

### CR-5 🔴 — Le fallback "family-aware Tier 2" du commit `0762fad` n'est pas sur le chemin actif de l'API
**Localisation** :
- Code de la Tier 2 : `src/analysis/article_analyzer.py:455-487` (dans `analyze_article`)
- API call site : `src/api/cgi_routes.py:712` appelle `_analyze_modification` (PAS `analyze_article`)
- Filtre actif : `_filter_chunks_by_article_relevance` (`article_analyzer.py:972+`)

**Preuve** :
- Mon instrumentation `[DIAG:FILTER]` placée dans `analyze_article` n'a généré
  AUCUN log durant les 3 benchmarks (vs. `[DIAG:TOPICAL]` qui fire pour chaque
  modification).
- Le filtre actif (logs `[enhanced_filter]`) est `_filter_chunks_by_article_relevance`,
  qui ne dispose PAS du Tier 2 family-aware introduit par le commit récent.

**Conséquence** : le fix qu'on croyait actif (et qui n'aide PAS les articles peu
documentés selon les logs) est en réalité un peu plus inquiétant — il est
DANS le code mais n'est jamais exécuté.

### CR-6 🟠 — `_filter_amendment_chunks` rejette 87-93 % des amendements
**Localisation** : `src/analysis/article_analyzer.py:1150+`
**Preuve** :
- 973 : `[amend_filter] 15 → 1 amendments (rejected 14)` répété
- 39 C : `[amend_filter] X → 0` fréquent
**Sévérité** : l'oracle 973 identifie 24 amendements pertinents dont 8 adoptés —
le filtre amendement n'en retient quasiment aucun.

### CR-7 🟠 — Métadonnée 973 obsolète (lignée ISF→IFI non trackée)
**Localisation** : `data/article_modifications.json` clé `"973"`
**Preuve** : `titre='Droit de timbre - permis de conduire'` ; `analysis_cache`
mélange 9 "modifications" dont 6 d'avant 2017 (qui concernaient l'ancien article
973, abrogé). L'oracle SQL identifie 3 lois post-IFI uniquement.

---

## 2 — Causes secondaires plausibles

### CS-1 🟡 — Couverture chunk_embeddings inégale pour la jurisprudence
Oracle SQL : 39 C a 183 jurisprudence, mais chunks/sources ratio = 25 %.
À investiguer : la jurisprudence est-elle correctement chunkée et embeddée
côté `chunk_embeddings` ? (Hors scope direct mais explique partiellement
pourquoi 39 C reste sec.)

### CS-2 🟡 — Filtre temporel dur ±18 mois pour articles très anciens
`src/indexing/search.py:1834-1857` : `mod_date - 540 days ≤ chunk_date ≤ mod_date + 30 days`.
Pour 39 C modifié en LFR 2014, des débats antérieurs peuvent contenir l'historique
mais ce filtre les exclut.

### CS-3 🟡 — Coupling reranker LF-article rescue capé par `top_k`
`src/indexing/search.py:1926` : `if len(reranked) < top_k`. Si `top_k=20`
chunks reranked ≥ 20, le rescue ne peut plus injecter. Question ouverte —
on n'a pas pu vérifier (DIAG:RERANK pas fired, vraisemblablement non rechargé
par le serveur).

### CS-4 🟡 — Optimisations historiques calibrées exclusivement sur 150-0 B ter
15 fichiers `data/benchmarks/150-0-B-ter_*` versionnés mars-avril, 0 pour
973/39 C avant ce diagnostic. Overfitting empirique probable des seuils.

---

## 3 — Liste priorisée d'améliorations

> Critères : impact, effort, **risque de régression sur 150-0 B ter** (audit obligatoire).
> Notation effort : XS = <1h, S = 1-3h, M = 1j, L = >1j.
> Notation impact : ★ à ★★★★★.

### P0 — Quick wins à fort impact (à faire en premier)

#### A1 — Brancher `_build_contextualized_patterns` sur `_topical_relevance_cleanup` et `_filter_chunks_by_article_relevance`
**Fichiers** : `src/analysis/article_analyzer.py:1123` (et 492), `src/indexing/search.py:1900-1902`
**Diff conceptuel** :
```python
# Remplacer la branche substring brute :
if ref_lower in content_lower or ref_nospace in content_lower:
    kept.append((chunk, score))
# par :
ctx_patterns = _build_contextualized_patterns(article_ref)  # cache au niveau article
if any(p.search(chunk.content) for p in ctx_patterns):
    kept.append((chunk, score))
```
**Impact attendu** : ★★★★ — élimine la conflation 39 C/39 C bis/39 CA et les
faux-positifs "973" dans textes contenant "1973", "973 €", n° de question 973.
Surtout : permet de DÉTECTER correctement les vraies mentions explicites pour 39 C
(actuellement le `0 explicit` est partiellement dû à la conflation forçant le rejet).
**Risque 150-0 B ter** : **faible** — pour les long refs, `_is_short_article_ref`
retourne False et `_build_contextualized_patterns` génère des variants larges
équivalentes à ce que fait `in` brut + `ref_nospace`. À valider via bench.
**Effort** : S (1-3h). Tests : rerun benchmarks, vérifier que kept ≥ baseline
pour 150-0 B ter.

#### A2 — Adapter le seuil de `_topical_relevance_cleanup` à la sparsité de l'article
**Fichier** : `src/analysis/article_analyzer.py:1138`
**Diff conceptuel** :
```python
# Au lieu de :
if weighted_score >= 0.6 or hit_count >= 3:

# Calibrer dynamiquement :
n_kw = len(scoreable_kw)
ws_threshold = 0.6 if n_kw >= 20 else (0.4 if n_kw >= 10 else 0.25)
hc_threshold = 3 if n_kw >= 20 else (2 if n_kw >= 10 else 1)
if weighted_score >= ws_threshold or hit_count >= hc_threshold:
```
**Impact attendu** : ★★★★★ — corrige la cause racine n°1. 973 et 39 C devraient
voir leur `kept` passer de 0 à au moins quelques chunks par modification.
**Risque 150-0 B ter** : **faible** (n_kw=29-30 → seuil reste 0.6, comportement identique).
Bench v1 + bench post-fix sur 150-0 B ter doivent être ≤ 5 % d'écart sur les métriques.
**Effort** : XS (30 min) + bench. **À faire en premier après A1**.

#### A3 — Nettoyer les cross-références parasites de `article_modifications.json`
**Fichier** : `data/article_modifications.json` (et le générateur sous-jacent
`scripts/` — à identifier via grep `article_modifications.json`).
**Action** : pour chaque article, supprimer des `keywords` toute string qui contient
"article" suivi d'une référence à un autre article CGI.
**Preuve à corriger** :
- 973 → `"2° du iii de l'article 150-0 b ter"` (vire vers la référence)
- 39 C → `"articles 239 quater"` (vire vers un autre article)

Et corriger la métadonnée :
- 973 → `titre = "Évaluation des biens immobiliers - IFI"` (au lieu du droit de timbre)

**Impact attendu** : ★★★ — supprime les keywords "fantômes" qui consomment du
poids de spécificité sans jamais hitter.
**Risque 150-0 B ter** : **nul** (ses keywords ne sont pas pollués).
**Effort** : S (1-3h pour audit + script de nettoyage + re-géneration).

#### A4 — Ajouter `_build_contextualized_patterns` à `_filter_chunks_by_article_relevance` ("enhanced_filter")
**Fichier** : `src/analysis/article_analyzer.py:972+`
**Action** : remplacer la détection `explicit` actuelle par les patterns regex
contextualisés. Le compteur `0 explicit` pour 973 et 39 C s'explique partiellement
par le fait que la détection actuelle est trop stricte ou trop laxiste — utiliser
les patterns contextualisés clarifie le comportement.
**Impact attendu** : ★★★ — la valeur compteur `explicit` deviendra fiable, et
les chunks réellement explicites passeront tous, sans confusion 39 C/39 C bis.
**Risque 150-0 B ter** : **faible** (cf. A1).
**Effort** : S.

### P1 — Améliorations structurelles (à programmer)

#### B1 — Activer la Tier 2 family-aware fallback sur le chemin actif
**Fichiers** : porter la logique `_chunk_passes_relaxed` (`article_analyzer.py:455`) +
le déclencheur Tier-2 (lignes 504-521) dans `_filter_chunks_by_article_relevance`
(article_analyzer.py:972) **OU** appeler la méthode after-fail de ce filtre.
**Preuve nécessaire** : DIAG en ré-exécution pour confirmer l'activation.
**Impact attendu** : ★★★ — pour les modifications où `enhanced_filter` retourne
0 chunks ("0 explicit, 0 same_debate, 0 other"), le family-aware permettrait
de récupérer des chunks LF-article ou des chunks discutant d'articles voisins.
**Risque 150-0 B ter** : **moyen** — les chunks "famille" sont moins précis ; un
filtre family-aware mal réglé peut polluer la référence. Bench obligatoire.
**Effort** : M (1j).

#### B2 — Étendre les keyword expansions curatées au-delà de la famille 150-0
**Fichier** : `data/legal_term_expansions.json` (cf. code-map agent : "9.1 KB,
curated manuellement : 150-0 B ter, 150-0 B, 150-0 A ONLY")
**Action** : ajouter des entrées pour les articles cibles importants. Exemples :
- `"973"`: ["IFI", "impôt sur la fortune immobilière", "valeur vénale immobilière", "dettes déductibles", "résidence principale", "actif immobilier", "société à prépondérance immobilière", "passif déductible"]
- `"39 C"`: ["amortissement réputé différé", "crédit-bail", "location avec option d'achat", "déduction étalée", "amortissement fiscal", "biens donnés en location", "BIC", "BNC"]
**Impact attendu** : ★★★★ — élargit la couverture tier-2 keyword search.
**Risque 150-0 B ter** : **nul** (additif).
**Effort** : S (lecture spécialiste + intégration).

#### B3 — Tests de régression sur articles "sparse" et "old"
**Fichier nouveau** : `tests/test_analysis_sparse_articles.py`
**Action** : pour chaque amélioration, un check non-régression sur (973, 39 C, 1052,
1043, 238 bis, 150-0 B ter) avec assertions souples (e.g.
`sources_parl_total ≥ floor(oracle/X)`).
**Impact attendu** : ★★ — empêche les futures régressions silencieuses.
**Risque 150-0 B ter** : nul.
**Effort** : M.

### P2 — Améliorations à enquêter

#### C1 — Indexation FTS sur `interventions.contenu`
**Justification** : `02_manual_oracle.md` observe que les regex sur les 7,2 M
interventions prennent 13-48s sans index FTS. La pipeline live ne peut pas
chercher en regex à chaque requête → l'enhanced_filter passe par `articles_mentionnes`
qui ne ramène que ce qui était pré-extrait, possiblement avec des trous.
**Action** : ajouter un index `tsvector` sur `interventions.contenu`, et
`amendements.contenu+expose_motifs`.
**Impact attendu** : ★★★ — élargit le recall en amont des filtres.
**Risque 150-0 B ter** : **faible** (additif, sauf si on remplace une logique
existante).
**Effort** : M (création index + adapter requêtes).

#### C2 — Re-évaluer la fenêtre temporelle ±18 mois
Pour 39 C très ancien, des débats hors fenêtre peuvent porter l'historique. Évaluer
si une fenêtre "élargie sur les premiers usages" aide pour les articles à modifications
quasi-annuelles.
**Effort** : S à investiguer, M à implémenter.

#### C3 — Corriger / supprimer le code mort `analyze_article` (lignes 489+)
Le commit `0762fad` (family-aware fallback) a été appliqué à une méthode jamais
appelée. Soit re-router le streaming endpoint vers `analyze_article`, soit
porter le fix à `_analyze_modification` (cf. B1).
**Effort** : S (audit + route).

---

## 4 — Recommandation : par où commencer

### Sprint 1 (1-2 jours, gain immédiat sur 973/39 C sans risque pour la référence)

1. **A2 — Calibration dynamique du seuil topical_cleanup** (XS, ★★★★★)
2. **A3 — Nettoyer `article_modifications.json` (cross-refs parasites + titre 973)** (S, ★★★)
3. Bench 973 + 39 C + 150-0 B ter post-fix → comparer aux v1-baseline.
4. **A1 — Remplacer `in` brut par `_build_contextualized_patterns`** dans topical_cleanup (S, ★★★★)
5. Re-bench. Confirmer que 150-0 B ter ne régresse pas (≤ 5 % écart).

**Métrique de succès Sprint 1** :
- 973 : passer de 1 source parlementaire à ≥ 10 (oracle = 32 int + 24 amend).
- 39 C : passer de 0 à ≥ 5 sources parlementaires.
- 150-0 B ter : maintenir 17 sources, avg confidence ≥ 1.9.

### Sprint 2 (3-5 jours, traitement structurel)

6. **B2 — Curated expansions pour 973, 39 C, autres articles cibles** (S, ★★★★)
7. **A4 — Brancher patterns contextualisés sur enhanced_filter** (S, ★★★)
8. **B1 — Activer Tier 2 family-aware sur le chemin actif** (M, ★★★)
9. **B3 — Tests de régression sparse-articles** (M, ★★)

### Sprint 3 (R&D)

10. **C1 — Index FTS sur interventions/amendements** (M, ★★★)
11. **C2 — Audit fenêtre temporelle** (S+M, ★★)
12. Investiguer l'indexation jurisprudence pour 39 C (CS-1).

---

## 5 — Rétro-suppression des `[DIAG:*]`

Pour retirer toutes les instrumentations ajoutées par ce diagnostic :
```bash
grep -rln '\[DIAG:' src/ scripts/
```

Les `[DIAG:FILTER]` (article_analyzer.py:490-560) sont DANS UNE MÉTHODE
NON UTILISÉE (`analyze_article`) — leur retrait est sans risque. Les
`[DIAG:TOPICAL]` (article_analyzer.py:1149-1156), `[DIAG:LLM]` (article_analyzer.py:2765-2771)
et `[DIAG:RERANK]` (search.py:1865-1934) sont des `logger.info` purs, ajout
seul, retrait par suppression de bloc.

---

## 6 — Limites du diagnostic

- **`[DIAG:RERANK]` n'a pas fired** — soit le serveur n'a pas rechargé `search.py`,
  soit le code path d'API n'exerce pas la rescue. À re-vérifier après restart du
  serveur.
- **`[DIAG:FILTER]` placé dans du code mort** — ne livre pas d'évidence sur le
  filtre actif `enhanced_filter`. Pour la prochaine session, déplacer le DIAG
  dans `_filter_chunks_by_article_relevance` (article_analyzer.py:972).
- **Pas de bench post-fix** — les bénéfices estimés sont fondés sur l'analyse
  de code, pas sur une mesure empirique. Le Sprint 1 doit valider chaque hypothèse.
- **Couverture jurisprudence non investiguée en profondeur** — l'asymétrie 39 C
  (183 oracle, peu remontant) suggère un problème côté chunking jurisprudence
  qui mérite un diagnostic séparé.
- **Le serveur tournant sur :8000 (instance utilisateur) n'a pas été touché** —
  toute la diagnostic s'est faite sur :8001 sans interférer.

---

**Tous les benchmarks v1-baseline et logs DIAG bruts sont conservés à :**
- `data/benchmarks/150-0-B-ter-v1-baseline.json`
- `data/benchmarks/973-v1-baseline.json`
- `data/benchmarks/39C-v1-baseline.json`
- `/tmp/diag-api.log`
