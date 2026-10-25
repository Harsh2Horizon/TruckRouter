# Truckroute geocoding pipeline

This branch contains one supported pipeline for geocoding truck-refuelling records in the United States and Canada: parse the input, acquire three POI datasets, import them into PostgreSQL, enrich missing locality fields, retrieve and rank source candidates, and complete unresolved observations with explicit geographic approximations. Routing and fuel-cost optimisation are future work.

Matching policy: `brand-locality-3.1`. Locality enrichment: `locality-enrichment-1.0`. Geographic completion: `coverage-4.0`. Scores are explainable heuristics, not probabilities. All coordinates are WGS84 decimal degrees; external GeoJSON uses `[longitude, latitude]` and application coordinates use named `latitude`/`longitude` fields.

## 1. Input and address parsing

The input CSV contains `OPIS Truckstop ID`, `Truckstop Name`, `Address`, `City`, `State`, `Rack ID`, and `Retail Price`. The supplied file has **8,151 observations and 6,738 unique OPIS IDs**, including 7,531 US and 620 Canadian rows. Repeated prices and conflicting observations are preserved. Country is inferred from a recognized state/province when omitted. Price currency, unit and observation time remain unknown unless supplied.

### Canonical comparison schema

```text
CanonicalAddress
  schema_version: string
  raw_name, raw_address: string                 # preserved input text
  name, brand, brand_wikidata, store_id: string
  house_number, street, unit, postcode: string
  city, state, country: string
  highways: RoadReference[]
  exits: ExitReference[]
  unparsed_address: string
  warnings: string[]

RoadReference
  network: interstate | us_highway | state_route | county_route |
           farm_to_market | provincial_highway | trans_canada | unknown_highway
  number: string
  jurisdiction: string                         # US, US-WI, CA-ON, etc.
  variant: main | business | alternate | bypass | spur | loop
  direction: string                            # N/S/E/W when recognized
  raw: string
  key: jurisdiction:network:number:variant      # computed property

ExitReference
  identifier: string
  highway_key: string | null
  association: inferred_preceding_route | unknown
  raw: string
```

Store identifiers are strings: `KWIK TRIP #00796-A` produces brand `KWIK TRIP` and store ID `00796-A`. Everything after the first `#` is retained as the store identifier. Recognized trailing `STORE 796` and `LOCATION NO. 796` forms are also separated. Name scoring removes store-number suffixes; source store identifiers never have to equal OPIS IDs. Meaningful brand digits such as `7-Eleven` remain.

For example, the input `{name: "KWIK TRIP #796", address: "I-94, EXIT 143 & US-12 & SR-21", city: "Tomah", state: "WI"}` produces this comparison projection:

```json
{
  "brand": "KWIK TRIP", "store_id": "796",
  "city": "TOMAH", "state": "WI", "country": "US",
  "highways": [
    {"network": "interstate", "number": "94", "jurisdiction": "US", "variant": "main"},
    {"network": "us_highway", "number": "12", "jurisdiction": "US", "variant": "main"},
    {"network": "state_route", "number": "21", "jurisdiction": "US-WI", "variant": "main"}
  ],
  "exits": [{"identifier": "143", "highway_key": "US:interstate:94:main"}]
}
```

Canonical name/city text removes accents and punctuation, normalizes case and spaces, and retains the original text separately. Locality comparisons additionally expand aliases such as `ST → SAINT`, `FT → FORT`, `MT → MOUNT`, `NYC → NEW YORK`, and specific Census legal names such as `Indianapolis city (balance) → Indianapolis`. This does not strip `City` from ordinary names such as `Kansas City`. State/province aliases normalize full names, codes, supported abbreviations and ISO forms; country aliases include `USA`, `U.S.`, `United States`, `CAN` and `Canada`. Unknown or conflicting geography is not forced into a recognized locality.

### Address case catalogue

Examples are syntactic illustrations, not assertions about real station locations. Unless stated otherwise, the city/state fields are supplied separately.

