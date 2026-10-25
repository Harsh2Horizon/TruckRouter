# Why coverage changed, and how to obtain coordinates for every input row

The comparison uses the saved `evidence-2.0` and `brand-locality-3.0` runs over the same 6,738 unique OPIS stop IDs and 8,151 original price rows. Source releases and stored record counts are identical. The loss of displayed coordinates came from changed matching rules, not missing downloads.

The new `coverage-4.0` pass gives every original row a final coordinate, while retaining the distinction between a matched POI, a mapped exit, a highway segment and a locality point. **Complete geographic coverage does not mean every fuel station has been identified exactly.**

## 1. The exact counts

| Measure | Older v2 | Stricter v3 | Coverage v4 |
|---|---:|---:|---:|
| All three provider POI coordinate pairs missing | 379 | 1,980 | 1,980; original source results retained |
| Final coordinate missing after available fallback | 379 | 1,295 | **0** |
| Original input rows with all three provider pairs missing | 438 | 2,250 | 2,250; original source results retained |
| Original input rows with no final coordinate | 438 | 1,474 | **0** |

The approximately 300 versus approximately 1,200 figures mix two definitions: provider-candidate availability and the final result after exit fallback. There is another distinction between unique stops and original rows, because a stop can have multiple price observations. The v3 `unmatched` status count was 1,277; another 18 review records had no final point, producing 1,295 final blanks.

In v3, 685 of the 1,980 stops with no provider point already received a verified-exit approximation. The coverage pass fills the remaining 1,295 unique-stop gaps, or 1,474 original rows. Three other canonical stops with conflicting input observations are handled conservatively at raw-row level, which slightly changes the POI-versus-approximation breakdown.

At the provider level:

| Source | v2 stops with a displayed point | v3 stops with an eligible point | Net change |
|---|---:|---:|---:|
| AllThePlaces | 3,400 | 3,575 | +175 |
| Overture | 4,217 | 3,360 | −857 |
| OpenStreetMap | 6,025 | 1,399 | −4,626 |

Of the old stops with a point, 1,694 lost all provider points; 93 previously empty stops gained a provider point. The net increase in provider-empty stops is therefore 1,601. Another 286 stops were empty in both runs.

## 2. Why the rules produced this change

### Missing city tags became a hard rejection

Previously, absent candidate fields reduced evidence, but did not automatically block a coordinate. A country/state-compatible record could therefore be shown even when its city field was empty.

V3 requires complete city, state/province and country evidence together. The country and state must normalize to the same recognized codes, and city similarity must reach 90%. Missing city means no eligible provider prediction, regardless of the name score.

The audit reloaded the actual provider records selected by v2 and evaluated the new rules against them:

| Rejection reason among lost old provider candidates | AllThePlaces | Overture | OSM |
|---|---:|---:|---:|
| Missing city | 117 | 535 | **4,448** |
| Missing state | 1 | 517 | 0 |
| City below 90% similarity | 92 | 131 | 116 |
| Business-name similarity below 70% | 128 | 944 | 1,245 |

These reason counts overlap: one record may fail both name and locality. They must not be added together to infer the number of lost stops.

OSM was affected most because many fuel POIs have names and coordinates but lack `addr:city`. Missing an address tag is not proof that the POI is in the wrong city. It means the strict textual matcher has insufficient evidence to confirm the city.

### Previously displayed coordinates were often weak candidates

The median v2 OSM candidate score was **46.281**. Of its 6,025 displayed source points, **4,571 had source status `unmatched`**, 1,448 required review, and only six had independent source status `accepted`.

Consequently, “only 379 blanks” did not mean that 6,359 stops had been accurately identified. V2 exported its best eligible coordinate even when that provider's decision remained unmatched. V3 rejects more candidates before exporting a provider point. This changes availability; neither comparison establishes real-world precision without labeled ground truth.

### The requested name policy changes which candidates survive

V3 gives 60% to fuzzy brand/business name, 30% to joint locality, 7% to highway and 3% to scoped exit. Store IDs, postal/street matching weights and provider-confidence bonuses are absent. Name similarity below 70% blocks a match. The city retrieval threshold also rose from 70% to 90%, and a matching store number no longer bypasses city retrieval.

Removing store-ID dependence correctly avoids comparing OPIS identifiers with unrelated provider identifiers. However, name plus city cannot always distinguish several branches of the same chain. Similar branches need spatial road/exit evidence or review; simply lowering thresholds would increase coordinates without establishing the correct branch.

### The original exit fallback was deliberately narrow

