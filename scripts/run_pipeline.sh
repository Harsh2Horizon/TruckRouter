#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

input_csv="${1:-fuel-prices-for-be-assessment.csv}"
run_dir="${2:-output/latest}"

if [[ -f "$run_dir/locality/report.json" ]]; then
  printf '%s\n' "This run already has a locality audit. Choose a new run directory." >&2
  exit 1
fi

uv sync --cache-dir .cache/uv
docker compose up -d --wait --wait-timeout 60 postgres
.venv/bin/python manage.py migrate --noinput
.venv/bin/python -u manage.py download_geocoding_sources --csv "$input_csv" \
  --output-dir "$run_dir/acquisition"
.venv/bin/python -u manage.py run_geocoding_pipeline --csv "$input_csv" \
  --acquisition-report "$run_dir/acquisition/report.json" \
  --import-only --output-dir "$run_dir/imports"

if [[ ! -f data/reference/geographic-index.json ]]; then
  .venv/bin/python -u manage.py prepare_geographic_reference \
    --acquisition-report "$run_dir/acquisition/report.json"
fi
if [[ ! -f data/reference/coverage/manifest.json ]]; then
  .venv/bin/python -u manage.py prepare_coverage_reference
fi
if [[ ! -f data/reference/localities/manifest.json ]]; then
  .venv/bin/python -u manage.py prepare_locality_reference
fi

.venv/bin/python -u manage.py enrich_source_localities --apply \
  --output-dir "$run_dir/locality"
.venv/bin/python -u manage.py validate_locality_enrichment --run "$run_dir/locality"
.venv/bin/python -u manage.py run_geocoding_pipeline --csv "$input_csv" \
  --acquisition-report "$run_dir/acquisition/report.json" \
  --reuse-import-report "$run_dir/imports/report.json" \
  --enrichment-report "$run_dir/locality/report.json" --output-dir "$run_dir/poi"
.venv/bin/python -u manage.py complete_geocoding_coverage --csv "$input_csv" \
  --base-run "$run_dir/poi" --output-dir "$run_dir/final" --fetch-census
.venv/bin/python -u manage.py validate_geocoding_coverage --csv "$input_csv" \
  --base-run "$run_dir/poi" --run "$run_dir/final"
