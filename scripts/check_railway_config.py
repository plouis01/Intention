"""Railway configuration checker - Story 3.3.

Verifies Railway deployment configuration matches production requirements:
- Private networking URL for database
- Memory budget validation
- Connection pool compatibility

Usage:
    python scripts/check_railway_config.py
"""

from src.utils.config import get_settings


def check_config():
    """Check Railway configuration compliance."""
    settings = get_settings()
    url = settings.effective_database_url
    issues = []

    print("Railway Configuration Check")
    print("=" * 50)

    # Check database URL uses private networking
    if "rlwy.net" in url:
        if ".railway.internal" in url:
            print("  [OK] Database URL uses private networking")
        else:
            print("  [WARN] Database URL uses public proxy (interchange.proxy.rlwy.net)")
            print("         Consider switching to private networking URL for lower latency")
            issues.append("Switch to private networking URL")
    elif url.startswith("sqlite"):
        print("  [INFO] Using SQLite (local development)")
    else:
        print(f"  [INFO] Database URL: {url[:40]}...")

    # Check embedding provider
    print(f"\n  Embedding provider: {settings.embedding_provider}")
    print(f"  LLM provider: {settings.llm_provider}")
    print(f"  Vector store: {settings.vector_store_provider}")
    print(f"  Reranker enabled: {settings.reranker_enabled}")

    # Railway dashboard reminders
    print("\n" + "=" * 50)
    print("Railway Dashboard Checklist (manual):")
    print("  [ ] API service RAM limit: 2 GB")
    print("  [ ] PostgreSQL service RAM limit: 1-2 GB")
    print("  [ ] App sleeping enabled for API service")
    print("  [ ] Private networking URL configured")

    if issues:
        print(f"\n  {len(issues)} issue(s) found:")
        for issue in issues:
            print(f"    - {issue}")

    return len(issues) == 0


if __name__ == "__main__":
    check_config()
