"""Verified highway/exit anchors for approximate locations, never invented fuel POIs."""

import json
import re
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

from .acquisition import FastDownloadClient, duckdb_connection, native_export, write_json
from .matching import city_similarity, distance_meters
from .normalization import city_key
from .schemas import Coordinate, RoadNetwork

REFERENCE_DIR = Path(__file__).resolve().parent.parent / "data/reference"
MAX_CITY_EXIT_DISTANCE_M = 20000.0
MAX_INTERCHANGE_SPREAD_M = 2000.0


def boundary_state_code(properties):
    # The published US ADM1 file contains SU-SD for South Dakota. Use its final
    # subdivision component, rather than assuming every source prefix is US-.
    return properties["shapeISO"].rsplit("-", 1)[-1]


def build_reference(directory=REFERENCE_DIR):
    """Build indexes from versioned cached sources; inputs are acquired separately."""
    directory = Path(directory)
    target = directory / "geographic-index.json"
    if target.exists():
        return json.loads(target.read_text())
    places = json.loads((directory / "places-manifest.json").read_text())
    states = json.loads((directory / "usa-states-manifest.json").read_text())
    release = json.loads((directory / "interstate-release.json").read_text())
    boundaries = json.loads(Path(states["filename"]).read_text())
    client = FastDownloadClient(REFERENCE_DIR.parent / "sources")
    with duckdb_connection(client) as connection:
        connection.execute("CREATE TABLE states (state VARCHAR, geometry GEOMETRY)")
        connection.executemany(
            "INSERT INTO states VALUES (?,ST_GeomFromGeoJSON(?))",
            [
                (boundary_state_code(f["properties"]), json.dumps(f["geometry"]))
                for f in boundaries["features"]
            ],
        )
        exits_file = directory / "exits.ndjson"
        exit_count = native_export(
            connection,
            """
            SELECT DISTINCT e.exit_id, e.interstate_name, e.exit_number, e.direction_code,
                   e.lat, e.lon, s.state, 'US' AS country
            FROM (SELECT exit_id,interstate_name,exit_number,direction_code,
                         try_cast(lat AS DOUBLE) AS lat,try_cast(lon AS DOUBLE) AS lon
                  FROM read_csv(?, all_varchar=true)) e
            JOIN states s ON ST_Contains(s.geometry,ST_Point(e.lon,e.lat))
            WHERE e.exit_number IS NOT NULL AND e.exit_number <> ''
        """,
            [str(directory / "interstate-exits.csv")],
            exits_file,
        )
        cities_file = directory / "cities.ndjson"
        city_count = native_export(
            connection,
            """
            SELECT osm_id,country,state_iso,name,tags,place,
                   ST_Y(geometry) AS latitude,ST_X(geometry) AS longitude
            FROM read_parquet(?,hive_partitioning=false)
            WHERE place IN ('city','town','village','hamlet','municipality','locality')
                  AND name IS NOT NULL
        """,
            [[row["filename"] for row in places["regions"]]],
            cities_file,
        )
    manifest = {
        "exits_file": str(exits_file),
        "cities_file": str(cities_file),
        "exit_records": exit_count,
        "city_records": city_count,
        "exit_source": release,
        "place_snapshot": places["snapshot"],
        "state_boundary_metadata": states["metadata"],
        "coverage": "US signed interstates; named cities in all input US/Canada regions",
        "max_city_exit_distance_m": MAX_CITY_EXIT_DISTANCE_M,
        "locality_basis": "state polygon containment plus named-city proximity",
    }
    write_json(target, manifest)
    return manifest


