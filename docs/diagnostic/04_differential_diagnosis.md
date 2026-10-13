# Phase 5 — Diagnostic différentiel

> Pour chaque branche de l'arbre des causes (Phase 1), on confronte aux preuves
> récoltées via : oracle SQL (Phase 2), code map (Phase 3),
> logs `[DIAG:*]` (instrumentation), benchmarks v1 baseline (Phase 4).
>
> Discipline : **une cause = au moins une preuve concrète**. Pas d'intuition.

---

## Tableau de scoring synthétique (baseline v1)

| Métrique | 150-0 B ter (REF) | 973 | 39 C | Source |
|---|---|---|---|---|
| Sections analysées | 8 | 8 | 14 | bench JSON |
| Sources AN totales | 4 | **0** | **0** | bench JSON |
| Sources Sénat totales | 13 | **1** | **0** | bench JSON |
| Sources parl totales | 17 | 1 | **0** | bench JSON |
| Avg confidence (échelle 0-3) | **2.0** | **1.0** | **1.0** | bench JSON |
| Sections "faible" | 2/8 | **8/8** | **14/14** | bench JSON |
| Cross-references | 0 | 0 | 0 | bench JSON |
| Temps total (s) | 321 | 320 | 544 | bench JSON |
| Section max (s) | 43 | 66 | **180** (LLM en panique) | bench JSON |
| Oracle SQL : interventions | 119 | 32 | 53 | `02_manual_oracle.md` |
| Oracle SQL : amendements | 56 | 24 | 12 | `02_manual_oracle.md` |
| Oracle SQL : sources externes | 162 | 10 | 205 | `02_manual_oracle.md` |
| Oracle SQL : chunks embeddings | 190 | 58 | 67 | `02_manual_oracle.md` |
| article_modifications.json : keywords | 55 | 10 | 16 | code-map agent |
| article_modifications.json : modifications | 17 | 3 | 6 | code-map agent |

→ **Ratio oracle/bench** : 150-0 B ter remonte 17 sources sur 337 oracle (~5 %),
973 remonte 1 sur 66 oracle (~1.5 %), soit **3× plus mauvais** que la référence.

---

## A. Couverture des sources amont

### A.1.1 — Légifrance n'a pas l'historique antérieur de 39 C 🟢 *partiellement confirmé*

L'oracle SQL trouve **53 interventions ancrées** de 1984 à 2025 pour 39 C. Donc
les sources EXISTENT dans la base. La couverture amont n'est PAS le facteur
limitant pour 39 C — c'est le **traitement aval** qui décroche.

### A.1.2 — 973 lignée ISF→IFI 🔴 *confirmé*

Preuves convergentes :
- `article_modifications.json` indexe 973 avec le titre **`'Droit de timbre - permis de conduire'`** (ancien usage avant IFI) → la métadonnée est obsolète et fausse (cf. `01_inspect`).
- `analysis_cache.modification_count=9` pour 973 mais l'oracle SQL n'identifie que **3 lois** modifiant l'art 973 actuel (LF 2018, LF 2019, LF 2024). Les 6 autres "modifications" sont d'anciennes lois (1976, 1999, 2001…) qui visaient l'ancien article 973 (droit de timbre permis de conduire), totalement déconnecté du régime IFI.
- L'analyse cachée mélange donc des modifications d'articles différents portant le même identifiant — **confusion historique typique de l'absence de tracking des refontes**.

### A.1.3 / A.1.4 — DILA et Sénat archives 🟡 *à creuser hors scope*

Pas de preuve directe que la couverture amont est défaillante pour les périodes
problématiques. À explorer si on monte un budget de re-collecte ciblé.

---

## B. Parsing & normalisation

### B.1.1 — "39 C" / "39 C bis" / "39 CA" : conflation par substring 🔴 *confirmé*

`article_analyzer.py:1123` (topical_cleanup) :
```python
if ref_lower in content_lower or ref_nospace in content_lower:
```
`article_analyzer.py:492` (le filter dans le code mort `analyze_article`) :
```python
if article_info.reference.lower() in chunk.content.lower():
```
Les deux utilisent `in` brut → `"39 c" in "article 39 c bis du cgi"` → True (faux-positif).

