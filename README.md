# Truckroute

Django foundation for normalizing truck refuelling locations and resolving them against Overture Places, AllThePlaces and OpenStreetMap. Routing is a subsequent phase.

The geocoder parses business/store identity, postal addresses, highway networks and route variants, exits, city, state/province and country. It scores candidate records with an auditable 0–100 heuristic and returns `accepted`, `needs_review`, or `unmatched`. Only accepted results receive a resolved coordinate.

## Start locally

Requires Python 3.12–3.14, uv and Docker. Django 5.2 LTS uses PostgreSQL 17 by default. The local `.env` configures database `truckroute`, user `truckrouter`, password `truckrouter`, host `127.0.0.1`, port `55432`. The container is named `truckroute-postgres`, binds to localhost and stores its data in a named Docker volume.

```sh
uv sync --cache-dir .cache/uv
docker compose up -d --wait --wait-timeout 60 postgres
uv run --cache-dir .cache/uv python manage.py migrate
uv run --cache-dir .cache/uv python manage.py import_fuel_csv fuel-prices-for-be-assessment.csv
uv run --cache-dir .cache/uv python manage.py runserver
```

```sh
curl -X POST http://127.0.0.1:8000/api/v1/addresses/parse \
  -H 'Content-Type: application/json' --data-binary @examples/kwik-trip-input.json
curl -X POST http://127.0.0.1:8000/api/v1/geocode \
  -H 'Content-Type: application/json' --data-binary @examples/kwik-trip-input.json
```

`POST /api/v1/geocode` searches imported records. It returns unmatched until you supply source data. `GET /api/v1/geocode/runs/{run_id}` retrieves the saved decision and field scores. Live network calls are kept out of the HTTP request path.

The API is a localhost development prototype with no authentication. Add authentication, access controls, throttling, production settings and background jobs before exposing it externally. Django automatically reads `.env`; existing shell environment variables take precedence. `.env.example` lists the configuration. `DATABASE_ENGINE=sqlite` is available for isolated tests.

## Run all source adapters and export geocodes

```sh
bash scripts/run_pipeline.sh
```