class ExitLocator:
    def __init__(self, manifest):
        self.manifest = manifest
        self.exits = defaultdict(list)
        self.cities = defaultdict(list)
        for line in Path(manifest["exits_file"]).open():
            row = json.loads(line)
            route = re.sub(r"^I[- ]*", "", row["interstate_name"], flags=re.I).upper()
            key = (row["country"], row["state"], route, str(row["exit_number"]).upper())
            self.exits[key].append(row)
        for line in Path(manifest["cities_file"]).open():
            row = json.loads(line)
            country, state = row["country"], row["state_iso"].split("-", 1)[-1]
            names = {row["name"]}
            for tag in ("name:en", "official_name", "alt_name", "short_name"):
                names.update((row.get("tags", {}).get(tag) or "").split(";"))
            for name in names:
                if name.strip():
                    self.cities[(country, state, city_key(name))].append(row)
        self.region_cities = defaultdict(dict)
        for (country, state, name), rows in self.cities.items():
            self.region_cities[(country, state)][name] = rows
        self._city_cache = {}

    def city_candidates(self, country, state, city):
        key = (country, state, city_key(city))
        if key in self._city_cache:
            return self._city_cache[key]
        exact = self.cities.get((country, state, city_key(city)), [])
        if exact:
            return [(row, 1.0) for row in exact]
        candidates = []
        for name, rows in self.region_cities.get((country, state), {}).items():
            score = city_similarity(city, name)
            if score >= 0.90:
                candidates.extend((row, score) for row in rows)
        self._city_cache[key] = candidates
        return candidates

    def locate(self, query):
        base = {
            "status": "unresolved",
            "coordinate": None,
            "method": "highway_exit",
            "precision": "motorway_exit",
            "approximate": True,
            "fuel_station_verified": False,
            "source": "OpenInterstate / OpenStreetMap",
            "source_release": self.manifest["exit_source"]["release"],
            "locality_basis": self.manifest["locality_basis"],
        }
        if not all((query.city, query.state, query.country)):
            return {**base, "reason": "incomplete_input_locality"}
        if query.country != "US":
            return {**base, "reason": "no_verified_exit_coverage_for_country"}
        roads = {road.key: road for road in query.highways}
        scoped = [
            (roads[e.highway_key], e)
            for e in query.exits
            if e.highway_key in roads and roads[e.highway_key].network == RoadNetwork.INTERSTATE
        ]
        if not scoped:
            return {**base, "reason": "no_supported_highway_with_scoped_exit"}
        city_candidates = self.city_candidates(query.country, query.state, query.city)
        if not city_candidates:
            return {**base, "reason": "city_anchor_not_found"}
        choices = []
        for road, exit_ref in scoped:
            number = str(int(road.number)) if road.number.isdigit() else road.number.upper()
            key = (query.country, query.state, number, exit_ref.identifier.upper())
            for junction in self.exits.get(key, []):
                point = Coordinate(latitude=junction["lat"], longitude=junction["lon"])
                for city, similarity in city_candidates:
                    anchor = Coordinate(latitude=city["latitude"], longitude=city["longitude"])
                    distance = distance_meters(point, anchor)
                    if distance <= MAX_CITY_EXIT_DISTANCE_M:
                        choices.append(
                            {
                                "coordinate": point.model_dump(),
                                "exit_id": junction["exit_id"],
                                "highway": junction["interstate_name"],
                                "exit_number": junction["exit_number"],
                                "direction": junction["direction_code"],
                                "city_name": city["name"],
                                "city_osm_id": city["osm_id"],
                                "city_distance_m": round(distance, 3),
                                "city_similarity": similarity,
                            }
                        )
        if not choices:
            return {**base, "reason": "no_verified_highway_exit_near_named_city"}
        choices.sort(
            key=lambda row: (-row["city_similarity"], row["city_distance_m"], row["exit_id"])
        )
        first = choices[0]
        point = Coordinate(**first["coordinate"])
        if any(
            distance_meters(point, Coordinate(**row["coordinate"])) > MAX_INTERCHANGE_SPREAD_M
            for row in choices
            if row["city_similarity"] == first["city_similarity"]
        ):
            return {
                **base,
                "reason": "ambiguous_highway_exit_locations",
                "candidates": choices[:10],
            }
        return {
            **base,
            **first,
            "status": "approximate",
            "reason": "verified_exit_point_approximate_station",
            "city": query.city,
            "state": query.state,
            "country": query.country,
            "criteria": {
                "name": 0.0,
                "locality": first["city_similarity"],
                "highways": 1.0,
                "exits": 1.0,
            },
            "geographic_score": round(100 * (0.30 * first["city_similarity"] + 0.10) / 0.40, 3),
            "station_match_score": round(30 * first["city_similarity"] + 10, 3),
        }


@lru_cache(maxsize=1)
def get_exit_locator():
    path = REFERENCE_DIR / "geographic-index.json"
    return ExitLocator(json.loads(path.read_text())) if path.exists() else None


def apply_geographic_fallback(result, query, *, input_conflict=False):
    if result.get("highest_matching_coordinate"):
        result["geographic_fallback"] = {
            "status": "not_needed",
            "reason": "name_and_locality_candidate_found",
        }
        return
    if input_conflict:
        result["geographic_fallback"] = {"status": "unresolved", "reason": "input_records_disagree"}
        return
    locator = get_exit_locator()
    fallback = (
        locator.locate(query)
        if locator
        else {
            "status": "unresolved",
            "reason": "geographic_reference_not_prepared",
            "coordinate": None,
        }
    )
    if locator and fallback["status"] == "unresolved":
        from .coverage import get_coverage_resolver

        coverage = get_coverage_resolver()
        if coverage:
            fallback = coverage.resolve(query)
    result["geographic_fallback"] = fallback
    if fallback["status"] == "approximate":
        result["status"] = "approximate"
        result["reasons"].append("geographic_approximation_not_verified_fuel_station")
        result["approximate_coordinate"] = fallback["coordinate"]