La fonction `_build_contextualized_patterns()` (search.py:595-619) existe avec
negative-lookahead correct (`(?-i:[A-H])(?:[\s.,;:)]|$)`) pour distinguer
"39 C" de "39 C bis"/"39 CA", **mais elle n'est utilisée nulle part dans le
flux de filtrage final**. Grep : utilisée seulement à `search.py:3040` (chemin
spécifique).

### B.1.2 — "973" matching trop large 🔴 *confirmé*

Oracle SQL : sans ancrage `article` devant, "973" remonte 17 179 interventions
(99 % FP). Avec ancrage `article\s+973` : 32 (100 % vrais positifs).

Le code `search.py:_generate_article_variants` ligne 472 ajoute correctement
le prefix `"article {ref} du c"` pour les short refs — donc le **scoring**
côté ILIKE est protégé. Mais le **filtre post-recherche** (`topical_cleanup`)
re-applique un `in` brut sans ce prefix → les FP qui auraient survécu peuvent
passer.

### B.2.1 / B.2.2 — `article_in_law` re-dérivation 🟡 *non testé*

Le commit `8a9b6d0` corrige la dérivation depuis le cache Légifrance. Aucune
preuve dans nos logs DIAG (le serveur a démarré post-fix). À vérifier si la
correction couvre les LFR/LFSS et pas seulement les LF.

---

## C. Liaison débat-dossier-article

### C.1.1 / C.1.2 — Couverture LFR / LFSS 🟠 *à confirmer*

L'oracle SQL trouve pour 39 C des modifs en LFR 2003, 2004, 2005, 2006, 2011,
2014 — soit 6 LFR. Les benchmarks bénéficient-ils de ces sources ? Le bench
973 montre `total_sources_an=0` → si les LFR n'apportent rien, c'est qu'elles
sont peut-être mal indexées. Question ouverte.

### C.2.1 — Amendement ↔ article CGI 🟢 *partiellement vérifié*

L'oracle pour 973 trouve **24 amendements** dont **8 adoptés**. Le bench
remonte 1 source Sénat (probablement amendement). L'`amend_filter` log :
- 973 : `[amend_filter] 15 → 1 amendments (rejected 14)` — rejette 93 % des amendements liés.

La discipline `_filter_amendment_chunks` (article_analyzer.py:1150) est très
agressive et calibrée sur des articles riches.

---

## D. Embeddings

### D.1 — Chunking 🟡 *non vérifié*
Pas d'instrumentation pour observer la longueur des chunks. À reporter pour
investigation séparée.

### D.3 — Threshold global 🟡 *non confirmé*
`min_similarity=0.30` (config.py:130). Pour 973, on observe 22-23 candidats
en entrée de `enhanced_filter`. Donc le seuil semantic n'est pas vide. Pas
de preuve qu'il soit défectueux.

---

## E. Recherche hybride keyword + sémantique

### E.1.1 — Tier 1 strict trop strict pour short refs 🔴 *confirmé*

Pour 973 chaque modification :
`[enhanced_filter] N → M chunks (0 explicit, 0 same_debate, M other | rejected R)`

**Le compteur `explicit=0` est constant** sur les 8 modifications de 973. Ce
qui signifie que **AUCUN chunk recherche ne contient "973" verbatim**. Les chunks
remontés discutent l'article via son contexte (IFI, dettes, valorisation), pas
par son numéro. Le tier-1 ne les retient que via la branche `no_other_cgi`
ou `same_debate`.

Pour 150-0 B ter : `(10 explicit, 2 same_debate, 0 other | rejected 28)` — la
moitié des chunks gardés sont **explicit ref**. La référence bénéficie de ce
que les débats la mentionnent par son numéro structuré "150-0 B ter".

### E.1.2 — Tier 2 keyword expansions hardcodées plus-values 🔴 *confirmé*

