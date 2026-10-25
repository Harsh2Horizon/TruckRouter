import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from geocoding.exports import comparison_row
from geocoding.geography import ExitLocator, apply_geographic_fallback, boundary_state_code
from geocoding.matching import rank_candidates
from geocoding.parsing import parse_address
from geocoding.scoring import WEIGHTS, input_addresses_disagree, score_addresses
from geocoding.tests.factories import candidate, query


class BrandLocalityTests(TestCase):
    def test_only_requested_criteria_have_weights_and_name_is_dominant(self):
        self.assertEqual(WEIGHTS, {"name": 0.60, "locality": 0.30, "highways": 0.07, "exits": 0.03})

    def test_ids_house_postcode_and_wikidata_do_not_change_the_score(self):
        original, changed = query(), query(name="KWIK TRIP #DIFFERENT")
        changed.store_id = "unrelated-provider-id"
        changed.house_number, changed.street, changed.postcode = "999", "Other street", "99999"
        changed.brand_wikidata, original.brand_wikidata = "Qwrong", "Qdifferent"
        self.assertEqual(score_addresses(original, changed).score, 100)
        self.assertFalse(input_addresses_disagree(original, changed))

    def test_raw_name_can_match_when_brand_field_is_missing_or_different(self):
        item = query(name="KWIK TRIP #OTHER")
        item.brand = "CORPORATE HOLDING COMPANY"
        self.assertEqual(score_addresses(query(), item).components["name"], 1)

    def test_joint_locality_normalizes_abbreviations_and_country_variants(self):
        left = parse_address(
            {"name": "Pilot #1", "city": "Ft. Worth", "state": "Tex.", "country": "USA"}
        )
        right = parse_address(
            {
                "name": "PILOT #987",
                "city": "Fort Worth",
                "state": "Texas",
                "country": "United States",
            }
        )
        score = score_addresses(left, right)
        self.assertTrue(score.locality["complete"])
        self.assertEqual(score.score, 100)
        left = parse_address(
            {"name": "Shell", "city": "St. Catharines", "state": "Ont.", "country": "Canada"}
        )
        right = parse_address(
            {"name": "Shell", "city": "Saint Catharines", "state": "Ontario", "country": "CAN"}
        )
        self.assertTrue(score_addresses(left, right).locality["complete"])

    def test_wrong_or_missing_city_country_or_state_blocks_prediction(self):
        for field, value in (
            ("city", "Tomahawk"),
            ("city", ""),
            ("state", "MN"),
            ("country", "CA"),
        ):
            item = candidate()
            setattr(item.address, field, value)
            result = rank_candidates(query(), [item])
            self.assertEqual(result["score"], 0, field)
            self.assertIsNone(result["highest_matching_coordinate"], field)

    def test_unknown_country_and_state_cannot_become_valid_by_equal_strings(self):
        left, right = query(), query()
        left.country = right.country = "ZZ"
        left.state = right.state = "XX"
        self.assertEqual(score_addresses(left, right).score, 0)

    def test_score_contributions_reconcile_and_missing_road_information_earns_no_points(self):
        score = score_addresses(query(), query(address=""))
        self.assertEqual(score.score, 90)
        self.assertEqual(score.contributions["name"], 60)
        self.assertEqual(score.contributions["locality"], 30)
        self.assertEqual(score.contributions["highways"], 0)
        self.assertAlmostEqual(sum(score.contributions.values()), score.score)

    def test_us_highway_alias_and_empty_road_jurisdiction_normalize(self):
        left = query(address="US-12")
        right = query(address="US Highway 12")
        self.assertEqual(score_addresses(left, right).components["highways"], 1)
        right.highways[0].jurisdiction = ""
        self.assertEqual(score_addresses(left, right).components["highways"], 1)

    def test_large_cross_source_deviation_is_exposed_and_requires_review(self):
        first, second = candidate("a"), candidate("b", latitude=44.08)
        first.provider, second.provider = "alltheplaces", "osm"
        result = rank_candidates(query(), [first, second])
        self.assertEqual(result["coordinate_disagreement"]["severity"], "severe")
        self.assertGreater(result["coordinate_disagreement"]["max_distance_m"], 5000)
        self.assertEqual(result["status"], "needs_review")
        self.assertIn("cross_source_coordinates_diverge", result["reasons"])


