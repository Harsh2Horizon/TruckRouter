# Coordinate disagreement analysis — scoring v3

Completed 2026-10-24T16:22:49.591685+00:00. Algorithm: `brand-locality-3.0`.

The run covers **6,738 stops** and preserves **8,151 original price rows**. Results: 2,685 accepted, 2,091 needs_review, 1,277 unmatched, 685 approximate.

## What is compared

Distances are WGS84 great-circle distances between each source's best eligible fuel POI candidate. A point can still require branch review. Missing coordinates are excluded; diagnostic rejected records and approximate highway exits are excluded. No coordinates are averaged.

**2,761 stops** have at least two source points. **986** have a maximum separation of at least 1 km; **576** reach 5 km. **885** have a separation over 1 km between two candidates scoring at least 85, which prevents combined automatic acceptance.

| Source pair | Compared stops | Median m | P90 m | P95 m | Maximum km | ≥1 km | ≥5 km |
|---|---:|---:|---:|---:|---:|---:|---:|
| atp_overture | 2,351 | 47.982 | 8655.469 | 12397.689 | 1852.20 | 753 | 394 |
| atp_osm | 1,033 | 62.612 | 10091.105 | 13765.738 | 43.70 | 413 | 254 |
| overture_osm | 1,007 | 1305.822 | 11896.09 | 17647.021 | 1852.17 | 513 | 290 |

## Largest differences

| OPIS ID | Input name | City / state | Maximum km | ATP score | Overture score | OSM score |
|---|---|---|---:|---:|---:|---:|
| 65522 | TURKEY HILL #950324 | EAST EARL, PA | 1852.20 | 92.784 | 92.784 | 92.784 |
| 72764 | CUMBERLAND FARMS #004783 | EAST WINDSOR, CT | 920.46 | 92.784 | 92.784 | 0.0 |
| 69193 | Certified Oil #400305 | NEW MARTINSVILLE, WV | 892.62 | 81.537 | 92.784 | 0.0 |
| 1596 | FLYING J #305 | JAMESTOWN, NM | 192.71 | 90.0 | 90.0 | 0.0 |
| 64352 | CHEVRON #383461 | FORT STOCKTON, TX | 58.74 | 90.0 | 97.0 | 0.0 |
| 71714 | RACETRAC #2576 | MIAMI, FL | 49.31 | 92.784 | 92.784 | 0.0 |
| 70590 | CIRCLE K #2722291 | SAINT AUGUSTINE, FL | 48.29 | 100.0 | 92.784 | 92.784 |
| 970 | CIRCLE K #2702885 | PHOENIX, AZ | 47.46 | 90.0 | 90.0 | 90.0 |
| 66424 | CIRCLE K #2741652 | PHOENIX, AZ | 47.46 | 90.0 | 90.0 | 90.0 |
| 72947 | Circle K #9556 | PHOENIX, AZ | 47.46 | 92.784 | 92.784 | 92.784 |
| 72347 | MARATHON | ALBION, MI | 43.70 | 90.0 | 90.0 | 90.0 |
| 72939 | CIRCLE K #2655872 | ATLANTA, GA | 39.12 | 73.636 | 90.0 | 90.0 |
| 70617 | QUIKTRIP #224 | KANSAS CITY, MO | 36.03 | 90.0 | 90.0 | 90.0 |
| 3585 | CHEVRON | LOVELOCK, NV | 35.71 | 90.0 | 90.0 | 90.0 |
| 64408 | QUIKTRIP #4058 | SAN ANTONIO, TX | 35.45 | 90.0 | 90.0 | 90.0 |

## How to interpret the flagged cases

A large distance does not establish which source is wrong. Brand and city can identify several branches, especially when provider records omit highway and exit clues. The new policy deliberately removes store IDs and street/postal evidence, so those fields cannot break such ties. The likely branch ambiguity below is an inference from the matching evidence, not a verified error attribution.

An additional geometric check compares US source points against the cached geoBoundaries state polygons. This is a diagnostic, not another score component. It checked 8,168 points and flagged 3 outside their reported state. Small border differences can reflect boundary precision; the extreme cases below provide stronger evidence of coordinate/metadata inconsistency. This does not verify a fuel-station entrance.

### TURKEY HILL #950324 — EAST EARL, PA (OPIS 65522)

Input road/exit clues: `US-322 & SR-23/SR-897`. Maximum separation: **1852.20 km**. Combined status: `needs_review`.

- alltheplaces: Turkey Hill; (40.114353, -76.031843); score 92.784; name 100.0%; city 100.0%; highway 0.0; exit not applicable; source address `168 Toddy Drive`; record `kUleWGDWs7urU_qK5EYxfstVX14=`.
- overture: Turkey Hill; (38.8085695, -97.593943); score 92.784; name 100.0%; city 100.0%; highway 0.0; exit not applicable; source address `168 Toddy Dr`; record `3de20528-6e86-4e15-a798-906b9a7a8bfd`.
  - Geometric diagnostic: this point lies in **KS**, while its source address reports **PA**. The coordinate contradicts the source locality metadata in this reference.
- osm: Turkey Hill; (40.11447340355561, -76.03220282096143); score 92.784; name 100.0%; city 100.0%; highway 0.0; exit not applicable; source address `168 Toddy Drive`; record `1392249066`.

Review the source records and road/exit access before assigning a unique branch. The highest-score point remains a candidate with its review status.

### CUMBERLAND FARMS #004783 — EAST WINDSOR, CT (OPIS 72764)

Input road/exit clues: `US-5`. Maximum separation: **920.46 km**. Combined status: `needs_review`.

