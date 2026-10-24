# Initial verification record

Run date: 2026-10-24. This records executed checks separately from checks that require dependencies unavailable in this environment.

## Executed

- 37 parser, provider-adapter and matching tests passed with Python 3.13, the installed Pydantic library and a locally cached RapidFuzz package. No network access or Django database was required for these tests.
- Ruff static checks and formatting were run using a locally cached executable.
- All Python source files were compiled for syntax validation.
- The CSV profiler processed all 8,151 rows without a parser exception and found 6,738 distinct external truckstop IDs.
- Parsed fields: 5,672 rows with store IDs; 8,078 with highway references; 4,582 with exits. 502 rows preserve unparsed highway/address context. Country inference: 7,531 US rows and 620 Canadian rows.
- JSON Schemas and the requested normalized example were generated from the Pydantic contracts.

Derived results are in `output/normalized-addresses.jsonl` and `output/parser-report.json`. These are parsing outputs; **zero store coordinates have been independently verified**.

## Not executed successfully

Dependency installation could not reach PyPI because shell network/DNS access is restricted. Automatic approval review rejected the elevated `uv sync` attempt because sandbox approval requests are disabled. The environment has no installed Django or ijson package.

Consequently, Django system checks, database migrations, migration drift checks, API/database integration tests and mock HTTP integration tests could not run. An initial migration and 13 integration tests are included for execution after dependencies are installed. The migration was authored with the models and remains unverified by Django's migration autodetector. No lockfile could be resolved.

No source release was downloaded or imported into a Django database, and no live geocoding request was made. The matching tests use synthetic coordinates rather than actual store positions. End-to-end accuracy, source coverage, positional error and threshold calibration remain to be measured on real labeled data.

## Complete these checks once dependencies are available

```sh
uv sync --cache-dir .cache/uv
uv run --cache-dir .cache/uv python manage.py check
uv run --cache-dir .cache/uv python manage.py makemigrations --check --dry-run
uv run --cache-dir .cache/uv python manage.py migrate
uv run --cache-dir .cache/uv python manage.py test
uv run --cache-dir .cache/uv ruff check .
```

Then import a real source release and evaluate candidate retrieval and resolution against known truckstop locations before enabling unattended acceptance.

## PostgreSQL and batch acquisition follow-up

The requested local configuration is now PostgreSQL 17 in `compose.yaml`, container `truckroute-postgres`, database `truckroute`, user `truckrouter`, port `127.0.0.1:55432`. The requested password is stored in the ignored, mode-0600 `.env`. Django loads that configuration automatically. Psycopg, python-dotenv and DuckDB were added to the dependency manifest.

`scripts/run_pipeline.sh` now installs dependencies, starts PostgreSQL, checks migrations/tests and runs all target adapters. The batch command acquires ATP's published archive, Overture fuel places through DuckDB/STAC and country-bounded OSM fuel extracts. It writes combined/per-source decisions with source-release scoping and explicit failed-source states.

Executed on this follow-up:

- Docker Compose configuration validation and shell syntax validation passed.
- 45 domain/acquisition unit tests passed; Ruff and Python syntax checks passed.
- The parser ran again on all 8,151 rows, retaining 6,738 unique input identities.
- `docker compose up` failed because the sandbox denied access to Docker's Unix socket. No PostgreSQL container was created or connected.
- Dependency installation failed again because PyPI DNS/network access was denied.
- Direct reachability checks for ATP, Overture STAC and Overpass also failed on DNS/network access. No real source records or geocodes were downloaded.

Machine-readable results are in `output/execution-status.json`. The pipeline's source acquisition, PostgreSQL migrations and 16 Django integration tests remain unverified at runtime. Enable Docker socket access and outbound networking for this task, then run `bash scripts/run_pipeline.sh`. Real geocoding output files are produced only after source acquisition succeeds.