| Case | Example address or name | Parsed result and handling |
|---|---|---|
| Numbered street | `123 Main Street` | House `123`, street `Main Street`; eligible for optional Census lookup in the US. |
| Lettered/ranged house number | `123A Main Street`; `123-125 Main Street` | House number remains a string; preserve the full range or suffix. Census must independently match the supplied house token. |
| Unit in freeform street | `123 Main Street Suite 200` | House `123`; suite text stays in `street` and `raw_address`. A freeform unit is not separately inferred; explicit provider `addr:unit` populates `unit`. |
| Street named after a highway | `215 US Highway 12` | House `215`, street text and a US-route reference. Excluded from Census lookup by the conservative highway eligibility rule. |
| Mixed street and interchange | `123 Main St & I-94, EXIT 143` | Retain street text, I-94 and scoped exit 143; residual `123 Main St` remains for audit. |
| Interstate only | `I-94` | Interstate route 94, no exit. It cannot establish an exact interchange. |
| Multiple road systems | `I-94, EXIT 143 & US-12 & SR-21` | Interstate 94, US route 12, state route 21; exit 143 links to the preceding I-94. |
| Shared route numbers | `I-80/94, EXIT 2` | Two interstate references; the exit remains unscoped because its numbering system is ambiguous. Geographic fallback can test supported routes against real exits. |
| Separate scoped exits | `I-40, EXIT 280 & I-55, EXIT 4` | Exit 280 links to I-40 and exit 4 to I-55. |
| Exit letter/suffix | `I-94, EXIT 143-A` | Preserve identifier `143-A`; expanded exit-reference lookup normalizes supported letter separators. POI exit scoring uses the canonical identifier literally. |
| Route variants | `US-12 BUS & SR-21 BYPASS & LOOP 410` | Business US-12, bypass SR-21 and state loop 410. Variants are distinct route identities. `ALT` and `SPUR` are also supported. |
| County routes | `CR-B & COUNTY ROAD 12` | County routes B and 12, scoped to the input state. |
| Farm/ranch routes | `FM 1960 & RM 620` | Farm-to-market network, with the input jurisdiction. |
| Canadian routes | `HWY 401 & PR 7 & TCH 1`, Toronto/ON | Provincial highways 401 and 7, plus Trans-Canada 1. State/province scope is retained. |
| Generic highway | `HWY 12`, US locality | `unknown_highway`; no unsupported assumption that this means US-12 or a state route. |
| Direction | `I-94 WB` | Westbound direction retained; direction adds no scoring points. |
| Mile marker | `I-80, MM 243` | Route 80 plus residual `MM 243`. No mile-marker interpolation is implemented. |
| Relative distance | `34 MILE WEST OF EXIT 11/146` | Preserve raw text/residual; do not interpret it as a verified postal address or calculate a distance offset. The parser recognizes unscoped exit 11; the unresolved remainder is not silently treated as another verified exit. |
| Exit without a road | `EXIT 143` | Unscoped exit; adds no exit scoring evidence and cannot identify an interstate exit by itself. |
| PO box | `PO BOX 123` | Preserve as freeform street text; not a physical station geocode and not Census-eligible. |
| Intersection/descriptive text | `Corner of Main St and Oak Ave` | Preserve freeform text; no intersection geocoder is implemented. Use name/locality candidates, then supported fallbacks. |
| Empty address | Empty address, known name/city/state | Name/locality matching is still possible; omit absent highway/exit criteria from the denominator. |
| Missing name or locality | Empty input name; unknown/missing city/state | API input requires a nonempty name. Provider records may use `UNKNOWN`, but generic-only names cannot establish a POI match. Incomplete locality blocks POI scoring. |
| OCR-like interstate typo | `1-40 EXIT 77` | The completion parser conservatively corrects leading `1-` to `I-` only before an exit clause and records `leading_1_corrected_to_interstate_I`. Base parsing preserves the original value. |
| Duplicate/conflicting OPIS records | Same ID, different city/state or incompatible routes | Retain every observation; block combined automatic acceptance and resolve conflicting raw rows separately during completion. |

