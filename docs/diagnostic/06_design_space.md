# Phase 1-2 — Formalisation mathématique et énumération du design space

> ⚠️ **Errata** : les chiffres de catalogue (médiane=14, 50 % sparse, etc.) sont
> tirés du mauvais fichier (`article_modifications.json` au lieu du fichier
> production `modifications_cgi_enriched.json`). Vrais chiffres :
> médiane=6 kw, **73.8 % ultra-sparse**. Cf. [`08_final_validation.md`](08_final_validation.md#catalogue-corrigé-depuis-le-bon-fichier).
> La **formalisation mathématique** et les **choix architecturaux** restent
> valides (la sparsité est même plus marquée qu'annoncé, ce qui RENFORCE
> la nécessité de la scale-invariance).


> Objectif : remplacer le pipeline calibré sur un point fixe de la distribution
> de densité (150-0 B ter, kw=55, outlier extrême) par un pipeline
> **scale-invariant** dont les décisions sont **homogènes de degré zéro** en
> volume de données.

---

## Phase 1 — Formalisation

### Notation

Soit `X` un article CGI cible. Soit le **corpus de candidats** :
$$C(X) = \{c_1, ..., c_n\}$$
où chaque chunk `c` est une intervention parlementaire ou un amendement
remonté par la recherche.

Pour chaque chunk `c` et chaque article `X`, on définit un **vecteur de
signaux** observables :

| Signal | Type | Description |
|---|---|---|
| $\sigma_{\text{sem}}(c, X)$ | $[0,1]$ | similarité sémantique requête/chunk |
| $\sigma_{\text{kw}}(c, X)$ | $\mathbb{R}_{\geq 0}$ | somme pondérée des hits de keywords (TF-IDF-like) |
| $\sigma_{\text{exp}}(c, X)$ | $\{0,1\}$ | mention contextuelle explicite de `X` |
| $\sigma_{\text{sib}}(c, X)$ | $\{0,1\}$ | mention d'un article voisin (39 C bis, 39 CA, ...) |
| $\sigma_{\text{lf}}(c, X)$ | $\{0,1\}$ | mention de l'article-de-loi modifiant `X` |
| $\sigma_{\text{date}}(c, X)$ | $[0,1]$ | proximité temporelle à la date de modification |

Une **fonction de sélection** :
$$S : (C(X), X) \mapsto S(C, X) \subseteq C$$
détermine les chunks transmis au LLM.

### Fonction objectif

On cherche `S` qui maximise :
$$\mathcal{F}(S) = \mathbb{E}_{c \sim S(C,X)}[\text{relevance}(c, X)] - \lambda \cdot |\text{noise}(S(C,X))|$$

Sous contraintes :
1. $|S(C, X)| \leq B$ où `B` = budget de contexte LLM (token-bound).
2. $|S(C, X)| \geq 0$ (peut être vide si starvation totale).
3. Latence et coût bornés.

### Définition opérationnelle

- **relevance(c, X)** ≈ probabilité que `c` discute du fond / de l'intention
  du législateur sur `X`. Mesurable a posteriori via l'oracle SQL
  (`02_manual_oracle.md`).
- **noise(c, X)** = chunks qui ne discutent pas réellement `X` (faux-positifs).
  Mesurable via le contre-exemple : mention d'un autre article CGI sans `X`.

### Principe directeur : Scale Invariance

**Définition formelle** : `S` est *scale-invariant* ssi pour toute transformation
$\phi$ qui préserve la forme de la distribution conjointe des signaux mais
multiplie la cardinalité par un facteur $k$ :
$$\frac{|S(\phi(C))|}{|\phi(C)|} = \frac{|S(C)|}{|C|}$$

**Implication** : un seuil absolu sur un score (par exemple
$\sigma_{\text{kw}}(c, X) \geq 0.6$) **n'est jamais scale-invariant**, sauf si
les scores sont eux-mêmes normalisés à une échelle invariante (CDF, rank, quantile).

**Test pratique de scale invariance** d'un mécanisme :
> Si on prend un article `X_2` de densité $1/2$ de `X_1` (moitié des chunks,
> moitié des keywords pertinents), le mécanisme doit produire `|S| ≈ |S(X_1)|/2`,
> pas `|S| = 0`.

Le filtre actuel `_topical_relevance_cleanup` produit `|S| = 0` dans ce
scénario (cf. logs `[DIAG:TOPICAL] kept=0`) → **viole grossièrement la scale
invariance**.

### Métriques observables

Trois métriques empiriquement mesurables :

| Métrique | Formule | Range | Falsifie quoi |
|---|---|---|---|
| **Surface ratio** | $\frac{n_{\text{sources, bench}}}{n_{\text{oracle, SQL}}}$ | $[0, 1]$ | Le filtre est-il sélectif au bon point ? |
| **Density-normalized confidence** | $\frac{\text{avg\_confidence}_{\text{bench}}}{\text{avg\_confidence}_{\text{ref}}} \cdot \frac{\rho_{\text{ref}}}{\rho_X}$ avec $\rho$ = densité (kw_count) | $\geq 0$ | Le système discrimine-t-il par densité ? |
| **Selection homogeneity** | $\frac{|S(X_{\text{dense}})| / |C(X_{\text{dense}})|}{|S(X_{\text{sparse}})| / |C(X_{\text{sparse}})|}$ | $\approx 1$ si scale-invariant | Scale invariance directe |

---

## Phase 2 — Énumération du design space

Pour chaque axe, on liste ≥2 formulations concurrentes avec :
- Formule mathématique
- Hypothèses sous-jacentes
- Asymptotes (comportement quand $n_{\text{kw}} \to 0$, $n_{\text{kw}} \to \infty$)
- Risque sur la référence (150-0 B ter)

### A. Calibration des seuils

#### A.1 — Seuil absolu statique (current)
$$\text{Keep}(c) \iff \sigma_{\text{kw}}(c, X) \geq \theta \text{ avec } \theta = 0.6 \text{ const.}$$

- **Hypothèses** : `θ = 0.6` est universel.
- **Asymptote** : $n_{\text{kw}} \to 0$ → `θ` jamais atteint → `kept = 0`.
- **Risque ref** : aucun (status quo).
- **VERDICT** : Pas scale-invariant. À écarter.

#### A.2 — Seuil top-k absolu
$$\text{Keep}(c) \iff c \in \text{argtop}_k(\sigma_{\text{kw}})$$
avec $k$ const.

- **Hypothèses** : `k` chunks pertinents en moyenne par article.
- **Asymptote** : $n_C \to 0$ → keep tous ($k > n_C$). $n_C \to \infty$ → keep $k$ best.
- **Risque ref** : si `k=10`, 150-0 B ter passerait de 17 à 10 sources → **régression**.
- **VERDICT** : Mauvaise calibration de $k$ → régression réf. À écarter sauf si $k$ dérivé du budget.

#### A.3 — Top-p quantile (rank-based)
$$\text{Keep}(c) \iff \text{rank}(\sigma_{\text{kw}}(c)) \leq \lceil p \cdot n_C \rceil$$
avec $p \in [0, 1]$ const.

- **Hypothèses** : fraction $p$ pertinente est universelle.
- **Asymptote** : $n_C \to 0$ → keep $\lceil p \cdot n_C \rceil \to 0$. $n_C \to \infty$ → keep $p \cdot n_C \to \infty$.
- **Scale-invariant** : ✓
- **Risque ref** : faible si $p$ raisonnable (ex: 0.5).
- **VERDICT** : Bonne candidate, mais $p$ reste arbitraire.

#### A.4 — Max-gap cut (paramétrique-libre)
Trier les scores décroissants : $s_{(1)} \geq s_{(2)} \geq ... \geq s_{(n)}$.
Calculer les gaps : $g_i = s_{(i)} - s_{(i+1)}$.
Cut à $i^* = \text{argmax}_i \, g_i$.
$$\text{Keep}(c) \iff \text{rank}(\sigma(c)) \leq i^*$$

- **Hypothèses** : la "vraie séparation" entre pertinent et bruit est le plus
  grand saut de score.
- **Asymptote** :
  - $n_C \to 1$ → 0 gap → keep tout.
  - $n_C \to \infty$ avec distribution bimodale → cut entre les modes.
  - distribution uniforme → max gap arbitraire (instable) — risque sur cas dégénéré.
- **Scale-invariant** : ✓ (le rank et les gaps sont des stats de forme).
- **Paramétrique-libre** : ✓ (aucune constante).
- **Risque ref** : si la distribution est uniforme (rare en pratique), comportement instable.
- **VERDICT** : Excellente candidate. Compléter avec un **gardien anti-dégénérescence**.

#### A.5 — Otsu / between-class variance maximization
$$\text{cut}^* = \text{argmax}_{cut} \left[ \omega_0(cut) \cdot \omega_1(cut) \cdot (\mu_0(cut) - \mu_1(cut))^2 \right]$$
avec $\omega_0, \mu_0$ = poids/moyenne des scores en dessous du cut, idem au-dessus.

- **Hypothèses** : 2 classes (pertinent / bruit), séparées par une frontière maximisant l'inter-variance.
- **Asymptote** : équivalent à max-gap sur les rangs pour des distributions bimodales typiques.
- **Scale-invariant** : ✓
- **Risque ref** : faible.
- **VERDICT** : Plus robuste que max-gap mais plus de code. À considérer en alternative si max-gap échoue.

#### A.6 — Z-score / IQR threshold
$$\text{Keep}(c) \iff \sigma(c) \geq \mu + \alpha \sigma_{\text{std}} \quad \text{ou} \quad \sigma(c) \geq Q_3 + \beta \cdot \text{IQR}$$

- **Hypothèses** : distribution gaussienne (z-score) ou asymétrique (IQR).
- **Asymptote** : si toutes les scores sont 0, $\sigma_{\text{std}} = 0$ → cut indéterminé.
- **Scale-invariant** : ✓ pour z-score; ✓ pour IQR.
- **Verdict** : Constantes $\alpha, \beta$ à choisir → moins paramétrique-libre. À écarter.

**Choix A** : **A.4 (max-gap) avec garde anti-dégénérescence** + protection
"toujours garder les chunks à mention explicite contextuelle".

### B. Matching de références d'article

#### B.1 — Substring `in` (current)
$$\sigma_{\text{exp}}(c, X) = \mathbb{1}[\text{lower}(X) \subseteq \text{lower}(c)]$$

- **Hypothèses** : les références d'article sont distinguables par substring.
- **Failure mode** : "39 C" ⊂ "39 C bis" → faux-positif. "973" ⊂ "1973" → faux-positif.
- **Asymptote** : pour références longues structurées (150-0 B ter), faible probabilité de collision; pour références courtes, probabilité élevée.
- **VERDICT** : Soundness brisée pour références courtes. À éliminer partout.

#### B.2 — Regex contextualisé (existant mais non-câblé)
$$\sigma_{\text{exp}}(c, X) = \mathbb{1}[\exists \text{ pattern } p \in P(X) : p.\text{search}(c) \neq \emptyset]$$
où $P(X)$ inclut `_build_contextualized_patterns(X)` :
- `\bX\b(?! bis| ter| [A-H]\b)` pour références courtes
- variantes orthographiques pour longues
- "article X du c(ode|gi)" pour disambig

- **Hypothèses** : la disambiguation lexicale via word-boundary + negative lookahead est suffisante.
- **Failure mode** : cas exotiques (OCR errors, abréviations rares).
- **Asymptote** : précision haute uniformément.
- **Verdict** : ✓ **À câbler partout** (topical_cleanup, enhanced_filter, reranker rescue).

#### B.3 — Token-aware exact match
Découper $c$ en tokens (langue française), comparer token-séquence avec une normalization (NFC, lowercase, accent-stripping).

- **Asymptote** : équivalent à B.2 sur le plan de la précision; plus complexe sur les variantes orthographiques.
- **Verdict** : B.2 est strict-superset. À écarter.

#### B.4 — Embedding similarity (référence ↔ chunk)
Surcoût computational élevé pour un signal binaire. **Overkill**. À écarter.

**Choix B** : **B.2** câblé sur tous les sites de détection de référence.

### C. Sources de keywords / qualité de la liste

#### C.1 — JSON statique (current)
Lecture de `data/article_modifications.json["articles"][X]["keywords"]`.

- **Failure modes confirmés** :
  - Cross-refs vers d'autres articles (cas 973 → "150-0 b ter")
  - Métadonnée titre obsolète (cas 973 → "Droit de timbre permis de conduire")
  - Coverage déséquilibrée (kw_count median = 14, max = 55)
- **VERDICT** : Source acceptable comme socle, mais à filtrer au runtime.

#### C.2 — TF-IDF dynamique sur corpus local
Définir $L(X)$ = chunks dont `articles_mentionnes` ou `articles_cgi_lies` contient `X`.
$$\text{score}(t, X) = \text{tf}(t, L(X)) \cdot \log\frac{N}{\text{df}(t)}$$
Top-K termes.

- **Hypothèses** : `L(X)` est un échantillon représentatif des termes spécifiques à X.
- **Asymptote** : $|L(X)| \to 0$ → estimation vide; $|L(X)| \to \infty$ → estimation robuste.
- **Cost** : 1 query par article (cacheable).
- **Risque ref** : pour 150-0 B ter, TF-IDF donnerait probablement des keywords similaires à la curation manuelle, donc non-régression.
- **Verdict** : ★★★ approche idéale mais nécessite un fetch initial du corpus local.
  Différable si C.3 suffit.

#### C.3 — Filtrage runtime des cross-refs (sanity)
$$\text{kw\_clean}(X) = \text{kw}(X) \setminus \{kw : \exists Y \neq X, Y \in \text{cgi\_refs}(kw)\}$$
où `cgi_refs(kw)` extrait les références CGI dans une string par regex.

- **Hypothèses** : un keyword qui CITE un autre article CGI est une cross-ref parasite.
- **Failure mode** : un keyword légitime qui mentionne incidemment un autre article (rare).
- **Asymptote** : pour 99 % du catalogue, supprime les self-refs (bénignes) et les rares cross-refs (toxiques).
  Le coût est l'effet collatéral sur self-refs ("article X" dans keywords de X) — mais ces self-refs sont déjà couvertes par B.2 explicite, donc redondantes.
- **Cost** : O(|kw|) par article au chargement.
- **Verdict** : ★★ Lean et data-driven. À implémenter en sanity step.

#### C.4 — Curation manuelle élargie
Étendre `data/legal_term_expansions.json` à la main pour 973, 39 C et autres cas problématiques.
- **VERDICT** : Non-scalable (2196 articles). Patch, pas solution. À écarter sauf comme dernier recours.

**Choix C** : **C.3 (runtime cross-ref sanity)** comme sanitation suffisante.
C.2 (TF-IDF) défère à une itération suivante si C.3 + filtrage scale-invariant ne passe pas le floor.

### D. Fallback & robustesse

#### D.1 — Cascade tier multi-niveau (current, en partie)
Si tier-1 produit `kept = 0`, essayer tier-2 (family-aware). Si tier-2 vide, tier-3 (no filter).

- **Failure mode** : déclenche seulement quand TOUT est rejeté. Pour kept=1 (à peine non-vide), même problème, pas de fallback.
- **Verdict** : Insuffisant comme défense unique; redondant si scale-invariant fonctionne.

#### D.2 — Selection function intrinsèquement adaptative (A.4 max-gap)
Une formulation scale-invariante de la sélection est sa propre cascade : elle adapte le cut au profil de la distribution.

- **Avantage** : pas de seuil à décider.
- **Verdict** : ★★★ **Le bon filtre RESPECTE déjà ces invariants — pas besoin de cascade.**

#### D.3 — Re-recherche si starvation (feedback loop)
Si après sélection $|S| < \tau$, relancer la recherche avec paramètres relâchés (seuil semantique plus bas, top-K plus large).

- **Coût** : surcoût de latence (2× recherche).
- **Verdict** : Différable. Pas nécessaire si A.4 fonctionne.

**Choix D** : **D.2** — pas de cascade explicite, la scale invariance suffit.

### E. Scoring / reranking

#### E.1 — Cross-encoder reranker avec min_score absolu (current)
`settings.reranker_min_score` = constante.

- **Failure mode** : pour articles courts, scores reranker plus bas → tous coupés.
- **Verdict** : Mêmes risques que A.1 (seuil absolu).

#### E.2 — Reranker rescue via LF-article (current, partiel)
Recouvre des chunks coupés à condition qu'ils mentionnent l'article-de-loi.

- **Cap** : `len(reranked) < top_k` — la rescue cesse si reranker a rempli son quota.
- **Verdict** : Mécanisme utile mais sous-utilisé. À conserver, à mieux instrumenter.

#### E.3 — Reranker rescue + max-gap sur scores rerank
Après rerank, appliquer A.4 sur les scores rerank au lieu de `min_score`.

- **Asymptote** : si tous les scores rerank sont bas, max-gap garde quand même les meilleurs.
- **Verdict** : ★★ amélioration différable; E.2 + B.2 contextuel devraient suffire.

**Choix E** : Conserver E.2 (rescue). Différer E.3.

### F. Architecture pipeline

#### F.1 — Pipeline linéaire fixe (current)
search → enhanced_filter → amend_filter → topical_cleanup → LLM.

- **Failure mode** : les filtres en cascade composent leurs précisions/rappels, et les calibrations absolues s'accumulent.

#### F.2 — Pipeline scale-invariant linéaire
search → enhanced_filter (B.2 + A.4 sur sa propre métrique) → amend_filter (idem) → topical_cleanup (B.2 + A.4) → LLM.

- **Avantage** : chaque étage est scale-invariant individuellement → composition scale-invariante.
- **Verdict** : ★★★ Implémentation directe.

#### F.3 — Pipeline conditionnel branches
Détecter la densité d'article a priori → router vers branche low-data ou high-data.

- **Inconvénient** : seuil de routage = magic number.
- **Verdict** : Anti-pattern. À écarter.

**Choix F** : **F.2**.

---

## Synthèse des choix

| Axe | Choix | Justification |
|---|---|---|
| A. Calibration | **A.4** Max-gap + garde dégénérescence | Scale-invariant, paramétrique-libre |
| B. Référence matching | **B.2** Regex contextualisé câblé partout | Soundness, fonction déjà existante |
| C. Keywords | **C.3** Cross-ref runtime sanity | Lean, additif. TF-IDF (C.2) en backup. |
| D. Fallback | **D.2** Implicite via A.4 | Le filtre adapté SE charge de la cascade |
| E. Rerank | **E.2** Conserver rescue | Mécanisme déjà calibré |
| F. Architecture | **F.2** Pipeline linéaire scale-invariant | Composition naturelle |

---

## Vérification de cohérence

| Cause racine v1 | Choix résolutif | Mécanisme |
|---|---|---|
| #1 — topical_cleanup calibré sur abondance | A.4 + F.2 | Remplace `>= 0.6 OR >= 3` par max-gap |
| #2 — JSON déséquilibré (cross-refs) | C.3 + (B.2 protège contre toxicité) | Cross-refs filtrées au runtime |
| #3 — Substring `in` | B.2 partout | Regex contextualisé |
| Bonus : Generalization au catalogue | A.4 scale-invariant | Pipeline produit un % stable de chunks quelle que soit la densité |

---

## Asymptotic behavior summary

| Profil article | Comportement attendu (architecture finale) |
|---|---|
| Ultra-dense (kw=55+) | Max-gap cut produit ~k% chunks de plus haut score. Comportement quasi-identique au tier-3 actuel mais sans seuil absolu. |
| Dense (kw=20-40) | Max-gap cut produit ~k% chunks. Pas de "kept=0". |
| Sparse (kw=8-15, profil médian + 973/39 C) | Max-gap maintenant FONCTIONNE : trie par score (même si scores bas), garde les meilleurs. Pas le cas actuellement (0.6 jamais atteint). |
| Ultra-sparse (kw≤7) | Si presque tous scores=0, max-gap = 0, garde tout (n=quelques chunks). Acceptable. |
| Ambigu ("39 C", "39") | B.2 distingue "39 C" de "39 C bis"; max-gap utilise les scores réels (non polluables par bruit). |
| Vide / inexistant | `n_C = 0` → pas d'analyse, pas d'erreur, pipeline gracieux. |

---

## Falsifiabilité (à mesurer en Phase 6)

| Hypothèse | Test falsifiant |
|---|---|
| H1: A.4 max-gap est scale-invariant en pratique | `selection_homogeneity(dense, sparse) ∈ [0.5, 2]` |
| H2: B.2 contextuel résout la conflation 39 C/39 C bis | Inspection manuelle de 10 chunks rejetés sur 39 C : aucun ne porte une mention de "39 C" simple. |
| H3: C.3 réduit le bruit pour 973 | scoreable_kw pour 973 ne contient plus "2° du iii de l'article 150-0 b ter" après filtre. |
| H4: Architecture finale ne régresse pas 150-0 B ter | `n_sources_v2 >= 17 AND avg_confidence_v2 >= 2.0` strictement. |
| H5: Architecture résout 973/39 C | `n_sources_v2(973) >= 5 AND avg_confidence_v2(973) > 1.0` ; idem 39 C. |
| H6: Architecture généralise (controls) | Sur articles non-design (4 B, 1518 A bis, 77), confiance moyenne ≥ ref scaled by density. |

---

**Phase 1-2 livrée. Phase 3-4 (candidates + stress test) suit.**
