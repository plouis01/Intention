# Phase 1 — Arbre exhaustif des causes plausibles

> Diagnostic différentiel : dégradation de qualité d'analyse sur articles CGI peu
> documentés / anciens (cibles : **973**, **39 C**) vs. référence bien couverte
> (**150-0 B ter**).
>
> Cet arbre est construit AVANT toute exploration code/SQL, à partir de
> l'architecture documentée (`CLAUDE.md`) + commits récents. Chaque branche est
> une **hypothèse** : sera confirmée OU explicitement écartée en Phase 5 avec
> preuve.

---

## Légende de criticité a priori

- 🔴 **Forte présomption** — pattern typique de dégradation sparse-vs-dense
- 🟠 **Plausible** — mécanisme connu, à instrumenter
- 🟡 **À écarter ou confirmer** — possible mais moins typique
- ⚫ **Couplage** — n'est pas une cause directe, mais masque ou amplifie

---

## A. Couverture des sources de données

### A.1 Sources amont incomplètes pour l'article cible
- 🔴 **A.1.1** — Légifrance ne renvoie pas l'historique antérieur à une certaine
  date pour 39 C (article ancien, probablement antérieur à la version
  numérique des dossiers législatifs structurés AN/Sénat).
- 🔴 **A.1.2** — 973 est créé par la loi de finances 2018 (IFI). Sa généalogie
  remonte partiellement à l'ISF (885 et suivants) — si le système ne suit pas
  le lien ancien-numéro → nouveau-numéro, on perd toute l'antériorité ISF.
- 🟠 **A.1.3** — DILA archives : profondeur historique limitée ; les vieilles
  séances peuvent manquer ou être encodées différemment.
- 🟠 **A.1.4** — Sénat archives : couverture asymétrique vs. AN sur certaines
  périodes ; un article modifié principalement au Sénat aux origines peut
  être invisible.
- 🟡 **A.1.5** — Couverture "Rapports" (rapporteur général, commissions
  spéciales) : les rapports anciens peuvent être en PDF scannés non OCRisés.

### A.2 Collecte fonctionnelle mais filtrée trop tôt
- 🟠 **A.2.1** — Collectors avec date-cutoff codée en dur (ex: ≥ 2012 par
  conception, mais 39 C a une histoire bien antérieure ; idem ISF→IFI pour 973).
- 🟠 **A.2.2** — Pagination ou rate-limit qui interrompent silencieusement la
  collecte sur les vieux dossiers (volume + retries → timeouts).

---

## B. Import & parsing

### B.1 Parsing de l'identifiant d'article
- 🔴 **B.1.1** — "39 C" contient un espace + lettre ; selon le parser, peut
  être normalisé en "39C", "39", "39 c" (casse), ou collisionné avec "39 C bis"
  / "39 CA" (préfixe). Référence "150-0 B ter" a une structure plus rigide,
  donc moins de risque de collision.
- 🔴 **B.1.2** — "973" est un nombre court à 3 chiffres : risque énorme de
  faux-positifs dans le matching keyword (collision avec dates, montants,
  numéros d'amendement, alinéas). À l'inverse, après filtrage anti-collision,
  les vrais positifs peuvent aussi être éliminés.
- 🟠 **B.1.3** — Normalisation "ter / bis / quater" et romain vs. latin
  inconsistante.

### B.2 article_in_law re-dérivé du cache Légifrance (commit 8a9b6d0)
- 🟠 **B.2.1** — Si la re-dérivation a corrigé la cible "article du code" mais
  laissé inchangé l'index des "articles du projet de loi" (l'article DANS la
  loi de finances, p. ex. art. 18 du PLF), le pont entre intervention
  parlementaire et article CGI peut être cassé pour les articles peu modifiés.
- 🟠 **B.2.2** — La re-dérivation ne s'applique peut-être qu'aux lois récentes
  ré-fetchées du cache, laissant les anciennes lois avec un mapping legacy.

### B.3 Encodage (phase 4 du pipeline)
- 🟡 **B.3.1** — Fixes d'encodage récents sur interventions/external sources —
  est-ce que les vieux débats ont été couverts ? Mojibake masquant les
  mots-clés ("amortissement" → "amortissement") tuerait le tier-2 keyword.

---

