# Geocoding v4 — technical summary

**Versions:** POI matching `brand-locality-3.0`; completion `coverage-4.0`. Django/PostgreSQL indexes candidates, RapidFuzz scores identity, and DuckDB Spatial queries geographic references. V4 reuses POI rankings and adds geographic fallbacks.

This describes the original v4 run. The subsequent [locality enrichment run](locality-enrichment-results.md) uses matching `brand-locality-3.1`, fills missing source city/state fields from coordinates and reports updated coverage.

## 1. Raw input → comparison schema

Input: `OPIS Truckstop ID`, `Truckstop Name`, `Address`, `City`, `State`, `Rack ID`, `Retail Price`. Values, row order and repeated observations are preserved.

Comparison projection of `CanonicalAddress`, with separate input identity:

```text
input_identity: {source: str, external_id: str, row_number: int}
address:
  raw_name: str; raw_address: str
  name: str; brand: str; store_id: str
  city: str; state: str; country: str
  house_number: str; street: str; postcode: str
  highways: [{network: enum, number: str, jurisdiction: str,
              variant: enum, direction: str, raw: str}]
  exits: [{identifier: str, highway_key: str|null, association: str, raw: str}]
```

Example: `KWIK TRIP #796`, `I-94, EXIT 143 & US-12 & SR-21`, `Tomah/WI` → brand `KWIK TRIP`, store `796`, locality `TOMAH/WI/US`, three highways, exit `143` linked to I-94.

Normalize case, punctuation, accents and brand/locality aliases; infer country from recognized state/province. Remove store-number suffixes from fuzzy name terms; ignore generic-only names. Store IDs, street fields, postcodes and Wikidata IDs are unweighted. Street fields support address fallback.

All adapters return:

```text
PlaceCandidate = {
  provider: str, namespace: str, source_id: str, release: str,
  address: CanonicalAddress, coordinate: {latitude: float, longitude: float}|null,
  precision: str, operating_status: str, existence_confidence: float|null,
  provenance: list[dict], license: str, raw: dict}
```

Coordinates are WGS84 decimal degrees. Provider record IDs identify provenance, not equivalent OPIS store identities.

## 2. Search and dataset comparison

1. **Index:** import selected fuel/place exports into `SourcePlace`, retaining release/provenance.
2. **Retrieve:** filter acquired releases, compatible country/state and city similarity ≥90%. Blank fields may enter diagnostics. Brand/business-name prefilter: ≥50% similarity.
3. **Limit:** 500 candidates/provider; prioritize canonical city, then brand. Truncation blocks automatic acceptance.
4. **Compare:** score independently per provider; cluster/rank across sources using the same criteria.
5. **Complete:** reparse raw observations, validate geography and apply fallback. Resolve conflicting observations individually.

Downloads, search keys, geographic indexes and Census responses are cached. Missing city fields fail POI eligibility; municipality-polygon enrichment remains a future improvement.

## 3. Scoring, ranking and selection

| Criterion | Weight | Similarity |
|---|---:|---|
| Brand/business name | 60% | Maximum `ratio`/`token_sort_ratio` across normalized brand/business-name pairs |
| Joint locality | 30% | Normalized city `token_sort_ratio`, when city/state/country all pass |
| Highway | 7% | Set overlap on jurisdiction + network + route number + variant |
| Scoped exit | 3% | Set overlap on highway key + exit identifier |

Road/exit overlap is `intersection_size / min(input_size, candidate_size)`; missing provider evidence earns zero.

```text
W = 0.60 + 0.30 + applicable input highway/exit weights
points[k] = 100 × weight[k] × similarity[k] / W
score = round(sum(points), 3)
```

**Gates:** name ≥70%, city ≥90%, and complete locality with equal, recognized state/country codes. Failure sets score to zero. Name/locality always enter `W`; road/exit weights require input evidence. Scores are heuristics; no provider bonus applies.

Sort by descending score/name similarity, then source identity. Closed, zero-existence-confidence and non-POI records cannot supply eligible POI predictions.

Complete-link clusters require name ≥90% and matching locality: within 100 m, or 100–250 m with shared highway **and** scoped exit. Accepted coordinates use a provider-point medoid.

**Accept:** score ≥85, name ≥85%, next-cluster margin ≥8, eligible coordinate and no blocking acquisition/truncation/input conflict. Scores ≥65 failing acceptance require review. Highest-score ties are recorded; alphabetical provider order selects the display point.

Haversine source separation ≥1 km is flagged; ≥5 km is severe. Two candidates scoring ≥85 and separated by >1 km block combined acceptance.

## 4. V4 fallback policies

