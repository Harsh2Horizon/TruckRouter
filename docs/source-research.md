# Provider research and field mappings

Checked against primary documentation on 2026-10-24. Record release IDs explicitly; upstream schemas and inventories can change.

| Source | How to retrieve records | Identity | Useful evidence | Adapter |
| --- | --- | --- | --- | --- |
| Overture Places | Published GeoParquet on object storage; regional exports via official tooling | Place/GERS `id`, plus release | `names`, `brand`, `addresses[]`, geometry, `sources[]`, operating status, existence confidence | `providers.overture` |
| AllThePlaces | Run metadata and spider-specific GeoJSON downloads | Feature `id` in `@spider` namespace, plus run ID | `name`, `brand`, `ref`, `addr:*`, Point geometry, `@source_uri`, lifecycle tags | `providers.alltheplaces` |
| OSM via Overpass/extract | Bounded fuel POI extracts, including `out center` for ways/relations | OSM element type and ID, plus extract date | `name`, `brand`, `brand:wikidata`, `ref:store`, `addr:*`, `amenity=fuel`, lifecycle tags | `providers.osm` |
| OSM via Nominatim | Text search on self-hosted instance | `osm_type` + `osm_id`; installation `place_id` is unsuitable as a durable external key | `addressdetails`, `namedetails`, `extratags`, coordinates, feature type | `providers.nominatim` |

Overture and ATP are not interchangeable address-search APIs. Their records already contain coordinates when available; our task is to retrieve likely records and resolve their identity against the fuel feed.

## Overture

The [Places guide](https://docs.overturemaps.org/guides/places/) and [Place schema](https://docs.overturemaps.org/schema/reference/places/place/) describe names, brand, addresses, coordinates and provenance. Current schema uses `basic_category`/`taxonomy`; older exports used `categories`. This adapter does not depend on either category layout.

`confidence` measures **existence**, not correctness of an input match or position accuracy. Store it separately from our similarity score. Preserve source lineage: ATP can contribute to Overture, so an Overture record and an ATP record can describe the same upstream observation. Agreement is not automatically independent evidence.

The adapter uses the first address as its matching address; all additional addresses, names, source records and fields remain in the raw payload. A later version should expand address alternatives during candidate scoring. GERS IDs are valuable join keys but are not a promise that all releases preserve every identity unchanged.

Overture place licensing depends on the upstream source. Retain source licenses and attribution rather than assigning OSM's license to all Overture records. See [Overture attribution](https://docs.overturemaps.org/attribution/).

## AllThePlaces

The [API specification](https://github.com/alltheplaces/alltheplaces/blob/master/API.md) exposes run metadata and per-spider output. For example, `/runs/latest.json` discovers a run; `/runs/latest/output/{spider_name}.geojson` retrieves its successful output. Resolve and record a concrete run ID for reproducibility rather than treating `latest` as a version.

The [data format](https://github.com/alltheplaces/alltheplaces/blob/master/DATA_FORMAT.md) uses GeoJSON and OSM-style `addr:*` properties. Geometry can be null. Only `@spider` is guaranteed among the properties. A spider's `ref` is its identifier; its semantics are not guaranteed to be the visible store number. Feature IDs can change when a spider or its source identifiers change. The published data is [CC0](https://github.com/alltheplaces/alltheplaces/blob/master/LICENSE); the scraper software has a separate MIT license.

## OpenStreetMap

Use [fuel POI tags](https://wiki.openstreetmap.org/wiki/Tag:amenity=fuel) and preserve `node`/`way`/`relation` identity. A way's center is an approximate POI position, not its truck entrance. Fuel type, HGV access and opening-hour evidence can be retained in raw tags for the routing phase.

[Nominatim search](https://nominatim.org/release-docs/latest/api/Search/) supports free text, structured address search and extra details. Highway/exit descriptions are contextual clues rather than reliable postal street strings, so this client searches business identity and locality first. Search rank/importance is not a calibrated match probability. City or road results must not masquerade as a fuel-station coordinate.

The [public Nominatim usage policy](https://operations.osmfoundation.org/policies/nominatim/) specifies an absolute maximum of one request per second, an identifying User-Agent, attribution and caching, while discouraging bulk tasks and restricting systematic extraction. The implementation therefore rejects the public OSMF host and requires an explicitly configured self-hosted instance for network lookup. OSM records retain ODbL attribution. Dataset distribution and combinations should follow the applicable [OSM licensing guidance](https://osmfoundation.org/wiki/Licence).

## Normalized field mapping

| Canonical field | Overture | ATP | OSM/Nominatim |
| --- | --- | --- | --- |
| `name` | `names.primary` | `name` | `name` / namedetails |
| `brand` | `brand.names.primary` | `brand` | `brand` |
| `brand_wikidata` | `brand.wikidata` | `brand:wikidata` | `brand:wikidata` |
| `store_id` | Explicit marker in name; no universal store field | Name marker, `ref:store`, `store_number`; opt-in verified `ref` | Name marker or explicit `ref:store` |
| `house_number`, `street` | Parse `addresses[0].freeform` | `addr:housenumber`, `addr:street`, street-address fallback | `addr:*` / address breakdown |
| `city` | `addresses[0].locality` | `addr:city` | City/town/village fallback |
| `state` | `addresses[0].region` | `addr:state` | ISO subdivision code, then state name |
| `country` | `addresses[0].country` | `addr:country` | `addr:country` / `country_code` |
| `postcode` | `addresses[0].postcode` | `addr:postcode` | `addr:postcode` / `postcode` |
| `highways`, `exits` | Parse freeform context, when present | Parse address context, when present | Parse address context; do not interpret a store `ref` as a highway |
| Coordinate | Point `[longitude, latitude]` | Point `[longitude, latitude]` | Explicit `lat`/`lon`, or requested center |

Country inference is limited to recognized US states and Canadian provinces/territories when country is missing. Unknown region text is preserved. The parser currently targets these two countries rather than claiming global address support.
