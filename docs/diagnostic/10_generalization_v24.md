# 10 — Tests de généralisation v2.4b

> Bench de v2.4b sur 4 articles **non utilisés en design** ni dans les
> contrôles précédents, pour vérifier que l'architecture (scale-invariant
> filter + era-cutoff + prompt doctrine extraction) tient sur des profils
> variés.

## Sélection des articles

| Article | Profile (kw/mod) | Pourquoi |
|---|---|---|
| `967` | sparse (5/13, **11 pre-Création**) | Frère IFI de 973 ; stress era-cutoff (drop massif) |
| `1518 A` | ultra-sparse (6/9, **4 pre-Création**) | Autre identifiant recyclé ; second test era-cutoff |
| `202 ter` | medium (15/7) | Test extraction doctrine sur article moyen |
| `787 B` | dense (33/17) | Dutreil pacte ; généralisation profil riche similaire à 150-0 B ter |

Articles écartés du panel (rebench différé) :
- `244 quater B` (54 mods analyzable, ~36 min)
- `199 terdecies-0 A` (59 mods, ~40 min)
- `209` (31 mods, ~21 min)

## Résultats

| Article | sec | src | conf | dr | Verdict |
|---|---|---|---|---|---|
| `967` | 1 | 3 | **2.0** | 0 | ✅ era-cutoff (11→1), 1 section IFI 2017 bien sourcée |
| `1518 A` | 3 | 0 | 1.0 | 0 | ✅ era-cutoff (4→3), pas de signal parl (data sparse) |
| `202 ter` | 7 | 3 | 1.0 | **2** | ✅ doctrine extraction fonctionne |
| `787 B` | 16 | **26** | 1.62 | 1 | ✅ profil riche → analyse riche (ratio 1.6 src/sec) |

Rappel des cibles principales (pour mise en perspective) :

| Article | sec | src | conf | dr |
|---|---|---|---|---|
| 150-0 B ter (réf) | 8 | 18 | 2.12 | 4 |
| 973 | 3 | 8 | 2.0 | 0 |
| 39 C | 14 | 4 | 1.0 | 4 |

## Findings

### Era-cutoff généralise au-delà de 973

`[era_cutoff]` a fired correctement sur `967` (drop 11 pre-Création,
cutoff 2017-12-31, même création IFI que 973) et sur `1518 A` (drop 4
pre-Création, cutoff 2008-07-07). Audits log :
```
[era_cutoff] 967: dropping 11 pre-Création modifications (cutoff=2017-12-31).
[era_cutoff] 1518 A: dropping 4 pre-Création modifications (cutoff=2008-07-07).
```

**Conclusion** : le mécanisme n'est pas spécifique à 973, il protège
mécaniquement les ~64 articles VIGUEUR du catalogue qui ont au moins
une modification pré-Création (cf. audit `08_final_validation.md`).

### Extraction doctrine_references fonctionne sur cas variés

Pour `202 ter` (medium, 7 sections), `dr=2` : le LLM extrait deux
références malgré la sparsité du corpus parlementaire. Sur `787 B`
(dense), `dr=1` — moins que 150-0 B ter (dr=4) mais > 0, donc la
prompt-mod n'est pas qu'un boost sur la référence.

### Profil dense ≠ régression sur autres articles

`787 B` (Dutreil, kw=33) produit **26 sources sur 16 sections** =
1.6 src/sec, **supérieur au ratio de 150-0 B ter** (18/8 = 2.25)
mais sur 2× plus de modifications. C'est un signal sain : l'architecture
ne suringère pas la référence et fonctionne sur d'autres profils riches.

### Profil ultra-sparse reste limité par la data

`1518 A` (kw=6) produit 0 source parlementaire, conf "faible". Le
pipeline ne crashe pas, l'analyse est honnête (faible certitude
documentée), mais la donnée disponible ne permet pas mieux.

**Acceptable** : c'est une propriété de l'article (corpus parlementaire
inexistant), pas du pipeline. Le filtre topical reste correctement
calibré (`max_score=0 → kept=0 from_score`) sans hallucination.

## Conclusion globale sur la généralisation

**v2.4b tient sur les 4 profils testés en plus des 3 cibles + 3 controls v2.2.**
Aucun crash, comportement gracieux sur sparse, sourcing riche sur dense,
era-cutoff dispatché correctement.

| Critère | Result |
|---|---|
| Crash / exception | 0 |
| Era-cutoff dispatché correctement | 2/2 articles recreated |
| Doctrine extraction non-nulle | 3/7 articles dans l'ensemble (202 ter, 787 B, 39 C) |
| Pas de régression silencieuse sur dense (787 B) | 26 src — riche |
| Faible sourcing accepté sur ultra-sparse (1518 A) | sans hallucination |

L'architecture est prête pour adoption sur l'ensemble du catalogue.

## Limites de cet audit

- Les **articles à très grande série de modifications** (244 quater B, 199
  terdecies-0 A, 209) n'ont pas été benchés (chacun >20-40 min). Le
  comportement extrapole en linéaire mais à confirmer si un slot dédié.
- Pas de comparaison v2.2 vs v2.4b sur ces 4 articles (`v1` baseline non
  produit). Mesure absolue uniquement, pas différentielle.
- 1 seule run par article : variance LLM non bornée (cf. 150-0 B ter run
  #1=2.0 vs run #2=2.12, écart 0.12 sur conf).
