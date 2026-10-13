# Scripts de migration et maintenance pgvector

Ce dossier contient tous les scripts nécessaires pour migrer vers pgvector et maintenir les index HNSW.

---

## 📁 Fichiers disponibles

### Scripts SQL

| Fichier | Description | Durée | Usage |
|---------|-------------|-------|-------|
| **`migrate_to_pgvector.sql`** | Migration complète JSON → vector + index HNSW | 2h-2h30 | Une seule fois (migration initiale) |
| **`monitor_migration.sql`** | Dashboard monitoring temps réel | <1s | Pendant migration (watch -n 30) |
| **`reindex_new_chunks.sql`** | Rebuild intelligent des index HNSW | 1h-2h | Après gros imports (>100K chunks) |

### Scripts Bash

| Fichier | Description | Durée | Usage |
|---------|-------------|-------|-------|
| **`clone_railway_db_to_local.sh`** | Clone Railway → PostgreSQL local | 10-20 min | Avant migration prod (tests locaux) |

### Scripts Python

| Fichier | Description | Durée | Usage |
|---------|-------------|-------|-------|
| **`test_pgvector_performance.py`** | Benchmark performance pgvector | 1-2 min | Après migration (validation) |

---

## 🚀 Workflows d'utilisation

### 1️⃣ Tester la migration en local (AVANT Railway)

```bash
# Étape 1 : Cloner Railway → local
./scripts/clone_railway_db_to_local.sh

# Étape 2 : Charger l'env local
export $(grep -v '^#' .env.local | xargs)

# Étape 3 : Migrer vers pgvector
# Terminal 1 : Migration
psql $DATABASE_URL -f scripts/migrate_to_pgvector.sql

# Terminal 2 : Monitoring
watch -n 30 'psql $DATABASE_URL -f scripts/monitor_migration.sql'

# Étape 4 : Valider performance
python scripts/test_pgvector_performance.py
```

**Résultat attendu :**
- ✅ Index HNSW créés
- ✅ Recherches < 50ms
- ✅ Démarrage API < 2s

### 2️⃣ Migrer Railway en production

```bash
# Étape 1 : Backup Railway
pg_dump "$RAILWAY_DATABASE_URL" --no-owner --no-acl \
  --file="data/railway_backup_$(date +%Y%m%d).sql"

# Étape 2 : Charger l'env Railway
export $(grep -v '^#' .env | xargs)

# Étape 3 : Migrer
# Terminal 1 : Migration
psql $DATABASE_URL -f scripts/migrate_to_pgvector.sql

# Terminal 2 : Monitoring
watch -n 30 'psql $DATABASE_URL -f scripts/monitor_migration.sql'

# Étape 4 : Ajouter variable Railway
# Dans Railway Dashboard → Variables :
# VECTOR_STORE_PROVIDER=pgvector

# Étape 5 : Redéployer API et valider
curl https://your-app.railway.app/health
python scripts/test_pgvector_performance.py
```

### 3️⃣ Reindexer après gros import

```bash
# Après avoir importé >100K nouveaux chunks
export $(grep -v '^#' .env | xargs)
psql $DATABASE_URL -f scripts/reindex_new_chunks.sql
```

---

## 📖 Guides détaillés

Consultez les guides complets dans `/docs` :

- **`docs/testing-pgvector-locally.md`** : Guide pas-à-pas clone + test + migration
- **`docs/pgvector-incremental-indexing.md`** : Gestion des nouvelles données
- **`docs/pgvector-migration-summary.md`** : Résumé complet de la migration

---

## 🛠️ Détails des scripts

### `migrate_to_pgvector.sql`

**Ce qu'il fait :**
1. ✅ Active l'extension pgvector
2. ✅ Renomme `embedding` → `embedding_backup` (sécurité)
3. ✅ Crée nouvelle colonne `embedding vector(384)`
4. ✅ Migre les données JSON → vector
5. ✅ Crée index B-tree (type, hash)
6. ✅ Crée index HNSW partiels (intervention, amendement)
7. ✅ VACUUM ANALYZE

**Paramètres importants :**
- `m = 16` : Connexions par nœud HNSW (optimal pour 1-10M vecteurs)
- `ef_construction = 64` : Qualité de build (higher = better recall)

**Rollback :**
```sql
DROP INDEX idx_embeddings_intervention_hnsw;
DROP INDEX idx_embeddings_amendement_hnsw;
ALTER TABLE chunk_embeddings DROP COLUMN embedding;
ALTER TABLE chunk_embeddings RENAME COLUMN embedding_backup TO embedding;
```

---

### `monitor_migration.sql`

**Ce qu'il affiche :**
1. Statut extension pgvector
2. Type de colonne embedding (JSON vs vector)
3. Progression migration data (% rows migrées)
4. **Progression build index HNSW** (temps réel)
5. Liste des index et leur statut
6. Taille table + index
7. Distribution par chunk_type
8. Activité récente
9. Estimation temps restant

**Usage recommandé :**
```bash
# Refresh toutes les 30 secondes
watch -n 30 'psql $DATABASE_URL -f scripts/monitor_migration.sql'
```

**Métriques clés à surveiller :**
- `Index Build Progress` → % completion
- `Estimated time remaining` → combien de temps encore
- `Index Status` → doit être "Valid" à la fin

---

### `reindex_new_chunks.sql`

