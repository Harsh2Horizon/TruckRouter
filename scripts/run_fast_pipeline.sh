#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

uv sync --cache-dir .cache/uv
docker compose up -d --wait --wait-timeout 60 postgres
.venv/bin/python manage.py migrate --noinput
.venv/bin/python -u manage.py download_geocoding_sources "$@"
.venv/bin/python -u manage.py run_geocoding_pipeline \
  --acquisition-report output/acquisition/report.json
