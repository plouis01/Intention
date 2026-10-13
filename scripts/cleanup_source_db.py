#!/usr/bin/env python3
"""
Nettoyer la DB source pour réduire la consommation mémoire.
La DB pgvector contient maintenant toutes les données nécessaires.

Usage:
    export SOURCE_DATABASE_URL="postgresql://..."
    python scripts/cleanup_source_db.py [--option OPTION] [--dry-run]

Options:
    1. drop-embeddings-table    : DROP chunk_embeddings (22 GB → 3.8 GB)
    2. drop-embedding-column    : DROP column embedding (22 GB → 11 GB)
    3. vacuum-full             : VACUUM FULL pour récupérer l'espace
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

def analyze_current_state(cur):
    """Analyse l'état actuel de la DB."""
    print("="*70)
    print("ÉTAT ACTUEL DE LA BASE")
    print("="*70)

    # Taille totale
    cur.execute("SELECT pg_database_size(current_database())")
    total_size = cur.fetchone()[0]
    print(f"\n📊 Taille totale: {total_size / 1024**3:.2f} GB")

    # Taille par table
    print(f"\n📋 Taille par table:")
    cur.execute("""
        SELECT
            tablename,
            pg_total_relation_size(schemaname||'.'||tablename) as size_bytes
        FROM pg_tables
        WHERE schemaname = 'public'
        ORDER BY pg_total_relation_size(schemaname||'.'||tablename) DESC
    """)

    for table_name, size_bytes in cur.fetchall():
        print(f"   {table_name:30} {size_bytes / 1024**3:>8.2f} GB")

    # Chunks count
    try:
        cur.execute("SELECT COUNT(*) FROM chunk_embeddings")
        chunks = cur.fetchone()[0]
        print(f"\n📦 chunk_embeddings: {chunks:,} rows")
    except:
        print(f"\n⚠️  chunk_embeddings: table n'existe pas")

    return total_size

def option1_drop_table(cur, dry_run=False):
    """Option 1: Supprimer la table chunk_embeddings complètement."""
    print("\n" + "="*70)
    print("OPTION 1: SUPPRIMER LA TABLE chunk_embeddings")
    print("="*70)

    # Check if table exists
    cur.execute("""
        SELECT EXISTS (
            SELECT FROM pg_tables
            WHERE schemaname = 'public'
            AND tablename = 'chunk_embeddings'
        )
    """)
    exists = cur.fetchone()[0]

    if not exists:
        print("✅ Table chunk_embeddings déjà supprimée")
        return

    # Get table size
    cur.execute("""
        SELECT pg_total_relation_size('chunk_embeddings')
    """)
    table_size = cur.fetchone()[0]

    print(f"\n📊 Taille chunk_embeddings: {table_size / 1024**3:.2f} GB")
    print(f"📉 Espace à libérer: {table_size / 1024**3:.2f} GB")

    if dry_run:
        print("\n🔍 DRY RUN - Aucune modification")
        print("\nCommandes qui seraient exécutées:")
        print("   DROP TABLE chunk_embeddings CASCADE;")
        print("   VACUUM FULL;")
        return

    print("\n⚠️  ATTENTION: Cette action est IRRÉVERSIBLE")
    print("   - Supprime TOUTES les embeddings")
    print("   - Garde les données sources (interventions, amendements, etc.)")
    print("   - Peut être régénéré (mais prend ~2h)")
    print()

    confirm = input("Confirmer la suppression (tapez 'DELETE' en majuscules): ")
    if confirm != "DELETE":
        print("❌ Annulé")
        return

    print("\n🗑️  Suppression de chunk_embeddings...")
    cur.execute("DROP TABLE chunk_embeddings CASCADE")
    print("✅ Table supprimée")

    print("\n🧹 VACUUM FULL pour récupérer l'espace...")
    cur.execute("VACUUM FULL")
    print("✅ VACUUM terminé")

