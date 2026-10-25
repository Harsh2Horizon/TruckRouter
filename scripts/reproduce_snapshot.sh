#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

run_dir="${1:-output/reproduced}"
test -f .env || cp .env.example .env
uv sync --cache-dir .cache/uv
.venv/bin/python scripts/snapshot.py fetch
docker compose up -d --build --wait --wait-timeout 60 postgres
.venv/bin/python manage.py migrate --noinput
.venv/bin/python scripts/snapshot.py restore
.venv/bin/python scripts/snapshot.py extensions
.venv/bin/python -u manage.py run_geocoding_pipeline \
  --acquisition-report data/replay/acquisition-report.json \
  --reuse-import-report data/replay/import-report.json \
  --enrichment-report data/replay/locality-report.json \
  --output-dir "$run_dir/poi"
.venv/bin/python -u manage.py complete_geocoding_coverage \
  --base-run "$run_dir/poi" --output-dir "$run_dir/final"
.venv/bin/python manage.py validate_geocoding_coverage \
  --base-run "$run_dir/poi" --run "$run_dir/final"
.venv/bin/python scripts/snapshot.py verify --run "$run_dir"
