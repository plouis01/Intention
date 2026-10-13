# 09 — Expérience v2.3 : TF-IDF dynamique (rejetée par bench)

> **Status** : rejected. Architecture v2.2 reste la version de production.
> Cette note documente l'expérience pour éviter de la reproduire et pour
> identifier la voie correcte si l'on souhaite re-tenter.

## Hypothèse testée

Pour les articles ultra-sparse (73.8 % du catalogue : kw ≤ 7) dont l'art. 77
est le cas-école (toutes les kw sont des article-refs strippées par la sanity),
on a postulé qu'**augmenter `article_info.keywords` au runtime via TF-IDF**
sur le corpus parlementaire local (interventions citant l'article comme
référence CGI dans un dossier LF/LFR/LFSS) **lèverait le bottleneck du
filtre `_topical_relevance_cleanup`**.

## Conception

Module `src/analysis/dynamic_keywords.py` (rejeté, supprimé du repo).

- **Corpus local** : `interventions.contenu` ILIKE sur patterns
  CGI-stricts ("article X du code général des impôts", "art. X du CGI",
  etc.), joint à `dossiers_legislatifs` filtré par `loi.type ∈ {LF, LFR, LFSS}`.
- **n-grammes** : bigrammes + trigrammes de mots français (3+ chars,
  alphabétique), stopwords filtrés (déterminants, pronoms, verbes auxiliaires,
  vocabulaire parlementaire générique).
- **Score** : TF (local) × IDF (sample 10 k interventions, lru_cache(1)).
  Seuils : `min_idf=3.0`, `min_doc_freq=2`, `min_corpus_size=3`.
- **Gating** : augmentation **uniquement si** `multiword_kw < 15` (count
  pré-cleanup). 150-0 B ter avec 32 multiword reste intouché.
- **Intégration** : `cgi_routes.py` entre era-cutoff et le timeline,
  mutation de `article_info.keywords`.

## Résultats benchs v2.3

| Article | Profile | v2.2 (réf) | v2.3 (TF-IDF) | Δ |
|---|---|---|---|---|
| 150-0 B ter | rich, gate skip | 18 / 2.12 | 18 / 2.12 (gated) | **0** |
| 973 | sparse, augmented +15 | 5 / 1.67 | 5 / 1.67 | **0** |
| 39 C | sparse, augmented +15 | 2 / 1.0 | **0 / 1.0** | **–2** ❌ |
| 77 | ultra-sparse, augmented +15 | 0 / 1.0 | 0 / 1.0 | **0** |

→ **Aucune amélioration mesurée. Régression sur 39 C.**

## Pourquoi ça n'a pas marché

### R1 — Le bottleneck n'est pas en aval, il est en amont
Pour les articles ultra-sparse (cas 77), le pipeline a déjà éliminé la
plupart des candidats AVANT `_topical_relevance_cleanup`. Le filtre topical
reçoit **1 seul chunk** pour 77. Augmenter ses keywords ne fait rien quand
il n'y a rien à filtrer. Le DIAG le confirme :
```
[DIAG:TOPICAL] art='77' input=1 kept=0 scoreable_kw_count=15 ws_max=0.000 hc_max=0
```
Les 15 nouvelles kw augmentées ne hit aucune fois dans le seul chunk d'entrée.

### R2 — Petits corpus = TF-IDF biaisé vers boilerplate
973 a 6 documents dans son corpus local CGI-strict. Les n-grammes les plus
fréquents y sont :
```
('taux perte', 5.68, 4)
('taux perte recettes', 5.68, 4)
('additionnelle accise', 4.68, 4)
('tabacs prévue', 4.68, 4)
```
C'est la **formule de gage des amendements** ("perte de recettes compensée
par une taxe additionnelle sur les accises sur les tabacs prévue à
l'article…"), récitée dans 4 des 6 interventions. Même avec `min_idf=3.0`
qui filtre les n-grammes trop globaux, ces n-grammes restent (rares
hors finance-law mais omniprésents dans les interventions amendement).
Bumping `min_idf` à 5 supprimerait ces gages, mais aussi des candidats
topiques légitimes ("crédit bail" idf 3.33, "option achat" idf 3.04).

### R3 — Sur articles modérément sparse, l'augmentation déplace la médiane
Pour 39 C, l'augmentation a ajouté 15 kw topiquement valides
("crédit bail", "soumise régime", "option achat"). MAIS leur ajout au
`scoreable_kw` change la distribution observée par
`_topical_relevance_cleanup` (médiane adaptative). Chunks qui passaient
sur 8 keywords avec hit_count ≥ 1 ne passent plus sur 23 keywords avec
median(hits) = 2. Le filtre devient plus strict ; quelques chunks que le
LLM aurait cités basculent en dessous du cut → 2 sources → 0 source.

Le scale-invariant filter v2.2 est **équilibré par construction** sur la
distribution NATURELLE des keywords ; injecter des kw au runtime DÉSÉQUILIBRE
cette distribution.

## Voie correcte (future itération)

Si l'on souhaite re-tenter, le levier est en amont, pas en aval :

- **Hybrid retrieval avec query expansion** : injecter les n-grammes topiques
  comme **termes de recherche** dans `search_article_modification`, pas
  comme keywords de filtrage. Cela ramènerait plus de candidats pertinents
  pour 77 (corpus de search élargi) sans déséquilibrer le filtre topical
  qui resterait calibré sur les kw curatées.
- **Pré-calcul offline** : la latence de `_fetch_corpus` (90-110 s par
  article au premier hit) est rédhibitoire pour la production. Un script
  qui pré-calcule les keywords TF-IDF pour les 4380 articles et persiste
  dans `data/dynamic_keywords_cache.json` éliminerait le coût runtime.
- **IDF plus discriminant** : sample > 50 k, séparer corpus
  amendement vs intervention pour identifier le gage. Filtre n-grammes
  contenant des codes (livre, chapitre, titre).

## Décision

Architecture **v2.2 reste la production**. Les commits v2.2 (17cc7ba +
d98c4da) ne sont pas modifiés. Le module `dynamic_keywords.py` n'est PAS
ajouté au repo.

Le seul commit lié à cette expérience est cette note documentaire.
