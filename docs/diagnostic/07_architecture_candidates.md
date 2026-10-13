# Phase 3-4 — Architectures candidates + stress test théorique

## Phase 3 — Trois architectures candidates

### Architecture LEAN (préférée)

**Principe** : minimum viable, applique uniquement les choix Pareto-dominants
de chaque axe. **Aucun nouvel module**, modifications surgicales.

**Composition** :
- `B.2` — `_build_contextualized_patterns` câblé sur tous les sites de
  détection de référence (3 sites : topical_cleanup, enhanced_filter
  explicit-counter, reranker rescue).
- `A.4` — Max-gap selection avec garde dégénérescence remplaçant
  `weighted_score >= 0.6 OR hit_count >= 3` dans `_topical_relevance_cleanup`.
- `C.3` — Sanity filter runtime sur la liste de keywords lue de `article_modifications.json`
  (supprime les keywords qui citent un AUTRE article CGI). Cache au niveau article.
- Architecture pipeline F.2 préservée (composition linéaire).

**Coût d'implémentation estimé** : ~80 lignes modifiées, 3 fichiers
(`article_analyzer.py`, `search.py`, optionnel `data/utils`).

**Falsificateur principal** : si LEAN ne porte pas 973/39 C au-dessus du
plancher minimum (`n_sources >= 5` et `avg_confidence > 1.0`), passer à RICH.

---

### Architecture RICH (escalation si LEAN échoue)

**Principe** : LEAN augmenté par la sourcing data-driven des keywords et un
feedback loop léger.

**Composition** :
- Tout LEAN.
- `C.2` — TF-IDF dynamique sur le corpus local de chaque article, calculé
  on-demand au premier appel et caché. Augmente la liste statique au lieu
  de la remplacer.
- `D.3` — feedback loop : si après tous les filtres `|S| < ⌈median(|S_corpus|)⌉`,
  relance la recherche avec `top_k *= 2` et seuils relâchés.

**Coût d'implémentation estimé** : ~200 lignes, 4-5 fichiers.

**Risque** : régression latence (2× recherche dans le cas worst).

---

### Architecture HYBRID-DEFENSIVE (rejected)

**Principe** : LEAN + conservation de tous les anciens seuils en mode "safety net".

**Composition** :
- LEAN appliqué.
- Si max-gap garde trop peu de chunks (`|S| < k_min`), tomber sur les
  anciens filtres comme backup.

**Pourquoi rejetée** :
- `k_min` réintroduit un magic number → viole le principe directeur.
- L'architecture devient un Frankenstein avec deux logiques en parallèle.
- Le scale-invariant DOIT être sa propre garantie. Si on a besoin d'une
  sauvegarde, c'est qu'on n'a pas confiance — l'autre approche (RICH) est
  plus propre.

---

## Phase 4 — Stress test théorique de LEAN

Pour chaque profil d'article, on prédit le comportement de LEAN à partir
de l'analyse mathématique.

### Profil 1 — Ultra-dense (kw=55, mod=17 — i.e. 150-0 B ter actuel)

**Hypothèses sur le pool de candidats** :
- $n_C \approx 20\text{-}40$ chunks par modification (semantique + keyword)
- scores `weighted_score` distribués sur $[0, 5+]$ (variance importante)
- Plusieurs chunks ont une mention explicite contextuelle

**Comportement LEAN attendu** :
1. B.2 détecte ~5-10 chunks à mention explicite → toujours gardés.
2. Sur le reste (chunks sans mention), max-gap trouve un saut significatif
   entre "scoreable" et "rares hits" → garde la partie haute.
3. La taille finale par modification ≈ 5-15, total ≥ 17 sources sur 8 mods.
4. **Non-régression attendue** : oui (le filtre reste effectif sur les chunks
   non-relevant, et ne perd pas plus que le tier-1 actuel).

**Risque détecté** : si la distribution des scores est plat (tous à 0 ou
tous à valeur médiane), max-gap = 0 → garde tout (incluant bruit).
- **Mitigation** : la "garde anti-dégénérescence" (cf. ci-dessous) ne s'active
  que si max_gap < seuil-de-rang-1 ; dans ce cas, on revient à un tri par
  rerank-score uniquement.

### Profil 2 — Médian (kw=14, mod=4 — i.e. 1518 A bis)

**Hypothèses pool** :
- $n_C \approx 10\text{-}20$ par modification
- `weighted_score` distribué sur $[0, 2]$, principalement 0 sauf quelques hits