Highway and exit extraction is conservative regular-expression parsing. The preceding-route association is a hypothesis, verified against actual reference geometry for geographic fallback. Postal locality is not extracted from arbitrary street prose. Unsupported text remains in raw/residual fields rather than generating an invented coordinate.

## 2. Downloaded sources and PostgreSQL storage

### Current acquired snapshot

These counts describe the cached acquisition used for the latest validated run, not live worldwide source sizes.

| Source | Release | Downloaded candidate rows | Unique stored POIs | Download and filtering |
|---|---|---:|---:|---|
| AllThePlaces | `2026-10-10-13-32-41` | 111,611 | 111,440 | Selected spider GeoJSON files or an existing archive; explicit brand aliases and first-feature fuel/brand discovery; stream selected fuel/input-brand features to NDJSON. Duplicate identities are upserted. |
| Overture Places | `2026-10-21.0` | 182,986 | 182,986 | Discover STAC partitions intersecting the US/Canada envelope; download six relevant Parquet containers concurrently; filter country, bounding box and fuel categories/input brands with DuckDB Spatial; native NDJSON export. |
| OpenStreetMap | Snapshot `2026-10-23`, PBF timestamp `2026-10-20T20:23:47Z` | 121,829 | 121,829 | OSM-only Geomermaids regional GeoParquet mirror, 57 input regions; retain `amenity=fuel` nodes and polygonal ways/relations, complete tags and original identities. |
| **Total** | | **416,426** | **416,255** | |

Acquisition runs the sources concurrently. The curl client supports bounded timeouts, retries, partial-file resumption and checksum/size verification when published. Cache files, source URLs, hashes, concrete releases and query scope are recorded in manifests. Overture also supports remote Parquet filtering for small areas; bulk local filtering is the validated nationwide path. AllThePlaces spider selection is not a guarantee of an exhaustive fuel inventory. Regional OSM coverage follows the input regions rather than an arbitrary nationwide live query.

The active adapters read these provider fields:

| Canonical field | AllThePlaces GeoJSON | Overture GeoJSON exported from Parquet | OpenStreetMap normalized element |
|---|---|---|---|
| Identity | `properties.@spider` + feature `id` | Place/GERS `id`, namespace `places` | Element `type` + `id` |
| Name | `name`, then `brand`/`operator` | `names.primary`, then brand name | `name`, then `brand`/`operator` |
| Brand | `brand`, otherwise parsed name | `brand.names.primary`, otherwise parsed name | `brand`, otherwise parsed name |
| Address | `addr:street_address`/`addr:street`/`addr:full`, house number | First address `freeform` | `addr:street_address`/`addr:street`/`addr:full`, house number |
| City | `addr:city` | First address `locality` | `addr:city` |
| State/country | `addr:state`, `addr:country` | First address `region`, `country` | Tags, then regional extract context for missing values |
| Postcode/unit | `addr:postcode`, `addr:unit` | First address `postcode` | `addr:postcode`, `addr:unit` |
| Coordinates | Point geometry, possibly absent | Point geometry | Node latitude/longitude or an explicitly derived polygon centroid |
| Provenance | Spider, feature ID, source URI and opaque ref | Source lineage, licenses, release, existence confidence and status | Element identity, mirror, regional snapshot, extraction context and original tags |

All adapters return `PlaceCandidate`: `{provider, namespace, source_id, release, address: CanonicalAddress, coordinate: Coordinate|null, precision, operating_status, existence_confidence, provenance, license, raw}`. Raw provider store IDs are retained as metadata. OSM polygon centroids use `poi_center` precision; they are not surveyed entrances. Overture/ATP have local place indexes here, rather than being called as public text-geocoding endpoints. The retained pipeline uses no live Nominatim or Overpass adapter.

### Tables and constraints

PostgreSQL 17 runs in Docker on `127.0.0.1:55432`. This implementation uses ordinary PostgreSQL scalar indexes and JSONB, with DuckDB Spatial for reference geometry; it does not require PostGIS.

