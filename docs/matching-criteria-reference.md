# Matching criteria reference — brand and locality v3

Current algorithm: `brand-locality-3.1`. This retains the v3 weights and gates and adds coordinate-based locality inference: polygon-derived city values can satisfy locality; nearest-settlement city estimates require review. Explicit Census legal-name aliases support city comparison. See [the locality enrichment results](locality-enrichment-results.md) for the audit, policy and current coverage. The old `evidence-2.0` rules are retained in [the v2 archive](matching-criteria-v2-archive.md). Historical v3 output counts below refer to the original `brand-locality-3.0` run.

## 1. What changed

The score uses **only business name/brand, joint city–state–country locality, highway and scoped exit evidence**. Name has the largest weight. Provider store IDs, OPIS IDs, street numbers, streets, postcodes, brand Wikidata IDs, source count and provider confidence add **zero points**. They do not break branch ties or affect candidate retrieval priority. IDs remain in the data for provenance and joining results to the original observations.

The project was initialized in Git before these edits. Baseline commit: `068d7ab` (`Baseline existing geocoding pipeline before matching redesign`). Large cached datasets, generated outputs, dependencies and `.env` are ignored.

## 2. Common model and source mappings

All three adapters produce the same `CanonicalAddress` plus a `PlaceCandidate`. Raw strings, parsed fields, WGS84 coordinates, record identity, release, precision, license and provenance remain available for auditing.

| Common field | AllThePlaces | Overture Places | OpenStreetMap |
|---|---|---|---|
| Business name | Feature properties `name` | `names.primary` and adapter name handling | `name` tag |
| Brand | `brand`, with parser fallback to name | `brand.names.primary`, with name fallback | `brand`, with name fallback |
| City | `addr:city` | Address `locality` | `addr:city` |
| State/province | `addr:state` | Address `region` | `addr:state`; extract context can establish the state |
| Country | `addr:country` | Address `country` | `addr:country`; regional extract context can establish the country |
| Highway/exit clues | Parse available address text | Parse available address text | Parse address and supported route/exit text |
| Coordinates | Feature point geometry | Place point geometry | Node point or polygon centroid, with provenance |
| Source record identity | Spider namespace plus source feature ID | Overture record ID and release | OSM element type/ID and snapshot |

See [`providers.py`](../geocoding/providers.py) for exact adapter extraction and [`schemas.py`](../geocoding/schemas.py) for contracts. Not every source publishes every field. The matcher does not assume that a nearby address has the desired highway exit, and does not manufacture missing provider clues.

Overture and AllThePlaces are place datasets used as local candidate indexes; they are not interchangeable public text geocoding APIs. This batch uses the imported releases in its acquisition report. Live Nominatim and Overpass are not used by this batch.

## 3. Name and brand normalization

For the input and every candidate, compare both the normalized brand and the normalized raw business name. The business name excludes the `#` suffix and recognized trailing `STORE`, `LOCATION` or `NO` store-number forms. Explicit store labels are parsed separately; known IDs are not inserted into the name comparison.

Normalization removes accents and punctuation, normalizes case and spaces, and applies explicit brand aliases. Examples include `PILOT TRAVEL CENTER → PILOT`, `LOVES TRAVEL STOPS → LOVES`, `MAVERIK COUNTRY STORE → MAVERIK` and `KWIKTRIP → KWIK TRIP`. Generic descriptors such as `TRAVEL CENTER`, `TRUCK STOP` and `TRAVEL PLAZA` are removed from identity terms. Generic names such as `UNKNOWN`, `FUEL` or `GAS STATION` cannot establish a brand match on their own.

For every informative input/candidate name pair:

```text
pair_similarity = max(RapidFuzz ratio, RapidFuzz token_sort_ratio) / 100
name_similarity = maximum pair_similarity across brand and business-name combinations
```

The selected comparison terms are exported as `compared_input_name` and `compared_source_name`. A missing or inaccurate brand field can therefore be rescued by the raw business name. `KWIK TRIP` and `QUIKTRIP` are explicitly distinct brands despite their similar spelling.