class ExitFallbackTests(TestCase):
    def test_published_boundary_subdivision_code_survives_country_prefix_typo(self):
        self.assertEqual(boundary_state_code({"shapeISO": "SU-SD"}), "SD")
        self.assertEqual(boundary_state_code({"shapeISO": "US-WI"}), "WI")

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.exits_file, self.cities_file = root / "exits.jsonl", root / "cities.jsonl"
        self.junction = {
            "exit_id": "node/1",
            "interstate_name": "I-94",
            "exit_number": "143",
            "direction_code": "west",
            "lat": 44.01,
            "lon": -90.5,
            "state": "WI",
            "country": "US",
        }
        city = {
            "country": "US",
            "state_iso": "US-WI",
            "name": "Tomah",
            "osm_id": 1,
            "latitude": 44.0,
            "longitude": -90.5,
            "tags": {},
        }
        self.cities_file.write_text(json.dumps(city) + "\n")
        self.manifest = {
            "exits_file": str(self.exits_file),
            "cities_file": str(self.cities_file),
            "exit_source": {"release": "test"},
            "locality_basis": "state and named-city proximity",
        }

    def locator(self, rows=None):
        self.exits_file.write_text(
            "".join(json.dumps(row) + "\n" for row in (rows or [self.junction]))
        )
        return ExitLocator(self.manifest)

    def test_fallback_uses_actual_verified_exit_point_and_marks_station_approximate(self):
        locator = self.locator()
        fallback = locator.locate(query())
        self.assertEqual(fallback["status"], "approximate")
        self.assertEqual(fallback["coordinate"]["latitude"], self.junction["lat"])
        self.assertFalse(fallback["fuel_station_verified"])
        self.assertEqual(fallback["geographic_score"], 100)
        self.assertEqual(fallback["station_match_score"], 40)
        result = rank_candidates(query(), [], providers=["alltheplaces", "overture", "osm"])
        with patch("geocoding.geography.get_exit_locator", return_value=locator):
            apply_geographic_fallback(result, query())
        row = comparison_row("9", query(), result, "run")
        self.assertEqual(row["prediction_method"], "highway_exit")
        self.assertTrue(row["is_approximate"])
        self.assertEqual(row["predicted_latitude"], 44.01)
        self.assertEqual(row["alltheplaces_latitude"], "")
        self.assertEqual(row["highest_matching_score"], 0)
        self.assertIsNone(result["coordinate"])

    def test_wrong_highway_and_unscoped_exit_are_not_guessed(self):
        locator = self.locator()
        self.assertEqual(locator.locate(query(address="I-90, EXIT 143"))["status"], "unresolved")
        self.assertEqual(locator.locate(query(address="I-80/94, EXIT 143"))["status"], "unresolved")

    def test_fallback_normalizes_leading_zeroes_in_route_number(self):
        self.assertEqual(
            self.locator().locate(query(address="I-094, EXIT 143"))["status"], "approximate"
        )

    def test_far_exit_or_ambiguous_exit_locations_do_not_produce_a_prediction(self):
        locator = self.locator([{**self.junction, "lat": 45.0}])
        self.assertEqual(locator.locate(query())["status"], "unresolved")
        locator = self.locator(
            [self.junction, {**self.junction, "exit_id": "node/2", "lat": 44.06}]
        )
        self.assertEqual(locator.locate(query())["reason"], "ambiguous_highway_exit_locations")

    def test_canadian_or_missing_city_coverage_is_explicit(self):
        locator = self.locator()
        ca = query(city="Toronto", state="ON")
        self.assertEqual(locator.locate(ca)["reason"], "no_verified_exit_coverage_for_country")
        self.assertEqual(locator.locate(query(city=""))["reason"], "incomplete_input_locality")

    def test_input_conflict_prevents_approximation(self):
        result = rank_candidates(query(), [])
        apply_geographic_fallback(result, query(), input_conflict=True)
        self.assertNotIn("approximate_coordinate", result)
        self.assertEqual(result["geographic_fallback"]["reason"], "input_records_disagree")