The first fallback covered US signed interstates with a scoped exit, a city anchor within 20 km, and no interchange ambiguity beyond 2 km. It did not cover every US/state/provincial highway. Missing locality aliases, shared-route exit notation and typos could also prevent it from finding a valid reference.

Among the 1,295 final blanks, 1,273 had parsed highway clues, 288 had a scoped exit, and 1,256 already had a usable OSM locality anchor. The remaining 39 anchor failures included variants such as `MC CALLA`, `MC GRAW`, `MC LEAN`, `DU BOIS` and `ONEILL`, as well as localities absent from the original settlement index. Only three entries looked like suitable structured street addresses for Census geocoding.

## 3. Implemented recovery order

1. **Retain the best existing provider POI candidate.** Preserve its score and review status. Check US coordinates against the input state polygon; exclude a point outside the claimed state from final selection and use a geographically consistent source alternative where available. This corrected the Certified Oil/New Martinsville case by selecting the AllThePlaces point in West Virginia instead of the higher-scoring Overture point in Massachusetts.
2. **Use a real street-address geocoder when the raw input is structured.** Census returned two usable address-range interpolations from three eligible US addresses. City/state and requested house number are checked, followed by state containment. These points locate addresses, not verified brands.
3. **Use the mapped highway plus exit near the specified city.** Normalize route/exit variants, including hyphenated exit letters. Shared-route inputs can be resolved against actual route membership and a nearby city reference; the association is labeled inferred. Competing interchange locations more than 2 km apart do not become a verified exit.
4. **Use a point on the named highway near a city reference.** Match route network, number and variant against Census TIGER road geometry. Require a point inside the input state and within 20 km of the locality anchor. If the requested exit cannot be verified, `Exit Matched` remains false.
5. **Use a named locality's representative point.** Search OSM settlements and GeoNames, including alternate and compact city names, within the same country/state. Label the result `locality`, not `poi`. Flag same-name locality ambiguity and retain the spread between alternative anchors.
6. **Use an administrative-area representative only as a final coarse fallback.** This is implemented for valid geographic input, with explicit state/province precision. It was **not needed for this dataset**. Invalid or wholly unresolvable future geographic input can still remain unresolved rather than receiving invented coordinates.

Every approximate result requires review. Fallbacks add no POI identity points. In the new completion CSV, `Match Score` is the retained provider POI score, or zero when no business match was established. A precise-looking decimal coordinate does not imply precise knowledge of the requested station.

The raw address is retained unchanged. Parsing corrections have a separate field. For example, the obvious `1-40 EXIT 77` interstate typo is interpreted as `I-40 EXIT 77`. Observations sharing an OPIS ID but disagreeing on locality are resolved from their own raw row rather than inheriting another observation's coordinate.

## 4. Completed results

| Final method | Unique canonical stops | Meaning |
|---|---:|---|
| Provider fuel POI candidate | 4,755 | Actual source point; original acceptance/review status retained |
| Highway plus exit | 717 | Actual mapped interchange point; station position approximate |
| Highway near named city | 451 | Point on the named road; requested exit/station not verified |
| Census street address | 2 | Address-range interpolation; business not verified |
| Named locality | 813 | Coarse settlement reference point |
| Administrative-area fallback | 0 | Not needed |
| Missing final coordinate | **0** | All 6,738 canonical stops and 8,151 raw rows have a point |

Use the row-level file as the complete output: it preserves every original field, price precision, row order and repeated observation. The one-row-per-OPIS file is a canonical view and cannot represent every conflicting observation with a single coordinate.

The locality-level results are the remaining accuracy backlog. Some have multiple same-name reference localities; those are explicitly flagged. They should not be treated as confirmed refuelling stops for automatic route-cost decisions. The retained POI candidates also include records requiring branch review.

## 5. Other sources: checked and used versus future improvements