The matcher conservatively removes identifiable store-number syntax. It does not blindly delete every digit from business names: digits can be part of a brand. For example, `7-Eleven` must retain its brand identity.

## 4. Joint locality requirement

City, state/province and country are one **required locality group**. All three must be present on both sides. A missing city cannot be compensated by a strong name or a matching exit.

| Component | Comparison | Requirement |
|---|---|---|
| Country | Exact canonical code after explicit aliases | Equal, recognized `US` or `CA` |
| State/province | Exact canonical subdivision code after aliases | Equal and valid for the country |
| City | Fuzzy token-sort comparison after city normalization | Similarity **≥0.90** |

Examples of equivalent values include `United States`, `USA`, `U.S.` and `US`; `Canada`, `CAN` and `CA`; `Texas`, `Tex.` and `TX`; `Ontario`, `Ont.` and `ON`. Country can be inferred from an unambiguous recognized state/province code when not supplied. Country/state comparisons are not unrestricted fuzzy comparisons because confusing states is especially damaging.

City normalization expands common tokens such as `ST → SAINT`, `FT → FORT`, `MT → MOUNT` and compass directions. Explicit aliases include `NYC → NEW YORK`. `Ft. Worth, Tex., USA` therefore matches `Fort Worth, Texas, United States`. `Tomah` does not automatically match `Tomahawk`.

```text
locality_complete = all fields present
                    AND recognized, equal state and country
                    AND city_similarity >= 0.90
locality_similarity = city_similarity if locality_complete else 0
```

If locality is incomplete or conflicts, the final candidate score is **0**, its coordinate is excluded from provider predictions, and its diagnostic record may still be shown for inspection. Equal unknown strings such as `XX / ZZ` do not satisfy locality.

## 5. Highway and exit comparisons

Road references retain network, number, jurisdiction, variant, direction and original text. Supported networks include interstate, US highway, state, county, farm-to-market, provincial, Trans-Canada and unknown highway. Main, business, alternate, bypass, spur and loop variants remain distinct.

Scoring road keys use **jurisdiction + network + normalized route number + variant**. Direction is retained as metadata and is not a scoring component. Leading numeric zeroes are normalized. A missing route jurisdiction is filled from the address country/state when unambiguous. `US Highway 12`, `US Route 12` and `US-12` become equivalent US-route references.

For either the highway set or the scoped-exit set:

```text
similarity = size(input_set ∩ candidate_set) / min(size(input_set), size(candidate_set))
```

This is a containment-style overlap score: a provider listing one of several input roads can receive full highway credit. It is not a distance score or proof of road access. If the input has a clue and the provider does not, the provider receives zero points for that criterion.

Exit identity is **highway key + exit identifier**. Exit `143` on I-94 is different from Exit `143` on I-90. An exit is linked conservatively to a preceding single route. Shared-route text such as `I-80/94, EXIT 143` leaves the numbering system unscoped and earns no scoped-exit points. The approximate fallback additionally verifies route membership against its exit reference.

## 6. Weights, formula and gates

| Criterion | Weight | When included in the denominator |
|---|---:|---|
| Fuzzy brand/business name | **60%** | Always |
| Joint city–state–country locality | **30%** | Always |
| Highways | **7%** | Input has parsed highway keys |
| Scoped exits | **3%** | Input has a highway-associated exit |

```text
W = sum(weights applicable to the input)
criterion_points[k] = 100 × weight[k] × similarity[k] / W
ungated_score = sum(criterion_points)
final_score = round(ungated_score, 3)

if name_similarity < 0.70 OR locality_complete is false:
    final_score = 0
```

The denominator depends on the input, not on which fields a provider happened to publish. This prevents missing provider fields from increasing a candidate's score. If there is no highway or exit in the input, `W=0.90`, so the effective name/locality split is two-thirds/one-third. If both clues exist, `W=1.00` and the published 60/30/7/3 split applies directly.

