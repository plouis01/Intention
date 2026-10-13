# Phase 2 — Oracle manuel quantifié

Construction d'un oracle « ground-truth » pour trois articles du CGI (973 / 39 C / 150-0 B ter) à partir de requêtes SQL directes sur la base PostgreSQL `intention_backup`. Le but est de mesurer ce que le système *devrait au minimum* surfacer.

## Méthodologie

### Outillage

- Connexion : `psycopg2` sur `postgresql://localhost/intention_backup` (lecture seule).
- Toutes les requêtes sont des `SELECT`, pas de modification.
- Les variantes textuelles d'un article CGI sont matchées en `~*` (regex insensible à la casse) avec frontières de mots PostgreSQL `\m` / `\M` pour éviter les faux positifs sur des chiffres voisins (« 1973 », « 973 km », question n° 973, etc.). Échec rencontré et corrigé : `\b` n'existe pas en regex POSIX PostgreSQL — il faut utiliser `\m` / `\M` (sinon : 0 résultat silencieux).

### Variantes par article

| Article | Regex utilisée (interventions / sources) |
|--------|------------------------------------------|
| 973 | `\marticle\s+973\M` OR `\m973\s+du\s+(code\|CGI)` |
| 39 C | `(article\|art\.?)\s+39\s*C\M(\s\|,\|\.\|;\|\)\|du\s\|$)` |
| 150-0 B ter | `150[\s\-‑]?0\s*B\s*ter` (gère espace, hyphen ASCII, hyphen U+2011) |

### Contrôle de faux positifs

Pour chaque article, un échantillon aléatoire de 15 interventions (`ORDER BY random() LIMIT 15`) a été inspecté manuellement.

| Article | Échantillon | Vrais positifs | Précision |
|--------|-------------|----------------|-----------|
| 973 (avec « article » devant) | 15 | 15 | 100 % |
| 39 C (avec « article » devant) | 15 | 15 | 100 % |
| 150-0 B ter | 15 | 15 | 100 % |

À noter : sans ancrage `article …`, « 973 » seul produit 17 179 lignes dont la quasi-totalité sont des faux positifs (années « 1973 », numéros de questions, montants). C'est précisément le type de bruit que la stratégie de tiered keyword filtering (cf. `CLAUDE.md`) doit gérer.

### Latence