## C. Liaison débat-dossier-article (LINK)

### C.1 Orphan debates
- 🔴 **C.1.1** — Si l'article 39 C est modifié par un cavalier ou un amendement
  hors-dossier-principal, le débat peut rester orphelin. Le mécanisme
  "orphaned debates" est-il agressif sur ces cas ?
- 🔴 **C.1.2** — Pour les articles anciens, beaucoup de modifs sont en LFR
  (loi de finances rectificative) ou LFSS — la couverture LF/LFR/LFSS est-elle
  symétrique ?

### C.2 Amendment ↔ article CGI
- 🟠 **C.2.1** — Un amendement modifiant l'article 39 C est-il lié à 39 C
  spécifiquement, ou à l'article du PLF qui modifie 39 C ? Si le lien
  reste au niveau PLF, la recherche "39 C" via la table des amendements
  retourne vide.
- 🟠 **C.2.2** — Amendements rejetés / non-soutenus filtrés out : pour un
  article peu modifié, les amendements rejetés sont parfois la SEULE source
  riche d'intention.

### C.3 Article-refs regenerator (commit 66c7dfb)
- 🟠 **C.3.1** — Le régénérateur d'article-refs couvre-t-il les anciennes
  conventions de nommage (ex: anciennes versions de l'art 39 sans suffixe) ?

---

## D. Embeddings & vectorisation

### D.1 Stratégie de chunking
- 🟠 **D.1.1** — Découpage par taille fixe ou par paragraphe ; un débat ancien
  formaté différemment (longues tirades, peu de paragraphes) produit des
  chunks géants → embeddings moins ciblés.
- 🟡 **D.1.2** — Le chunking inclut-il un contexte minimal (titre intervention,
  orateur) ? Sans contexte, "M. le rapporteur. — Avis défavorable." est
  inutile mais embeddable.

### D.2 Modèle multilingue
- 🟡 **D.2.1** — `paraphrase-multilingual-MiniLM-L12-v2` est entraîné sur du
  texte moderne ; vocabulaire juridique ancien ("amortissement dégressif",
  "déclaration spéciale") peut être moins bien représenté que "plus-value de
  cession de titres de participation".

### D.3 Vector store
- 🟡 **D.3.1** — Si Qdrant indexe avec un score-threshold global, les articles
  peu documentés ont des scores moyens plus bas → tout passe sous le seuil.

---

## E. Recherche hybride keyword + sémantique 🔴 **ZONE CRITIQUE**

### E.1 Keyword tiering (CLAUDE.md mentionne explicitement)
- 🔴 **E.1.1** — **Tier 1 strict** : exige le match exact "150-0 B ter".
  Pour "973", le match exact remonte une avalanche de bruit (collision
  numérique). Pour "39 C", la normalisation peut empêcher tout match
  ("39 C", "39C", "art. 39 C", "article 39-C", "39C du CGI"...).
- 🔴 **E.1.2** — **Tier 2 relaxed** : "plus-value", "cession", "holding"
  cités explicitement dans CLAUDE.md. Ces keywords sont **calibrés sur
  150-0 B ter**. Pour 973 (IFI immobilier) ou 39 C (amortissement), il n'y a
  PAS d'équivalent vocabulary expansion — donc tier-2 dégénère en "vide" ou
  en bruit générique.
- 🔴 **E.1.3** — Le filtre "termes génériques uniquement si contexte article"
  peut TUER les chunks 973/39 C : le vrai débat parle de "patrimoine
  immobilier" sans répéter "973" ; le filtre exige "973" + "immobilier" → 0.

### E.2 Recherche sémantique
- 🟠 **E.2.1** — Si le vecteur de requête est construit à partir du nom de
  l'article + extrait du code, la similarité avec les débats anciens (style
  différent) est mécaniquement plus faible que pour les débats modernes.
- 🟠 **E.2.2** — Top-K hard-coded (k=20, 50, 100 ?) : si 150-0 B ter a 300
  candidats forts et 973 en a 30, le même K coupe au mauvais endroit.

### E.3 Fusion hybride
- 🟠 **E.3.1** — Si la fusion RRF/poids favorise les chunks "à la fois
  keyword-hit ET sémantiquement proches", les articles peu documentés perdent
  par absence de double-validation.

---