The heuristic is not a calibrated probability. No evidence-strength multiplier, square-root coverage penalty, provider-count bonus or hidden store-ID bonus is applied.

| Example, with highway and scoped exit in the input | Name points | Locality points | Highway points | Exit points | Final score |
|---|---:|---:|---:|---:|---:|
| Exact name/locality/roads/exit; different provider store ID | 60 | 30 | 7 | 3 | **100** |
| Exact name/locality; provider has no road/exit clues | 60 | 30 | 0 | 0 | **90** |
| Name 90%, complete locality with city 95%, exact road/exit | 54 | 28.5 | 7 | 3 | **92.5** |
| Exact name/roads/exit; wrong state or missing city | 60 | 0 | 7 | 3 | **0**, locality gate |
| Name below 70%; otherwise exact | Diagnostic points retained | 30 | 7 | 3 | **0**, name gate |

Ungated criterion contributions remain visible for rejected diagnostic candidates, together with the blocking gate. They must not be read as an eligible source match.

## 7. Candidate retrieval and eligibility

The database search is scoped to acquired provider releases, compatible country/state, and city variants meeting the same 90% comparison threshold; blank provider locality fields can enter diagnostics but cannot pass the final locality gate. A broad 50% brand/name text prefilter reduces retrieval volume before final scoring.

Each source has a 500-candidate limit. Exact canonical city then canonical brand receives retrieval priority; store IDs do not. Truncation is reported and prevents automatic acceptance. Within the scored candidates, deterministic ordering uses score, name similarity, provider namespace and source ID. Record IDs stabilize ordering and carry provenance; matching digits do not add evidence.

Closed/temporarily closed records, records with explicit zero existence confidence and non-POI coordinates cannot become final POI predictions. These are availability/precision checks, not additional matching weights. Positive provider confidence values do not change the score.

Input rows sharing an OPIS ID are checked for conflicting name, locality, highways or scoped exits. Different store IDs, postal addresses or postcodes do not create a v3 input conflict. Real conflicting input evidence forces review and disables approximate fallback. All original price observations remain preserved.

## 8. Branch ambiguity, clustering and acceptance

Each provider is scored independently, followed by the combined decision. Nearby records can form a complete-link cluster when their normalized names/locality agree and their coordinates are within 100 m. Between 100 and 250 m they also need shared highway and scoped-exit evidence. The 250 m maximum prevents a chain of points from joining distant stations. Store numbers, streets and postcodes do not merge clusters.

Automatic acceptance requires:

1. Best eligible cluster score **≥85**.
2. Name similarity **≥0.85** and complete joint locality.
3. Margin to the next distinct cluster **≥8 points**.
4. A real eligible POI coordinate, without truncated retrieval, input conflict or incomplete acquisition.
5. No conflicting high-score source points more than 1 km apart.

Candidates scoring at least 65 that fail acceptance conditions require review. Candidates below that decision threshold are unmatched. An eligible provider candidate can still be displayed with its independent status even when the combined match requires review.

The accepted combined coordinate is a real provider point selected as a medoid of the winning cluster. It is never an average of coordinates from different stations.

## 9. Highest source score and final prediction

`highest_matching_*` always comes from one eligible provider record. The score and latitude/longitude belong to that same record. Exact score ties are retained in `highest_matching_tied_sources`; alphabetical provider order chooses a deterministic display point. Ties do not prove one provider is more reliable.

The final prediction columns choose, in order:

1. Accepted combined POI coordinate.
2. Highest-scoring eligible provider POI candidate, retaining the review status.
3. Verified highway/exit approximation, when no eligible named POI exists.
4. Blank coordinates with an explicit unresolved reason.

`latitude` / `longitude` remain acceptance-controlled. `predicted_latitude` / `predicted_longitude` are broader operational candidates and must be read with `status`, `prediction_method`, `prediction_score` and `is_approximate`.

## 10. Highway/exit fallback when name matching fails

The fallback uses an actual mapped interchange point; it does not invent a fuel-station coordinate or use a city center as the station.