**Comportement LEAN attendu** :
1. B.2 détecte ~0-3 chunks à mention explicite → gardés.
2. Max-gap : avec 14 keywords, la majorité des chunks ont 0 hits, quelques-uns
   ont 1-2 hits → gros gap entre 0-hit et 1-hit → cut là.
3. Garde 1-5 chunks par modification → total ~10-20 sources sur 8 mods.
4. **Prédiction** : meilleure couverture que current (qui rejette tout au 0.6).

### Profil 3 — Sparse (kw=9-16, mod=3-6 — i.e. 973, 39 C)

**Hypothèses pool** :
- $n_C \approx 5\text{-}15$ par modification
- `weighted_score` quasi tous 0, quelques 1-hit

**Comportement LEAN attendu** :
1. B.2 contextuel détecte les rares mentions explicites de "973" ou "39 C"
   (PAS de conflation 39 C/39 C bis) → 0-2 chunks gardés selon mod.
2. Max-gap sur les scoreable : gros gap entre les 0-hit chunks (la majorité)
   et les rares hits → cut sépare bruit et signal.
3. Garde 1-3 chunks par modification → total ~5-15 sources sur 8 mods.
4. **Prédiction** : passe de 0-1 source à 5-15 sources.
5. **Risque résiduel** : si C.3 ne suffit pas à neutraliser une cross-ref
   parasite (ex: la cross-ref "150-0 b ter" pour 973), un chunk discutant de
   150-0 B ter peut être promu pour 973 → bruit final.
   - **Détectabilité** : oui, par inspection des chunks finaux. Si confirmé,
     escalade vers RICH (TF-IDF data-driven remplace les keywords pollués).

### Profil 4 — Ultra-sparse à beaucoup de mods (kw=6, mod=25 — article 77)

**Hypothèses pool** :
- $n_C$ variable par mod, possiblement 0-5 sur certaines mods
- scoreable_kw=6 → quasi-toujours 0 hits
- Mais beaucoup de modifications → cumulent

**Comportement LEAN attendu** :
1. B.2 contextuel : "77" est ultra-court → patterns `_build_contextualized_patterns`
   doivent inclure "article 77 du c" → fonctionnera comme pour "973".
2. Sur la majorité des mods : très peu de chunks, max-gap → garde 1-2.
3. Sur les modifications "anciennes" (1980s ?) : possible peu de chunks
   amont mais on garde au moins ceux trouvés.
4. **Prédiction** : couverture pauvre mais positive (>0 vs current=0).
5. **Risque** : un article qui n'a vraiment AUCUNE intervention sur la
   plupart de ses mods. La pipeline est limitée par les données, pas par
   le filtre. C'est acceptable (pas le rôle du filtre de fabriquer des données).

### Profil 5 — Article à nom ambigu sous-chaîne ("39", "1")

**Hypothèses pool** :
- Recherche keyword amont remonte BEAUCOUP de candidats (collisions massives)
- $n_C$ très grand, mais signal/bruit faible

**Comportement LEAN attendu** :
1. B.2 contextuel : `_build_contextualized_patterns("39")` produit
   `\b39\b(?!\s+(?:bis|ter|...|[A-H])(?:\s|$))` → distingue "39" de "39 A",
   "39 B", "39 bis"... → précision haute.
2. Mais 39 vrai (pas une année !) doit être anchored : "article 39 du c"
   pour passer.
3. Max-gap sur scores : si la recherche amont a produit ~100 candidats,
   tri sémantique + reranker met les vrais positifs en haut, max-gap cut au
   bon endroit.
4. **Prédiction** : LEAN gère bien, à condition que `_is_short_article_ref`
   bascule correctement sur le mode short ref.

### Profil 6 — Inexistant ou fautif ("999999", "abc")

**Comportement LEAN attendu** :
1. Recherche amont retourne 0 chunks → pipeline ne fait rien.
2. Si l'article a une entrée dans `article_modifications.json` mais aucun
   chunk → `analyze_article` produit un rapport vide.
3. Pas d'erreur, juste un rapport "Aucune donnée".
4. **Prédiction** : gracieux.

---

## Conclusion stress test

| Profil | LEAN résolution ? | Risque résiduel |
|---|---|---|
| Ultra-dense | ✓ Non-régression | Faible |
| Médian (50% catalog) | ✓ Amélioration significative attendue | Faible |
| Sparse (973, 39 C) | ✓ Solution attendue | Cross-ref parasite si C.3 imparfait |
| Ultra-sparse + mods | ✓ Couverture limitée par data | Acceptable |
| Ambigu | ✓ B.2 désambiguïse | Aucun |
| Inexistant | ✓ Graceful | Aucun |

