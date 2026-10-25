import csv
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from django.core.management import call_command

from geocoding.census import address_key, eligible_address
from geocoding.coverage import CoverageResolver, SettlementIndex, parse_raw
from geocoding.exports import comparison_row
from geocoding.geography import apply_geographic_fallback
from geocoding.matching import rank_candidates
from geocoding.tests.test_domain import candidate, query


class CoverageTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.city = {
            "name": "Tomah",
            "latitude": 44.0,
            "longitude": -90.5,
            "id": "city/1",
            "source": "test gazetteer",
            "feature_code": "PPL",
            "country": "US",
            "state": "WI",
        }
        self.settlements = Mock()
        self.settlements.candidates.return_value = [(self.city, 1.0)]
        self.settlements.admin = {}
        self.roads = Mock()
        self.roads.closest.return_value = None
        self.roads.point_in_state.return_value = True
        self.roads.state_point.return_value = {"latitude": 44.5, "longitude": -89.0}
        self.exit = {
            "exit_id": "node/1",
            "interstate_name": "I-94",
            "exit_number": "143",
            "lat": 44.01,
            "lon": -90.5,
        }

    def resolver(self, exits=None):
        locator = SimpleNamespace(exits=exits or {("US", "WI", "94", "143"): [self.exit]})
        return CoverageResolver(
            self.temp.name, settlements=self.settlements, roads=self.roads, exit_locator=locator
        )

    def test_known_exit_is_used_before_highway_or_city_and_is_not_a_station_match(self):
        result = self.resolver().resolve(query())
        self.assertEqual(result["method"], "highway_exit")
        self.assertEqual(result["coordinate"]["latitude"], 44.01)
        self.assertTrue(result["exit_matched"])
        self.assertFalse(result["station_verified"])
        self.assertEqual(result["match_score"], 0)
        self.roads.closest.assert_not_called()

    def test_shared_route_exit_is_inferred_only_from_real_nearby_route_membership(self):
        result = self.resolver().resolve(query(address="I-80/94, EXIT 143"))
        self.assertEqual(result["method"], "highway_exit")
        self.assertEqual(result["exit_association"], "route_and_city_inferred")
        self.assertEqual(result["reference_name"], "I-94 Exit 143")

    def test_exit_letter_hyphen_is_normalized_against_reference(self):
        result = self.resolver(
            {("US", "WI", "94", "143A"): [{**self.exit, "exit_number": "143A"}]}
        ).resolve(query(address="I-94, EXIT 143-A"))
        self.assertEqual(result["method"], "highway_exit")

    def test_wrong_route_or_far_exit_does_not_become_a_verified_exit(self):
        result = self.resolver().resolve(query(address="I-90, EXIT 143"))
        self.assertEqual(result["method"], "named_locality")
        self.assertFalse(result["exit_matched"])
        self.assertEqual(result["coordinate"]["latitude"], 44.0)
        distant = {**self.exit, "lat": 46.0}
        result = self.resolver({("US", "WI", "94", "143"): [distant]}).resolve(query())
        self.assertEqual(result["method"], "named_locality")

    def test_highway_point_is_explicitly_coarser_than_an_exit(self):
        self.roads.closest.return_value = {
            "coordinate": {"latitude": 44.02, "longitude": -90.51},
            "reference_id": "road/1",
            "reference_name": "I-90",
        }
        result = self.resolver().resolve(query(address="I-90, EXIT 143"))
        self.assertEqual(result["precision"], "road_segment")
        self.assertFalse(result["exit_matched"])
        self.assertTrue(result["needs_review"])

    def test_out_of_state_locality_anchor_is_not_used(self):
        self.roads.point_in_state.return_value = False
        result = self.resolver().resolve(query(address=""))
        self.assertEqual(result["method"], "administrative_area")
        self.assertEqual(result["precision"], "state_or_province")

    def test_unknown_geography_returns_unresolved_instead_of_zero_coordinates(self):
        self.settlements.candidates.return_value = []
        self.roads.state_point.return_value = None
        result = self.resolver().resolve(query(state="XX", city="Unknown"))
        self.assertIsNone(result["coordinate"])
        self.assertEqual(result["status"], "unresolved")

    def test_census_address_is_an_interpolation_without_brand_points(self):
        q = query(address="123 Main Street")
        resolver = self.resolver()
        resolver.census[address_key(q)] = {
            "status": "matched",
            "coordinate": {"latitude": 44.03, "longitude": -90.5},
            "matched_address": "123 MAIN ST, TOMAH, WI",
            "response_file": "cached.json",
        }
        result = resolver.resolve(q)
        self.assertEqual(result["method"], "census_address")
        self.assertEqual(result["precision"], "address_range")
        self.assertEqual(result["match_score"], 0)

    def test_raw_interstate_typo_and_census_eligibility(self):
        q, corrections = parse_raw(
            {"name": "ROAD RANGER", "address": "1-40 EXIT 77", "city": "Amarillo", "state": "TX"}
        )
        self.assertEqual(q.highways[0].number, "40")
        self.assertTrue(corrections)
        self.assertFalse(eligible_address(q))
        self.assertFalse(eligible_address(query(address="34 MILE WEST OF EXIT 11/146")))
        self.assertTrue(eligible_address(query(address="123 Main Street")))

    def test_coarse_api_export_keeps_original_provider_coordinates_blank(self):
        result = rank_candidates(query(), [], providers=["alltheplaces", "overture", "osm"])
        fallback = self.resolver().resolve(query(address=""))
        locator = Mock()
        locator.locate.return_value = {"status": "unresolved", "reason": "no_verified_exit"}
        coverage = Mock()
        coverage.resolve.return_value = fallback
        with (
            patch("geocoding.geography.get_exit_locator", return_value=locator),
            patch("geocoding.coverage.get_coverage_resolver", return_value=coverage),
        ):
            apply_geographic_fallback(result, query())
        row = comparison_row("9", query(), result, "run")
        self.assertEqual(row["prediction_method"], "named_locality")
        self.assertEqual(row["prediction_precision"], "locality")
        self.assertTrue(row["is_approximate"])
        self.assertEqual(row["predicted_latitude"], 44.0)
        self.assertEqual(row["osm_latitude"], "")
        self.assertEqual(row["highest_matching_score"], 0)

    def test_compound_city_variants_match_without_crossing_state_or_country(self):
        path = Path(self.temp.name) / "cities.ndjson"
        row = {
            **self.city,
            "name": "McCalla",
            "ascii_name": "McCalla",
            "aliases": [],
            "state": "AL",
            "population": 1000,
        }
        path.write_text(json.dumps(row) + "\n")
        index = SettlementIndex({"settlements_file": str(path)}, None)
        self.assertEqual(index.candidates(query(city="MC CALLA", state="AL"))[0][1], 1)
        self.assertEqual(index.candidates(query(city="MC CALLA", state="WI")), [])

    def test_raw_rows_with_conflicting_city_state_are_resolved_individually(self):
        root = Path(self.temp.name)
        raw = root / "raw.csv"
        raw.write_text(
            "OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price\n"
            "9,KWIK TRIP #796,,Tomah,WI,420,3.28000000\n"
            "9,KWIK TRIP #796,,Duluth,MN,420,3.29000000\n"
        )
        base = root / "base"
        base.mkdir()
        (base / "report.json").write_text(
            json.dumps({"status": "completed", "algorithm_version": "test"})
        )
        q = query(address="")
        result = rank_candidates(q, [candidate()], providers=["alltheplaces", "overture", "osm"])
        row = comparison_row("9", q, result, "run")
        with (base / "geocodes.csv").open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(row))
            writer.writeheader()
            writer.writerow(row)
        resolver = Mock()
        resolver.manifest = {}
        resolver.roads.point_in_state.return_value = True
        resolver.resolve.return_value = {
            "coordinate": {"latitude": 46.8, "longitude": -92.1},
            "method": "named_locality",
            "precision": "locality",
            "source": "test gazetteer",
            "is_approximate": True,
            "match_score": 0,
            "needs_review": True,
        }
        with patch(
            "geocoding.management.commands.complete_geocoding_coverage.CoverageResolver",
            return_value=resolver,
        ):
            call_command(
                "complete_geocoding_coverage",
                csv=str(raw),
                base_run=str(base),
                output_dir=str(root / "output"),
                stdout=io.StringIO(),
            )
        with (root / "output/all-records-geocoded.csv").open() as stream:
            exported = list(csv.DictReader(stream))
        self.assertEqual([r["Retail Price"] for r in exported], ["3.28000000", "3.29000000"])
        self.assertEqual(exported[0]["Location Method"], "fuel_poi")
        self.assertEqual(exported[1]["Location Method"], "named_locality")
        self.assertEqual(float(exported[1]["Latitude"]), 46.8)
        self.assertEqual(exported[1]["Input Conflict"], "True")

    def test_completion_preserves_existing_city_and_road_approximation_methods(self):
        root = Path(self.temp.name)
        raw = root / "raw.csv"
        raw.write_text(
            "OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price\n"
            "9,KWIK TRIP #796,,Tomah,WI,420,3.28000000\n"
        )
        base = root / "base"
        base.mkdir()
        (base / "report.json").write_text(
            json.dumps({"status": "completed", "algorithm_version": "test"})
        )
        resolver = Mock()
        resolver.manifest = {}
        resolver.roads.point_in_state.return_value = True
        q = query(address="")
        result = rank_candidates(q, [], providers=["alltheplaces", "overture", "osm"])
        for method, precision in [
            ("named_locality", "locality"),
            ("highway_near_city", "road_segment"),
        ]:
            with self.subTest(method=method):
                row = comparison_row("9", q, result, "run")
                row.update(
                    predicted_latitude=44.0,
                    predicted_longitude=-90.5,
                    prediction_method=method,
                    prediction_precision=precision,
                    prediction_source="test gazetteer",
                    prediction_score=0,
                    status="approximate",
                    fallback_reason="geographic approximation",
                )
                with (base / "geocodes.csv").open("w", newline="") as stream:
                    writer = csv.DictWriter(stream, fieldnames=list(row))
                    writer.writeheader()
                    writer.writerow(row)
                with patch(
                    "geocoding.management.commands.complete_geocoding_coverage.CoverageResolver",
                    return_value=resolver,
                ):
                    call_command(
                        "complete_geocoding_coverage",
                        csv=str(raw),
                        base_run=str(base),
                        output_dir=str(root / "output"),
                        stdout=io.StringIO(),
                    )
                with (root / "output/all-records-geocoded.csv").open() as stream:
                    exported = next(csv.DictReader(stream))
                self.assertEqual(exported["Location Method"], method)
                self.assertEqual(exported["Coordinate Precision"], precision)
                self.assertEqual(exported["Exit Matched"], "False")
                self.assertEqual(float(exported["Match Score"]), 0)
                self.assertEqual(exported["Needs Review"], "True")
                resolver.resolve.assert_not_called()
