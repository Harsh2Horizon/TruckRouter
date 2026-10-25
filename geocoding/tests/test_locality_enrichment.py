from copy import deepcopy
from unittest import TestCase

from geocoding.locality_enrichment import enrich_record, original_locality_address
from geocoding.matching import rank_candidates
from geocoding.normalization import BOUNDARY_CITY_WARNING, NEAREST_CITY_WARNING, city_key
from geocoding.tests.test_domain import candidate, query


class LocalityEnrichmentTests(TestCase):
    def record(self, city="", state=""):
        address = query(city=city, state=state).model_dump(mode="json")
        # The query helper infers country from state; empty-state fixtures still carry US.
        address.update(city=city, state=state, country="US")
        return {
            "id": 1,
            "provider": "osm",
            "city_key": city,
            "state": state,
            "country": "US",
            "latitude": 44.0,
            "longitude": -90.5,
            "address": address,
            "provenance": [{"dataset": "openstreetmap"}],
            "raw": {"tags": {}},
        }

    def mapping(self, method="point_in_polygon"):
        return {
            "country": "US",
            "state": "WI",
            "region_source": "test states",
            "city": "Tomah",
            "city_method": method,
            "city_source": "test places",
            "city_reference_id": "place/1",
            "city_distance_m": 1000,
            "reference_latitude": 44.01,
            "reference_longitude": -90.5,
        }

    def test_fills_index_and_canonical_address_without_mutating_raw_or_original(self):
        original = self.record()
        snapshot = deepcopy(original)
        result = enrich_record(original, self.mapping(), "test-time")
        self.assertEqual(original, snapshot)
        self.assertEqual(result["city_key"], "TOMAH")
        self.assertEqual(result["address"]["city"], "TOMAH")
        self.assertEqual(result["state"], "WI")
        self.assertEqual(result["raw"], snapshot["raw"])
        self.assertEqual(result["latitude"], snapshot["latitude"])
        self.assertIn(BOUNDARY_CITY_WARNING, result["address"]["warnings"])
        self.assertEqual(result["provenance"][-1]["original"]["city"], "")

    def test_known_fields_are_never_overwritten(self):
        original = self.record(city="TOMAH", state="WI")
        mapping = {**self.mapping(), "city": "Other Town"}
        self.assertIsNone(enrich_record(original, mapping, "test-time"))

    def test_conflicting_known_state_or_country_does_not_receive_a_city(self):
        self.assertIsNone(enrich_record(self.record(state="MN"), self.mapping(), "test"))
        self.assertIsNone(enrich_record(self.record(), {**self.mapping(), "country": "CA"}, "test"))

    def test_missing_coordinates_or_unresolved_geometry_are_not_fabricated(self):
        record = self.record()
        record["latitude"] = None
        self.assertIsNone(enrich_record(record, self.mapping(), "test"))
        self.assertIsNone(enrich_record(self.record(), None, "test"))

    def test_nearest_locality_keeps_point_but_requires_review(self):
        updated = enrich_record(self.record(), self.mapping("nearest_settlement"), "test")
        self.assertIn(NEAREST_CITY_WARNING, updated["address"]["warnings"])
        place = candidate()
        place.address.warnings.append(NEAREST_CITY_WARNING)
        result = rank_candidates(query(), [place])
        self.assertIsNotNone(result["highest_matching_coordinate"])
        self.assertEqual(result["status"], "needs_review")
        self.assertIn("approximate_locality_requires_review", result["reasons"])
        self.assertEqual(
            result["per_source"]["test"]["criteria"]["locality"]["candidate_city_method"],
            "nearest_settlement",
        )

    def test_polygon_locality_can_pass_normal_acceptance(self):
        place = candidate()
        place.address.warnings.append(BOUNDARY_CITY_WARNING)
        result = rank_candidates(query(), [place])
        self.assertEqual(result["status"], "accepted")

    def test_enrichment_is_idempotent(self):
        updated = enrich_record(self.record(), self.mapping(), "first")
        self.assertIsNone(enrich_record(updated, self.mapping(), "second"))

    def test_non_indexable_city_is_not_recorded_as_a_successful_fill(self):
        record = self.record(state="WI")
        self.assertIsNone(enrich_record(record, {**self.mapping(), "city": "ᑰᔾᔪᐊᖅ"}, "test"))

    def test_historical_audit_recovers_provider_fields_before_enrichment(self):
        record = self.record()
        enriched = enrich_record(record, self.mapping("nearest_settlement"), "today")
        restored = original_locality_address(enriched["address"], enriched["provenance"])
        self.assertEqual(restored, record["address"])

    def test_census_legal_city_labels_match_common_names_without_generic_suffix_stripping(self):
        self.assertEqual(city_key("Indianapolis city (balance)"), city_key("Indianapolis"))
        self.assertEqual(
            city_key("Louisville/Jefferson County metro government (balance)"),
            city_key("Louisville"),
        )
        self.assertEqual(city_key("Kansas City"), "KANSAS CITY")