| Source | Current use or assessment |
|---|---|
| [GeoNames](https://www.geonames.org/export/) | Added US/Canada bulk data: 215,594 settlement/ADM1 records, alternate names and country/state mapping. Used for locality and exit-search anchors. CC BY 4.0 attribution is retained. |
| [Census TIGER/Line primary roads](https://www2.census.gov/geo/tiger/TIGER2025/PRIMARYROADS/) | Downloaded the 2025 nationwide archive and indexed 11,063 parsed route-geometry records. This is limited-access primary-road coverage, not the complete US/state/local road network. |
| [US Census Geocoder](https://geocoding.geo.census.gov/geocoder/Geocoding_Services_API.html) | Added a cached address adapter. Useful when a structure number and street are supplied; unsuitable for arbitrary highway/exit prose. Returned two accepted address-range results in this batch. |
| [OpenAddresses](https://openaddresses.io/) | Good additional address-point data for structured street addresses; not imported here. It does not solve business identity or exit notation by itself. Source licenses and geographic coverage vary. |
| [Self-hosted Pelias](https://github.com/pelias/pelias) | Suitable next geocoding service for combining OpenAddresses, OSM, GeoNames and administrative data, with address interpolation and explicit match types. It is infrastructure to combine datasets, not another independent POI database. Not deployed in this change. |
| [NRCan Geolocator](https://natural-resources.canada.ca/maps-tools-publications/satellite-elevation-air-photos/geolocation-service) | Checked the current Canadian service documentation, including official place names and address/locality search. Useful future adapter for Canadian gaps. High-volume use should be coordinated with the provider. Not queried for this batch. |
| Official brand/store locators | Best next source for resolving missing or ambiguous chain branches. Expand the existing AllThePlaces spider coverage and retain locator provenance. Public store IDs still must not be assumed equivalent to OPIS IDs. |
| Nominatim/Photon | Another API over OSM is not independent geographic evidence. For a recurring bulk pipeline, use a self-hosted or appropriately contracted service. The public OSMF Nominatim endpoint was not used; its [usage policy](https://operations.osmfoundation.org/policies/nominatim/) restricts heavy/systematic use. |

## 6. Best next step for better station accuracy

Keep name dominant and retain strict country/state consistency. Improve **locality evidence and candidate retrieval** rather than treating a missing city tag as permanently unusable:

- Derive missing administrative fields from the POI point against trustworthy municipality/state/country polygons. Record the field's origin as spatially derived; retain the provider's original tag separately. A nearest-town lookup alone is weaker than polygon membership.
- Resolve postal-city, municipality and neighborhood aliases explicitly. They can differ legitimately even when both sources are describing the same place.
- Retrieve nearby fuel POIs by a spatial search around the matched locality/exit, then apply fuzzy name and geographic locality evidence. Avoid using unreliable store IDs to retrieve across cities.
- Expand route geometry with county-level TIGER All Roads or a complete regional OSM road graph, and add Canadian exit/road references. The current primary-road file does not cover every US or state highway.
- Verify same-brand branches against the actual interchange and driveway/access geometry. Store counts, positional precision and identity confidence should remain separate metrics.

This would recover useful POIs with incomplete tags while preserving the locality requirement. It is a better accuracy strategy than restoring all v2 coordinates or making every fuzzy threshold lower. The present completion layer supplies an honest fallback for every current record while that stronger evidence is built.

## 7. Files, commands and validation

```sh
.venv/bin/python manage.py compare_geocoding_runs
.venv/bin/python manage.py prepare_coverage_reference
.venv/bin/python manage.py complete_geocoding_coverage --fetch-census
.venv/bin/python manage.py validate_geocoding_coverage
```

Completed reference downloads and Census responses are cached. The completion command reuses the completed three-source v3 run; it does not repeat the large source downloads or rerank every POI. `--fetch-census` makes external requests only for eligible structured addresses; ordinary resolution uses cached results.

- `outputs/truckroute-coverage-v4/all-records-geocoded.csv`: every original observation, final latitude/longitude, method, precision, source, review status and fallback evidence.
- `outputs/truckroute-coverage-v4/final-stops.csv`: first/canonical observation per OPIS ID.
- `outputs/truckroute-coverage-v4/recovered-records.csv`: rows whose previous final coordinate was blank.
- `output/geocoding-coverage-analysis/comparison.json`: exact run comparison and overlapping rejection counts.
- `output/geocoding-coverage-analysis/lost-provider-candidates.csv`: record-level evidence for the lost old candidates.
- `output/geocoding-v4/report.json` / `validation.json`: coverage totals, source manifests and checks.

The Django geocoder can also use the new local coverage references when the strict POI/exit stage fails. Coarse results remain separate from accepted POI coordinates and from all three provider-specific coordinate columns. Restart an existing API process after preparing or replacing reference data so its cached index is refreshed.

Validation checks every original row and field, coordinates and ranges, source-point/score ownership, exit/highway search distances, approximation labels, and all US output points against the input state polygon. Canadian results use country/province metadata; a Canadian province-polygon validation has not been added. No guaranteed positional-error radius is asserted. Synthetic regression tests check fallback ordering, route membership, city aliases, input conflicts and the separation of source matches from approximate geography.

Executed validation: all **8,151 rows** passed, every original field was preserved, and all **7,531 US output rows** were inside their input state polygon. All **115 Django tests** passed, Ruff reported no issues, and no model migrations were required. The locality fallback flagged **109 original rows** with same-name anchor ambiguity; those remain review items.