## F. Filtres de chunks 🔴 **ZONE CRITIQUE (commits récents)**

### F.1 Family-aware chunk filter + fallback (commit 0762fad)
- 🔴 **F.1.1** — Si la "famille" est définie par mots-clés (ex: famille
  "plus-values mobilières" pour 150-0 B ter), 973 (IFI) et 39 C
  (amortissement) tombent peut-être dans une famille mal calibrée ou
  inconnue → fallback dégénéré.
- 🔴 **F.1.2** — Le fallback "family-aware" est récent : peut être trop
  permissif (bruit) ou trop restrictif (vide) — il faut voir le code.
- 🔴 **F.1.3** — Si la famille est inférée depuis le contenu de l'article CGI,
  un article court (39 C) ou très spécifique (973) peut être mal classé.

### F.2 Reranker cutoff (commit 4cdcef0 — "rescue LF-article chunks")
- 🔴 **F.2.1** — Le fix "rescue" est récent : indique que le reranker
  coupait des chunks "article de loi de finances" pertinents. Le fix est-il
  exhaustif ? Peut-être qu'il rescue uniquement la majorité (150-0 B ter
  bénéficie) mais pas les cas de niche.
- 🔴 **F.2.2** — Un cross-encoder reranker (style cross-encoder/ms-marco-*)
  est entraîné sur des QA web ; il sous-évalue les chunks parlementaires
  anciens (registre formel, longs préambules).
- 🟠 **F.2.3** — Score-threshold du reranker calibré sur la distribution
  "150-0 B ter" → coupe trop sur les distributions plus plates de 973/39 C.

### F.3 Dédup
- 🟠 **F.3.1** — Dédup sur similarité texte ou hash de phrase : pour 39 C,
  les rares chunks utiles peuvent se ressembler (mêmes orateurs, mêmes
  arguments) et être fusionnés à 1 seul.

---

## G. Pipeline LLM (`article_analyzer.py`)

### G.1 Construction du contexte
- 🟠 **G.1.1** — Budget de contexte rempli avec du bruit faute de signal :
  pour 39 C, top-K récupère 20 chunks dont 18 hors-sujet → prompt diluté.
- 🟠 **G.1.2** — Inversement, contexte vide / quasi-vide → prompt "compense"
  par hallucination plausible.
- 🟡 **G.1.3** — Sources non-citées dans le prompt (pas de [src]) → LLM ne
  peut pas étayer, génère du blabla générique.