**Aucun profil ne fait échouer LEAN catastrophiquement**.

→ **Décision : implémenter LEAN. Escalation conditionnelle vers RICH** uniquement
si bench v2 ne passe pas H4 ou H5 (cf. design_space.md).

---

## Plan d'incréments d'implémentation

Trois incréments mesurables, chacun benché avant le suivant. Si un incrément
régresse 150-0 B ter, on annule et on diagnostique.

### Increment 1 — B.2 : Contextual regex partout
**Sites de modification** :
- `src/analysis/article_analyzer.py:1123` (`_topical_relevance_cleanup`, branche explicit)
- `src/indexing/search.py:1900-1904` (rescue pool, `has_cgi_ref`)
- Pour `enhanced_filter` (`_filter_chunks_by_article_relevance`, line ~972) : à inspecter, brancher si simple

**Risque ref** : très faible (les patterns générés couvrent les variantes
substring existantes + ajoutent des word-boundaries).

**Bench attendu** : 150-0 B ter ≈ inchangé (long ref insensible). 973 et 39 C :
moins de FP, peut-être un poil moins de sources mais plus pertinentes.

### Increment 2 — A.4 : Max-gap selection
**Site de modification** :
- `src/analysis/article_analyzer.py:1138` (`_topical_relevance_cleanup`,
  branche scoring)

**Implémentation** :
```python
def _max_gap_cut(scored_chunks):
    """Returns indices to keep based on max gap in sorted scores."""
    if len(scored_chunks) <= 1:
        return list(range(len(scored_chunks)))
    sorted_with_idx = sorted(
        enumerate(scored_chunks),
        key=lambda x: x[1][1],  # score (the second element of (chunk, score))
        reverse=True,
    )
    scores = [s for _, (_, s) in sorted_with_idx]
    # Gap-finding
    gaps = [scores[i] - scores[i+1] for i in range(len(scores) - 1)]
    max_gap = max(gaps)
    if max_gap <= 0:
        # Degenerate: all equal. Keep all.
        return list(range(len(scored_chunks)))
    cut = gaps.index(max_gap) + 1  # keep up to and including this index
    kept_indices = {sorted_with_idx[i][0] for i in range(cut)}
    return [i for i in range(len(scored_chunks)) if i in kept_indices]
```

**Garde anti-dégénérescence** : si toutes les scores sont 0, max_gap = 0 → keep all.

**Risque ref** : modéré. La distribution des scores pour 150-0 B ter peut
avoir un comportement non-trivial. Bench obligatoire.

**Bench attendu** : 973 et 39 C → de 0 sources à plusieurs. Référence → idem ou marginalement plus.

### Increment 3 — C.3 : Cross-ref runtime sanity
**Site de modification** :
- Helper dans `src/analysis/article_analyzer.py` ou `src/utils/keyword_sanity.py`
- Appliqué dans `get_keywords_for_article` (search.py:337) ou lors du chargement.

**Implémentation conceptuelle** :
```python
_CGI_REF_RE = re.compile(
    r"(?:l['']article|articles?|art\.?)\s+"
    r"(\d+(?:\s*-\s*\d+)?(?:\s+[A-Z](?:\s+(?:bis|ter|quater|...))?)?)",
    re.IGNORECASE,
)

def _drop_cross_ref_keywords(keywords, target_ref):
    target_norm = _norm_ref(target_ref)
    cleaned = []
    for kw in keywords:
        refs = _CGI_REF_RE.findall(kw)
        if not refs:
            cleaned.append(kw)  # No CGI ref in kw
            continue
        # If any extracted ref is NOT the target, drop the keyword
        if all(_norm_ref(r) == target_norm for r in refs):
            cleaned.append(kw)
        # else: parasite, drop silently
    return cleaned
```

**Risque ref** : nul à faible. Pour 150-0 B ter, les self-refs (`article 150-0 B ter`)
sont conservées (toutes les refs extraites = target).

**Bench attendu** : 973 perd la cross-ref "150-0 b ter" parasite → meilleure
spécificité du scoreable_kw. Bench mesurable.

### Logs `[DIAG:*]`
**Conservés pour cette itération** : oui, car le diagnostic des nouveaux
mécanismes (max-gap, contextual regex) nécessite l'observation des outputs
à chaque incrément. Retrait final après bench validation.

---

**Phase 3-4 livrée. Implémentation suit.**
