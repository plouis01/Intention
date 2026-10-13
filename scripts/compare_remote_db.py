#!/usr/bin/env python3
"""
Compare remote PostgreSQL database with local reference files.
"""

import json
import os
import psycopg2
from datetime import datetime
from collections import defaultdict

# Remote DB connection string
REMOTE_DB_URL = os.environ["DATABASE_URL"]

# Local reference files
LOCAL_CGI_FILE = "/Users/thib/Documents/sides/legal-project/data/lois_cgi_1958_2026.json"
LOCAL_FINANCES_FILE = "/Users/thib/Documents/sides/legal-project/data/lois_finances_2012_2025.json"

def load_local_laws():
    """Load and merge local law lists, filtering to LF/LFR/LFSS from 2002+"""
    cutoff_date = datetime(2002, 1, 1)

    # Load CGI laws
    with open(LOCAL_CGI_FILE, 'r', encoding='utf-8') as f:
        cgi_data = json.load(f)
        cgi_laws = cgi_data.get('lois', []) if isinstance(cgi_data, dict) else cgi_data

    # Load finances laws
    with open(LOCAL_FINANCES_FILE, 'r', encoding='utf-8') as f:
        finances_data = json.load(f)
        finances_laws = finances_data.get('lois', []) if isinstance(finances_data, dict) else finances_data

    # Filter and merge
    all_laws = {}
    cgi_count = 0
    finances_count = 0

    # First, add all laws from finances file (already filtered to LF/LFR/LFSS 2012-2025)
    for law in finances_laws:
        date_str = law.get('date_promulgation', '')
        if not date_str:
            continue

        try:
            law_date = datetime.fromisoformat(date_str.replace('Z', '+00:00')).replace(tzinfo=None)
        except:
            continue

        if law_date < cutoff_date:
            continue

        numero = law.get('numero')
        if numero:
            all_laws[numero] = law
            finances_count += 1

    # Then, add LF/LFR/LFSS from CGI file (back to 2002, but may include pre-2012)
    for law in cgi_laws:
        date_str = law.get('date_promulgation', '')
        if not date_str:
            continue

        try:
            law_date = datetime.fromisoformat(date_str.replace('Z', '+00:00')).replace(tzinfo=None)
        except:
            continue

        if law_date < cutoff_date:
            continue

        # Check if it's LF/LFR/LFSS by looking at 'type' field
        law_type = law.get('type', '').upper()

        is_finance_law = law_type in ['LF', 'LFR', 'LFSS']

        if is_finance_law:
            numero = law.get('numero')
            if numero and numero not in all_laws:  # Don't overwrite finances_laws data
                all_laws[numero] = law
                cgi_count += 1

    print(f"  - From finances file (2012-2025): {finances_count} laws")
    print(f"  - From CGI file (2002-2011, LF/LFR/LFSS): {cgi_count} laws")
    print(f"  - Total merged: {len(all_laws)} laws")

    return all_laws

def query_remote_db():
    """Query remote PostgreSQL database"""
    conn = psycopg2.connect(REMOTE_DB_URL)
    cur = conn.cursor()

    # First, check what tables exist
    cur.execute("""
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
        ORDER BY table_name
    """)
    tables = [row[0] for row in cur.fetchall()]
    print(f"  - Available tables: {', '.join(tables)}")

    if not tables:
        print("  ⚠ No tables found in database!")
        cur.close()
        conn.close()
        return {
            'loi_count': 0,
            'lois': [],
            'dossier_count': 0,
            'article_count': 0,
            'debat_count': 0,
            'amendement_count': 0,
            'intervention_count': 0,
            'tables': []
        }

    # Count of Loi records
    cur.execute("SELECT COUNT(*) FROM lois")
    loi_count = cur.fetchone()[0]

    # List of all numero values in Loi table (sorted by date_promulgation)
    cur.execute("""
        SELECT numero, titre, date_promulgation, type
        FROM lois
        ORDER BY date_promulgation
    """)
    lois = cur.fetchall()

    # Count of DossierLegislatif records
    cur.execute("SELECT COUNT(*) FROM dossiers_legislatifs")
    dossier_count = cur.fetchone()[0]

    # Count of Article records
    cur.execute("SELECT COUNT(*) FROM articles")
    article_count = cur.fetchone()[0]

    # Count of Debat records
    cur.execute("SELECT COUNT(*) FROM debats")
    debat_count = cur.fetchone()[0]

    # Count of Amendement records
    cur.execute("SELECT COUNT(*) FROM amendements")
    amendement_count = cur.fetchone()[0]

    # Count of Intervention records
    cur.execute("SELECT COUNT(*) FROM interventions")
    intervention_count = cur.fetchone()[0]

    cur.close()
    conn.close()

    return {
        'loi_count': loi_count,
        'lois': lois,
        'dossier_count': dossier_count,
        'article_count': article_count,
        'debat_count': debat_count,
        'amendement_count': amendement_count,
        'intervention_count': intervention_count,
        'tables': tables
    }

