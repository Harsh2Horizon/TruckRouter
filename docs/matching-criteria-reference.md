# Truck refuelling location matching criteria

Implementation reference for the Django geocoding pipeline

Version `evidence-2.0` | Reviewed 24 October 2026 | Scope US and Canada

This document explains how an input truck stop is matched to Overture Places, AllThePlaces and OpenStreetMap, how records from those sources are grouped as the same physical place, and how the resulting coordinates are selected. It describes the implemented Python code and the completed v2 run. The same numerical scoring policy applies to all three sources; source differences arise from their available fields, identifiers and coordinate types.

The pipeline distinguishes a best available candidate from an accepted location. A source can return a coordinate while its status remains `needs_review` or `unmatched`. The highest score identifies the strongest available scored record, and does not establish that its position belongs to the requested branch. Scores are evidence heuristics from 0 to 100, not calibrated probabilities of correctness.

## 1 The three matching operations

| Operation | What is compared | Result |
|---|---|---|
| Input to each source | The canonical input address against candidate addresses from that source | Field similarities, evidence coverage, score and a source decision |
| Record to record | Eligible candidate records against other candidates, including records from different sources | Spatial and identity clusters representing possible physical stations |
| Source to source selection | Each source's best eligible geocoded candidate score | Highest score, source, original provider coordinate and explicit score ties |

Input scoring uses brand, business name, store ID, city, state, country, postcode, street, house number, highway references and scoped exits. Record clustering additionally uses the distance between provider coordinates. Input-to-source scoring does not use a distance to the input because this feed supplies textual locations rather than known input coordinates.

The pipeline does not numerically compare OPIS IDs to GERS IDs, ATP feature IDs or OSM element IDs. Those identifiers belong to different namespaces. They identify and audit records; only an explicitly interpreted public store number contributes store identity evidence.

## 2 Source acquisition and eligibility

The current dataset contains 8,151 price observations for 6,738 distinct OPIS stops. Acquisition covers the US and Canada, with OSM files limited to the 57 states and provinces represented in the input.

| Source | Current acquired scope | Stored identity | Source-specific restrictions |
|---|---|---|---|
| Overture Places | Release `2026-10-21.0`; six relevant spatial Parquet files, filtered to US/CA fuel categories or input brands | Provider `overture`, namespace `places`, place ID and release | First address only; existence confidence is separate from match confidence |
| AllThePlaces | Published run `2026-10-10-13-32-41`; 69 selected spiders, filtered by country and fuel/input-brand relevance | Provider `alltheplaces`, spider namespace, feature ID and run | Selected-spider coverage; generic `ref` is opaque by default |
| OpenStreetMap | GeoParquet mirror snapshot `2026-10-23`, with underlying OSM timestamp `2026-10-20T20:23:47Z` | Provider `osm`, node/way/relation namespace, element ID and snapshot | Current acquisition selects `amenity=fuel` from POI and amenity polygon themes |

ATP spider discovery uses exact normalized spider names, explicit aliases and a sample of the first feature in each cached spider file. It does not claim an exhaustive inventory of every fuel-related spider. Overture's country filter uses the first address country. OSM acquisition preserves original IDs and tags, verifies published file checksums, and deduplicates border objects by element type and ID. These are acquisition rules, not additional score components.

The current OSM transport is a published mirror, rather than a fresh Overpass query. A separate Overpass loader exists. The self-hosted Nominatim adapter is optional and is outside the three-source v2 comparison.

## 3 How source fields become comparable

All adapters produce `CanonicalAddress` and `PlaceCandidate`. These contracts separate identity and address evidence from coordinates, lifecycle, provenance and licensing. The table records what the current adapters read, rather than every field an upstream dataset might support.