Reference sources are [OpenInterstate's OSM-derived corridor exits](https://github.com/tldev/openinterstate), named OSM settlements from the [OSM GeoParquet mirror](https://github.com/gsueur/osm-geoparquet), and US state polygons from [geoBoundaries](https://www.geoboundaries.org/api/current/gbOpen/USA/ADM1/). Releases, manifests, checksums, licenses and locality derivation are cached. The current prepared index contains **74,217 state-assigned exit records** and **166,748 named settlements**. These are records, not unique physical interchanges.

Required evidence:

1. Input city, valid state and country.
2. A supported interstate with a single scoped exit number.
3. A mapped exit assigned to that interstate/number and inside the correct state polygon.
4. A canonical or fuzzy ≥90% named city anchor in the same state/country, within **20 km** of the exit.
5. No equally ranked candidate interchange spread beyond **2 km**.

The nearest candidate to the best city anchor is retained. State containment is geometric; the city test is proximity to a named settlement point, not proof of municipal-boundary containment. Opposite-direction exit nodes can differ within one interchange.

Results have `status=approximate`, `prediction_method=highway_exit`, `precision=motorway_exit`, `is_approximate=true` and `fuel_station_verified=false`. The coordinate is the verified exit point, so it is an approximate location for the requested refuelling stop.

```text
geographic_score = 100 × (0.30 × city_similarity + 0.07 + 0.03) / 0.40
station_match_score = 30 × city_similarity + 7 + 3   # at most 40
```

A perfect geographic score of 100 is not a perfect business match. The reported prediction score is the station score, at most **40**, because no name evidence exists. Provider POI coordinate columns and `highest_matching_*` stay blank/zero for these fallback-only cases.

For example, the real I-94 Exit 143 point near Tomah is available at approximately `44.0157724, -90.4927721`. It verifies an interchange, not the identity or driveway of KWIK TRIP #796.

**Current exit coverage is US signed interstates.** Canadian, US/state/provincial routes and unscoped exits are not covered by this reference. They remain unresolved when no eligible named POI exists. The output records the exact reason, including missing city anchors, no nearby verified exit and ambiguous interchanges. Expanding the exit reference can add coverage without changing the scoring scheme.

## 11. Cross-source coordinate differences

For each stop with at least two eligible provider points, compute all available pairwise great-circle distances using radius `6,371,008.8 m`. Approximate exits, rejected diagnostic points and missing coordinates are excluded.

The analysis also checks US source points against the cached state polygons as a diagnostic. This can reveal a point in Kansas whose address claims Pennsylvania, even when the textual locality matches perfectly. It adds no scoring weight and does not decide which provider is ground truth. The analysis records the detected state and reports any metadata/coordinate conflicts separately. The state-reference loader normalizes the published `SU-SD` typo to `SD` for South Dakota.

| Maximum pairwise separation | Flag |
|---|---|
| No or one point | `no_coordinates` / `single_source` |
| ≤250 m | `consistent` |
| >250 m and <1,000 m | `moderate` |
| ≥1,000 m and <5,000 m | `divergent` |
| ≥5,000 m | `severe` |

If two source candidates both score at least 85 and differ by **more than 1,000 m**, add `cross_source_coordinates_diverge` and downgrade combined automatic acceptance to review. All ≥1 km differences remain visible, including those involving lower-scoring candidates. Pairwise distances and maximum distance are exported explicitly.

Large differences may reflect different branches sharing a brand/city, fuzzy-name substitutions, missing road/exit metadata, or stale/mislocated provider records. Those are possible causes, not verified attribution of error. Agreement is also not independent corroboration when sources share upstream data.

See [the actual v3 divergence analysis](coordinate-disagreement-analysis-v3.md) for counts, pair statistics and the largest cases from the completed batch.

## 12. Output files and reproducible run

Prepare the optional geographic reference once, then reuse downloaded/imported POI data:

```sh
.venv/bin/python manage.py prepare_geographic_reference \
  --acquisition-report output/acquisition/report.json
.venv/bin/python -u manage.py run_geocoding_pipeline \
  --acquisition-report output/acquisition/report.json \
  --reuse-import-report output/geocoding-v2/report.json \
  --output-dir output/geocoding-v3
.venv/bin/python scripts/brand-locality-v3/analyze.py
```

For the optional state-polygon diagnostics, run `.venv/bin/python scripts/brand-locality-v3/coordinate_states.py` before `analyze.py`.

`prepare_geographic_reference --cached-only` builds/reuses the prepared reference without network access. The main pipeline works without the reference but reports `geographic_reference_not_prepared` when fallback would be needed.

| File | Grain and contents |
|---|---|
| `source-comparison.csv` | One row per stop/provider: input/candidate names and locality, fuzzy similarities, all four point contributions, gates, independent source score/status and predicted source coordinates. Rejected coordinates have separate diagnostic columns. |
| `final-stops.csv` | One row per unique OPIS stop, final predicted coordinate/method/status, highest source score, all provider coordinates and deviation flags. |
| `fuel-prices-geocoded.csv` | All original price rows, in original order, with appended results. |
| `coordinate-disagreements.csv` | All stops with at least two source points, largest maximum deviation first. |
| `geocodes.csv` / `geocodes.jsonl` | Wide comparison and full candidate/evidence audit. |
| `per-source.jsonl` | Independent source decisions, best candidates and diagnostics. |
| `report.json` / `validation.json` | Releases, import counts, result totals, fallback coverage, disagreement counts and output-integrity checks. |

Two workbooks in `outputs/truckroute-brand-locality-v3/` present the same completed run:

- `source-matching-analysis.xlsx`: summary, all source comparison rows, ≥1 km coordinate disagreements and state-polygon diagnostics; Excel formulas expose the weighted point calculations.
- `final-geocoded-locations.xlsx`: summary, unique final stops and all original fuel-price observations.

Matching and coordinates are run snapshots. Editing spreadsheet inputs does not perform new fuzzy matching or geocoding; rerun the pipeline. Currency, price unit and observation time remain unspecified in the input, so route-cost optimization remains a later phase.

Workbook generation uses the bundled `@oai/artifact-tool` runtime. `prepare_data.py` prepares typed JSON from a completed run; `build.mjs` creates and verifies the two XLSX files. The generated outputs are intentionally excluded from Git.

## 13. Implementation and validation

| Responsibility | Implementation |
|---|---|
| Brand, locality aliases | [`normalization.py`](../geocoding/normalization.py) |
| Highway and scoped-exit parsing | [`parsing.py`](../geocoding/parsing.py) |
| Four weights, fuzzy scores, gates, clustering and distance analysis | [`matching.py`](../geocoding/matching.py) |
| Provider retrieval and persisted decisions | [`services.py`](../geocoding/services.py) |
| Verified exit fallback | [`geography.py`](../geocoding/geography.py) |
| Flat exports and prediction selection | [`exports.py`](../geocoding/exports.py) |
| Full-output validation and disagreement report | [`analyze.py`](../scripts/brand-locality-v3/analyze.py) |

Regression tests cover store-ID invariance, raw-name rescue, city/state/country aliases, missing/conflicting locality gates, exact four-factor contributions, scoped highways/exits, cross-source separation, verified approximate exits and unsupported/ambiguous fallback. Full-output validation additionally checks every original price value and row order, all unique stop/source pairs, eligible-coordinate gates, coordinate bounds, accepted-only resolved columns, highest-score ownership and independently recomputed pairwise distances.

On 24 October 2026, all **103 Django tests passed**, Ruff reported no issues, and Django reported no pending model migrations. Native workbook validation passed for **20,214 source rows, 6,738 stops and 8,151 price observations**, including the cached formulas and zero spreadsheet error cells. All worksheets were rendered and visually inspected. These are implementation/output-integrity checks; they do not replace labeled real-world location ground truth.