def main():
    print("=" * 80)
    print("REMOTE DATABASE vs LOCAL REFERENCE FILES COMPARISON")
    print("=" * 80)
    print()

    # Load local laws
    print("Loading local reference files...")
    local_laws = load_local_laws()
    local_numeros = set(local_laws.keys())
    print(f"  - Found {len(local_laws)} laws in local files (LF/LFR/LFSS from 2002+)")
    print()

    # Query remote DB
    print("Querying remote PostgreSQL database...")
    remote_data = query_remote_db()
    print(f"  - Loi records: {remote_data['loi_count']}")
    print(f"  - DossierLegislatif records: {remote_data['dossier_count']}")
    print(f"  - Article records: {remote_data['article_count']}")
    print(f"  - Debat records: {remote_data['debat_count']}")
    print(f"  - Amendement records: {remote_data['amendement_count']}")
    print(f"  - Intervention records: {remote_data['intervention_count']}")
    print()

    # Build remote numeros set
    remote_numeros = set()
    remote_laws_dict = {}
    for numero, titre, date_prom, law_type in remote_data['lois']:
        remote_numeros.add(numero)
        remote_laws_dict[numero] = {
            'numero': numero,
            'titre': titre,
            'date_promulgation': date_prom,
            'type': law_type
        }

    # Comparison
    print("=" * 80)
    print("COMPARISON RESULTS")
    print("=" * 80)
    print()

    # Laws in both
    in_both = local_numeros & remote_numeros
    print(f"1. Laws in BOTH local and remote ({len(in_both)} laws):")
    if in_both:
        for numero in sorted(in_both):
            local_law = local_laws[numero]
            print(f"   ✓ {numero} - {local_law.get('titre', '')[:80]}...")
    else:
        print("   (none)")
    print()

    # Laws in local but NOT in remote
    in_local_not_remote = local_numeros - remote_numeros
    print(f"2. Laws in LOCAL but NOT in remote ({len(in_local_not_remote)} laws):")
    if in_local_not_remote:
        for numero in sorted(in_local_not_remote):
            local_law = local_laws[numero]
            date_prom = local_law.get('date_promulgation', 'N/A')
            print(f"   ✗ {numero} ({date_prom[:10]}) - {local_law.get('titre', '')[:70]}...")
    else:
        print("   (none)")
    print()

    # Laws in remote but NOT in local
    in_remote_not_local = remote_numeros - local_numeros
    print(f"3. Laws in REMOTE but NOT in local ({len(in_remote_not_local)} laws):")
    if in_remote_not_local:
        for numero in sorted(in_remote_not_local):
            remote_law = remote_laws_dict[numero]
            date_prom = remote_law.get('date_promulgation')
            date_str = date_prom.strftime('%Y-%m-%d') if date_prom else 'N/A'
            print(f"   + {numero} ({date_str}) - {remote_law.get('titre', '')[:70]}...")
    else:
        print("   (none)")
    print()

    # Summary
    print("=" * 80)
    print("IMPORT ANALYSIS")
    print("=" * 80)
    print()

    if len(in_both) == len(local_numeros):
        print("✓ ALL local laws are already in remote DB")
        print("  → Running --import WITHOUT --reset would SKIP all laws (already exist)")
        print("  → Running --import WITH --reset would RE-IMPORT everything fresh")
    elif len(in_both) == 0:
        print("✗ NONE of the local laws are in remote DB")
        print("  → Running --import WITHOUT --reset would IMPORT all laws fresh")
        print("  → Running --import WITH --reset would IMPORT all laws fresh")
    else:
        print(f"⚠ PARTIAL overlap: {len(in_both)}/{len(local_numeros)} laws already in remote")
        print(f"  → Running --import WITHOUT --reset would:")
        print(f"    - SKIP {len(in_both)} existing laws (2012-2025)")
        print(f"    - IMPORT {len(in_local_not_remote)} new laws (2002-2011 + 2024 fin de gestion)")
        print(f"  → Running --import WITH --reset would RE-IMPORT all {len(local_numeros)} laws fresh")
    print()

    if in_remote_not_local:
        print(f"⚠ Remote DB has {len(in_remote_not_local)} laws NOT in local reference")
        print("  → These would be preserved during --import (unless --reset is used)")

    print()
    print(f"Expected local law count: {len(local_laws)}")
    print(f"Actual remote law count: {remote_data['loi_count']}")
    print()

    # Analyze the missing laws by year
    missing_by_year = defaultdict(list)
    for numero in sorted(in_local_not_remote):
        year = numero.split('-')[0]
        missing_by_year[year].append(numero)

    print("Missing laws by year:")
    for year in sorted(missing_by_year.keys()):
        laws = missing_by_year[year]
        print(f"  {year}: {len(laws)} laws - {', '.join(laws)}")
    print()

if __name__ == '__main__':
    main()
