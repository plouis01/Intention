#!/usr/bin/env python3
"""
Vérification de la qualité des résultats: JSON vs pgvector
- Compare les résultats retournés par les deux méthodes
- Vérifie qu'on ne perd pas en précision
- Analyse les différences de ranking et de similarité
"""
import os
import psycopg2
from psycopg2.extras import RealDictCursor
import numpy as np

# Connexion à la base locale
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    print("❌ DATABASE_URL non défini. Lancez: export $(grep -v '^#' .env.sample | xargs)")
    exit(1)

conn = psycopg2.connect(DATABASE_URL)

def get_random_query_embedding():
    """Récupère un embedding existant pour tester"""
    with conn.cursor() as cur:
        # Récupérer directement le vector pour éviter les problèmes d'encodage JSON
        cur.execute("SELECT id, embedding_vector, content FROM chunk_embeddings WHERE chunk_type = 'intervention' AND embedding_vector IS NOT NULL LIMIT 1")
        result = cur.fetchone()
        return {
            'id': result[0],
            'embedding_vector': result[1],  # Format vector natif
            'content': result[2][:200] if result[2] else ""  # Aperçu
        }

def search_with_json(query_vector_str, top_k=20):
    """Recherche avec calcul cosine sur embedding (JSON) en SQL"""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        # Utiliser le vector comme référence pour comparer avec embedding (JSON)
        cur.execute("""
            SELECT
                id,
                chunk_type,
                -- Calcul cosine similarity: embedding (JSON) vs query (vector)
                1 - (embedding::text::vector <=> %s::vector) as similarity
            FROM chunk_embeddings
            WHERE chunk_type = 'intervention'
              AND embedding IS NOT NULL
            ORDER BY similarity DESC
            LIMIT %s
        """, (query_vector_str, top_k))

        return cur.fetchall()

def search_with_pgvector(query_vector_str, top_k=20):
    """Recherche avec pgvector + index HNSW"""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SET hnsw.ef_search = 100")

        cur.execute("""
            SELECT
                id,
                chunk_type,
                1 - (embedding_vector <=> %s::vector) as similarity
            FROM chunk_embeddings
            WHERE chunk_type = 'intervention'
              AND embedding_vector IS NOT NULL
            ORDER BY embedding_vector <=> %s::vector
            LIMIT %s
        """, (query_vector_str, query_vector_str, top_k))

        return cur.fetchall()

def compare_results(json_results, pgvector_results):
    """Compare les résultats des deux méthodes"""

    # Extraire les IDs dans l'ordre
    json_ids = [r['id'] for r in json_results]
    pgvector_ids = [r['id'] for r in pgvector_results]

    # Calculer l'overlap
    json_set = set(json_ids)
    pgvector_set = set(pgvector_ids)

    common_ids = json_set & pgvector_set
    only_json = json_set - pgvector_set
    only_pgvector = pgvector_set - json_set

    overlap_percentage = (len(common_ids) / len(json_ids)) * 100

    # Calculer le Rank Correlation (Spearman) pour les IDs communs
    if len(common_ids) > 0:
        # Créer des dictionnaires de ranking
        json_ranks = {id: rank for rank, id in enumerate(json_ids)}
        pgvector_ranks = {id: rank for rank, id in enumerate(pgvector_ids)}

        # Pour les IDs communs, comparer les rangs
        rank_diffs = []
        for id in common_ids:
            rank_diffs.append(abs(json_ranks[id] - pgvector_ranks[id]))

        avg_rank_diff = np.mean(rank_diffs)
        max_rank_diff = max(rank_diffs)
    else:
        avg_rank_diff = None
        max_rank_diff = None

    # Comparer les similarités pour les IDs communs
    similarity_diffs = []
    for id in common_ids:
        json_sim = next(r['similarity'] for r in json_results if r['id'] == id)
        pgvector_sim = next(r['similarity'] for r in pgvector_results if r['id'] == id)
        similarity_diffs.append(abs(json_sim - pgvector_sim))

    avg_sim_diff = np.mean(similarity_diffs) if similarity_diffs else None
    max_sim_diff = max(similarity_diffs) if similarity_diffs else None

    return {
        'total_results': len(json_ids),
        'common_ids': len(common_ids),
        'only_json': len(only_json),
        'only_pgvector': len(only_pgvector),
        'overlap_percentage': overlap_percentage,
        'avg_rank_diff': avg_rank_diff,
        'max_rank_diff': max_rank_diff,
        'avg_sim_diff': avg_sim_diff,
        'max_sim_diff': max_sim_diff,
        'json_ids': json_ids,
        'pgvector_ids': pgvector_ids,
        'json_results': json_results,
        'pgvector_results': pgvector_results,
    }