| Table/model | Stored data and purpose |
|---|---|
| `geocoding_fuellocation` / `FuelLocation` | Unique `(source, external_id)` input identity, canonical address JSONB, aliases, input warnings, resolution status and accepted coordinate. |
| `geocoding_fuelobservation` / `FuelObservation` | Every original CSV row as JSONB, linked to its stop; dataset SHA256 and row number are unique. Price uses `numeric(16,8)`; currency/unit/time remain explicit unknowns. |
| `geocoding_sourceplace` / `SourcePlace` | Unique `(provider, namespace, source_id)`; release; normalized name/brand/store/locality columns; canonical address JSONB; latitude/longitude; precision/status/confidence; raw JSONB, provenance and license. |
| `geocoding_geocodingrun` / `GeocodingRun` | UUID, input query JSONB, algorithm version, status and complete result JSONB; optional input-stop foreign key. |
| `geocoding_matchcandidate` / `MatchCandidate` | Each considered source record's score, evidence and complete immutable snapshot, linked to the run and current source row. Historical decisions survive a source update. |

`SourcePlace` has a composite B-tree index on `(country, state, city_key)` plus scalar brand/city indexes. The retained schema also carries its existing store-ID index for migration compatibility; the matcher never searches or scores by it. A database check requires either two null coordinate fields or a valid latitude/longitude pair. Confidence is null or within `[0,1]`. Source imports batch 500 records with conflict updates, preserve per-record rejection logs, and roll back a source transaction if its export cannot be read or contains zero usable records. Rejected individual provider records are counted in the import report.

Files are stored under `data/sources/` as cached containers/NDJSON and under `data/reference/` as polygon, road, exit and settlement references. These large caches and generated outputs are Git-ignored. The database and cache are retained during this refactor.

## 3. Fill missing source locality from coordinates

Candidate scoring requires city, state and country together. Before matching, backfill empty source locality fields using the existing station coordinate:

1. Confirm country/state using point containment (`ST_Covers`) in US Census TIGER/Line 2026 state polygons or geoBoundaries Canadian province polygons. Preserve nonempty source values and refuse conflicting evidence.
2. Find a unique US municipality/CDP polygon containing the point. The reference has **32,071 Census place polygons and 69 administrative regions**.
3. Where no unique city polygon resolves the point, find the nearest named GeoNames/OSM settlement within **10 km**, restricted to the same verified state/province and country. Canadian cities use this bounded estimate because only Canadian province polygons were prepared.
4. Write canonical/index fields plus original values, reference ID, source, method, timestamp and nearest distance/reference coordinates to provenance. Preserve raw provider payloads and POI coordinates. Nearest-settlement cities require review; polygon cities can satisfy normal locality gates.

The current backfill updated **83,984 source records**, filling **83,695 cities** (63,908 polygon assignments and 19,787 nearest settlements) and **348 states**:

| Source | Empty indexed city: before → after | Empty indexed state: before → after |
|---|---:|---:|
| AllThePlaces | 2,451 → 66 | 86 → 82 |
| Overture | 970 → 13 | 345 → 1 |
| OSM | 80,459 → 106 | 0 → 0 |

OSM's raw `addr:state` is absent on 88,975 records; regional context had already filled the indexed state before this enrichment. Raw tags therefore remain empty where the source omitted them. Of the 234 records still missing city/state, 34 lack coordinates, 92 lack a compatible supported region/boundary match, and 108 lack a suitable nearby locality. Unsupported fills are not forced.

A municipality/CDP label may differ from a mailing-city label. Neither polygon containment nor proximity establishes that two addresses refer to the same business. The separate name and locality matching gates still apply.

## 4. Candidate retrieval and location selection

### Narrow the PostgreSQL candidate set

For each unique input stop:

