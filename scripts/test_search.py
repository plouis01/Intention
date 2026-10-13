#!/usr/bin/env python3
"""
Test script for the search pipeline (Phase 2).

Requires OPENAI_API_KEY and ANTHROPIC_API_KEY to be configured in .env

Usage:
    python scripts/test_search.py
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.db.models import Article, Debat, Intervention, get_session, init_db
from src.indexing.chunking import Chunk, chunk_by_intervention
from src.indexing.embeddings import EmbeddingService, VectorStore
from src.indexing.search import SearchService, format_search_result_markdown
from src.parsers.debates import Intervention as ParsedIntervention, ParsedDebat
from src.utils.config import get_settings


def check_api_keys():
    """Check if required API keys are configured."""
    settings = get_settings()

    print(f"Embedding provider: {settings.embedding_provider}")
    print(f"LLM provider: {settings.llm_provider}")

    # Check embedding provider
    if settings.embedding_provider == "openai" and not settings.openai_api_key:
        print("ERROR: OPENAI_API_KEY not configured but EMBEDDING_PROVIDER=openai")
        print("Either set OPENAI_API_KEY or use EMBEDDING_PROVIDER=local (free)")
        return False

    # Check LLM provider
    if settings.llm_provider == "groq" and not settings.groq_api_key:
        print("WARNING: GROQ_API_KEY not configured")
        print("Intent extraction requires a Groq API key (free at https://console.groq.com/)")
    elif settings.llm_provider == "anthropic" and not settings.anthropic_api_key:
        print("WARNING: ANTHROPIC_API_KEY not configured")
        print("Intent extraction requires an Anthropic API key")

    return True


def test_embedding_service():
    """Test the embedding service with a simple query."""
    print("\n1. Testing Embedding Service...")

    service = EmbeddingService()

    # Test with a simple text
    test_text = "L'article 12 de la loi de finances prévoit une modification du taux de TVA."

    try:
        embedding = service.generate_embedding(test_text)
        print(f"   Generated embedding with {len(embedding)} dimensions")
        print(f"   First 5 values: {embedding[:5]}")
        return True
    except Exception as e:
        print(f"   ERROR: {e}")
        return False


def test_vector_store():
    """Test the vector store with sample data."""
    print("\n2. Testing Vector Store...")

    init_db()
    session = get_session()

    # Get sample interventions
    interventions = session.query(Intervention).limit(10).all()

    if not interventions:
        print("   ERROR: No interventions found in database")
        return False

    print(f"   Found {len(interventions)} sample interventions")

    # Create chunks
    chunks = []
    for interv in interventions:
        if len(interv.contenu) < 50:
            continue

        debat = interv.debat
        chunk = Chunk(
            content=f"{interv.orateur}: {interv.contenu[:500]}",
            metadata={
                "orateur": interv.orateur,
                "fonction": interv.fonction,
                "chambre": debat.chambre if debat else "AN",
            },
            chunk_type="intervention",
            source_id=str(interv.debat_id),
            position=interv.ordre or 0,
        )
        chunks.append(chunk)

    if not chunks:
        print("   ERROR: No valid chunks created")
        return False

    print(f"   Created {len(chunks)} chunks")

    # Generate embeddings
    service = EmbeddingService()
    store = VectorStore()

    try:
        for chunk in chunks[:5]:  # Only test with 5 to save API calls
            embedding = service.generate_embedding(chunk.content)
            store.add(chunk, embedding)

        print(f"   Added {len(store)} items to vector store")

        # Test search
        query = "article sur la TVA"
        query_embedding = service.generate_embedding(query)
        results = store.search(query_embedding, top_k=3)

        print(f"   Search returned {len(results)} results")
        if results:
            chunk, score = results[0]
            print(f"   Top result (score={score:.3f}): {chunk.content[:100]}...")

        return True
    except Exception as e:
        print(f"   ERROR: {e}")
        return False
    finally:
        session.close()


def test_full_search():
    """Test the full search pipeline with an article."""
    print("\n3. Testing Full Search Pipeline...")

    init_db()
    session = get_session()

    # Get a sample article
    article = session.query(Article).first()

    if not article:
        print("   ERROR: No articles found in database")
        return False

    print(f"   Testing with article {article.numero_article}")
    print(f"   Content preview: {article.contenu_texte[:200]}...")

    # Load vector stores using configured provider
    search_service = SearchService.create_configured()

    try:
        # Perform search
        result = search_service.search_article(
            article_numero=article.numero_article,
            article_contenu=article.contenu_texte,
            top_k_debates=5,
            top_k_amendments=3,
            extract_intent=True,  # This requires ANTHROPIC_API_KEY
        )

        print(f"\n   Found {len(result.relevant_debates)} relevant debates")
        print(f"   Found {len(result.relevant_amendments)} relevant amendments")

        if result.intent:
            print(f"\n   Intent extracted:")
            print(f"   - Summary: {result.intent.intention_resume[:200]}...")
            print(f"   - Confidence: {result.intent.niveau_certitude.value}")

        # Format as markdown
        markdown = format_search_result_markdown(result)

        # Save result
        output_file = project_root / "data" / "test_search_result.md"
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(markdown)

        print(f"\n   Full result saved to: {output_file}")
        return True

    except Exception as e:
        print(f"   ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        session.close()


def main():
    """Run all tests."""
    print("=" * 60)
    print("Search Pipeline Test (Phase 2)")
    print("=" * 60)

    # Check API keys
    if not check_api_keys():
        return 1

    # Run tests
    results = []

    results.append(("Embedding Service", test_embedding_service()))
    results.append(("Vector Store", test_vector_store()))
    results.append(("Full Search", test_full_search()))

    # Summary
    print("\n" + "=" * 60)
    print("Test Results Summary")
    print("=" * 60)

    passed = 0
    for name, result in results:
        status = "PASS" if result else "FAIL"
        print(f"  {name}: {status}")
        if result:
            passed += 1

    print(f"\nPassed: {passed}/{len(results)}")

    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