`search.py:272-298` (`_get_fallback_expansions`) :
```python
return {
    "150-0 B ter": ["apport-cession", "report d'imposition", ...],
    "150-0 B":     ["apport-cession", ...],
    "150-0 A":     ["plus-value mobiliere", ...],
}
```
Ces expansions curatées n'existent QUE pour la famille 150-0. Pour 973 et
39 C, la pipeline tombe sur l'expansion auto-générée depuis `article_modifications.json.keywords` qui contient seulement **9** keywords pour 973 (cf. log `[DIAG:TOPICAL]` : `scoreable_kw_count=9`) et **~16** pour 39 C, vs. 29-30 pour 150-0 B ter.

### E.1.3 — Le top_scoreable de 973 contient une référence à 150-0 B ter 🔴 *confirmé*

Log `[DIAG:TOPICAL] art='973' ... top_scoreable=["2° du iii de l'article 150-0 b ter", 'droits de mutation', 'iii règles', 'impôt sur la fortune immobilière', "l'évaluation biens"]`

**Le premier keyword "spécifique" pour 973 EST une référence à 150-0 B ter !**
Ce keyword n'apparaît jamais dans les chunks réels qui parlent d'IFI/973 →
contribue à la spécificité computée mais ne hit jamais → topical_cleanup
échoue.

---

## F. Filtres de chunks 🔴 ZONE CRITIQUE

### F.1 — Family-aware filter "Tier 2" 🔴 *confirmé inactif sur le chemin live*

Le code "three-tier" que j'ai instrumenté (article_analyzer.py:489-560) est
dans `analyze_article` qui n'est **PAS** appelé par l'API streaming. Le chemin
actif est `_analyze_modification` → `_filter_chunks_by_article_relevance`
(ligne 972) → `_filter_amendment_chunks` → `_topical_relevance_cleanup`.

Le "Tier 2 family-aware fallback" (commit `0762fad`) n'est donc actif que
sur un chemin non utilisé par la production. → C'est une cause confirmée mais
indirecte : le fix existe mais ne s'applique pas au flux LLM en direct.

### F.2 — Reranker rescue (LF-article) 🟢 *vraisemblablement actif (non vérifié)*

Mon `[DIAG:RERANK]` ajouté à `search.py:1865-1934` n'a pas fired dans le log,
soit parce que le serveur n'a pas rechargé `search.py`, soit parce que ce
chemin n'est pas exercé par `search_chamber`. À reverifier après server reload.

### F.3 — `_topical_relevance_cleanup` est l'EXÉCUTEUR principal 🔴🔴 *confirmé*

**Tous les chunks débats sont supprimés pour 973** sur chaque modification :

```
[DIAG:TOPICAL] art='973' input=7 kept=0 removed=7 scoreable_kw_count=9 ...
[DIAG:TOPICAL] art='973' input=6 kept=0 removed=6 ...
[DIAG:TOPICAL] art='973' input=7 kept=0 removed=7 ...
[DIAG:TOPICAL] art='973' input=6 kept=0 removed=6 ...
[DIAG:TOPICAL] art='973' input=1 kept=0 removed=1 ...
```

Pour 150-0 B ter, le même filtre supprime aussi beaucoup de chunks mais en
garde quelques-uns :
```
[DIAG:TOPICAL] art='150-0 B ter' input=23 kept=9 removed=14 scoreable_kw_count=30
[DIAG:TOPICAL] art='150-0 B ter' input=18 kept=13 removed=5 scoreable_kw_count=29
[DIAG:TOPICAL] art='150-0 B ter' input=12 kept=0 removed=12 scoreable_kw_count=29  ← 100% removal aussi
[DIAG:TOPICAL] art='150-0 B ter' input=1 kept=0 removed=1 scoreable_kw_count=30   ← idem
```

**Le mécanisme** : `_topical_relevance_cleanup` (article_analyzer.py:1089-1148)
exige `weighted_score >= 0.6 OR hit_count >= 3` OU une mention substring du
ref. Pour 973 :
- scoreable_kw = 9 mots (vs. 29-30 pour 150-0 B ter)
- Le premier mot scoreable est `"2° du iii de l'article 150-0 b ter"` (artefact de
  cross-référence du JSON) → ne matche jamais → spécificité gaspillée