1. Restrict queries to the three releases in the completed acquisition/import report. Reuse verifies input SHA256, source release/path/count and, when supplied, locality-enrichment audit counts. Incomplete acquisition fails before matching.
2. Filter by canonical country and state. Blank provider values may be retained for diagnostics; they cannot pass locality scoring until resolved.
3. Fetch distinct city keys in the scoped region, compare them with normalized input city using fuzzy similarity, and retain cities at **≥0.90**. Blank city candidates remain diagnostic.
4. Fetch distinct brand/name keys in those cities. Retain keys whose identity-text similarity to input brand or business name reaches a **0.50 retrieval prefilter**. This is deliberately broader than the final name gate.
5. Fetch up to **500 records per source**, prioritizing exact city, fuzzy city, then blank city; within these, exact brand and stable primary key. A batch caches text-key lists only while its source index is unchanged. Reaching the cap blocks automatic acceptance.
6. Score every retrieved source record, form branch clusters, rank independently for each provider and across providers, then retain separate source coordinates, criteria and statuses.

Missing city was mainly a scoring blocker: old retrieval could return the record, but incomplete locality forced its score to zero. Broad blank-city retrieval also produced state-wide noise. Enrichment reduced OSM candidate-row retrievals from 438,564 to 10,772 across the input queries and eliminated 74 truncated queries while increasing usable OSM POI coverage.

### Five location methods

| Method | How a point is selected | Precision | Main references | Interpretation |
|---|---|---|---|---|
| `fuel_poi` | Eligible name/locality candidate. Prefer the accepted combined cluster coordinate; otherwise expose the highest-scoring eligible source point for review. | `poi` or `poi_center` | AllThePlaces, Overture, OSM | Actual provider POI coordinate, with acceptance/review status. Not necessarily a truck-access entrance. |
| `highway_exit` | Match country/state, supported interstate and exit number to real junction geometry near a matching named city. Require ≤20 km from city anchor and reject equally plausible junctions spread >2 km. | `motorway_exit` | OpenInterstate/OSM exits and named settlements | Approximate station position at a verified exit; no business identity claim. |
| `highway_near_city` | Match network/number/variant in Census primary-road geometry. Choose the closest point on a matching road to a named city anchor within 20 km, inside the input state. | `road_segment` | US Census TIGER/Line 2025 Primary Roads | Approximate road position; requested exit and station are unverified. Primary roads do not cover every route. |
| `named_locality` | Match city aliases or fuzzy name within the same state/country. Rank named settlements by similarity, current populated-place class, population and deterministic source/ID order. | `locality` | GeoNames and OSM settlements | City/town representative point. Equally ranked distant same-name anchors are reported as ambiguous. |
| `census_address` | Optional US structured-address lookup. Require matching house token, city ≥0.90 and equal state/country; cache the response and check the point against the state. | `address_range` | US Census Geocoder | Interpolated address-range point; business identity is unverified. |

The retained behavior has two exit passes. When there is no eligible source point, first test an explicitly scoped interstate exit against the OSM city index. If that does not resolve, the coverage resolver tries a cached validated Census address, then an expanded exit search using GeoNames/OSM cities and supported route inference, then highway-near-city, then named-locality. Thus the five methods are selected fallbacks, not five independent geocoders run successfully for every row. Optional Census fetching in completion targets still-unresolved structured US observations; it is not a query for every highway description.

Reference inventory: 74,217 exit records, 166,748 OSM settlement rows, 215,594 GeoNames populated-place/ADM1 rows and 11,063 prepared primary-road rows. US exit/road/point containment uses the cached geoBoundaries state reference; source locality enrichment uses the newer Census state/place reference. Canadian exit and primary-road matching are not implemented. A last-resort `administrative_area` representative is supported if the city cannot be located; otherwise the result is `unresolved`. Neither exceptional case occurs in the current five-method output.

Every non-POI method has POI match score **0**, `Is Approximate=True` and `Needs Review=True`. A high geographic agreement score on an exit is not a high station-identity score.

### Preserve and validate each raw observation

Completion reuses the chosen POI/approximation when it is compatible with the raw row. It handles conflicting city/state/name/route observations independently. For US points outside the input state, it selects an in-state provider alternative when available; otherwise it uses geographic fallback. It preserves all original columns, values, row order and eight-decimal price strings. Distinct-stop output retains the first raw-row result for each OPIS ID, while the full observation CSV keeps all row-specific differences.

