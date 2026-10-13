#!/usr/bin/env python3
"""
Optimiser la DB source pour réduire la consommation mémoire SANS supprimer de données.

Usage:
    export SOURCE_DATABASE_URL="postgresql://..."
    python scripts/optimize_source_db.py [--dry-run]
"""

import os
import sys
import psycopg2
from urllib.parse import urlparse

def parse_db_url(url):
    parsed = urlparse(url)
    return {
        'host': parsed.hostname,
        'port': parsed.port or 5432,
        'database': parsed.path[1:],
        'user': parsed.username,
        'password': parsed.password
    }

def analyze_db(cur):
    """Analyse la DB pour identifier les optimisations possibles."""
    print("="*70)
    print("ANALYSE DE LA BASE DE DONNÉES")
    print("="*70)

    # Taille totale
    cur.execute("SELECT pg_database_size(current_database())")
    total_size = cur.fetchone()[0]
    print(f"\n📊 Taille totale: {total_size / 1024**3:.2f} GB")

    # Taille par table
    print(f"\n📋 Taille détaillée par table:")
    cur.execute("""
        SELECT
            tablename,
            pg_total_relation_size(schemaname||'.'||tablename) as total_size,
            pg_relation_size(schemaname||'.'||tablename) as table_size,
            pg_total_relation_size(schemaname||'.'||tablename) - pg_relation_size(schemaname||'.'||tablename) as index_size
        FROM pg_tables
        WHERE schemaname = 'public'
        ORDER BY pg_total_relation_size(schemaname||'.'||tablename) DESC
    """)

    for table, total, data, indexes in cur.fetchall():
        print(f"\n   {table}:")
        print(f"      Total:   {total / 1024**3:>8.2f} GB")
        print(f"      Data:    {data / 1024**3:>8.2f} GB")
        print(f"      Indexes: {indexes / 1024**3:>8.2f} GB")

    # Dead tuples (espace récupérable avec VACUUM)
    print(f"\n🗑️  Dead tuples (espace récupérable):")
    cur.execute("""
        SELECT
            schemaname || '.' || relname as table_name,
            n_dead_tup,
            pg_size_pretty(pg_relation_size(schemaname||'.'||relname)) as table_size,
            ROUND(100.0 * n_dead_tup / NULLIF(n_live_tup + n_dead_tup, 0), 2) as dead_ratio
        FROM pg_stat_user_tables
        WHERE n_dead_tup > 0
        ORDER BY n_dead_tup DESC
    """)

    dead_tuples_found = False
    for table, dead, size, ratio in cur.fetchall():
        if not dead_tuples_found:
            dead_tuples_found = True
        print(f"   {table:40} {dead:>10,} dead ({ratio:>5.1f}%) - {size}")

    if not dead_tuples_found:
        print("   ✅ Aucun dead tuple (DB déjà optimisée)")

    # Bloat (fragmentation)
    print(f"\n📊 Fragmentation estimée:")
    try:
        cur.execute("""
            SELECT
                schemaname || '.' || tablename as table_name,
                pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename)) as size,
                ROUND(100.0 * (pg_total_relation_size(schemaname||'.'||tablename)::float8 /
                    NULLIF(pg_relation_size(schemaname||'.'||tablename), 0) - 1.0), 2) as overhead_ratio
            FROM pg_tables
            WHERE schemaname = 'public'
            AND pg_relation_size(schemaname||'.'||tablename) > 0
            ORDER BY pg_total_relation_size(schemaname||'.'||tablename) DESC
            LIMIT 5
        """)

        for table, size, overhead in cur.fetchall():
            if overhead and overhead > 50:
                print(f"   {table:40} {size:>10} (overhead: {overhead:.1f}%)")
    except Exception as e:
        print(f"   ⚠️  Impossible de calculer la fragmentation: {e}")

    return total_size

def optimize_vacuum(cur, dry_run=False):
    """VACUUM et ANALYZE pour récupérer l'espace et optimiser les stats."""
    print("\n" + "="*70)
    print("OPTIMISATION 1: VACUUM & ANALYZE")
    print("="*70)

    print("\n📋 Actions:")
    print("   - VACUUM: Récupère l'espace des rows supprimées")
    print("   - ANALYZE: Met à jour les statistiques pour le query planner")
    print("   - Ne supprime AUCUNE donnée")
    print("   - Améliore les performances")

    if dry_run:
        print("\n🔍 DRY RUN - Aucune modification")
        print("\nCommandes qui seraient exécutées:")
        print("   VACUUM ANALYZE;")
        return

    print("\n🧹 Exécution de VACUUM ANALYZE...")
    print("   (peut prendre 5-10 minutes)")

    try:
        cur.execute("VACUUM ANALYZE")
        print("✅ VACUUM ANALYZE terminé")
    except Exception as e:
        print(f"❌ Erreur: {e}")

