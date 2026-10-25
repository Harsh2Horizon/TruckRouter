"""Progressive geographic fallbacks, separate from fuel-POI identity scores."""

import json
import re
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from threading import RLock

from .acquisition import DownloadClient, duckdb_connection
from .census import address_key
from .geography import boundary_state_code, get_exit_locator
from .normalization import city_key
from .parsing import parse_address
from .schemas import Coordinate, RoadNetwork
from .scoring import city_similarity, distance_meters

VERSION = "coverage-4.0"
CITY_RADIUS_M = 20000.0


def compact_city(value):
    return city_key(value).replace(" ", "")


def exit_number(value):
    return re.sub(r"(?<=\d)[ -]+(?=[A-Z]$)", "", value.upper().strip())


def parse_raw(data):
    raw = data.get("address", "")
    corrected = re.sub(r"^1\s*-\s*(\d{1,3})(?=\s*[, ]?\s*EXIT\b)", r"I-\1", raw, flags=re.I)
    return parse_address({**data, "address": corrected}), (
        ["leading_1_corrected_to_interstate_I"] if raw != corrected else []
    )


class SettlementIndex:
    def __init__(self, manifest, exit_locator):
        self.exact = defaultdict(dict)
        self.regions = defaultdict(dict)
        self.admin = {}
        self.cache = {}
        rows = []
        for line in Path(manifest["settlements_file"]).open():
            row = json.loads(line)
            if row["feature_code"] == "ADM1":
                self.admin[(row["country"], row["state"])] = row
            else:
                rows.append(row)
        if exit_locator:
            seen = set()
            for places in exit_locator.cities.values():
                for place in places:
                    key = (place["country"], str(place["osm_id"]))
                    if key in seen:
                        continue
                    seen.add(key)
                    tags = place.get("tags") or {}
                    rows.append(
                        {
                            "id": str(place["osm_id"]),
                            "name": place["name"],
                            "country": place["country"],
                            "state": place["state_iso"].split("-")[-1],
                            "latitude": place["latitude"],
                            "longitude": place["longitude"],
                            "source": "OpenStreetMap settlement",
                            "population": 0,
                            "feature_code": place.get("place", "locality"),
                            "aliases": [
                                v
                                for tag in ("name:en", "official_name", "alt_name", "short_name")
                                for v in (tags.get(tag) or "").split(";")
                                if v
                            ],
                        }
                    )
        for row in rows:
            identity = (row["source"], row["id"])
            names = {row["name"], row.get("ascii_name", ""), *row.get("aliases", [])}
            for name in names:
                if not compact_city(name):
                    continue
                key = (row["country"], row["state"], compact_city(name))
                self.exact[key][identity] = row
                self.regions[(row["country"], row["state"])][city_key(name)] = key

    def candidates(self, query):
        key = (query.country, query.state, compact_city(query.city))
        if key in self.cache:
            return self.cache[key]
        exact = self.exact.get(key, {})
        candidates = [(row, 1.0) for row in exact.values()]
        if not candidates:
            found = {}
            for name, alias in self.regions.get((query.country, query.state), {}).items():
                score = city_similarity(query.city, name)
                if score >= 0.90:
                    for identity, row in self.exact[alias].items():
                        if score > found.get(identity, ({}, 0))[1]:
                            found[identity] = (row, score)
            candidates = list(found.values())
        current_codes = {"PPL", "PPLA", "PPLA2", "PPLA3", "PPLC", "PPLG", "city", "town", "village"}
        candidates.sort(
            key=lambda item: (
                -item[1],
                item[0]["feature_code"] not in current_codes,
                -item[0].get("population", 0),
                item[0]["source"],
                str(item[0]["id"]),
            )
        )
        self.cache[key] = candidates
        return candidates