- La mention substring "973" ne fait qu'attraper "des faux-positifs sur années/montants" qu'on a déjà filtrés en amont
- `hit_count >= 3` quasi-impossible avec 9 keywords dont 1 est un cross-ref

→ **F.3 est la cause racine n°1**.

---

## G. Pipeline LLM

### G.1.1 / G.1.2 — Contexte vide → hallucination plausible 🔴 *confirmé*

Log `[DIAG:LLM] phase=section_extraction provider=anthropic prompt_chars=2854`
pour la 1ère mod de 973. Comparé à 20-48K chars pour 150-0 B ter. Le LLM
travaille sur **10× moins de contexte** et **génère néanmoins 8 sections
"faible" confidence**. Sortie : `total_sources_an=0`, `confidence_levels=['faible']*8`.

Le système poursuit l'analyse même quand `debate_chunks=0` (cf. log
`Found 0 debate chunks, 8 amendment chunks (after relevance filter)`).

### G.4 — Multi-stage orchestration

`Found 0 directly linked amendments for LF 2023 art. 24` — pour 150-0 B ter
art. 24 (LF 2024). Le DB-level linking ne ramène rien, ILIKE-fallback prend
le relais. Pour 973, on a vu `[amend_filter] 15 → 1 amendments` — la pipe LLM
reçoit donc 1 seul amendement + l'exposé des motifs + BOFiP. Très peu.

---

## H. Index pré-calculé `article_modifications.json` 🔴 *confirmé*

| Article | keywords | modifications | quotes | jurisprudence | rapports |
|---|---|---|---|---|---|
| 150-0 B ter | 55 | 17 | 5 années | 6 | 4 |
| 39 C | 16 | 6 | 1 année | 0 | 5 |
| 973 | 10 | 3 | 1 année | 0 | 3 |
| 238 bis | 20 | 22 | 3 années | 4 | 1 |

**Constat** : 150-0 B ter est de loin l'article avec l'indexation la plus riche.
Tous les autres ont 50-80 % moins de keywords. Cet index alimente DIRECTEMENT
le `_topical_relevance_cleanup` (cf. `_get_kw_specificity` lignes 1062-1087)
et la pondération de pertinence.

**Le déséquilibre du JSON propage le déséquilibre à tout le pipeline aval.**

Bonus toxique : pour 973, le 1er keyword est `"2° du iii de l'article 150-0 b ter"` — preuve d'une fuite de cross-référence ou d'un mauvais extraction d'index.

---

## I. Couplages cachés / heuristiques calibrées sur 150-0 B ter

### I.1 — Seuils numériques calibrés sur abondance 🔴 *confirmé*

- `min_similarity=0.30` (config.py:130)
- `keyword_boost=0.25`
- `signal_weight_*` config dans config.py:170-173
- `weighted_score >= 0.6 OR hit_count >= 3` (topical_cleanup line 1138)

Ces seuils ne tiennent compte ni de la rareté de l'article ni de la sparsité
de son vocabulaire. Pour un article à 9 keywords : le seuil 0.6 est
quasi-inaccessible.

### I.2 — Keyword expansions hardcodées plus-values 🔴 confirmé (E.1.2)

### I.4 — Tests historiques sur 150-0 B ter 🔴 *fortement présumé*

`data/benchmarks/` contient **15 fichiers** `150-0-B-ter_*` versionnés à
chaque optimisation entre mars et avril 2026, et **0 fichier** pour 973 et
39 C avant ce diagnostic. L'évolution du système a été calibrée
exclusivement sur 150-0 B ter — typique de regression-by-overfitting sur
la "happy case".

---

## J. Spécificités par article

### J.1 (973)

Réalités confirmées :
- Article jeune (2018) avec ligneage ISF→IFI mal tracké
- Métadonnée titre obsolète dans article_modifications.json
- Sources externes : 10 BOFiP, ZÉRO jurisprudence, ZÉRO rapport
- 0 chunk débat passe le `topical_cleanup` sur 8/8 modifications
- Résultat bench : 0 source AN, 1 source Sénat, 8/8 sections "faible"

### J.2 (39 C) — pire que 973