This installs dependencies, starts PostgreSQL, applies migrations, downloads the three sources concurrently, imports candidates in batches and resolves each unique input location. Completed downloads are reused. AllThePlaces selects spider files by input brands, explicit aliases and fuel samples when a published archive is cached; fresh installs download selected spiders individually. Selection coverage and errors are recorded. Overture discovers six relevant spatial Parquet partitions for the default US/Canada envelope, transfers them concurrently and filters locally using DuckDB. For a small bounding box, use remote filtering instead. OSM uses daily state/province GeoParquet extracts from the [Geomermaids OSM mirror](https://github.com/gsueur/osm-geoparquet), selecting fuel nodes and fuel polygons with original IDs and tags. It verifies published file sizes and SHA256 checksums. Polygon coordinates are centroids, with their derivation recorded in provenance.

To download without importing or matching:

```sh
.venv/bin/python -u manage.py download_geocoding_sources
```

The progress and source manifests are saved in `output/acquisition/report.json`. Each completed file is cached under `data/sources`; rerunning resumes transfers and reuses completed exports. The published OSM snapshot date and underlying PBF timestamp are both retained. The default scope comes from all input states/provinces, rather than a single example city.

For a small Overture area, use `--overture-mode remote --overture-bbox=-90.8,43.8,-90.3,44.2`. This intentionally limits Overture coverage; use it for local investigations, not a complete nationwide batch.

After an initial setup, rerun only the data pipeline with:

```sh
uv run --cache-dir .cache/uv python manage.py run_geocoding_pipeline \
  --acquisition-report output/acquisition/report.json
```

Successful source acquisition produces:

- `output/geocoding/geocodes.csv`: one row per input location with score, status, accepted coordinates and separate proposed coordinates for review.
- `output/geocoding/geocodes.jsonl`: full combined match decisions and evidence.
- `output/geocoding/per-source.jsonl`: independent adapter comparisons; failed adapters are marked unavailable.
- `output/geocoding/report.json`: source releases, import/rejection counts and result totals.

Scoring version `evidence-2.0` adds each source's latitude, longitude, score, status, matched record and evidence coverage to `geocodes.csv`. `highest_matching_*` identifies the highest-scoring available provider point, including explicit score ties. Candidate coordinates can be shown for review or low-score matches; the source status explains their meaning. The combined `latitude`/`longitude` remain acceptance-controlled. `fuel-prices-geocoded.csv` preserves all original price rows and appends these comparison fields. See [scoring v2](docs/scoring-v2.md) for the formula and a local-only rescore command.

No-source failures produce a failure report rather than an empty successful geocoding run. Source imports are atomic, and a run matches only the successful releases it acquired. If any requested adapter fails, combined candidates require review. Proposed coordinates are not accepted geocodes and must be interpreted with their status and evidence.

The legacy `run_geocoding_pipeline` path without an acquisition report still supports Overpass. Public Overpass is subject to its [resource guidance](https://dev.overpass-api.de/overpass-doc/en/preface/commons.html); use your own `OVERPASS_URL` for operational queries. The fast acquisition path uses published OSM mirror files and makes no Overpass or Nominatim requests.

## Import place candidates

Overture and ATP provide downloadable datasets, not equivalent text geocoding endpoints. Download a relevant, preferably fuel-only regional export, then import it:

```sh
uv run --cache-dir .cache/uv python manage.py import_places alltheplaces ./data/kwik_trip.geojson --release RUN_ID
uv run --cache-dir .cache/uv python manage.py import_places overture ./data/places.geojson --release RELEASE_ID
uv run --cache-dir .cache/uv python manage.py import_places osm ./data/fuel-overpass.json --format overpass --release EXTRACT_DATE
uv run --cache-dir .cache/uv python manage.py import_places nominatim ./data/results.json --format json --release SNAPSHOT_DATE
uv run --cache-dir .cache/uv python manage.py geocode_locations --external-id 9 --limit 1
```

These filenames are inputs you supply, not bundled datasets. Import supports GeoJSON FeatureCollections, Overpass `elements`, JSON arrays and NDJSON. The importer streams records and upserts `(provider, namespace, source_id)`. Invalid records roll back that import. Export Overture GeoParquet to GeoJSON first using the [official Overture client](https://docs.overturemaps.org/getting-data/overturemaps-py/).

ATP `ref` is an opaque spider-specific identifier by default. If a particular spider's `ref` is independently confirmed to be its public store number, import that file with `--ref-is-store-id`. The decision is recorded in provenance. OSM `ref` and Nominatim `place_id` are never treated as public store IDs.

For optional live retrieval, configure `NOMINATIM_BASE_URL` for your own Nominatim instance and `GEOCODER_USER_AGENT`, then add `--fetch-nominatim` to `geocode_locations`. There is no default remote endpoint. The public OSMF host is rejected: its [usage policy](https://operations.osmfoundation.org/policies/nominatim/) limits requests, discourages bulk geocoding, requires identification/caching/attribution, and restricts systematic queries. This pipeline imports local OSM exports or uses a self-hosted service.

## Inspect the supplied CSV

```sh
uv run --cache-dir .cache/uv python -m geocoding.profile_csv fuel-prices-for-be-assessment.csv
```

This writes `output/normalized-addresses.jsonl` and `output/parser-report.json` without touching the CSV. The current file has 8,151 rows, 6,738 distinct truckstop IDs, 7,531 US rows and 620 Canadian rows. Raw rows, aliases and repeated price observations are preserved by the database importer. Currency, price unit and price observation time remain unknown unless supplied; they must be established before route cost comparisons.

## Verification

```sh
uv run --cache-dir .cache/uv python -m unittest geocoding.tests.test_domain geocoding.tests.test_downloads -v
uv run --cache-dir .cache/uv python manage.py test
uv run --cache-dir .cache/uv python manage.py makemigrations --check --dry-run
uv run --cache-dir .cache/uv ruff check .
```

Tests use synthetic coordinates and mock HTTP responses. They do not establish the real coordinates of any store. See [the implementation verification record](docs/verification.md) for checks actually executed in the initial environment.

Read the [detailed matching criteria reference](docs/matching-criteria-reference.md) for source field mappings, scoring cases, cross-source clustering, acceptance rules and export interpretation. Also see [the model and matching design](docs/geocoding-design.md), [source research and mappings](docs/source-research.md), and the generated [canonical address JSON Schema](docs/canonical-address.schema.json).