## 5. Scoring, branch matching and cross-source checks

### Evidence and formula

| Evidence | Weight | Similarity |
|---|---:|---|
| Business name/brand | 0.60 | Maximum RapidFuzz ratio/token-sort ratio across normalized brand and business-name combinations. Strip identifiable store-number forms and generic business descriptors. Generic-only names cannot match. Kwik Trip and QuikTrip remain distinct. |
| Joint locality | 0.30 | City similarity when both sides have complete, recognized and equal state/country and city ≥0.90; otherwise fail the locality gate. |
| Highways | 0.07 | Overlap of route identity sets: intersection size divided by the smaller nonempty set size. |
| Scoped exits | 0.03 | Same overlap calculation on `(normalized highway key, exit identifier)` pairs; unscoped exits add no evidence. |

```text
applicable_weight = 0.60 + 0.30
                  + 0.07 if the input has highways
                  + 0.03 if the input has scoped exits

score = 100 × sum(weight × similarity) / applicable_weight
```

Only input-applicable evidence enters the denominator. An applicable input highway/exit missing from the candidate scores zero; it is not removed to inflate the score. For POI eligibility, final name similarity must be **≥0.70** and joint locality must pass; otherwise final score is **0**, even when the diagnostic ungated score is high. Provider IDs, OPIS ID, store number, house number, street, unit, postcode, Wikidata, source count and provider confidence contribute **zero points**. Closed places and zero-existence-confidence records are excluded from usable POI selection.

Examples:

- Input has name, locality, highways and scoped exit. Exact name/locality but no provider road/exit evidence gives `60 + 30 = 90`, not 100.
- Input has only name and locality. Exact matches give `100 × (0.60 + 0.30) / 0.90 = 100`.
- Complete locality and 0.80 name similarity, with absent candidate road/exit clues on a fully specified input, gives `48 + 30 = 78`, requiring review.
- Exact brand/highways but missing or wrong city gives score 0. A different store ID alone never reduces the score.

`evidence_coverage` reports available input-applicable evidence separately from similarity. `ungated_score` and per-criterion contributions remain diagnostics when eligibility fails. None of these values is a calibrated probability.

### Cluster and accept a branch

Eligible, open candidates with score ≥65 can form branch clusters. Coordinates must be within 250 m, names ≥0.90 and locality complete. Within 100 m, strong name/locality is sufficient; between 100 and 250 m, shared highway and scoped-exit evidence is also required. Complete-link clustering requires every member to agree with every other member, preventing chains of nearby points from joining distant stations.

A cluster is represented by a real provider coordinate chosen as a medoid, never by an averaged location between stations. Repeated providers or shared source lineage do not increase the score. A combined automatic acceptance requires:

- Score **≥85**, name similarity **≥0.85**, valid joint locality and a usable POI coordinate.
- A score margin **≥8** over the next distinct branch cluster.
- No retrieval truncation, conflicting input observations, nearest-settlement locality warning or credible cross-source coordinate conflict.

Scores ≥65 that fail acceptance checks require review. Positive eligible POI points can remain visible in source/highest-match exports even below the review threshold; visibility is not acceptance. Multiple same-brand stations in the same city can therefore remain ambiguous when route/exit evidence is unavailable. The implementation does not use store IDs to resolve that ambiguity.

### Independent source predictions and deviations

Each source exports its own best eligible record, latitude, longitude, score, name/locality/highway/exit contributions, gate results, locality method and status. The highest-match score and coordinates always refer to the same source record. Ties are explicitly listed and ordered deterministically by provider/identity. Accepted combined coordinates and review proposals are separate fields.

Compare provider coordinates pairwise using haversine distance. Deviation categories are consistent at ≤250 m, moderate above 250 m, divergent at ≥1 km, and severe at ≥5 km. Zero/one-point cases are labeled separately. When two source candidates both score ≥85 and differ by more than 1 km, combined acceptance is blocked. Distances are warning evidence, not proof that either provider is correct; inspect competing branches, names and road clues.