def main():
    print("=" * 70)
    print("VÉRIFICATION QUALITÉ: JSON vs pgvector")
    print("=" * 70)
    print()

    # Test 1: Requête avec un embedding existant (devrait retourner lui-même en premier)
    print("🔍 Test 1: Auto-requête (embedding recherche lui-même)")
    print("-" * 70)
    query = get_random_query_embedding()
    print(f"Query ID: {query['id']}")
    print(f"Query content: {query['content']}...")
    print()

    # embedding_vector est déjà une string retournée par psycopg2
    query_vector_str = query['embedding_vector']

    print("⏱️  Recherche avec embedding (JSON) converti en vector...")
    json_results = search_with_json(query_vector_str, top_k=20)

    print("⚡ Recherche avec embedding_vector (pgvector natif)...")
    pgvector_results = search_with_pgvector(query_vector_str, top_k=20)

    print()
    print("📊 RÉSULTATS:")
    print()

    # Vérifier que l'ID de la requête est en première position
    json_first = json_results[0]['id'] if json_results else None
    pgvector_first = pgvector_results[0]['id'] if pgvector_results else None

    print(f"JSON - Premier résultat:      ID {json_first} (similarité: {json_results[0]['similarity']:.6f})")
    print(f"pgvector - Premier résultat:  ID {pgvector_first} (similarité: {pgvector_results[0]['similarity']:.6f})")
    print()

    if json_first == query['id']:
        print("✅ JSON: Retourne correctement l'embedding source en premier")
    else:
        print("❌ JSON: N'a PAS retourné l'embedding source en premier")

    if pgvector_first == query['id']:
        print("✅ pgvector: Retourne correctement l'embedding source en premier")
    else:
        print("❌ pgvector: N'a PAS retourné l'embedding source en premier")

    print()
    print("-" * 70)
    print()

    # Comparer les résultats
    comparison = compare_results(json_results, pgvector_results)

    print("📈 COMPARAISON DES RÉSULTATS (Top 20)")
    print("=" * 70)
    print(f"Résultats communs:        {comparison['common_ids']}/{comparison['total_results']} ({comparison['overlap_percentage']:.1f}%)")
    print(f"Uniquement JSON:          {comparison['only_json']}")
    print(f"Uniquement pgvector:      {comparison['only_pgvector']}")
    print()

    if comparison['avg_rank_diff'] is not None:
        print(f"Différence de rang moyenne:   {comparison['avg_rank_diff']:.2f} positions")
        print(f"Différence de rang max:       {comparison['max_rank_diff']} positions")
        print()

    if comparison['avg_sim_diff'] is not None:
        print(f"Différence de similarité moy: {comparison['avg_sim_diff']:.6f}")
        print(f"Différence de similarité max: {comparison['max_sim_diff']:.6f}")
        print()

    # Afficher le top 10 côte à côte
    print("=" * 70)
    print("TOP 10 - COMPARAISON DÉTAILLÉE")
    print("=" * 70)
    print()
    print(f"{'Rank':<6} {'JSON ID':<10} {'JSON Sim':<12} {'PGV ID':<10} {'PGV Sim':<12} {'Match':<6}")
    print("-" * 70)

    for i in range(min(10, len(json_results))):
        json_r = json_results[i]
        pgv_r = pgvector_results[i] if i < len(pgvector_results) else None

        match = "✅" if pgv_r and json_r['id'] == pgv_r['id'] else "❌"

        json_id = json_r['id']
        json_sim = json_r['similarity']

        if pgv_r:
            pgv_id = pgv_r['id']
            pgv_sim = pgv_r['similarity']
        else:
            pgv_id = "N/A"
            pgv_sim = 0.0

        print(f"{i+1:<6} {json_id:<10} {json_sim:<12.6f} {pgv_id:<10} {pgv_sim:<12.6f} {match:<6}")

    print()
    print("=" * 70)
    print("🎯 ANALYSE")
    print("=" * 70)

    # Critères de qualité
    quality_score = 0
    max_score = 4

    # 1. Auto-requête correcte
    if json_first == query['id'] and pgvector_first == query['id']:
        print("✅ Auto-requête: Les deux retournent l'embedding source en premier")
        quality_score += 1
    else:
        print("⚠️  Auto-requête: Problème détecté")

    # 2. Overlap élevé
    if comparison['overlap_percentage'] >= 90:
        print(f"✅ Overlap: {comparison['overlap_percentage']:.1f}% (excellent)")
        quality_score += 1
    elif comparison['overlap_percentage'] >= 70:
        print(f"⚠️  Overlap: {comparison['overlap_percentage']:.1f}% (acceptable)")
        quality_score += 0.5
    else:
        print(f"❌ Overlap: {comparison['overlap_percentage']:.1f}% (problématique)")

    # 3. Différence de rang faible
    if comparison['avg_rank_diff'] is not None:
        if comparison['avg_rank_diff'] <= 2:
            print(f"✅ Ranking: Différence moyenne {comparison['avg_rank_diff']:.2f} positions (excellent)")
            quality_score += 1
        elif comparison['avg_rank_diff'] <= 5:
            print(f"⚠️  Ranking: Différence moyenne {comparison['avg_rank_diff']:.2f} positions (acceptable)")
            quality_score += 0.5
        else:
            print(f"❌ Ranking: Différence moyenne {comparison['avg_rank_diff']:.2f} positions (problématique)")

    # 4. Différence de similarité faible
    if comparison['avg_sim_diff'] is not None:
        if comparison['avg_sim_diff'] <= 0.001:
            print(f"✅ Similarité: Différence moyenne {comparison['avg_sim_diff']:.6f} (excellent)")
            quality_score += 1
        elif comparison['avg_sim_diff'] <= 0.01:
            print(f"⚠️  Similarité: Différence moyenne {comparison['avg_sim_diff']:.6f} (acceptable)")
            quality_score += 0.5
        else:
            print(f"❌ Similarité: Différence moyenne {comparison['avg_sim_diff']:.6f} (attention)")

    print()
    print(f"Score de qualité: {quality_score}/{max_score}")
    print()

    if quality_score >= 3.5:
        print("✅ CONCLUSION: pgvector produit des résultats ÉQUIVALENTS à JSON")
        print("   Migration recommandée sans perte de qualité")
    elif quality_score >= 2.5:
        print("⚠️  CONCLUSION: pgvector produit des résultats SIMILAIRES à JSON")
        print("   Légères différences acceptables")
    else:
        print("❌ CONCLUSION: Différences significatives détectées")
        print("   Investigation nécessaire avant migration")

    print("=" * 70)

    conn.close()

if __name__ == "__main__":
    main()