| Canonical field | Overture Places | AllThePlaces properties | OpenStreetMap tags |
|---|---|---|---|
| Business name | `names.primary`; brand primary name fallback | `name`, then `brand`, then `operator` | `name`, then `brand`, then `operator` |
| Brand | `brand.names.primary`; parsed business name fallback | `brand`; parsed name fallback | `brand`; parsed name fallback |
| Brand Wikidata | `brand.wikidata` | `brand:wikidata` | `brand:wikidata` |
| Store ID | Explicit marker in the selected name | `ref:store`, then `store_number`, then parsed name; verified `ref` opt-in | `ref:store`, then `store_number`, then parsed name; generic `ref` excluded |
| Street and house number | Parse `addresses[0].freeform` | `addr:street_address`, or house number plus `addr:street`; `addr:full` fallback | Same tagged-address rules as ATP |
| City | `addresses[0].locality` | `addr:city` | `addr:city` |
| State or province | `addresses[0].region` | `addr:state` | `addr:state`; mirror region context can fill a blank |
| Country | `addresses[0].country` | `addr:country`; recognized subdivision inference | `addr:country`; subdivision or acquisition-region inference |
| Postcode | `addresses[0].postcode` | `addr:postcode` | `addr:postcode` |
| Highways and exits | Parse selected freeform address | Parse selected address text | Parse selected address text |
| Coordinate | Point geometry in longitude/latitude order | Point geometry in longitude/latitude order | Node latitude/longitude, or explicit center for a way/relation |

### Overture interpretation

Only the first address in `addresses` is scored. Other addresses, alternate names and upstream source records remain available in the raw payload but are not expanded into separate matching alternatives. The adapter does not infer a store number from a GERS ID, phone number or source record ID.