class TigerRoadLocator:
    def __init__(self, manifest):
        self.connection = duckdb_connection(DownloadClient("data/sources"))
        self.connection.execute(
            "CREATE TABLE roads AS SELECT * FROM read_parquet(?)", [manifest["roads_file"]]
        )
        states = json.loads(Path("data/reference/usa-states-manifest.json").read_text())
        boundaries = json.loads(Path(states["filename"]).read_text())
        self.connection.execute("CREATE TABLE states (state VARCHAR,geometry GEOMETRY)")
        self.connection.executemany(
            "INSERT INTO states VALUES (?,ST_GeomFromGeoJSON(?))",
            [
                (boundary_state_code(f["properties"]), json.dumps(f["geometry"]))
                for f in boundaries["features"]
            ],
        )

    def closest(self, query, cities):
        if query.country != "US":
            return None
        choices = []
        seen = set()
        for city, similarity in cities[:5]:
            anchor = Coordinate(latitude=city["latitude"], longitude=city["longitude"])
            for road in query.highways:
                number = str(int(road.number)) if road.number.isdigit() else road.number.upper()
                key = (
                    str(road.network),
                    number,
                    str(road.variant),
                    city["latitude"],
                    city["longitude"],
                )
                if key in seen:
                    continue
                seen.add(key)
                rows = self.connection.execute(
                    """
                    WITH candidates AS (
                        SELECT id,name,ST_ClosestPoint(geometry,ST_Point($lon,$lat)) AS point
                        FROM roads WHERE network=$network AND number=$number AND variant=$variant
                          AND ST_Intersects(geometry,ST_MakeEnvelope($lon-1,$lat-.3,$lon+1,$lat+.3))
                    ) SELECT id,name,ST_Y(point),ST_X(point)
                      FROM candidates,states s
                      WHERE s.state=$state AND ST_Covers(s.geometry,point)
                      ORDER BY ST_Distance(point,ST_Point($lon,$lat)) LIMIT 8
                """,
                    {
                        "lon": anchor.longitude,
                        "lat": anchor.latitude,
                        "network": str(road.network),
                        "number": number,
                        "variant": str(road.variant),
                        "state": query.state,
                    },
                ).fetchall()
                for identity, name, lat, lon in rows:
                    point = Coordinate(latitude=lat, longitude=lon)
                    distance = distance_meters(anchor, point)
                    if distance <= CITY_RADIUS_M:
                        choices.append(
                            {
                                "coordinate": point.model_dump(),
                                "reference_id": identity,
                                "reference_name": name,
                                "city_anchor": city["name"],
                                "city_similarity": similarity,
                                "city_distance_m": distance,
                                "city_source": city["source"],
                            }
                        )
        if not choices:
            return None
        choices.sort(
            key=lambda row: (-row["city_similarity"], row["city_distance_m"], row["reference_id"])
        )
        return choices[0]

    def state_point(self, query):
        if query.country != "US":
            return None
        row = self.connection.execute(
            "SELECT ST_Y(ST_PointOnSurface(geometry)),"
            "ST_X(ST_PointOnSurface(geometry)) FROM states WHERE state=?",
            [query.state],
        ).fetchone()
        return Coordinate(latitude=row[0], longitude=row[1]).model_dump() if row else None

    def point_in_state(self, query, point):
        row = self.connection.execute(
            "SELECT ST_Covers(geometry,ST_Point(?,?)) FROM states WHERE state=?",
            [point["longitude"], point["latitude"], query.state],
        ).fetchone()
        return bool(row and row[0])

    def close(self):
        self.connection.close()