## 6. Latest validated results and exports

The latest enriched source indexes yield eligible provider coordinates for **3,659 ATP**, **3,385 Overture** and **2,694 OSM** input stops. Any-source POI availability is **4,970/6,738**, including review candidates; counts overlap across providers. Completion chooses POIs for 4,967 unique stops after observation conflicts and geographic checks.

| Final selected method | Raw observations | Unique stops |
|---|---:|---:|
| `fuel_poi` | 6,132 | 4,967 |
| `highway_exit` | 742 | 644 |
| `highway_near_city` | 432 | 395 |
| `named_locality` | 842 | 731 |
| `census_address` | 3 | 1 |
| **Total with coordinates** | **8,151** | **6,738** |

Primary export contracts:

| Stage/file | Contents |
|---|---|
| Acquisition `report.json` | Input hash, source release, downloaded scope/files/counts/checksums and failures. |
| Import/matching `report.json` | Input/source import verification, scoring version, status/coverage counts, source releases and run completion/failure. |
| Matching `normalized-addresses.jsonl`, `parser-report.json` | Canonical input addresses and parsing coverage. |
| Matching `geocodes.csv`, `geocodes.jsonl` | One input stop per row/object; separate accepted, proposed, highest-match and fallback coordinates with full evidence. |
| Matching `source-comparison.csv`, `per-source.jsonl` | One stop/source comparison, criteria, diagnostic/eligible points and locality origin. |
| Matching `coordinate-disagreements.csv` | Provider distances, severity and review context. |
| Locality `original-records.jsonl`, `changes.csv`, `remaining-missing-localities.csv` | Before-update snapshot, inferred values/methods and unresolved records. |
| Completion `all-records-geocoded.csv` | Every raw observation plus final point, method, precision, source, score, review and validation fields. |
| Completion `final-stops.csv` | First raw-row result for each input ID. |
| Completion `report.json`, `validation.json` | Method totals, preservation and geographic verification. |

The previously delivered review CSVs remain in `outputs/truckroute-locality-enriched/`. Refactor regression results are written to `output/refactor-verification/`; cached datasets are reused rather than downloaded again. Complete coordinate coverage includes geographic approximations and does not imply complete exact-station identification.

## 7. Run and maintain the pipeline

Requirements: Python 3.12–3.14, uv, Docker, curl and network access for initial downloads. DuckDB Spatial/httpfs extensions are installed during source preparation.

```sh
# Preserve an existing .env; .env.example contains the local development DB settings.
test -f .env || cp .env.example .env

# Full fresh workflow: acquire, import, prepare references, enrich, match, complete, validate.
# Use a new run directory so completed locality audit backups are preserved.
bash scripts/run_pipeline.sh fuel-prices-for-be-assessment.csv output/run-20261025
```

The single shell entry point starts PostgreSQL, installs locked dependencies, applies migrations and calls the explicit Django stages. Every source import replaces that source's canonical rows, so enrichment runs after import and before matching. Completed reference caches are reused. For changed input regions/snapshots, refresh the geographic reference with the new acquisition report; do not assume an old settlement extract covers newly added regions. The script refuses a run directory with an existing locality audit.

For a rescore of the currently enriched database, without imports or downloads:

```sh
.venv/bin/python manage.py run_geocoding_pipeline \
  --acquisition-report output/acquisition/report.json \
  --reuse-import-report output/geocoding-v5/report.json \
  --enrichment-report output/locality-enrichment/report.json \
  --output-dir output/my-rescore/poi
.venv/bin/python manage.py complete_geocoding_coverage \
  --base-run output/my-rescore/poi --output-dir output/my-rescore/final
.venv/bin/python manage.py validate_geocoding_coverage \
  --base-run output/my-rescore/poi --run output/my-rescore/final
```