Bench v1 baseline : 14 sections, **0 sources AN, 0 sources Sénat**, 14/14
"faible". Avg confidence 1.0/3.0. Une section a pris 180s (LLM bouclant sur
un contexte vide).

`[DIAG:TOPICAL] scoreable_kw_count=9` pour 39 C aussi. Top scoreable
contient `'articles 239 quater'` — référence à un AUTRE article CGI
(239 quater) — preuve supplémentaire de cross-référence parasite dans
`article_modifications.json` (cf. H).

`[enhanced_filter] 40 → 1 chunks (0 explicit, 0 same_debate, 1 other)` sur
la 1ère mod, puis `40 → 14 chunks (1 explicit, 14 same_debate)` sur la 2ème.
La voie `same_debate` est le principal rescue pour 39 C — l'article 39 C
est mentionné dans des débats qui touchent à beaucoup d'autres articles, et
ces chunks sont indirectement retenus. Mais `topical_cleanup` les massacre
ensuite (13/14 removed pour la 2ème mod).

Les 183 jurisprudence de l'oracle SQL ne remontent PAS dans `total_sources_an/senat`
(qui ne compte que le parlementaire). Mais la jurisprudence est utilisée dans
le contexte LLM (`Found relevant jurisprudence (X chars)` dans les logs).
La somme finale `0 sources parlementaires` malgré 53 interventions oracle
montre que **le filtrage débats est totalement bloquant pour 39 C**.

### J.3 (150-0 B ter)

- Résultats bench cohérents avec la "qualité de référence"
- MAIS la dégradation y est présente aussi (kept=0 sur plusieurs mods)
- L'overfit historique masque les régressions silencieuses sur la référence

---

## Résumé des causes par criticité

| Rang | Cause | Preuve | Branche arbre |
|---|---|---|---|
| 🔴 1 | `_topical_relevance_cleanup` calibré sur articles riches | `[DIAG:TOPICAL]` 100% removal sur 973 (8/8 mods) | F.3 |
| 🔴 2 | `article_modifications.json` déséquilibré (55 kw vs 9-16) | Inspection direct + DIAG | H |
| 🔴 3 | Cross-référence parasite dans keywords de 973 | top_scoreable pour 973 contient "150-0 b ter" | H + E.1.3 |
| 🔴 4 | Substring `in` au lieu de `_build_contextualized_patterns` | code:1123, 492 ; conflation 39 C/39 C bis | B.1.1 |
| 🔴 5 | Hardcoded plus-values expansions monopolisent le tier-2 | search.py:272-298 | E.1.2 |
| 🔴 6 | Family-aware Tier 2 fix sur chemin mort (`analyze_article`) | grep API: only `_analyze_modification` called | F.1 |
| 🔴 7 | Métadonnée 973 obsolète (ISF→IFI tracking absent) | titre = "Droit de timbre permis de conduire" | A.1.2 |
| 🟠 8 | `_filter_amendment_chunks` agressif (15→1 sur 973) | `[amend_filter]` logs | C.2.1 |
| 🟠 9 | Optimisations calibrées exclusivement sur 150-0 B ter | 15 versions benchmark, 0 sur 973/39 C | I.4 |
| 🟠 10 | Seuils numériques non-conditionnels (kw_count, mod_count) | code: line 1138 etc. | I.1 |

---

## Branches explicitement ÉCARTÉES

| Branche | Raison de l'écart |
|---|---|
| A.1.1, A.1.5 (DILA/Sénat coverage) | Oracle montre que les sources EXISTENT pour 39 C (53 int) |
| B.3 (Encodage) | Logs ne montrent pas d'erreur unicode ; les samples sont lisibles |
| D.1, D.2 (Chunking, modèle d'embedding) | Pas de preuve directe ; embeddings produisent des candidats raisonnables (cf. enhanced_filter input >0) |
| D.3 (Score threshold global) | Le pipeline reçoit des candidats avant filtre — le bottleneck est aval |
| G.3 (Modèle LLM / température) | Le LLM répond, le problème est le contexte vide, pas la génération |

---

**Prochain livrable** : Phase 6 — rapport priorisé d'améliorations avec
estimation d'effort et risque de régression sur 150-0 B ter.
