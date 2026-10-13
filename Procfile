web: python -m uvicorn src.api.main:app --host 0.0.0.0 --port $PORT --limit-concurrency 20 --limit-max-requests 5000 --timeout-keep-alive 120 --timeout-graceful-shutdown 120
worker: python scripts/db_idle_cleaner.py
