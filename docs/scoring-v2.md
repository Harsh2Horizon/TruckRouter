# Evidence score v2

Algorithm version: `evidence-2.0`. Scores are comparable heuristics from 0 to 100. They are not calibrated probabilities and are not percentages of positional accuracy.

## Field weights

| Field | Weight |
|---|---:|
| Brand | 20% |
| Business name | 10% |
| Store number | 22% |
| City | 14% |
| State/province | 7% |
| Country | 3% |
| Postcode | 4% |
| Street | 8% |
| House number | 3% |
| Highway references | 7% |
| Scoped exits | 2% |

Business names exclude the explicit store token so `KWIK TRIP #796` and `KWIK TRIP` have identical business names. The missing store ID is recorded as missing evidence. Store IDs are compared separately. Explicit aliases normalize descriptions such as `PILOT TRAVEL CENTER` and `PILOT`, while `KWIK TRIP` and `QUIKTRIP` remain different brands.

Brand/name/city use normalized token-sort similarity. State, country, postcode, store and house number use normalized exact equality. Streets use normalized edit similarity. Highway and exit comparisons use overlap of normalized references divided by the smaller reference-set size: a source that names one of three access roads can support that road without claiming the other two. Exit identifiers must belong to the same highway key.

## Formula

Only clues supplied by the input are applicable. A blank provider value has no similarity component and reduces coverage. A blank input field is listed in `missing_in_query` and does not reduce coverage.

```text
A = sum of weights for applicable input fields
W = sum of weights for fields comparable on both records
M = sum(weight × field similarity) / W     (0 when W = 0)
C = W / A                                  (0 when A = 0)
E = min(1, W / 0.60)
score = 100 × M × sqrt(C) × E
```

`C` measures how much of the input's evidence can be checked. `E` limits high scores based on only a few weak fields. Evidence, missing fields, conflicts and the formula are saved in the audit records. Adding duplicate records or another provider does not increase the score.

For example, input `KWIK TRIP #796` in Tomah/WI with three highways and an exit has applicable weight `0.85`. A source with exact business identity, city, state and country but no store/highway/exit information has `W=0.54`, `M=1`, `C=0.6353`, `E=0.9` and score `71.735`. An exact comparison of all applicable fields scores `100`. The former remains a candidate for review because several branches can share a brand and city.

Conflicting known country, state, store ID, house number or strong brand disagreement gives score zero. A place's existence confidence is kept separately; zero existence confidence and known closure make the place ineligible. City centroids and non-POI coordinates are excluded from source-coordinate comparisons.

## Decisions and source comparisons

Automatic acceptance requires score at least 90, at least eight points over the next distinct location, strong brand agreement, exact store identity or matching house/street identity, country/state agreement, and city/postcode support. Input conflicts, incomplete acquisition and a truncated combined search prevent acceptance. Scores of at least 65 can enter the review decision. A lower scoring best candidate can still be shown in source comparison columns with status `unmatched`.

Each provider gets up to 500 candidates. Exact store and city evidence is retrieved first. A source reaching its cap marks its own search as truncated; other sources retain their own search status. This prevents early database rows from one provider consuming another provider's entire candidate allocation.

Every source's score and coordinate refer to the **same eligible provider record**. `{source}_latitude` and `{source}_longitude` show that source's highest-scoring available POI candidate. They can be populated for review or low-score candidates. `{source}_status`, `{source}_margin`, `{source}_evidence_coverage`, matched address and reasons explain the selection. Blank coordinates mean no eligible geocoded candidate was found in the searched source scope.

`highest_matching_score` is the maximum of the three source scores. Its provider, source ID, latitude, longitude and status are copied from that same record. Equal scores are listed in `highest_matching_tied_sources`; the first provider alphabetically is the deterministic displayed winner. A tie is not evidence that one source is better. The combined decision can require review when the highest-scoring candidates describe different branches.

The original `status`, `score`, `latitude` and `longitude` columns describe the combined clustering decision. Only an accepted combined decision fills `latitude`/`longitude`. Its representative point may differ from the highest-scoring individual record because clustering chooses an actual member medoid. Source comparison columns and `highest_matching_*` retain each individual point and its score.

## Files and rerun

`output/geocoding-v2/geocodes.csv` has one row per distinct OPIS stop. `fuel-prices-geocoded.csv` preserves all original price rows and appends the source comparison and highest-match columns. Previous scoring results remain in `output/geocoding`.

Rescore the imported release without downloading or importing again:

```sh
.venv/bin/python -u manage.py run_geocoding_pipeline \
  --acquisition-report output/acquisition/report.json \
  --reuse-import-report output/geocoding/report.json \
  --output-dir output/geocoding-v2
```

The reuse path verifies the dataset checksum, source releases, export paths and database record counts before scoring.
