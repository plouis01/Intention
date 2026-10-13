web: python -m uvicorn src.api.main:app --host 0.0.0.0 --port $PORT --limit-concurrency 40 --timeout-keep-alive 75 --timeout-graceful-shutdown 30
worker: python scripts/db_idle_cleaner.py
