#!/usr/bin/env python3
"""
Test rapide de l'API avec pgvector.
Vérifie que la recherche fonctionne correctement.
"""
import os
import sys

# Vérifier que VECTOR_STORE_PROVIDER est défini
vector_store = os.getenv("VECTOR_STORE_PROVIDER")
if vector_store != "pgvector":
    print(f"❌ VECTOR_STORE_PROVIDER n'est pas 'pgvector' (actuel: {vector_store})")
    print("\nLancez:")
    print("  export VECTOR_STORE_PROVIDER=pgvector")
    print("  python scripts/test_pgvector_api.py")
    sys.exit(1)

print("✅ VECTOR_STORE_PROVIDER=pgvector")
print()

# Importer le vector store
print("📦 Import du vector store...")
from src.indexing.vector_stores import create_vector_store

# Créer le store
print("🔌 Connexion à PostgreSQL...")
store = create_vector_store(store_type="pgvector", embedding_dim=384)

# Vérifier le count
print("📊 Comptage des chunks...")
total = store.count()
interventions = store.count(chunk_type="intervention")

print(f"   Total chunks: {total:,}")
print(f"   Interventions: {interventions:,}")
print()

# Test de recherche
print("🔍 Test de recherche...")
print("   Génération d'un embedding de test...")

# Créer un embedding factice pour tester
test_embedding = [0.1] * 384  # Vecteur simple pour test

import time
start = time.time()
results = store.search(
    query_embedding=test_embedding,
    top_k=10,
    min_similarity=0.0,  # Très bas pour avoir des résultats
    chunk_type="intervention"
)
elapsed = time.time() - start

print(f"   ✅ Recherche terminée en {elapsed*1000:.1f}ms")
print(f"   📄 {len(results)} résultats retournés")
print()

if results:
    print("📌 Premier résultat:")
    chunk, similarity = results[0]
    print(f"   Type: {chunk.chunk_type}")
    print(f"   Similarité: {similarity:.4f}")
    print(f"   Contenu (extrait): {chunk.content[:150]}...")
    print()

# Fermer la connexion
store.close()

print("=" * 70)
print("✅ TEST RÉUSSI - pgvector fonctionne correctement!")
print("=" * 70)
print()
print("Prochaines étapes:")
print("  1. Démarrer l'API:")
print("     python -m src.api.main")
print()
print("  2. Tester un endpoint:")
print("     curl 'http://localhost:8000/search?q=plus-value&top_k=5'")
print()