- alltheplaces: Cumberland Farms; (41.911983, -72.612692); score 92.784; name 100.0%; city 100.0%; highway 0.0; exit not applicable; source address `9 SOUTH MAIN ST`; record `-TIPSEpedVI7jwrJ5kaJ8ClhYyI=`.
- overture: Cumberland Farms; (39.9454263, -83.263804); score 92.784; name 100.0%; city 100.0%; highway 0.0; exit not applicable; source address `9 S Main St`; record `f27d40a9-5020-4792-b401-a107664abc73`.
  - Geometric diagnostic: this point lies in **OH**, while its source address reports **CT**. The coordinate contradicts the source locality metadata in this reference.

Review the source records and road/exit access before assigning a unique branch. The highest-score point remains a candidate with its review status.

### Certified Oil #400305 — NEW MARTINSVILLE, WV (OPIS 69193)

Input road/exit clues: `SR-2 & SR-7`. Maximum separation: **892.62 km**. Combined status: `needs_review`.

- alltheplaces: Certified; (39.656593, -80.857409); score 81.537; name 81.8%; city 100.0%; highway 0.0; exit not applicable; source address `167 3rd ST`; record `kU2Pw2m3ZBaHj_W8bIe0wPFG_r4=`.
- overture: Certified Oil; (41.9515587, -70.686988); score 92.784; name 100.0%; city 100.0%; highway 0.0; exit not applicable; source address `166 3rd St`; record `c22ad956-d783-4fec-a0f9-36a4641a29c7`.
  - Geometric diagnostic: this point lies in **MA**, while its source address reports **WV**. The coordinate contradicts the source locality metadata in this reference.

Review the source records and road/exit access before assigning a unique branch. The highest-score point remains a candidate with its review status.

### FLYING J #305 — JAMESTOWN, NM (OPIS 1596)

Input road/exit clues: `I-40, EXIT 39`. Maximum separation: **192.71 km**. Combined status: `needs_review`.

- alltheplaces: Flying J Travel Center; (35.4806895, -108.432612); score 90.0; name 100.0%; city 100.0%; highway 0.0; exit 0.0; source address `1 Giant Crossing`; record `QAyFqDckdZlvm8RZys7Jbm5DBOw=`.
- overture: Flying J Travel Center; (35.1660962, -106.3436801); score 90.0; name 100.0%; city 100.0%; highway 0.0; exit 0.0; source address `1 Giant Xing`; record `687fbbfe-a412-4be5-8704-79e9c06a63da`.

Review the source records and road/exit access before assigning a unique branch. The highest-score point remains a candidate with its review status.

### CHEVRON #383461 — FORT STOCKTON, TX (OPIS 64352)

Input road/exit clues: `I-10, EXIT 259`. Maximum separation: **58.74 km**. Combined status: `needs_review`.

- alltheplaces: Chevron; (30.8938962891622, -102.917113747885); score 90.0; name 100.0%; city 100.0%; highway 0.0; exit 0.0; source address `3301 W Dickinson Blvd`; record `8mz4lwrvglZZkIkgfEU5lyAo7rQ=`.
- overture: Chevron; (30.88236506, -102.30173365); score 97.0; name 100.0%; city 100.0%; highway 1.0; exit 0.0; source address `I 10 @ Fm 11`; record `4608971a-3489-4cb5-b8a6-59f51f2616ba`.

Review the source records and road/exit access before assigning a unique branch. The highest-score point remains a candidate with its review status.

## Approximate fallback and remaining coverage

Prediction methods: `{"fuel_poi": 4758, "highway_exit": 685, "unresolved": 1295}`.

Fallback uses real OpenInterstate/OSM interstate-exit points with route membership, state polygon containment and a named city anchor within 20 km. It marks the station location approximate. It currently covers US signed interstates; Canadian, state-route and other unsupported exits remain unresolved when no eligible named POI exists. Ambiguous interchanges are also left unresolved.

Fallback reason counts:

| Reason | Stops |
|---|---:|
| ambiguous_highway_exit_locations | 24 |
| city_anchor_not_found | 19 |
| input_records_disagree | 17 |
| name_and_locality_candidate_found | 4,758 |
| no_supported_highway_with_scoped_exit | 1,105 |
| no_verified_exit_coverage_for_country | 20 |
| no_verified_highway_exit_near_named_city | 110 |
| verified_exit_point_approximate_station | 685 |

## Files and validation

- `output/geocoding-v3/source-comparison.csv`: all three sources, all criterion scores, accepted and rejected-candidate diagnostics.
- `output/geocoding-v3/final-stops.csv`: one final prediction per stop, statuses and source coordinates.
- `output/geocoding-v3/coordinate-disagreements.csv`: all stops with at least two source points, largest differences first.
- `output/geocoding-v3/validation.json`: coverage, preserved prices, coordinate bounds, score contributions and independently recomputed distances.
- `outputs/truckroute-brand-locality-v3/source-matching-analysis.xlsx`: matching criteria and flagged coordinate differences.
- `outputs/truckroute-brand-locality-v3/final-geocoded-locations.xlsx`: distinct stops and original price observations.

The validation script passed for every output row. Exact coordinates still require external ground truth; agreement between sources is not independent corroboration when datasets share upstream lineage.

References: [OpenInterstate](https://github.com/tldev/openinterstate), [OSM mirror](https://github.com/gsueur/osm-geoparquet), [Overture Places](https://docs.overturemaps.org/guides/places/), [AllThePlaces](https://www.alltheplaces.xyz/).

Both exported XLSX workbooks also passed read-only verification against the CSVs: 20,214 source rows, 6,738 final stops and 8,151 price observations. Native ZIP integrity, formulas, cached criterion scores, source coordinates, summaries and absence of spreadsheet error cells were checked. Every worksheet was rendered and visually inspected.
