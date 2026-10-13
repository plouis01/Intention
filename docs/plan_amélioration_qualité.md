# Plan D’Amélioration Qualité RAG

## Résumé
- Le problème le plus critique n’est pas l’index seul: c’est l’**incohérence d’espace d’embedding**. `.env` configure `VERTEX_EMBEDDING_MODEL=text-embedding-004`, mais la base contient **5 336 254 vecteurs `gemini-embedding-001`** et **117 563 vecteurs `text-embedding-005`**, et **aucun `text-embedding-004`**. Tant que requêtes et corpus ne vivent pas dans le même espace, la qualité est structurellement plafonnée.
- Le backend `pgvector` en production n’a pas de vrai sparse retrieval: il fait **dense + ILIKE/trigram**, alors que le BM25 n’est utilisé que dans le backend mémoire. Pour un corpus juridique avec références exactes, amendements, numéros d’articles et synonymes métier, c’est une faiblesse majeure.
- L’index actuel est **IVFFLAT lists=2048** sur **5 453 817** vecteurs, avec `ivfflat.probes=50` forcé par l’application. Sur une requête représentative filtrée (`chunk_type`, date), `EXPLAIN ANALYZE` montre **930 ms** avec **2 084 lignes retirées par filtre après scan ANN**. `pgvector 0.8.1` est installé, donc les **iterative scans** sont disponibles mais non utilisés.
- Le vrai goulot de troncature n’est pas l’embedding Vertex: **0 chunk > 8000 chars**. En revanche, le reranker tronque à **1500 chars**, alors que **17,5 % des interventions** et **79,0 % des amendements** dépassent ce seuil.
- La qualité des filtres SQL est limitée par `chunk_metadata` en JSON texte: **224 594 chunks (4,1 %) n’ont pas de date**, et les filtres `chunk_metadata->>'date'` ne bénéficient pas d’index typé.
- La couche d’éval existe mais reste trop petite pour piloter une optimisation ambitieuse: **79 cas / 27 articles**. C’est utile pour la régression, insuffisant pour choisir finement embeddings, chunking, reranking et index.

## Recommandations Priorisées
| Priorité | Recommandation | Pourquoi | Impact | Coût | ROI |
|---|---|---|---|---|---|
| 1 | **Aligner immédiatement requêtes et corpus sur un seul `embedding_model`** et ajouter `WHERE embedding_model = :query_model` dans toutes les requêtes pgvector | Aujourd’hui, le retrieval compare des vecteurs de familles différentes | Fort | Faible à moyen | Très fort |
| 2 | **Ajouter `task_type=RETRIEVAL_QUERY` / `RETRIEVAL_DOCUMENT`** aux embeddings Vertex, avec `title` pour les documents si disponible | Les embeddings Vertex supportent explicitement l’asymétrie query/document; le code actuel ne l’utilise pas | Fort | Faible | Très fort |
| 3 | **Remplacer le pseudo-hybride dense+ILIKE par un vrai sparse+dense** en PostgreSQL (`tsvector` + GIN + RRF/weighted fusion) | En prod, vous n’avez pas de BM25 réel; ILIKE ne suffit pas sur le juridique | Fort | Moyen | Très fort |
| 4 | **Garder IVFFLAT à court terme mais le rendre “filter-aware”**: partial indexes par `chunk_type`/`embedding_model`, `ivfflat.iterative_scan=relaxed_order`, `ivfflat.max_probes`, distance filter déplacé dans un CTE matérialisé | Les filtres sont appliqués après ANN; c’est exactement le cas ciblé par la doc pgvector | Fort | Moyen | Fort |
| 5 | **Rechunker les longs contenus**: sous-chunks speaker-preserving pour interventions et champs séparés pour amendements (`objet`, `dispositif`, `exposé`) | Le reranker perd une partie importante du signal aujourd’hui | Fort | Moyen | Fort |
| 6 | **Passer l’extraction en orchestration à deux étages**: Haiku pour filtrage/faits, Sonnet 4.5/4.6 pour sections ambiguës ou faibles | Le même modèle n’est pas optimal pour toutes les phases | Moyen à fort | Moyen | Fort |
| 7 | **Normaliser les métadonnées en colonnes typées** (`chunk_date`, `chambre`, `law_year`, `article_ref_norm`, `speaker_role`) | Les filtres JSON texte coûtent en rappel et en performance | Fort | Moyen | Fort |
| 8 | **Étendre le golden set et instrumenter la pipeline** | Sans mesure fine, vous optimiserez à l’aveugle | Fort | Moyen | Fort |
| 9 | **Enrichir la donnée source**: backfill dates manquantes, hygiène des chambres/fonctions, couverture citations curées sur articles à fort trafic | Une partie de la “qualité LLM” est en fait une dette de donnée | Moyen à fort | Moyen | Moyen |
| 10 | **Benchmark HNSW seulement après 1–9** | HNSW a un meilleur tradeoff speed/recall, mais ce n’est pas votre premier plafond aujourd’hui | Moyen | Élevé | Moyen |

