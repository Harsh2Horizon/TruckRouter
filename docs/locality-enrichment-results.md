# Coordinate-based locality enrichment — results

Run: 25 October 2026 (Asia/Kolkata). Matching: `brand-locality-3.1`; enrichment: `locality-enrichment-1.0`; geographic completion: `coverage-4.0`.

## Missing-field audit and fills

Audited all **416,255 imported POIs**. Counts below describe the canonical fields used by candidate retrieval and scoring, before and after enrichment.

| Source | Imported POIs | Missing city: before → after | Missing state: before → after | Cities filled | States filled |
|---|---:|---:|---:|---:|---:|
| AllThePlaces | 111,440 | 2,451 → 66 | 86 → 82 | 2,385 | 4 |
| Overture | 182,986 | 970 → 13 | 345 → 1 | 957 | 344 |
| OpenStreetMap | 121,829 | 80,459 → 106 | 0 → 0 | 80,353 | 0 |
| **Total** | **416,255** | **83,880 → 185** | **431 → 83** | **83,695** | **348** |

The original OSM payloads lack `addr:state` on **88,975** records. Their indexed state had already been assigned from the regional extract during import, so those were not newly filled in this run. Native city/state tag omissions for ATP and Overture equal their “before” counts above. Provider payloads remain immutable; inferred labels are stored in `SourcePlace.address` and its `city_key`, `state`, `country` indexes.

## Mapping policy

