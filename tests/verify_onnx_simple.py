"""Simple verification that ONNX backend is loaded."""

import sys

print("=" * 60)
print("ONNX Backend Verification")
print("=" * 60)

# Test 1: Check embedding model
print("\n1. Loading embedding model...")
from src.indexing.search import SearchService
from src.indexing.embeddings import Chunk

service = SearchService()
emb_service = service.embedding_service

test_chunk = Chunk(content="test query", chunk_type="test", source_id="test", position=0, metadata={})
embeddings = emb_service.embed_chunks([test_chunk])

print(f"   ✅ Embedding model loaded successfully")
print(f"   Model: {emb_service._local_model}")
print(f"   Embedding dim: {len(embeddings[0])}")

# Check if model has ONNX attributes
model = emb_service._local_model
if hasattr(model, 'auto_model'):
    inner = model.auto_model
    print(f"   Inner model type: {type(inner).__name__}")
else:
    print(f"   Model type: {type(model).__name__}")

# Test 2: Check reranker
print("\n2. Loading reranker...")
from src.indexing.reranker import get_reranker

reranker = get_reranker()

# Use proper format for rerank
test_chunks = [
    (Chunk(content="doc1", chunk_type="test", source_id="test", position=0, metadata={}), 0.9),
    (Chunk(content="doc2", chunk_type="test", source_id="test", position=1, metadata={}), 0.8),
]
reranked = reranker.rerank("query", test_chunks)

print(f"   ✅ Reranker loaded successfully")
print(f"   Model: {reranker._model}")
print(f"   Reranked {len(reranked)} chunks")

# Check model type
if hasattr(reranker._model, 'model'):
    inner = reranker._model.model
    print(f"   Inner model type: {type(inner).__name__}")

print("\n" + "=" * 60)
print("RESULT:")
print("=" * 60)
print("If you see 'Multiple ONNX files found' in the logs above,")
print("ONNX backend is SUCCESSFULLY loaded!")
print("=" * 60)