## Analyse Par Composant

### 1. Qualité des embeddings
- **Action immédiate**: basculer le query-side sur `gemini-embedding-001` tant que le corpus n’est pas ré-embed, et filtrer `embedding_model='gemini-embedding-001'`. C’est la mitigation la plus rapide.
- **Action structurelle**: choisir un seul modèle cible et ré-embed 100 % du corpus. Mes recommandés à A/B tester: `gemini-embedding-001` et `text-embedding-005`. **Ne pas** garder `text-embedding-004` tant que le corpus n’est pas ré-aligné.
- **Action de qualité**: utiliser `RETRIEVAL_QUERY` pour les requêtes et `RETRIEVAL_DOCUMENT` pour les chunks. C’est un gain faible en effort et souvent net en rappel.
- **Action de signal**: ajouter un `title` document quand possible: `"{article_ref} | {law_type} {law_year} | {chambre} | {orateur}"` pour les chunks débats, et `"{amendement_numero} | {article_ref}"` pour les amendements.
- **Quick win**: ne pas perdre de temps à augmenter la limite de texte envoyée au modèle d’embedding; vos chunks ne dépassent pas 8000 caractères.

### 2. Configuration de l’index vectoriel
- **Ne pas changer d’abord IVFFLAT vers HNSW**. Le gain principal viendra d’abord de l’alignement embeddings + vrai hybrid search + filtres mieux structurés.
- **Créer des indexes partiels** par `chunk_type` et `embedding_model`, par exemple pour `intervention/gemini-embedding-001` et `amendement/gemini-embedding-001`. `pgvector` recommande les partial indexes quand le filtre a peu de valeurs distinctes, ce qui est exactement votre cas.
- **Activer `ivfflat.iterative_scan=relaxed_order`** et **`ivfflat.max_probes`**. Avec `pgvector 0.8.1`, c’est disponible et adapté aux requêtes filtrées.
- **Refactor SQL**: faire `WITH nearest_results AS MATERIALIZED (...) SELECT * FROM nearest_results WHERE distance <= ...` pour que le filtre de distance n’interfère pas avec l’ANN, conformément à la doc pgvector.
- **Rendre `probes` adaptatif** au lieu de le figer à 50. Proposition: base à `16–24`, montée progressive sur les requêtes avec filtres serrés, références courtes, ou peu de candidats.
- **Alternative moyen terme**: benchmark HNSW sur un replica ou sur l’index `intervention` uniquement, pas en premier move.

### 3. Stratégie de retrieval
- **Ajouter un vrai sparse retriever PostgreSQL**: `tsvector` pondéré sur `content`, `article_ref_exact`, `article_ref_norm`, `orateur`, `fonction`, `numero amendement`, `law_ref`. Fusionner dense + sparse par RRF.
- **Conserver trigram uniquement pour l’exact match** sur références courtes et numéros d’article. Le trigram est utile, mais ce n’est pas un remplaçant de BM25/FTS.
- **Réduire la dépendance au `min_similarity` précoce**. En qualité RAG, un seuil trop tôt coupe souvent les candidats utiles avant reranking.
- **Rechunking ciblé**: interventions longues en sous-chunks de 400–800 tokens avec recouvrement léger et `parent_intervention_id`; amendements séparés en chunks `objet`, `dispositif`, `exposé`. Le reranker ne doit plus voir un bloc monolithique tronqué.
- **Éviter d’embedder le “previous speaker preview” dans le même champ** que le contenu principal. Garder ce contexte dans une colonne secondaire ou ne l’utiliser qu’au moment de la synthèse.
- **Ajouter un fallback “article_ref_exact first”** avec colonnes normalisées plutôt que dépendre d’ILIKE sur `content`.