Les regex full-text sur `interventions` (7 215 754 lignes, pas d'index FTS, uniquement un btree `(debat_id, ordre)`) prennent 13–48 s par requête. Sur `chunk_embeddings`, l'index trigram aide pour les motifs littéraux mais pas pour les regex à classes/quantificateurs — exemple : le regex 39 C met 94 s là où 150-0 B ter met 6 s.

---

## Article 973 (IFI — Impôt sur la Fortune Immobilière)

Article CGI repurposé par la LF 2018 pour devenir la pierre angulaire du régime IFI (valorisation des actifs immobiliers détenus en société, déduction du passif). Avant 2018, l'identifiant « 973 » désignait des dispositions ISF/ENRC sans rapport direct.

### Lois modificatrices (via `articles.contenu_texte`)

3 articles de loi de finances modifient explicitement le 973 du CGI :

| Loi | Type | Année | Article de loi |
|-----|------|-------|----------------|
| 2017-1837 | LF | 2018 | art. 31 (création IFI) |
| 2018-1317 | LF | 2019 | art. 48 |
| 2023-1322 | LF | 2024 | art. 27 |

### Interventions parlementaires

- Total brut (avec ancrage `article 973` ou `973 du code/CGI`) : **32 interventions**
- Précision sample : 100 % → **count conservateur = 32**
- Sans ancrage : 17 179 (bruit massif)

Répartition par fonction :

| Fonction | Nb |
|----------|----|
| président de séance | 14 |
| autre | 13 |
| ministre | 2 |
| rapporteur | 2 |
| president | 1 |

Répartition par année (= année du débat) :

| Année | Nb |
|-------|----|
| 2017 | 2 |
| 2018 | 2 |
| 2019 | 2 |
| 2021 | 2 |
| 2022 | 6 |
| 2023 | 9 |
| 2024 | 6 |
| 2025 | 3 |

Couverture par dossier législatif : **9 lois distinctes** ont généré des interventions mentionnant l'article 973 du CGI (LF 2018 → LFSS 2025).

### Amendements

- Total (sur `objet` ∪ `contenu` ∪ `expose_motifs`) : **24 amendements**

| Sort | Nb |
|------|----|
| adopte | 8 |
| rejete | 7 |
| non_soutenu | 4 |
| tombe | 4 |
| retire | 1 |

Top 5 amendements adoptés (par ordre d'id) :

1. II-823 — A. de Montgolfier — vise à remplacer l'IFI par un « impôt sur la fortune improductive » (PLF 2021).
2. I-466 — S. Vermeillet — même esprit.
3. I-18 — B. Delcros — même esprit, autre PLF.
4. I-764 — S. Vermeillet — récidive.
5. I-128 — S. Vermeillet — récidive.

→ Pattern dominant : tentatives répétées (et adoptées au Sénat) de transformation de l'IFI en « impôt sur la fortune improductive ».

### Sources externes

- Via `articles_cgi_lies LIKE '%"973"%'` : 10 (toutes BOFIP).
- Via regex sur `contenu` : 8 BOFIP.
- Union : 10 sources, toutes de type **`bofip`**.
- Aucune jurisprudence, aucun rapport ni amendement Sénat externe indexé.

Top 5 sources externes (plus longues) :

| id | type | titre |
|----|------|-------|
| 7094 | bofip | PAT - IFI - Assiette - Biens soumis à l'impôt - Parts ou actions de sociétés… |
| 7105 | bofip | PAT - IFI - Assiette - Évaluation - Dettes contractées par la société |
| 7107 | bofip | PAT - IFI - Assiette - Déduction du passif - Conditions de déductibilité |
| 7103 | bofip | PAT - IFI - Assiette - Évaluation - Principes, valeur vénale |
| 7145 | bofip | PAT - IFI - Obligations des redevables - Souscription d'une déclaration |

### Couverture chunk_embeddings

- 58 chunks contiennent une référence à « article 973 »
  - 34 chunks `intervention`
  - 24 chunks `amendement`
- L'article est donc atteignable par recherche sémantique, mais avec un corpus très réduit.

### Baseline analysis_cache

| cgi_article | quality_score | sources_count | confidence | modification_count | llm_model | created_at |
|-------------|---------------|---------------|------------|--------------------|-----------| -----------|
| 973 | **0** | 1 | faible | 9 | claude-haiku-4-5-20251001 | 2026-05-21 |

L'analysis_json (36 ko) rapporte 8 « total_modifications » incluant des lois pré-2017 (1976, 1999, 2001, 2003, 2004) qui modifiaient l'*ancien* article 973 (totalement différent). Confusion historique caractéristique : l'index ne distingue pas la coquille recyclée par la LF 2018.

### Synthèse idéale (oracle)

> L'article 973 du CGI a été *recréé* par l'article 31 de la LF 2018 (n° 2017-1837) pour porter le régime de l'Impôt sur la Fortune Immobilière (IFI) qui remplace l'ISF supprimé la même année. Il définit les règles d'évaluation des actifs immobiliers, en particulier la valorisation des parts ou actions de sociétés à hauteur de leur quote-part immobilière, et encadre la déduction des dettes contractées par la société (anti-abus). La LF 2019 (art. 48) a renforcé les dispositifs anti-optimisation sur les dettes intragroupe ; la LF 2024 (art. 27) a élargi le champ pour viser les schémas de contournement par holding patrimoniale. L'intention principale est de cibler la fortune immobilière non productive et de fermer les voies de contournement par interposition de société. Les débats au Sénat (Vermeillet, Delcros, Montgolfier) montrent une opposition récurrente cherchant à substituer un « impôt sur la fortune improductive » plus large, sans succès à l'Assemblée. La doctrine BOFIP (10 documents) précise l'assiette et le passif déductible.

---

## Article 39 C (amortissement des biens loués)

Article ancien (années 1960) régissant l'amortissement fiscal des biens donnés en location ou mis à disposition, notamment dans les montages de crédit-bail et de location avec option d'achat. Cible favorite des dispositifs anti-abus (limitation des amortissements déductibles dans les schémas de défiscalisation).

### Lois modificatrices

12 articles de loi de finances modifient explicitement l'article 39 C :

| Loi | Type | Année | Article de loi | Contexte |
|-----|------|-------|----------------|----------|
| 2003-1312 | LFR | 2003 | 50 | refonte première phrase |
| 2004-1485 | LFR | 2004 | 97 | modification deuxième alinéa |
| 2005-1720 | LFR | 2005 | 41 | retouche deuxième alinéa |
| 2006-1771 | LFR | 2006 | 77 | refonte de l'article |
| 2008-1425 | LF | 2009 | 90 | exclusion outre-mer / 2 et 3 inapplicables |
| 2009-1673 | LF | 2010 | 2 | 1 du II — chiffre d'affaires |
| 2011-1978 | LFR | 2011 | 59 | clause d'assistance administrative |
| 2012-1509 | LF | 2013 | 23 | I — frais et prestations accessoires |
| 2014-891 | LFR | 2014 | 20 | 1 du II |
| 2018-1317 | LF | 2019 | 34 | reprise du I |
| 2022-1726 | LF | 2023 | 55 | renvois 217 undecies/duodecies/244 quater Y |
| 2025-127 | LF | 2025 | 84 | exclusion amortissements constitutifs |

### Interventions parlementaires

- Total brut avec ancrage : **53 interventions**
- Précision sample : 100 % → **count conservateur = 53**

Répartition par fonction :

| Fonction | Nb |
|----------|----|
| president | 22 |
| président de séance | 13 |
| autre | 9 |
| ministre | 4 |
| rapporteur général | 4 |
| rapporteur | 1 |

Répartition par année (étendue historique remarquable) :

| Année | Nb |  | Année | Nb |
|-------|----|--|-------|----|
| 1984 | 1 |  | 2005 | 2 |
| 1989 | 4 |  | 2006 | 2 |
| 1990 | 3 |  | 2008 | 1 |
| 1994 | 4 |  | 2009 | 1 |
| 1998 | 9 |  | 2012 | 4 |
| 1999 | 2 |  | 2018 | 2 |
| 2002 | 1 |  | 2024 | 4 |
| 2003 | 5 |  | 2025 | 6 |
| 2004 | 2 |  |  |  |

Couverture par dossier législatif : **22 lois distinctes** entre 1985 et 2025.

### Amendements

- Total : **12 amendements**

| Sort | Nb |
|------|----|
| rejete | 4 |
| tombe | 4 |
| adopte | 2 |
| non_soutenu | 2 |

Top amendements adoptés :

1. I-1601 — N. Goulet — amendement rédactionnel CGI/LPF, conditions d'application liées aux instruments de coopération administrative.
2. I-1 — Ph. Marini — refonte de l'intitulé de taxes (CET) — référence collatérale à 39 C dans un amendement plus large.

### Sources externes

- Via `articles_cgi_lies LIKE '%"39 C"%'` : 104 (85 jurisprudence + 19 bofip).
- Via regex sur `contenu` : 205 (183 jurisprudence + 22 bofip).
- Union : **205 sources** (183 `jurisprudence`, 22 `bofip`).

Top 5 sources externes (plus longues) :

| id | type | titre |
|----|------|-------|
| 3421 | bofip | BIC - Personnes imposables - EIRL |
| 8274 | bofip | SJ - Mesures fiscales soumises à agrément - Outre-mer |
| 3279 | bofip | BIC - Amortissements - Biens donnés en location - Calcul de l'amortissement fiscalement déductible |
| 6465 | bofip | IS - Charges financières - Plafonnement |
| 3392 | bofip | BIC - Location meublée - Régime fiscal |

### Couverture chunk_embeddings

- 67 chunks (55 intervention + 12 amendement).
- Latence : 94 s (regex peu friendly avec gin_trgm_ops à cause de `\.?` et `\s*`).

### Baseline analysis_cache

**Aucune entrée** dans `analysis_cache` pour l'article 39 C. → Le système n'a jamais produit (ou jamais cachée) d'analyse sur cet article.

### Synthèse idéale (oracle)

> L'article 39 C du CGI, en vigueur depuis les années 1960, plafonne les amortissements fiscalement déductibles sur les biens donnés en location ou mis à disposition, afin d'éviter que ces biens ne servent de support à des schémas de défiscalisation industrielle (notamment outre-mer) ou de crédit-bail abusifs. Il a fait l'objet de retouches quasi-annuelles entre 2003 et 2014 (LFR 2003, 2004, 2005, 2006, LF 2009, LF 2010, LFR 2011, LF 2013, LFR 2014), chacune ajustant les seuils, la définition du I/II, ou les conditions d'éligibilité (notamment l'exigence d'une clause d'assistance administrative — LFR 2011). Les LF 2019, 2023 et 2025 prolongent cette logique anti-abus en renvoyant aux nouveaux régimes d'aide à l'investissement (244 quater Y) et en excluant explicitement certains amortissements. Les débats parlementaires couvrent quatre décennies (1984–2025) et mobilisent essentiellement rapporteurs et ministres ; la doctrine administrative est étoffée (22 BOFIP) et le contentieux fiscal abondant (183 décisions de jurisprudence).

---

## Article 150-0 B ter (apport-cession — référence haute qualité)

Régime de report d'imposition des plus-values mobilières en cas d'apport de titres à une société contrôlée par l'apporteur (montage « apport-cession » via holding), créé par la 3ème LFR 2012.

### Lois modificatrices

**18 articles de loi de finances** modifient explicitement le 150-0 B ter :

| Loi | Type | Année | Article(s) de loi |
|-----|------|-------|-------------------|
| 2012-1510 | LFR | 2012 | 18 (création) |
| 2013-1278 | LF | 2014 | 17 |
| 2015-1786 | LFR | 2015 | 20, 26 |
| 2016-1918 | LFR | 2016 | 32, 33, 34 |
| 2016-1917 | LF | 2017 | 60 |
| 2017-1837 | LF | 2018 | 28, 31 |
| 2018-1203 | LFSS | 2019 | 26 |
| 2018-1317 | LF | 2019 | 111, 112, 115 |
| 2019-1479 | LF | 2020 | 106 |
| 2023-1322 | LF | 2024 | 24 |
| 2025-127 | LF | 2025 | 10, 93 |

Soit **12 lois** et **18 articles** distincts.

### Interventions parlementaires

- Total brut : **119 interventions**
- Précision sample : 100 % → **count conservateur = 119**

Répartition par fonction :

| Fonction | Nb |
|----------|----|
| président de séance | 43 |
| autre | 42 |
| ministre | 11 |
| secrétaire d'État | 8 |
| rapporteur | 7 |
| president | 5 |
| senateur | 2 |
| depute | 1 |

Répartition par année :

| Année | Nb |  | Année | Nb |
|-------|----|--|-------|----|
| 2012 | 4 |  | 2020 | 6 |
| 2013 | 4 |  | 2021 | 12 |
| 2015 | 6 |  | 2022 | 15 |
| 2016 | 6 |  | 2023 | 9 |
| 2017 | 6 |  | 2024 | 14 |
| 2018 | 13 |  | 2025 | 18 |
| 2019 | 6 |  |  |  |

Couverture par dossier : **19 lois distinctes** depuis 2012.

### Amendements

- Total : **56 amendements**

| Sort | Nb |
|------|----|
| rejete | 23 |
| retire | 13 |
| adopte | 11 |
| tombe | 5 |
| non_soutenu | 3 |
| (null) | 1 |

Top 5 amendements adoptés :

1. II-707 — É. Lamure — substitut à l'ISF-PME, amélioration du dispositif Madelin et compte PME innovation.
2. II-980 — A. de Montgolfier — assouplissement du quota d'investissement des fonds (conformité aides d'État).
3. II-1191 — Sénat — précisions sur le réinvestissement indirect via capital-investissement.
4. I-986 — V. Paoli-Gagin — corrige les incertitudes pesant sur les véhicules d'investissement éligibles au report.
5. I-627 — C. Lavarde — sécurise le dispositif pour les cessions à l'occasion d'un départ à la retraite.

### Sources externes

- Via `articles_cgi_lies` : 140 sources (89 jurisprudence + 42 bofip + 4 rapport + 2 amendement_senat + 2 seance + 1 cadf).
- Via regex `contenu` : 160 (105 jurisprudence + 47 bofip + 4 rapport + 2 amendement_senat + 2 seance).
- Union : **162 sources** (107 `jurisprudence`, 47 `bofip`, 4 `rapport`, 2 `amendement_senat`, 2 `seance`, 1 `cadf`).

Top 5 sources externes :

| id | type | titre |
|----|------|-------|
| 9052 | jurisprudence | Loi de finances pour 2026 |
| 7892 | bofip | RPPM - Plus-values - Report d'imposition - Apport à une société contrôlée |
| 4 | rapport | Rapport n° 214 - LFR 2016 |
| 6 | rapport | Rapport n° 140 - PLF 2020 |
| 108813 | jurisprudence | CAA Nancy, 2ème ch., 13/05/2024, 22NC00830 |

### Couverture chunk_embeddings

- **190 chunks** (131 intervention + 59 amendement). Le corpus indexé est le plus riche des trois cibles.

### Baseline analysis_cache

**Aucune entrée** dans `analysis_cache` pour 150-0 B ter. Pourtant c'est censé être l'article de référence — il a probablement été analysé puis évincé du cache, ou jamais ré-ingéré après une migration.

### Synthèse idéale (oracle)

> L'article 150-0 B ter, créé par l'article 18 de la 3ème LFR 2012 (n° 2012-1510), instaure un report d'imposition automatique sur les plus-values mobilières lorsque le contribuable apporte ses titres à une société qu'il contrôle (typiquement une holding). L'intention initiale est de favoriser le réinvestissement entrepreneurial — éviter qu'un dirigeant cédant son entreprise ne soit immédiatement taxé sur la plus-value latente, à condition que les fonds soient réinjectés dans l'économie productive. Le dispositif a été affiné par retouches quasi-annuelles : LF 2014 (art. 17), LFR 2015 (art. 20 et 26), trois articles dans la LFR 2016 (32–34) durcissant l'obligation de réinvestissement, LF 2017 (art. 60), deux articles dans la LF 2018 (28, 31), trois dans la LF 2019 (111, 112, 115), LF 2020 (art. 106) — chacun ajustant le quota (50 % puis 60 %), la définition des activités éligibles, la durée minimale, et le périmètre du contrôle. Les LF 2024 et 2025 prolongent ce cycle, notamment en élargissant les véhicules de réinvestissement éligibles (capital-investissement, fonds professionnels). Les débats (119 interventions, 19 dossiers législatifs) mobilisent fortement ministres et rapporteurs ; la jurisprudence (107 décisions) interprète les conditions de réinvestissement et la doctrine BOFIP (47 documents) précise les modalités opérationnelles. C'est l'article le mieux couvert des trois.

---

## Tableau comparatif

| Métrique | 973 | 39 C | 150-0 B ter |
|----------|-----|------|-------------|
| Année de création/refonte | 2018 | 1960s | 2012 |
| Lois modificatrices (finance, distinctes) | 3 | 12 | 12 |
| Articles de loi modificateurs | 3 | 12 | 18 |
| Interventions (total brut avec ancrage) | 32 | 53 | 119 |
| Précision sample | 100 % | 100 % | 100 % |
| Interventions (count conservateur) | 32 | 53 | 119 |
| Dossiers législatifs couverts | 9 | 22 | 19 |
| Amendements | 24 | 12 | 56 |
| Amendements adoptés | 8 | 2 | 11 |
| Sources externes (union) | 10 | 205 | 162 |
| — dont jurisprudence | 0 | 183 | 107 |
| — dont BOFIP | 10 | 22 | 47 |
| — dont rapports / amendement_senat / seance / cadf | 0 | 0 | 9 |
| Chunks embeddings indexés | 58 | 67 | 190 |
| `analysis_cache.quality_score` | 0 | (absent) | (absent) |
| `analysis_cache.sources_count` | 1 | — | — |
| `analysis_cache.confidence_level` | faible | — | — |

---

## Observations

### 1. Le cache d'analyse est dramatiquement en deçà de l'oracle

Le seul article qui a une entrée dans `analysis_cache` parmi nos trois cibles (973) annonce `sources_count=1` et `quality_score=0` — alors que notre oracle SQL identifie **24 amendements + 32 interventions + 10 BOFIP**, soit au minimum **66 documents distincts** pertinents. Le rapport oracle/baseline est de **66× pour 973**. Pour 39 C (270 sources oracle, 0 cachée) et 150-0 B ter (337 sources oracle, 0 cachée), la baseline est tout simplement vide. Le pipeline cache donc soit (a) jette des analyses, soit (b) ne les ré-ingère pas après les phases EMBED/INDEX. C'est la première chose à investiguer.

### 2. Asymétrie de couverture des sources externes

L'article 973 (IFI) ne dispose **d'aucune jurisprudence et d'aucun rapport parlementaire** dans `sources_externes` — uniquement 10 BOFIP. C'est cohérent avec son jeune âge (2018) et le fait qu'il s'agit d'un régime fiscal à contentieux émergent, mais cela explique pourquoi l'analyse cachée a `confidence=faible`. À l'inverse, 39 C est largement dominé par la **jurisprudence** (183 décisions) — un article ancien de droit administratif fiscal très contentieux. 150-0 B ter présente le profil le plus équilibré (rapport, séance, jurisprudence, BOFIP), ce qui en fait effectivement le cas de référence.

### 3. Le tiered keyword filtering est nécessaire mais sous-spécifié

Sans ancrage `article` devant le numéro, « 973 » produit 17 179 interventions, presque toutes des faux positifs (années, montants, n° de question). L'ancrage `\marticle\s+973\M` ramène à 32 (précision 100 %). Cette discipline doit être inconditionnelle pour les numéros courts. Pour 39 C, il y a en outre un risque réel de collision avec « 39 C bis », « 39 CA », « 39 CB » — le boundary `\M` règle le problème en SQL POSIX, mais pas en regex Python (où il faut `\b`) : double pipeline, double risque de bug.

### 4. Le coût computationnel du regex full-text est élevé

Les regex sur `interventions` (7,2 M lignes, pas d'index FTS) coûtent 13–48 s par requête. Sur `chunk_embeddings`, l'index `gin_trgm_ops` aide pour les motifs littéraux (`150-0 B ter` : 6 s) mais s'effondre pour les regex à classes (`39 C` : 94 s). Conséquence : un endpoint de recherche live ne peut pas se permettre de scanner `interventions` en regex pour chaque requête. Il faut soit un index FTS (`tsvector`), soit un index inversé sur les références d'articles pré-extraites.

### 5. Couverture chunk_embeddings cohérente avec le volume sources

Les rapports chunks / sources sont : 58/66 = 88 % pour 973, 67/270 = 25 % pour 39 C, 190/337 = 56 % pour 150-0 B ter. La couverture est très inégale, particulièrement basse pour 39 C — ce qui suggère que les sources `jurisprudence` (qui dominent pour 39 C) ne sont pas systématiquement chunkées et embeddées. À vérifier dans la phase EMBED du pipeline.