### G.2 Prompt
- 🟠 **G.2.1** — Prompt template attend une structure (ex: "table d'évolution
  des synthèses par LF") qui suppose plusieurs modifications. Pour un
  article modifié 1 ou 2 fois, le prompt force le LLM à inventer.
- 🟠 **G.2.2** — Few-shot examples calibrés sur des articles riches (150-0 B
  ter style) → LLM mimétique sur le style mais sans le contenu.

### G.3 Modèle / paramètres
- 🟡 **G.3.1** — Groq Llama 70B (probable) : taux d'hallucination sur context
  sparse plus élevé.
- 🟡 **G.3.2** — Température > 0 → variance dans la synthèse, pire quand
  le signal est faible.

### G.4 Orchestration multi-stage
- 🟠 **G.4.1** — Si la première étape (extraction de citations) ne trouve rien,
  les étapes suivantes (synthèse, évolution) opèrent sur du vide.
- 🟠 **G.4.2** — Seuils de "confidence minimum" par chunk : un chunk faible
  mais pertinent est jeté ; reste : rien.

---

## H. Index pré-calculé `article_modifications.json`

- 🟠 **H.1** — `data/article_modifications.json` est mentionné dans CLAUDE.md
  comme contenant "curated quotes". Si 973 et 39 C ne sont pas dans ce fichier
  ou y sont avec des entrées creuses → l'analyse part déjà sourde.
- 🟠 **H.2** — Le régénérateur de cet index (commit 66c7dfb) couvre-t-il les
  vieux articles ? Quel est son seuil de "modification significative" ?

---

## I. Couplages cachés / heuristiques calibrées sur 150-0 B ter ⚫

> Hypothèses transversales : ce qui marche pour la référence peut **masquer**
> un défaut sur les autres.

- ⚫ **I.1** — **Seuils numériques** (top-K, score-cutoffs, min-chunks)
  calibrés empiriquement sur 150-0 B ter → distribution biaisée vers
  l'abondance.
- ⚫ **I.2** — **Keyword expansions** hardcodées plus-values / cession /
  holding → tier-2 inutile hors plus-values.
- ⚫ **I.3** — **Few-shot LLM** issu de 150-0 B ter → biais vers le style
  "réforme structurelle moderne".
- ⚫ **I.4** — **Tests / validation** historiquement faits sur 150-0 B ter →
  on a optimisé sans s'en rendre compte.
- ⚫ **I.5** — **Famille de filtre** (F.1) — si la famille par défaut est
  "plus-values mobilières", on est calé sur la référence.

---

## J. Spécificités par article (sub-hypothèses ciblées)

### J.1 Article 973 (IFI — valeur des biens immobiliers)
- 🔴 Lignée ISF → IFI : si le code ne suit pas la généalogie, perte de toute
  la jurisprudence parlementaire ISF.
- 🔴 Vocabulaire IFI ("dette déductible", "passif", "abattement résidence
  principale") absent des keyword expansions plus-values.
- 🟠 Article créé en 2018 → toutes les modifs sont récentes : ne devrait PAS
  manquer de couverture. Si l'analyse est mauvaise quand même → problème
  côté traitement, pas côté données.

### J.2 Article 39 C (amortissement, immobilisations en crédit-bail / location)
- 🔴 Article très ancien (origines années 60-70) — sources DILA potentiellement
  limitées.
- 🔴 Modifié principalement par dispositifs anti-abus (LF chaque année dans
  les années 2000s pour fermer des optimisations crédit-bail), souvent par
  cavaliers ou amendements gouvernementaux courts — peu de débat ouvert.
- 🔴 Vocabulaire : "amortissement réputé différé", "déduction étalée",
  "lots de copropriété", "BIC" — orthogonal aux keyword expansions.
- 🟠 Sous-articles "39 C bis", "39 CA" — risque de confusion identifiant.

### J.3 Article 150-0 B ter (référence positive)
- ✅ Article récent (2012, apport-cession), modifié par chaque LF depuis.
- ✅ Vocabulaire calibre les keyword expansions ("plus-value", "cession",
  "apport", "holding", "report d'imposition", "réinvestissement").
- ✅ Famille de filtre probablement dédiée.
- ⚠️ Tout ce qui marche dessus DOIT être audité pour overfitting.

---

## K. Hypothèses non-causales mais aggravantes

- **K.1** — Logs actuels insuffisants : impossible de diagnostiquer
  finement sans instrumentation `[DIAG:*]` (Phase 3).
- **K.2** — Pas de tests de régression formels sur articles "peu documentés"
  → le filet de sécurité non-régression est unilatéral.
- **K.3** — Métriques de qualité sortie LLM (groundedness, citation coverage)
  probablement absentes — la dégradation n'est détectable que manuellement.

---

## Plan de vérification (qui sera mené en Phase 5)

Pour chaque branche 🔴 et 🟠, on cherchera AU MINIMUM une preuve :

| Branche | Méthode de vérification |
|---|---|
| A.1, A.2 | SQL : `SELECT COUNT(*) FROM debats JOIN ... WHERE article = '973'` etc. |
| B.1 | Lecture parser article-ref + tests avec variantes "39 C", "39C", "39  C" |
| B.2 | SQL sur le champ `article_in_law` pour les lignes 39 C / 973 |
| C.1, C.2 | SQL sur jointure orpheline + couverture LFR |
| C.3 | Re-run du régénérateur en mode verbeux sur les cibles |
| E.1, E.2 | `[DIAG:SEARCH]` : voir candidats avant filtres |
| F.1, F.2 | `[DIAG:FILTER]` + `[DIAG:RERANK]` : voir chunks rejetés et raisons |
| G.* | `[DIAG:CONTEXT]` + `[DIAG:LLM]` : voir prompt + réponse brute |
| H | Lecture directe de `article_modifications.json` pour 973/39C |
| I | Comparaison croisée des 3 benchmarks |

---

**Cet arbre est la grille de lecture pour le reste du diagnostic. Aucune
branche ne sera laissée pendante en Phase 5.**