def option2_drop_column(cur, dry_run=False):
    """Option 2: Supprimer la colonne embedding (JSON)."""
    print("\n" + "="*70)
    print("OPTION 2: SUPPRIMER LA COLONNE embedding (JSON)")
    print("="*70)

    # Check if column exists
    cur.execute("""
        SELECT EXISTS (
            SELECT FROM information_schema.columns
            WHERE table_schema = 'public'
            AND table_name = 'chunk_embeddings'
            AND column_name = 'embedding'
        )
    """)
    exists = cur.fetchone()[0]

    if not exists:
        print("✅ Colonne 'embedding' déjà supprimée")
        return

    # Estimate size (rough: ~4KB per row for JSON embedding)
    cur.execute("SELECT COUNT(*) FROM chunk_embeddings")
    row_count = cur.fetchone()[0]
    estimated_size_gb = (row_count * 4 * 1024) / (1024**3)

    print(f"\n📊 Rows: {row_count:,}")
    print(f"📊 Taille estimée colonne 'embedding': ~{estimated_size_gb:.2f} GB")
    print(f"📉 Espace à libérer: ~{estimated_size_gb:.2f} GB")

    if dry_run:
        print("\n🔍 DRY RUN - Aucune modification")
        print("\nCommandes qui seraient exécutées:")
        print("   ALTER TABLE chunk_embeddings DROP COLUMN embedding;")
        print("   VACUUM FULL;")
        return

    print("\n⚠️  ATTENTION:")
    print("   - Supprime la colonne embedding (JSON)")
    print("   - Garde embedding_vector (si elle existe)")
    print("   - Empêche d'ajouter de nouveaux chunks via pipeline")
    print()

    confirm = input("Confirmer la suppression (tapez 'DELETE' en majuscules): ")
    if confirm != "DELETE":
        print("❌ Annulé")
        return

    print("\n🗑️  Suppression de la colonne 'embedding'...")
    cur.execute("ALTER TABLE chunk_embeddings DROP COLUMN embedding")
    print("✅ Colonne supprimée")

    print("\n🧹 VACUUM FULL pour récupérer l'espace...")
    cur.execute("VACUUM FULL")
    print("✅ VACUUM terminé")

def option3_vacuum_only(cur, dry_run=False):
    """Option 3: VACUUM FULL seulement."""
    print("\n" + "="*70)
    print("OPTION 3: VACUUM FULL (récupérer l'espace)")
    print("="*70)

    print("\n📊 Cette opération va:")
    print("   - Réorganiser les tables")
    print("   - Récupérer l'espace des rows supprimées")
    print("   - Réduire la fragmentation")
    print("\n⏱️  Durée estimée: 5-10 minutes")

    if dry_run:
        print("\n🔍 DRY RUN - Aucune modification")
        print("\nCommandes qui seraient exécutées:")
        print("   VACUUM FULL;")
        return

    print("\n🧹 Lancement de VACUUM FULL...")
    cur.execute("VACUUM FULL")
    print("✅ VACUUM terminé")

def main():
    source_url = os.getenv("SOURCE_DATABASE_URL")
    if not source_url:
        print("❌ ERROR: Set SOURCE_DATABASE_URL")
        sys.exit(1)

    dry_run = "--dry-run" in sys.argv
    option = None

    # Parse option
    if "--option" in sys.argv:
        idx = sys.argv.index("--option")
        if idx + 1 < len(sys.argv):
            option = sys.argv[idx + 1]

    mode = "DRY RUN" if dry_run else "LIVE"
    print(f"MODE: {mode}")
    print()

    source_params = parse_db_url(source_url)

    try:
        conn = psycopg2.connect(**source_params)
        conn.set_session(autocommit=True)
        cur = conn.cursor()

        # Analyze current state
        initial_size = analyze_current_state(cur)

        # Execute option
        if option == "1" or option == "drop-embeddings-table":
            option1_drop_table(cur, dry_run)
        elif option == "2" or option == "drop-embedding-column":
            option2_drop_column(cur, dry_run)
        elif option == "3" or option == "vacuum-full":
            option3_vacuum_only(cur, dry_run)
        else:
            print("\n" + "="*70)
            print("OPTIONS DISPONIBLES")
            print("="*70)
            print()
            print("1. drop-embeddings-table   : Supprimer chunk_embeddings (22 GB → 3.8 GB)")
            print("2. drop-embedding-column   : Supprimer colonne embedding (22 GB → 11 GB)")
            print("3. vacuum-full            : VACUUM FULL seulement")
            print()
            print("Usage:")
            print("  python scripts/cleanup_source_db.py --option 1 [--dry-run]")
            sys.exit(0)

        # Show final state if not dry-run
        if not dry_run and option:
            print("\n" + "="*70)
            print("ÉTAT FINAL")
            print("="*70)

            cur.execute("SELECT pg_database_size(current_database())")
            final_size = cur.fetchone()[0]

            print(f"\n📊 Taille avant: {initial_size / 1024**3:.2f} GB")
            print(f"📊 Taille après: {final_size / 1024**3:.2f} GB")
            print(f"📉 Espace libéré: {(initial_size - final_size) / 1024**3:.2f} GB")
            print(f"📊 Réduction: {((initial_size - final_size) / initial_size) * 100:.1f}%")

        cur.close()
        conn.close()

    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