**Ce qu'il fait :**
1. ✅ Analyse santé actuelle des index
2. ✅ Décide si rebuild est nécessaire (heuristiques)
3. ✅ Rebuild avec `REINDEX CONCURRENTLY` (pas de downtime)
4. ✅ VACUUM ANALYZE

**Quand l'utiliser :**
- Après import de >100K nouveaux chunks
- Si performance de recherche dégradée (>100ms)
- Après beaucoup de DELETE
- Maintenance mensuelle

**Différence CONCURRENTLY vs normal :**
- `REINDEX CONCURRENTLY` : API continue de fonctionner (2x plus lent)
- `REINDEX` normal : API bloquée pendant rebuild (2x plus rapide)

---

### `clone_railway_db_to_local.sh`

**Ce qu'il fait :**
1. ✅ Vérifie PostgreSQL local installé
2. ✅ Dump Railway avec `pg_dump` (schema + data)
3. ✅ Crée base locale `legislateur_local`
4. ✅ Restore dump dans base locale
5. ✅ Génère `.env.local` avec URL locale
6. ✅ Vérifie row counts

**Prérequis :**
- PostgreSQL installé : `brew install postgresql@16`
- Service démarré : `brew services start postgresql@16`
- Railway DATABASE_URL dans `.env`

**Fichiers générés :**
- `data/railway_dump_YYYYMMDD_HHMMSS.sql` : Dump SQL complet
- `.env.local` : Variables d'environnement pour tests locaux

---

### `test_pgvector_performance.py`

**Ce qu'il fait :**
1. ✅ Charge 5 requêtes de test juridiques
2. ✅ Génère embeddings pour chaque requête
3. ✅ Lance 3 recherches par requête (warm-up + 2 mesures)
4. ✅ Calcule statistiques (avg, min, max, median)
5. ✅ Donne verdict et recommandations

**Résultat attendu :**
```
Overall Performance Summary
====================================================================

Search Performance:
  Average:   42.15ms
  Median:    40.32ms
  Min:       35.89ms
  Max:       48.72ms

Performance Verdict:
  ✓ EXCELLENT: pgvector is highly optimized (42ms avg)
    Perfect for production use with sub-50ms latency
```

**Recommandations selon performance :**
- **< 50ms** : Excellent, production-ready
- **50-100ms** : Bon, possibilité de tuning (ef_search)
- **100-200ms** : Acceptable, considérer reindex
- **> 200ms** : Problème, vérifier si index utilisé

---

## ❓ FAQ

### Q : Dans quel ordre exécuter les scripts ?

**R :** Pour migration complète :
1. `clone_railway_db_to_local.sh` (tests locaux)
2. `migrate_to_pgvector.sql` (local)
3. `monitor_migration.sql` (pendant migration local)
4. `test_pgvector_performance.py` (validation local)
5. `migrate_to_pgvector.sql` (Railway prod)
6. `test_pgvector_performance.py` (validation prod)

### Q : Peut-on interrompre une migration en cours ?

**R :** Oui, Ctrl+C dans le terminal qui exécute le SQL. PostgreSQL rollback automatiquement la transaction en cours.

### Q : Combien d'espace disque requis ?

**R :** Pour 1.6M chunks (384 dims) :
- Données : ~2.4 GB
- Index HNSW : ~1.2 GB (intervention) + ~50 MB (amendement)
- Dump SQL : ~3 GB (compressé : ~500 MB)
- **Total local : ~7-8 GB**

### Q : Les scripts sont-ils idempotents ?

**R :** Oui, tous les scripts peuvent être relancés sans danger :
- `migrate_to_pgvector.sql` : Vérifie si déjà migré avant d'agir
- `reindex_new_chunks.sql` : Rebuild seulement si nécessaire
- `clone_railway_db_to_local.sh` : Demande confirmation avant drop

### Q : Que faire si migration échoue ?

**R :** Rollback automatique pour :
- `migrate_to_pgvector.sql` : Transaction rollback si erreur
- `reindex_new_chunks.sql` : Ancien index reste valide avec CONCURRENTLY

Rollback manuel :
```sql
-- Voir section "ROLLBACK PROCEDURE" dans migrate_to_pgvector.sql
```

---

## 🆘 Support

### Problèmes courants

**1. "extension vector not found"**

```sql
-- Sur Railway, l'extension est déjà disponible
CREATE EXTENSION IF NOT EXISTS vector;
```

**2. Index build très lent (>4h)**

```sql
-- Vérifier work_mem
SHOW maintenance_work_mem;
-- Augmenter si < 1GB
SET maintenance_work_mem = '2GB';
```

**3. Out of memory**

→ Augmenter RAM Railway temporairement (8GB → 16GB)
→ Ou build en local puis dump/restore sur Railway

**4. Index non utilisé par les requêtes**

```sql
-- Vérifier avec EXPLAIN
EXPLAIN ANALYZE SELECT ... ORDER BY embedding <=> query;
-- Si "Seq Scan" au lieu de "Index Scan", forcer :
SET enable_seqscan = off;
```

---

## 📚 Ressources

- Documentation complète : `/docs/`
- pgvector GitHub : https://github.com/pgvector/pgvector
- Railway pgvector : https://blog.railway.com/p/hosting-postgres-with-pgvector
- PostgreSQL HNSW : https://www.crunchydata.com/blog/hnsw-indexes-with-postgres-and-pgvector