1. Use the POI's existing WGS84 longitude/latitude. Fill only empty locality fields; preserve known provider values and reject conflicting country/state evidence.
2. Resolve state/province by point containment (`ST_Covers`) in [US Census TIGER/Line 2026 state boundaries](https://www2.census.gov/geo/tiger/TIGER2026/STATE/) or [geoBoundaries CAN ADM1 province boundaries](https://www.geoboundaries.org/api/current/gbOpen/CAN/ADM1/) (2023 source update). The reference contains 69 administrative regions.
3. Resolve US city by containment in **32,071 municipality/CDP polygons** from [Census TIGER/Line 2026 places](https://www2.census.gov/geo/tiger/TIGER2026/PLACE/). This filled **63,908 cities**. A Census place name can differ from a postal address city; it is spatial evidence, not a verified mailing address.
4. Where no unique city polygon resolves the point, choose the nearest named populated settlement within **10 km**, in the same verified state/province and country, using the cached [GeoNames gazetteer](https://www.geonames.org/export/) and OSM settlement extract. This filled **19,787 cities**, including Canadian cities because this run has Canadian province polygons only. These assignments carry `city_from_nearest_settlement_requires_review` and cannot independently produce an automatically accepted match.
5. Record reference ID, source, method, original values and nearest-settlement distance/reference coordinates in `provenance`. Station coordinates are never replaced with city-center coordinates during enrichment.

Matching still uses name 60%, joint city/state/country 30%, highways 7%, scoped exits 3%. Store IDs remain unweighted. Explicit aliases normalize Census legal names such as `Indianapolis city (balance)` to `Indianapolis`; ordinary names such as `Kansas City` are preserved. Exports expose `city_method` as `provider`, `point_in_polygon` or `nearest_settlement`.

## POI matching improvement

Same source releases, record counts and **6,738 unique input stops** in both runs. “POI hit” means an eligible source point is available, including candidates requiring review; it does not mean every hit is an accepted or independently verified fuel station.

| Source | Input stops with POI point: before | After | Additional stops | Distinct matched source records after |
|---|---:|---:|---:|---:|
| AllThePlaces | 3,575 | 3,659 | +84 | 3,378 |
| Overture | 3,360 | 3,385 | +25 | 3,068 |
| OpenStreetMap | 1,399 | 2,694 | +1,295 | 2,388 |
| **At least one source** | **4,758** | **4,970** | **+212** | — |

No previously covered input stop lost all POI coverage; no provider lost coordinate availability for a previously covered stop. Provider gains overlap, so they must not be summed to obtain additional unique input coverage. Combined matching decisions are **2,696 accepted**, **2,291 requiring review**, and **1,751 approximate**; availability and acceptance are separate metrics.

### Why missing city tags mattered

The earlier retrieval query already admitted empty cities as diagnostic candidates. However, scoring required city, state and country together, so missing-city candidates received score zero and could not supply a provider prediction. Broad state-wide retrieval also consumed candidate capacity. Enrichment makes these candidates eligible when their inferred locality and business name match, and removes unrelated places from city searches.

| Source | Candidate rows returned across all stop queries: before → after | Queries hitting the 500-candidate cap: before → after |
|---|---:|---:|
| AllThePlaces | 28,647 → 17,945 | 0 → 0 |
| Overture | 19,727 → 16,040 | 0 → 0 |
| OpenStreetMap | 438,564 → 10,772 | 74 → 0 |

These row counts include repeated retrievals of the same POI for different inputs. Fewer returned rows reflect more selective locality filtering while usable POI coverage increases. Missing city was a major OSM blocker, but filling it does not establish a business identity: **20,853 OSM records have neither an informative name nor brand**. Other remaining failures include absent stations, name differences, and locality conflicts.

## Remaining missing fields and final coverage

**234 source records** retain at least one missing field: 34 lack coordinates, 92 do not resolve within their claimed region or supported boundary coverage, and 108 have no suitable locality within 10 km. They account for 185 empty cities and 83 empty states, with overlap. These are exported for correction rather than assigned unsupported locality values.

The completed output preserves every raw field and price for **8,151/8,151 rows**:

| Coordinate method | Raw rows | Unique stops |
|---|---:|---:|
| Source POI | 6,132 | 4,967 |
| Verified highway exit | 742 | 644 |
| Highway near city | 432 | 395 |
| Named locality | 842 | 731 |
| Census address interpolation | 3 | 1 |
| **Total** | **8,151** | **6,738** |

Final POI coverage is three stops lower than independent source availability because the completion stage handles conflicting raw inputs and geographically inconsistent points separately. Approximate methods have zero POI match score and require review. All 7,531 US rows passed state-polygon containment; Canadian completion points were checked against country/province metadata, without a polygon test. Complete coordinate coverage does not imply exact station identification.

## Files and verification

Deliverables: [`outputs/truckroute-locality-enriched/`](../outputs/truckroute-locality-enriched/).

- `source-poi-comparison.csv`: 20,214 rows, one per input stop/source, with criteria, scores, source coordinates and city inference method.
- `all-records-geocoded.csv`: all 8,151 original rows with final coordinates, method, precision and review flags.
- `final-stops.csv`: 6,738 unique input stops with final coordinates.
- `poi-coverage-before-after.csv`: per-stop/source coordinate availability and matching transitions.
- `source-locality-changes.csv`: 83,984 modified source records with original/new city/state and inference method.
- `remaining-missing-localities.csv`: 234 unresolved locality records and reasons.
- `comparison-report.json`, `locality-validation.json`, `coverage-validation.json`: exact metrics and validation results.

Validation compared all **84,212** originally incomplete records against the saved snapshot: no coordinate changes, no known locality overwrites, and no changes to other address fields. Independently checked 63,908 city polygon assignments, 348 state polygon assignments and 19,787 nearest-settlement distances. Native tag omission counts and source record counts stayed unchanged. The Django suite passes **126 tests**; Ruff and migration checks pass.

## Reproduce

The current enrichment has already been applied. Its backup and completed audit must be preserved. For a fresh import, prepare references once and apply enrichment into a new audit directory before matching:

```sh
.venv/bin/python manage.py prepare_locality_reference
.venv/bin/python manage.py enrich_source_localities --apply \
  --output-dir output/locality-enrichment-NEW
.venv/bin/python manage.py validate_locality_enrichment \
  --run output/locality-enrichment-NEW
.venv/bin/python manage.py run_geocoding_pipeline \
  --acquisition-report output/acquisition/report.json \
  --reuse-import-report output/geocoding-v3/report.json \
  --enrichment-report output/locality-enrichment-NEW/report.json \
  --output-dir output/geocoding-enriched-NEW
.venv/bin/python manage.py complete_geocoding_coverage \
  --base-run output/geocoding-enriched-NEW \
  --output-dir output/geocoding-enriched-complete-NEW
.venv/bin/python manage.py validate_geocoding_coverage \
  --run output/geocoding-enriched-complete-NEW \
  --base-run output/geocoding-enriched-NEW
```

For the completed current run, `.venv/bin/python manage.py analyze_locality_enrichment` regenerates the comparison and delivery files from saved results. Reuse requires identical imported source releases and counts. A fresh source import replaces canonical records, so locality enrichment must be applied again after import. City boundaries and nearest settlements describe administrative/approximate locality; no live public reverse-geocoder is called per POI.