Individual retained commands are `download_geocoding_sources`, `run_geocoding_pipeline` (`--import-only` separates import from matching), `prepare_geographic_reference`, `prepare_coverage_reference`, `prepare_locality_reference`, `enrich_source_localities --apply`, `validate_locality_enrichment`, `complete_geocoding_coverage` and `validate_geocoding_coverage`. `--fetch-census` on completion enables optional cached address acquisition. Never overwrite a completed locality audit: it contains the original values needed to verify the mutation.

API: `POST /api/v1/addresses/parse` accepts the input address object; `POST /api/v1/geocode` runs the same indexed matching/fallback services; `GET /api/v1/geocode/runs/<uuid>` retrieves saved evidence. POST bodies require JSON. Prepare the reference files first for geographic fallbacks. The API returns review proposals separately from accepted coordinates. The Django endpoints are a local prototype, without an external deployment/authentication flow.

### Code ownership

| Module | Responsibility |
|---|---|
| `schemas.py`, `parsing.py`, `normalization.py` | Contracts, raw-address parsing and explicit aliases. |
| `source_queries.py`, `acquisition.py`, `providers.py` | Source selection/SQL, cached concurrent transfers and the three adapters. |
| `models.py`, `ingestion.py` | PostgreSQL contracts, streaming input records and raw observation import. |
| `locality_enrichment.py` and its commands | Safe canonical locality updates, geometry joins and audit validation. |
| `scoring.py` | Name/locality/road/exit similarity, weights, gates and input conflict checks. |
| `matching.py` | Branch clusters, source rankings, highest-match selection and deviations. |
| `services.py` | Indexed candidate retrieval and persisted resolution snapshots. |
| `geography.py`, `coverage.py`, `census.py` | Verified exit references and geographic fallback/reference lookup. |
| `pipeline.py`, `completion.py`, `exports.py` | Stage orchestration, raw-observation completion and export projections. |
| `management/commands/`, `views.py` | CLI and HTTP entry points. |

Historical scoring/download implementations, live geocoding adapters, old comparison/workbook scripts and superseded documents have been removed. Django migrations remain because the existing database depends on their history. This README is the single maintained project document. The pre-refactor implementation and documents are recoverable from checkpoint commit `ac80c7b`.

### Verification

```sh
.venv/bin/python manage.py test geocoding.tests --noinput
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python manage.py check
.venv/bin/ruff check .
bash -n scripts/run_pipeline.sh
```

Unit/integration checks cover parser ambiguity, name/locality gates, branch ties, source upsert/rollback, original price preservation, inference review flags and geographic fallback semantics. Locality validation checks the pre-update snapshot, city/state containment and bounded nearest distances. Coverage validation checks every raw field, provider point/score correspondence and all 7,531 US rows against the state polygons. Canadian completion validation uses country/province metadata; no final-point polygon test is claimed. The refactor passes **119 retained tests**, Ruff, Django system/migration checks and shell syntax validation. The full-data regression found **zero differences** across 6,738 stop results (excluding generated run UUIDs), 20,214 source comparisons and all 8,151 final observations compared with the latest pre-refactor outputs.

### Source references and attribution

- AllThePlaces: <https://www.alltheplaces.xyz/>; selected source features under CC0, with spider/source URI provenance.
- Overture Places: <https://docs.overturemaps.org/guides/places/>; per-feature upstream lineage and applicable licenses retained.
- OSM regional mirror: <https://github.com/gsueur/osm-geoparquet>; original data © OpenStreetMap contributors, ODbL 1.0.
- OpenInterstate: <https://github.com/tldev/openinterstate>; signed-interstate exit reference with release/checksum metadata.
- GeoNames: <https://www.geonames.org/export/>; CC BY 4.0.
- Census TIGER/Line: <https://www2.census.gov/geo/tiger/>; prepared 2026 state/place and 2025 primary-road references.
- Census Geocoder: <https://geocoding.geo.census.gov/geocoder/>; cached address-range results.
- geoBoundaries: <https://www.geoboundaries.org/>; exact boundary metadata and licenses are retained with the downloaded reference manifests.