`confidence` measures whether the place exists. A value of zero makes a candidate ineligible. Other values do not multiply or increase the matching score. `operating_status` is used for lifecycle eligibility; the scorer excludes `closed`, `temporarily_closed` and `permanently_closed`. Unknown status is allowed. Category and taxonomy fields help acquisition but do not contribute score points. This distinction is consistent with the [Overture place schema](https://docs.overturemaps.org/schema/reference/places/place/).

### AllThePlaces interpretation

A feature must have an ID and an `@spider` namespace. The adapter retains `@source_uri` and `ref` in provenance. A generic numeric `ref` is not automatically a store number. A caller may enable `ref_is_store_id` for a source whose semantics have been verified; the normal three-source pipeline leaves that option disabled.

Consequently, input store `796` can remain uncorroborated even if a spider has a numeric `ref`. This lowers available evidence and blocks automatic branch acceptance unless full street identity establishes the branch. ATP's [data format documentation](https://github.com/alltheplaces/alltheplaces/blob/master/DATA_FORMAT.md) explains its feature identity and properties; the store-number interpretation here is our adapter policy.

For lifecycle, `end_date=yes` or an end date on or before the current date marks a place permanently closed. A future end date is treated as open, and an absent or unrecognized value as unknown. A null geometry is retained as an unlocated record; it cannot provide a source comparison coordinate.

### OpenStreetMap interpretation

OSM identity requires element type and ID. The adapter uses a node's explicit coordinates or a way/relation center. In the current mirror export, centers are calculated with `ST_Centroid`, and their derivation is retained in query context. A node is `poi` precision and a fuel way/relation center is `poi_center` precision. A non-fuel element has unknown precision and is excluded from matched-coordinate selection.

The adapter can fill a missing country from the acquisition region and a missing state from mirror context when its country agrees with the address country. It records warnings for this inferred context. It does not fill a missing city, postcode or store ID from the input, and it does not perform reverse geocoding. The current tagged-address path reads `addr:city` directly; town/village fallbacks belong to the optional Nominatim adapter.

The selected extraction includes active `amenity=fuel` objects. If lifecycle tags reach the adapter, `disused:amenity=fuel` or `abandoned:amenity=fuel` marks them permanently closed. Fuel types, HGV access and opening hours are retained when available but are not matching score components. See the [OSM fuel tag definition](https://wiki.openstreetmap.org/wiki/Tag:amenity=fuel) and [mirror implementation](https://github.com/gsueur/osm-geoparquet).

## 4 Normalization before comparison

Text keys remove accents, convert to uppercase and collapse punctuation to spaces. Brand keys also remove apostrophes and apply explicit aliases, including Pilot Travel Center to Pilot, Flying J Travel Plaza to Flying J, and Maverik Country Store to Maverik. Kwik Trip and QuikTrip have an explicit distinction and do not become the same brand.

Business-name scoring removes everything after the first `#`, and recognized trailing `STORE` or `LOCATION` number tokens. This prevents an input's store number being penalized twice through the name and store fields. The raw name is retained. If an explicit brand is absent, the parser's business-prefix interpretation may serve as the brand; it is not a separately verified brand assertion.

Store IDs remain strings. Comparison removes whitespace and a leading marker, uppercases letters, and normalizes purely numeric leading zeros, so `00796` and `796` compare equal. Internal punctuation in alphanumeric IDs remains significant: `AB-C` and `A-BC` differ.

Recognized US/Canadian subdivisions are normalized to codes. The tagged ATP/OSM adapters also normalize country aliases such as `USA` to `US`. Street comparison standardizes common suffixes and directions, including Street/St, Road/Rd, Avenue/Ave and North/N. Postcodes are uppercased with spaces removed, then compared exactly; the current matcher does not implement ZIP-prefix equivalence.

A highway key combines jurisdiction, network, number and variant. Interstate 94 differs from US Highway 94; a Wisconsin state route differs from a Minnesota route with the same number. Direction is retained but is not part of the scored highway key. `I-80/94` expands to two references. A following exit with no unique numbering highway remains unassociated and is excluded from exit scoring. Exit associations inferred from text have not been verified against a road graph.

## 5 Candidate retrieval before scoring

Retrieval narrows the database pool; scoring then evaluates the records that remain. These two stages use different thresholds.

| Retrieval rule | Implemented behavior |
|---|---|
| Release scope | Use only provider/release pairs from the acquired run |
| Country and state | If the input has a value, keep exact matches and blank source values; exclude known other values |
| City | Keep blank source cities or city similarity at least 0.70; an exact store ID can bypass this city filter |
| Brand | Keep blank source brands or normalized brand similarity at least 0.50 |
| Priority | Exact store ID first; then exact city, other admitted city, blank city; then exact canonical brand; then database key |
| Limit | Up to 500 candidates per provider, rather than 500 shared across all providers |
| Truncation | A 501st admitted record marks that provider's search as truncated |

An exact store ID does not bypass known country/state disagreement or the brand filter. The retrieval limits mean a match is the best found within the searched scope, rather than a proof that no alternative exists anywhere in the complete source dataset. A truncated provider cannot independently auto-accept; another provider with a complete search retains its own decision status. Any truncated provider blocks combined automatic acceptance.

## 6 Field similarity and weights

Each available field produces a similarity between zero and one. The same weights and functions apply to Overture, ATP and OSM.

| Field | Weight | Field comparison |
|---|---:|---|
| Brand | 20% | Normalized token-sort similarity; both known Wikidata IDs override text with exact equality |
| Business name | 10% | Token-sort similarity after store-token removal and explicit alias normalization |
| Store ID | 22% | Exact normalized store-key equality |
| City | 14% | Normalized token-sort similarity |
| State or province | 7% | Exact normalized text equality |
| Country | 3% | Exact normalized text equality |
| Postcode | 4% | Exact normalized text equality |
| Street | 8% | Edit similarity after street normalization |
| House number | 3% | Exact normalized text equality |
| Highways | 7% | Shared highway keys divided by the smaller reference-set size |
| Scoped exits | 2% | Shared highway-key/exit-ID pairs divided by the smaller scoped set |

The weights sum to 100%. A source naming one of three input access roads gets full highway-field overlap because its one known road is supported. A disjoint known highway set gets zero highway similarity. Equal exit numbers on different highways get zero exit similarity. An exit lacking a highway key does not supply comparable exit evidence.

Phone, website, fuel price, fuel type, HGV access, opening hours, source age, provider reputation, category, geometry distance to the text-only input, and source existence confidence are not numerical score components. Several are useful future signals or routing inputs, but the current code does not award points for them.

## 7 The aggregate score

Let A be the total weight of fields present in the input and W the total weight of fields comparable on both records. Let M be the weighted mean of their field similarities. Coverage C is W divided by A. Evidence strength E is the smaller of one and W divided by 0.60.

**Score equation**

`S = 100 × M × sqrt(C) × E`

The implementation returns zero for a zero denominator, caps the result at 100 and rounds the final score to three decimal places. It calculates with the unrounded intermediate values; the audit's M, C and E are rounded to four decimal places for display.

A field absent from the input is not applicable and does not reduce coverage. A field present in the input but absent from the source is recorded as missing and reduces W. A field present on both sides but disagreeing still counts in W and contributes zero or partial similarity. Coverage therefore measures comparability, not correctness; it must be interpreted with similarity and conflicts.

E prevents a few exact but weak attributes from receiving the same score as richer identity evidence. The 0.60 evidence-weight target, field weights and decision thresholds are explicit heuristic parameters. They have not been learned from a labeled validation set.

### Conflicts that force zero

| Conflict | Rule |
|---|---|
| Country | Both known and unequal |
| State | Both known and unequal |
| Store ID | Both known and different after store-key normalization |
| House number | Both known and unequal after text normalization |
| Brand | Comparable brand similarity below 0.55, including explicit Kwik Trip versus QuikTrip distinction |

If both brand Wikidata IDs are known, unequal IDs produce a zero brand component and thus a brand conflict. Missing fields are not conflicts. Postcode, city, street, highway and exit disagreements reduce similarity but do not independently force the aggregate score to zero. Lifecycle and coordinate eligibility are checked separately after address scoring.

## 8 Worked scoring cases

These synthetic examples use the input `KWIK TRIP #796`, `I-94, EXIT 143 & US-12 & SR-21`, Tomah, WI, US. Each source candidate has a valid synthetic POI point. Results assume a single candidate, no input conflict and a complete search. The numbers were calculated by the implemented scorer; they are not claims about store 796's real coordinates.

| Case | A | W | M | C | E | Score | Decision |
|---|---:|---:|---:|---:|---:|---:|---|
| Every applicable field matches | 0.85 | 0.85 | 1.0000 | 1.0000 | 1.0000 | 100.000 | Accepted |
| Store and locality match; road and exit absent | 0.85 | 0.76 | 1.0000 | 0.8941 | 1.0000 | 94.558 | Accepted |
| Brand/name/locality match; store and roads absent | 0.85 | 0.54 | 1.0000 | 0.6353 | 0.9000 | 71.735 | Review |
| Brand/name/state/country match; store, city and roads absent | 0.85 | 0.40 | 1.0000 | 0.4706 | 0.6667 | 45.733 | Unmatched |
| All fields match except city typo Tommah | 0.85 | 0.85 | 0.9850 | 1.0000 | 1.0000 | 98.503 | Accepted |
| Candidate says store 797 | 0.85 | 0.85 | 0.7412 | 1.0000 | 1.0000 | 0.000 | Store conflict |
| Candidate says state MN | 0.85 | 0.85 | 0.8902 | 1.0000 | 1.0000 | 0.000 | State conflict |
| Candidate says I-90 Exit 143 | 0.85 | 0.85 | 0.8941 | 1.0000 | 1.0000 | 89.412 | Review |
| Candidate names I-94 only; other identity matches | 0.85 | 0.83 | 1.0000 | 0.9765 | 1.0000 | 98.817 | Accepted |

For the 71.735 case, the comparable weights are brand 0.20, name 0.10, city 0.14, state 0.07 and country 0.03, totaling W=0.54. All these similarities equal one. C=0.54/0.85 and E=0.54/0.60. A perfect similarity on those fields still lacks the requested store and road evidence, so the result remains a review candidate.

The 45.733 case can still populate a source's comparison latitude/longitude as its best available eligible record. Its `unmatched` status and low score remain visible. The wrong-state case would usually be removed during retrieval before scoring; the zero score documents what happens if it reaches the scorer.

## 9 How records from different sources are grouped

The source pairs Overture–ATP, Overture–OSM and ATP–OSM all use the same `same_place` rules. The same rules can also group duplicate observations within a provider. There is no separate bonus or special matching formula for any pair.

| Pair | Shared comparison basis | Identifier treatment |
|---|---|---|
| Overture and ATP | Canonical identity/address fields plus coordinate distance | GERS/place ID and ATP feature ID remain distinct; Overture lineage is retained |
| Overture and OSM | Same canonical fields plus point/center distance | Overture ID is not equated with an OSM numeric element ID |
| ATP and OSM | Same canonical fields plus point/center distance | Spider ID, generic ref and OSM identity are not assumed to be public branch numbers |

A record first needs a query score of at least 65 and no hard conflict to enter decision clustering. Known closed and zero-existence records are excluded. Pairwise grouping then requires both records to have coordinates, distance at most 250 m, and no hard conflict between their addresses. At least one of these identity conditions must also hold:

1. Both carry the same normalized store ID.
2. Both carry the same house number and street similarity at least 0.95.
3. Distance is at most 35 m, business-name similarity is at least 0.95, and comparable brand similarity is at least 0.90. Generic names such as Unknown, Fuel, Gas Station and Truck Stop cannot establish this name-based identity.

The pairwise identity check does not require its own aggregate address score of 65. The 65 threshold applies to each candidate's match to the input before decision clustering. Missing brand evidence does not itself block the very-close name condition, but it also cannot satisfy automatic input branch acceptance's strong-brand requirement.

Cluster admission checks the new record against every existing member. If A is near B and B near C but A is more than 250 m from C, all three cannot join one cluster. This prevents a chain of nearby fuel stations becoming one place.

A cluster's score is its strongest member's input score. Ten duplicate records score no more than one identical record. Source lineage is retained because an Overture observation may share upstream data with ATP or OSM; three provider names do not automatically mean three independent observations.

The combined coordinate is the medoid among eligible POI/POI-center members of the winning cluster: the actual member point with the smallest total distance to the others. The code does not average coordinates. Equal medoid choices follow the stable ranking order.

## 10 Acceptance and review decisions

Automatic acceptance requires every applicable condition below. A high score alone is insufficient.

| Requirement | Threshold or condition |
|---|---|
| Score | At least 90 |
| Distinct-location margin | At least 8 points above the next decision cluster |
| Brand | Brand component at least 0.90 |
| Branch identity | Exact known store ID, or exact known house number plus street similarity at least 0.95 |
| Locality | City similarity at least 0.85 or exact known postcode |
| Geographic scope | Known matching country and state on both records |
| Position | Winning cluster contains an eligible POI or POI-center coordinate |
| Source lifecycle | No known closure or zero existence confidence |
| Search and input | No relevant truncation, no input disagreement, and complete combined acquisition |

When there is only one decision cluster, its score is also the reported decision margin. Margin eight is a distinct-location separation rule, not eight metres. A second record in the same physical cluster is not a competing location.

`accepted` fills the combined latitude/longitude. `needs_review` means there is a decision candidate, or an input disagreement, but an acceptance condition failed. `unmatched` means there is no eligible decision cluster in the searched scope; a lower scoring source comparison candidate can still exist. `unavailable` is an export label for a source that was not successfully acquired.

Useful reason codes include `score_below_accept_threshold`, `competing_location_with_similar_score`, `insufficient_branch_or_locality_evidence`, `no_poi_coordinate`, `candidate_limit_reached`, `no_eligible_candidate`, `input_records_disagree` and `source_acquisition_incomplete`.

An input disagreement prevents combined acceptance and downgrades otherwise accepted source decisions. Incomplete acquisition blocks combined acceptance but does not invalidate another successfully acquired source's independent decision. Two providers can each accept a branch independently yet leave the combined result under review if their high-scoring positions form different physical clusters.

## 11 Reading source and highest score columns

For each prefix `alltheplaces`, `overture` and `osm`, the flat comparison exposes these fields:

| Column suffix | Meaning |
|---|---|
| `_score` | Score of that source's best eligible geocoded candidate; zero when none exists |
| `_latitude` and `_longitude` | That same provider record's WGS84 point, including review or low-score candidates |
| `_status` | Source-level acceptance/review/unmatched decision |
| `_source_id` | ID of the record supplying the comparison point |
| `_matched_name` and `_matched_address` | Source-provided business name and selected raw address |
| `_matched_city` and `_matched_state` | Canonical locality from that candidate |
| `_evidence_coverage` | Comparable input weight fraction C, from zero to one |
| `_margin` | Score difference to the next eligible located candidate representing another place |
| `_reasons` | Conditions explaining the source decision |

Best-point selection requires positive score, no hard conflict, eligible POI precision, a coordinate, no known closure and nonzero-or-unknown existence confidence. It has no 65-point floor. Blank source coordinates mean there was no eligible geocoded candidate in the searched scope. An unlocated high-scoring address can affect a decision but cannot supply these coordinate columns. The JSON `decision_score` separates the decision cluster score from the best-point score when they differ.

`highest_matching_score` is the maximum score among eligible source points. The associated source, source ID, latitude, longitude and status are copied from the same record. Equal maxima appear in `highest_matching_tied_sources`. The displayed source is the first alphabetically, so ATP precedes OSM and Overture in a three-way tie; this ordering does not express a quality preference.

The unprefixed `score` describes the combined winning decision cluster. Unprefixed `latitude`/`longitude` are acceptance-controlled combined coordinates. `proposed_*` describes the combined cluster's proposed point. These may differ from `highest_matching_*`, which describes an individual provider record. Source-level margin compares eligible located alternatives; combined margin compares decision clusters whose members have query scores at least 65.

## 12 The actual Tomah example

For input OPIS ID 9, `KWIK TRIP #796` in Tomah, the completed run found exact brand/name/locality evidence but did not establish store 796 from the selected candidates. All three reported source maxima equal 71.735, and their source margins are zero.

| Source | Reported candidate address | Latitude | Longitude | Score |
|---|---|---:|---:|---:|
| AllThePlaces | 124 W McCoy Blvd | 44.018500 | -90.506580 | 71.735 |
| Overture | 1504 Superior Ave | 43.97228269 | -90.50389399 | 71.735 |
| OpenStreetMap | 1504 Superior Avenue | 43.9725227 | -90.5041962 | 71.735 |

These are reported candidate locations, not verified coordinates for store 796. The displayed highest source is ATP because all three scores tie. The tie list contains all three providers, and the combined result is `needs_review`. The different addresses show why brand plus city and a highest-score rule cannot identify a specific branch on their own. This example is read from the v2 CSV, rather than a live store-locator lookup.

## 13 Completed run and interpretation limits

The v2 run completed on 24 October 2026 with algorithm `evidence-2.0`. It reused 416,255 unique imported provider records: 182,986 Overture, 111,440 ATP and 121,829 OSM records. Candidate-coordinate coverage below counts stops with any eligible positive-score point from that source, including low-score candidates. It is not an accuracy or acceptance rate.

| Run measure | Count |
|---|---:|
| Distinct input stops scored | 6,738 |
| Original price observations preserved | 8,151 |
| Combined accepted | 16 |
| Combined needs review | 4,481 |
| Combined unmatched | 2,241 |
| Stops with an ATP comparison point | 3,400 |
| Stops with an Overture comparison point | 4,217 |
| Stops with an OSM comparison point | 6,025 |
| Stops with a tied highest source score | 1,692 |

The validation report confirms row preservation, coordinate ranges, score maxima, score/coordinate/source identity consistency and 20,214 per-source result rows. Structural validation does not establish that a station was assigned its true physical branch. The recorded automated tests check implementation behavior and edge cases; a labeled store-level truth set is still needed to measure precision, recall and calibrated confidence.

Current limitations include first-address-only Overture matching, unavailable store identifiers, missing source city/postcode fields, selected-spider ATP coverage and bounded candidate searches. The pipeline does not resolve exits to a road graph, verify route accessibility from a provider point, use truck entrance geometry, or infer a missing branch number from proximity. The geocoding scope ends before route cost and travel-time optimization.

Recommended next work is to validate store-number semantics per spider, expand alternate source addresses, build a reviewed truth set stratified by brand and address type, and add road/exit and truck-entry evidence. These are proposed improvements, not current scoring features.

## 14 Implementation and reproducibility

| File | Responsibility |
|---|---|
| `geocoding/providers.py` | Source-specific field mapping, coordinates and lifecycle |
| `geocoding/normalization.py` | Text keys, brand aliases, subdivisions, store and street normalization |
| `geocoding/parsing.py` | Store marker, postal address, highway and exit extraction |
| `geocoding/matching.py` | Weights, score, conflicts, clustering, decisions and highest-source selection |
| `geocoding/services.py` | Scoped retrieval, per-provider limits, input/acquisition overrides and persistence |
| `geocoding/exports.py` | Source columns, highest-score columns and original-price-row enrichment |
| `geocoding/acquisition.py` | Source acquisition, OSM centroid creation and provenance |

The retained outputs are `output/geocoding-v2/geocodes.csv` for distinct stops, `fuel-prices-geocoded.csv` for all original observations, `per-source.jsonl` for source decisions, `geocodes.jsonl` for detailed candidate evidence, and `report.json`, `summary.json` and `validation.json` for run metadata.

To rescore the same verified import locally:

```sh
.venv/bin/python -u manage.py run_geocoding_pipeline \
  --acquisition-report output/acquisition/report.json \
  --reuse-import-report output/geocoding/report.json \
  --output-dir output/geocoding-v2
```

This command checks the input checksum, source releases, export paths and existing database record counts. It writes a new result run and updates input resolution states. Scores and parameters should be versioned whenever weights, normalization, thresholds, candidate scope or interpretation of store IDs changes.

For the current policy constants and tests, inspect `geocoding/matching.py`, `geocoding/tests/test_scoring_v2.py`, `geocoding/tests/test_domain.py` and `geocoding/tests/test_pipeline.py`. This reference documents the current implementation; the code and retained run artifacts provide the authoritative evidence for its behavior.
