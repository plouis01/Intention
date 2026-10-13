#!/usr/bin/env bash
set -euo pipefail

python -m pip install --upgrade pip
python -m pip install --no-cache-dir -e .
python -c "import pydantic_settings; print('pydantic_settings ok')"

python scripts/run_full_pipeline.py --embed