class CoverageResolver:
    def __init__(
        self,
        directory="data/reference/coverage",
        *,
        settlements=None,
        roads=None,
        exit_locator=None,
    ):
        self.manifest = (
            json.loads((Path(directory) / "manifest.json").read_text())
            if settlements is None
            else {}
        )
        self.exits = exit_locator if exit_locator is not None else get_exit_locator()
        self.settlements = settlements or SettlementIndex(self.manifest, self.exits)
        self.roads = roads or TigerRoadLocator(self.manifest)
        self.exit_index = defaultdict(list)
        if self.exits:
            for (country, state, route, identifier), rows in self.exits.exits.items():
                self.exit_index[(country, state, route, exit_number(identifier))].extend(rows)
        self.cache = {}
        census_file = Path(directory) / "census-addresses.json"
        self.census = json.loads(census_file.read_text()) if census_file.exists() else {}
        self.lock = RLock()

    def highway_exit(self, query, cities):
        if query.country != "US" or not query.exits:
            return None
        by_key = {road.key: road for road in query.highways}
        choices = []
        for exit_ref in query.exits:
            scoped = (
                [by_key[exit_ref.highway_key]] if exit_ref.highway_key in by_key else query.highways
            )
            for road in scoped:
                if road.network != RoadNetwork.INTERSTATE:
                    continue
                route = str(int(road.number)) if road.number.isdigit() else road.number.upper()
                for junction in self.exit_index.get(
                    (query.country, query.state, route, exit_number(exit_ref.identifier)), []
                ):
                    point = Coordinate(latitude=junction["lat"], longitude=junction["lon"])
                    for city, similarity in cities[:10]:
                        distance = distance_meters(
                            point,
                            Coordinate(latitude=city["latitude"], longitude=city["longitude"]),
                        )
                        if distance <= CITY_RADIUS_M:
                            choices.append(
                                {
                                    "coordinate": point.model_dump(),
                                    "reference_id": junction["exit_id"],
                                    "reference_name": (
                                        f"{junction['interstate_name']} "
                                        f"Exit {junction['exit_number']}"
                                    ),
                                    "city_anchor": city["name"],
                                    "city_similarity": similarity,
                                    "city_distance_m": distance,
                                    "exit_matched": True,
                                    "exit_association": "input_scoped"
                                    if exit_ref.highway_key in by_key
                                    else "route_and_city_inferred",
                                    "city_source": city["source"],
                                }
                            )
        if not choices:
            return None
        choices.sort(
            key=lambda row: (-row["city_similarity"], row["city_distance_m"], row["reference_id"])
        )
        best = choices[0]
        if any(
            distance_meters(Coordinate(**best["coordinate"]), Coordinate(**r["coordinate"])) > 2000
            for r in choices
            if r["city_similarity"] == best["city_similarity"]
        ):
            return None
        return best

    def resolve(self, query):
        with self.lock:
            return self._resolve(query)

    def _resolve(self, query):
        key = (query.country, query.state, query.city, query.raw_address)
        if key in self.cache:
            return self.cache[key]
        cities = self.settlements.candidates(query)
        if query.country == "US":
            cities = [
                (city, score)
                for city, score in cities
                if self.roads.point_in_state(
                    query, {"latitude": city["latitude"], "longitude": city["longitude"]}
                )
            ]
        base = {
            "status": "approximate",
            "is_approximate": True,
            "station_verified": False,
            "needs_review": True,
            "match_score": 0.0,
            "exit_matched": False,
            "version": VERSION,
        }
        census = self.census.get(address_key(query), {})
        exact_exit = self.highway_exit(query, cities)
        if census.get("status") == "matched" and self.roads.point_in_state(
            query, census["coordinate"]
        ):
            result = {
                **base,
                "coordinate": census["coordinate"],
                "method": "census_address",
                "precision": "address_range",
                "source": "US Census Geocoder",
                "reference_id": (census.get("tiger_line") or {}).get("tigerLineId", ""),
                "reference_name": census["matched_address"],
                "reason": "interpolated address-range point; business identity not verified",
                "response_file": census["response_file"],
            }
        elif exact_exit:
            result = {
                **base,
                **exact_exit,
                "method": "highway_exit",
                "precision": "motorway_exit",
                "source": "OpenInterstate / OpenStreetMap",
                "reason": "verified_route_exit_near_named_city; station not verified",
            }
        else:
            highway = self.roads.closest(query, cities) if cities and query.highways else None
            if highway:
                result = {
                    **base,
                    **highway,
                    "method": "highway_near_city",
                    "precision": "road_segment",
                    "source": "US Census TIGER/Line 2025",
                    "reason": (
                        "named highway point near city; requested exit and station not verified"
                    ),
                }
            elif cities:
                city, similarity = cities[0]
                spread = max(
                    (
                        distance_meters(
                            Coordinate(latitude=city["latitude"], longitude=city["longitude"]),
                            Coordinate(latitude=other["latitude"], longitude=other["longitude"]),
                        )
                        for other, score in cities
                        if score == similarity
                    ),
                    default=0,
                )
                result = {
                    **base,
                    "coordinate": {"latitude": city["latitude"], "longitude": city["longitude"]},
                    "method": "named_locality",
                    "precision": "locality",
                    "source": city["source"],
                    "reference_id": city["id"],
                    "reference_name": city["name"],
                    "city_anchor": city["name"],
                    "city_similarity": similarity,
                    "city_distance_m": 0,
                    "alternative_anchor_spread_m": round(spread, 3),
                    "reason": (
                        "named locality representative point; "
                        "highway, exit and station not verified"
                    )
                    + ("; ambiguous same-name localities" if spread > 20000 else ""),
                }
            else:
                point = self.roads.state_point(query)
                admin = self.settlements.admin.get((query.country, query.state))
                if not point and admin:
                    point = {"latitude": admin["latitude"], "longitude": admin["longitude"]}
                result = {
                    **base,
                    "coordinate": point,
                    "method": "administrative_area" if point else "unresolved",
                    "status": "approximate" if point else "unresolved",
                    "precision": "state_or_province" if point else "unknown",
                    "source": "geoBoundaries state polygon"
                    if query.country == "US"
                    else "GeoNames ADM1",
                    "reference_id": query.state,
                    "reference_name": query.state,
                    "reason": "state/province representative only; input city could not be located",
                }
        self.cache[key] = result
        return result


@lru_cache(maxsize=1)
def get_coverage_resolver():
    directory = Path(__file__).resolve().parent.parent / "data/reference/coverage"
    return CoverageResolver(directory) if (directory / "manifest.json").exists() else None