def optimize_reindex(cur, dry_run=False):
    """REINDEX pour réorganiser les indexes fragmentés."""
    print("\n" + "="*70)
    print("OPTIMISATION 2: REINDEX (réorganiser les indexes)")
    print("="*70)

    # Get index sizes
    cur.execute("""
        SELECT
            schemaname || '.' || indexrelname as index_name,
            pg_size_pretty(pg_relation_size(indexrelid)) as size,
            pg_relation_size(indexrelid) as size_bytes
        FROM pg_stat_user_indexes
        WHERE schemaname = 'public'
        ORDER BY pg_relation_size(indexrelid) DESC
        LIMIT 10
    """)

    indexes = cur.fetchall()

    print("\n📊 Top 10 indexes:")
    for idx, size, _ in indexes:
        print(f"   {idx:50} {size:>10}")

    if dry_run:
        print("\n🔍 DRY RUN - Aucune modification")
        print("\nCommandes qui seraient exécutées:")
        print("   REINDEX DATABASE;")
        return

    print("\n⚠️  REINDEX peut prendre du temps (10-20 minutes)")
    print("   et verrouille les indexes pendant la reconstruction")

    confirm = input("\nContinuer? (yes/no): ")
    if confirm.lower() != "yes":
        print("❌ Annulé")
        return

    print("\n🔧 Exécution de REINDEX...")
    try:
        # REINDEX CONCURRENTLY serait mieux mais nécessite PostgreSQL 12+
        cur.execute("REINDEX DATABASE CONCURRENTLY")
        print("✅ REINDEX terminé")
    except Exception as e:
        print(f"⚠️  REINDEX CONCURRENTLY non supporté, tentative avec REINDEX standard...")
        try:
            cur.execute("REINDEX DATABASE")
            print("✅ REINDEX terminé")
        except Exception as e2:
            print(f"❌ Erreur: {e2}")

def optimize_change_column_type(cur, dry_run=False):
    """
    Changer le type de la colonne 'embedding' de JSON vers JSONB.
    JSONB est plus compact et plus performant que JSON.
    """
    print("\n" + "="*70)
    print("OPTIMISATION 3: JSON → JSONB (plus compact)")
    print("="*70)

    # Check current type
    cur.execute("""
        SELECT data_type
        FROM information_schema.columns
        WHERE table_name = 'chunk_embeddings'
        AND column_name = 'embedding'
    """)
    result = cur.fetchone()

    if not result:
        print("⚠️  Colonne 'embedding' non trouvée")
        return

    current_type = result[0]
    print(f"\n📊 Type actuel: {current_type}")

    if current_type == 'jsonb':
        print("✅ Déjà en JSONB (optimal)")
        return

    # Estimate current size
    cur.execute("SELECT COUNT(*) FROM chunk_embeddings")
    row_count = cur.fetchone()[0]

    print(f"📊 Rows: {row_count:,}")
    print(f"\n💡 JSONB est généralement 10-30% plus compact que JSON")
    print(f"   Réduction estimée: 2-6 GB")

    if dry_run:
        print("\n🔍 DRY RUN - Aucune modification")
        print("\nCommandes qui seraient exécutées:")
        print("   ALTER TABLE chunk_embeddings")
        print("   ALTER COLUMN embedding TYPE jsonb USING embedding::jsonb;")
        return

    print("\n⚠️  Cette opération:")
    print("   - Réécrit toute la table")
    print("   - Peut prendre 20-30 minutes")
    print("   - Verrouille la table en écriture")
    print("   - Réduit la taille de 10-30%")

    confirm = input("\nContinuer? (yes/no): ")
    if confirm.lower() != "yes":
        print("❌ Annulé")
        return

    print("\n🔧 Conversion JSON → JSONB...")
    print("   (peut prendre 20-30 minutes)")

    try:
        cur.execute("""
            ALTER TABLE chunk_embeddings
            ALTER COLUMN embedding TYPE jsonb USING embedding::jsonb
        """)
        print("✅ Conversion terminée")
    except Exception as e:
        print(f"❌ Erreur: {e}")

def main():
    source_url = os.getenv("SOURCE_DATABASE_URL")
    if not source_url:
        print("❌ ERROR: Set SOURCE_DATABASE_URL")
        sys.exit(1)

    dry_run = "--dry-run" in sys.argv

    mode = "DRY RUN" if dry_run else "LIVE"
    print(f"MODE: {mode}")
    print()

    source_params = parse_db_url(source_url)

    try:
        conn = psycopg2.connect(**source_params)
        conn.set_session(autocommit=True)
        cur = conn.cursor()

        # Analyze current state
        initial_size = analyze_db(cur)

        # Run optimizations
        optimize_vacuum(cur, dry_run)

        # Only ask for REINDEX if not dry-run
        if not dry_run:
            optimize_reindex(cur, dry_run)
            optimize_change_column_type(cur, dry_run)
        else:
            print("\n" + "="*70)
            print("DRY RUN - Autres optimisations disponibles:")
            print("="*70)
            print("\n2. REINDEX: Réorganiser les indexes")
            print("3. JSON → JSONB: Conversion pour réduire la taille")
            print("\nRelancez sans --dry-run pour appliquer toutes les optimisations")

        # Show final state if not dry-run
        if not dry_run:
            print("\n" + "="*70)
            print("ÉTAT FINAL")
            print("="*70)

            cur.execute("SELECT pg_database_size(current_database())")
            final_size = cur.fetchone()[0]

            print(f"\n📊 Taille avant: {initial_size / 1024**3:.2f} GB")
            print(f"📊 Taille après: {final_size / 1024**3:.2f} GB")

            if final_size < initial_size:
                print(f"📉 Espace libéré: {(initial_size - final_size) / 1024**3:.2f} GB")
                print(f"📊 Réduction: {((initial_size - final_size) / initial_size) * 100:.1f}%")
            else:
                print("⚠️  Pas de réduction significative")
                print("   (Normal si la DB était déjà optimisée)")

        cur.close()
        conn.close()

    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