Retain valid existing POI/review candidates or prior exit approximations. US points require state containment; invalid points trigger another provider or fallback. Canadian province checks use metadata.

| Order | Method / precision | Policy |
|---:|---|---|
| 1 | `census_address` / `address_range` | US structured street address; validate house number, city/state and state containment. Interpolated address position. |
| 2 | `highway_exit` / `motorway_exit` | OpenInterstate/OSM interstate + exit, correct state, city anchor ≤20 km. Normalize exit letters; label inferred shared-route associations. Reject equally ranked interchange spread >2 km. |
| 3 | `highway_near_city` / `road_segment` | Matching Census TIGER route point, inside state, city anchor ≤20 km. Unverified exit: `Exit Matched=false`. |
| 4 | `named_locality` / `locality` | OSM/GeoNames aliases: compact exact match, otherwise fuzzy ≥90%, within country/state. Rank by similarity, settlement class, population. Flag anchor spread >20 km. |
| 5 | `administrative_area` / `state_or_province` | Known administrative representative; otherwise unresolved. Unused in this dataset. |

Fallback index projections:

```text
Exit: (country, state, route, exit_number) → {exit_id, lat, lon}
Settlement: (country, state, normalized_alias) → {id, name, point, population}
Road: (network, number, variant) → {id, name, geometry}
Census: address_hash → {matched_address, coordinate, city, state, country, benchmark}
```

Fallbacks require review and have POI `Match Score=0` in the completion CSV. Provider-specific coordinates remain unchanged. Road/exit reference coverage is incomplete, particularly for Canadian exits.

## 5. Output and observed coverage

Output preserves raw columns and appends coordinates, method, precision, source, match score/status, approximation/review flags, reference IDs, exit association, geographic checks and parsing/conflict flags.

Coverage: **8,151/8,151 rows; 6,738 stops**—4,755 POI candidates, 717 exits, 451 highway points, 2 interpolated addresses, 813 locality points. All 7,531 US rows passed state containment. Locality points remain coarse approximations.

Implementation: [schemas](../geocoding/schemas.py), [adapters](../geocoding/providers.py), [retrieval](../geocoding/services.py), [matching](../geocoding/matching.py), [fallbacks](../geocoding/coverage.py), [completion command](../geocoding/management/commands/complete_geocoding_coverage.py). Detailed investigation: [coverage comparison](coverage-comparison-and-fallbacks.md).

## 6. Three-source schema and comparison

Paths describe imported snapshots. Counts cover this US/Canada subset; eligible POI predictions are pre-fallback and may require review.

| Attribute | AllThePlaces | Overture Places | OpenStreetMap |
|---|---|---|---|
| Import representation | GeoJSON Feature/NDJSON | GeoParquet → GeoJSON/NDJSON | Regional GeoParquet → OSM element JSON |
| Identity | Feature `id` + `properties.@spider` | Place `id`; namespace `places` | Element `type` + `id` |
| Name / brand | `properties.name`, `.brand`; name/operator fallback | `names.primary`, `brand.names.primary` | `tags.name`, `.brand`; name/operator fallback |
| Street address | `addr:street_address`, or house number + street, or `addr:full` | `addresses[0].freeform` | Same tagged-address mapping as ATP |
| City | `properties["addr:city"]` | `addresses[0].locality` | `tags["addr:city"]` |
| State / country | `addr:state`, `addr:country` | `addresses[0].region`, `.country` | Address tags; known regional context can supply missing state/country |
| Unweighted metadata | `ref:store`, `store_number`, `brand:wikidata`, `addr:postcode` | `brand.wikidata`, `addresses[0].postcode` | `ref:store`, `store_number`, `brand:wikidata`, `addr:postcode` |
| Highway / exit | Parsed available address text | Parsed `freeform` address | Parsed available address text |
| Coordinate | Feature Point `[longitude, latitude]` | Place Point geometry | Node `lat/lon`; polygon-derived `center.lat/lon` |
| Availability metadata | `end_date` | `operating_status`, `confidence`; positive confidence adds no points | `amenity=fuel`; disused/abandoned fuel tags exclude closed records |
| POI ranking | Common 60/30/7/3 formula and gates | Common 60/30/7/3 formula and gates | Common 60/30/7/3 formula and gates |
| Imported records | 111,440 | 182,986 | 121,829 |
| Eligible POI predictions | 3,575 | 3,360 | 1,399 |
| Snapshot | 2026-10-10-13-32-41 | 2026-10-21.0 | Mirror 2026-10-23; PBF 2026-10-20 |
| Main implemented limitation | Selected spider coverage; locality fields may be absent | Only first address currently compared; locality may be incomplete | Missing city tags reduce eligibility; polygon centers are not entrances |