### 4. Prompting et orchestration LLM
- **Promouvoir `EXTRACTION_MODE=two_step`** au moins pour les cas difficiles. Le code est déjà là; c’est un quick win expérimental crédible.
- **Routage de modèle**: Haiku pour `relevance_filter` et extraction de faits; Sonnet 4.5/4.6 pour synthèse finale si `sources<2`, `single_chamber`, `quote_rejection_ratio élevé`, ou `cross-law` détecté.
- **Retourner des citations ancrées à un `source_id` ou `chunk_id`**, pas seulement du texte libre. Aujourd’hui la validation se fait contre le texte concaténé; demain, faites-la contre la source exacte.
- **Gating automatique**: si la validation retire >50 % des citations ou si une seule chambre survit, relancer retrieval + synthèse avec budget supérieur plutôt que de figer une section faible.
- **Pilote qualité**: tester `LangExtract` uniquement sur les sections les plus sensibles en précision de citation; ne pas en faire le chemin par défaut tant qu’il n’est pas benchmarké.

### 5. Qualité et structuration des données sources
- **Backfill obligatoire** des dates manquantes dans `chunk_embeddings`; aujourd’hui 4,1 % des chunks sont exclus des recherches datées.
- **Normaliser `chambre`, `fonction`, HTML entities et formats d’orateur`** avant embedding et avant rendu. La cohérence de ces champs améliore à la fois les filtres, les boosts et la certitude.
- **Ajouter des colonnes dérivées stockées**: `chunk_date date`, `law_year int`, `article_ref_norm text`, `speaker_role text`, `is_procedural bool`.
- **Prioriser l’enrichissement de `article_modifications.json`** sur les articles à fort trafic / forte densité de modifications. La couverture actuelle est partielle (`1431/2196` avec citations curées; `132/2196` avec jurisprudence).

## Quick Wins
- Filtrer toutes les requêtes pgvector par `embedding_model` et aligner immédiatement le query model sur `gemini-embedding-001`.
- Ajouter `task_type` Vertex `RETRIEVAL_QUERY` / `RETRIEVAL_DOCUMENT`.
- Activer `ivfflat.iterative_scan=relaxed_order` et sortir le filtre de distance dans un CTE matérialisé.
- Lancer un A/B `single` vs `two_step` sur le golden set existant.
- Ajouter `pg_stat_statements` et journaliser `candidate_count`, `post_filter_count`, `quote_rejection_ratio`, `chamber_balance`.

## Métriques Et Validation
- **Retrieval**: Recall@20, MRR, nDCG@20, contamination rate, chamber balance@k, exact-article precision.
- **Grounding**: quote precision, quote recall, citation rejection rate après validation.
- **Analyse**: scores juge `precision / pertinence / complétude / cohérence`, calibration de `niveau_certitude`.
- **ANN**: recall approximate vs exact sur un échantillon de requêtes, par `chunk_type` et par filtre date.
- **Données**: % chunks sans date, % sections sans 2e chambre, couverture curated/forward/jurisprudence.
- **Cadence**: avant tout changement, augmenter le golden set à **300+ cas** avec surreprésentation des références courtes (`39`, `200`, etc.) et des articles amendement-heavy.

## Hypothèses
- La base locale `legislateur_local` est représentative de la prod pour `chunk_embeddings`.
- Le backend effectif est bien `ChunkEmbeddingsPgVectorStore` avec `pgvector 0.8.1`, `pg_trgm 1.6`, IVFFLAT `lists=2048`.
- Je n’ai pas pu exécuter l’éval Python du dépôt dans ce shell car l’environnement manque de dépendances (`numpy`, `pytest`), donc les priorités ci-dessus s’appuient sur l’inspection code + SQL + docs officielles, pas sur un score offline recalculé ici.

## Sources
- Local code: [search.py](/Users/thib/Documents/sides/legal-folder/legal-project-2nd-repo/src/indexing/search.py), [vector_stores.py](/Users/thib/Documents/sides/legal-folder/legal-project-2nd-repo/src/indexing/vector_stores.py), [article_analyzer.py](/Users/thib/Documents/sides/legal-folder/legal-project-2nd-repo/src/analysis/article_analyzer.py), [embeddings.py](/Users/thib/Documents/sides/legal-folder/legal-project-2nd-repo/src/indexing/embeddings.py), [config.py](/Users/thib/Documents/sides/legal-folder/legal-project-2nd-repo/src/utils/config.py)
- pgvector docs: https://github.com/pgvector/pgvector
- Vertex AI task types: https://docs.cloud.google.com/vertex-ai/generative-ai/docs/embeddings/task-types
- Vertex AI text embeddings reference: https://docs.cloud.google.com/vertex-ai/generative-ai/docs/model-reference/text-embeddings-api
- Claude models on Vertex AI: https://docs.cloud.google.com/vertex-ai/generative-ai/docs/partner-models/claude
- Anthropic prompt caching: https://docs.anthropic.com/fr/docs/build-with-claude/prompt-caching
