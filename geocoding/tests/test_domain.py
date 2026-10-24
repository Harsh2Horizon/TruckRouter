"""Run with unittest independently of Django or the database."""

import unittest

from pydantic import ValidationError

from geocoding.matching import distance_meters, rank_candidates, score_addresses
from geocoding.normalization import brand_key, store_key
from geocoding.parsing import parse_address
from geocoding.providers import alltheplaces, nominatim, osm, overture
from geocoding.schemas import Coordinate, PlaceCandidate, RoadNetwork


def query(**changes):
    return parse_address(
        {
            "name": "KWIK TRIP #796",
            "address": "I-94, EXIT 143 & US-12 & SR-21",
            "city": "Tomah",
            "state": "WI",
            **changes,
        }
    )


def candidate(source_id="one", *, latitude=44.0, longitude=-90.5, **changes):
    # These are deliberately synthetic points, not verified coordinates of store 796.
    return PlaceCandidate(
        provider="test",
        namespace="synthetic",
        source_id=source_id,
        address=query(**changes),
        coordinate=Coordinate(latitude=latitude, longitude=longitude),
        precision="poi",
    )


class ParsingTests(unittest.TestCase):
    def test_requested_example(self):
        address = query()
        self.assertEqual(
            (address.brand, address.store_id, address.city, address.state),
            ("KWIK TRIP", "796", "TOMAH", "WI"),
        )
        self.assertEqual(
            [r.network for r in address.highways],
            [RoadNetwork.INTERSTATE, RoadNetwork.US_HIGHWAY, RoadNetwork.STATE],
        )
        self.assertEqual([r.number for r in address.highways], ["94", "12", "21"])
        self.assertEqual(address.exits[0].identifier, "143")
        self.assertEqual(address.exits[0].highway_key, address.highways[0].key)
        self.assertEqual(address.country, "US")

    def test_shared_route_does_not_assign_exit_numbering(self):
        address = query(address="I-80/94, EXIT 2")
        self.assertEqual([r.number for r in address.highways], ["80", "94"])
        self.assertIsNone(address.exits[0].highway_key)

    def test_multiple_exits_attach_to_their_routes(self):
        address = query(address="I-40, EXIT 280 & I-55, EXIT 4")
        self.assertEqual([e.highway_key for e in address.exits], [r.key for r in address.highways])

    def test_route_variants_and_suffixes(self):
        address = query(address="US-20ALT & I-94 BUS & SR-21A & CR-138")
        self.assertEqual(
            [(r.number, r.variant) for r in address.highways],
            [("20", "alternate"), ("94", "business"), ("21A", "main"), ("138", "main")],
        )

    def test_canadian_country_inference(self):
        address = query(state="AB", address="HWY 2 SOUTH & TCH-16 & PR-305")
        self.assertEqual(address.country, "CA")
        self.assertEqual(address.highways[0].network, RoadNetwork.PROVINCIAL)
        self.assertEqual(address.highways[0].jurisdiction, "CA-AB")
        self.assertEqual(address.highways[0].direction, "S")

    def test_state_names_and_iso_codes(self):
        self.assertEqual(query(state="Wisconsin").state, "WI")
        self.assertEqual(query(state="US-WI").state, "WI")
        self.assertEqual(query(state="Ontario").country, "CA")

    def test_postal_address(self):
        address = query(address="123 Main Street")
        self.assertEqual((address.house_number, address.street), ("123", "Main Street"))

    def test_store_id_retains_zeroes_and_alphanumerics(self):
        address = query(name="KWIK TRIP #00796-A")
        self.assertEqual(address.store_id, "00796-A")
        self.assertEqual(query(name="KWIK TRIP STORE 796").store_id, "796")

    def test_unknown_context_is_retained(self):
        address = query(address="I-94, EXIT 143 & 3 MILES WEST")
        self.assertIn("3 MILES WEST", address.unparsed_address)

    def test_brands_are_not_overmerged(self):
        self.assertNotEqual(brand_key("KWIK TRIP"), brand_key("QuikTrip"))
        self.assertEqual(brand_key("PILOT TRAVEL CENTER"), brand_key("PILOT"))

    def test_invalid_input_is_rejected(self):
        with self.assertRaises(ValidationError):
            parse_address({"name": "", "unexpected": True})

    def test_alphanumeric_store_punctuation_remains_significant(self):
        self.assertNotEqual(store_key("AB-C"), store_key("A-BC"))
        self.assertEqual(store_key("00796"), store_key("796"))


class MatchingTests(unittest.TestCase):
    def test_small_city_typo_can_match_with_exact_branch_evidence(self):
        result = rank_candidates(query(), [candidate(city="Tommah")])
        self.assertEqual(result["status"], "accepted")
        self.assertLess(result["score"], 100)

    def test_house_and_street_can_resolve_a_branch_without_store_number(self):
        address = query(name="KWIK TRIP", address="123 Main Street")
        item = candidate(name="KWIK TRIP", address="123 Main St")
        self.assertEqual(rank_candidates(address, [item])["status"], "accepted")

    def test_house_number_conflict_is_not_masked_by_street_similarity(self):
        address = query(address="123 Main Street")
        item = candidate(address="125 Main Street")
        result = rank_candidates(address, [item])
        self.assertEqual(result["status"], "unmatched")
        self.assertIn("house_number_conflict", result["candidates"][0]["evidence"]["conflicts"])

    def test_exit_number_alone_is_not_evidence_across_different_highways(self):
        score = score_addresses(query(address="I-94, EXIT 143"), query(address="I-90, EXIT 143"))
        self.assertEqual(score.components["exits"], 0)

    def test_exact_match_accepts_and_explains_score(self):
        result = rank_candidates(query(), [candidate()])
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["score"], 100)  # Every applicable input field is corroborated.
        self.assertEqual(result["candidates"][0]["evidence"]["components"]["store_id"], 1)

    def test_different_store_is_hard_conflict(self):
        score = score_addresses(query(), query(name="KWIK TRIP #797"))
        self.assertEqual(score.score, 0)
        self.assertIn("store_id_conflict", score.conflicts)

    def test_different_state_is_hard_conflict(self):
        score = score_addresses(query(), query(state="MN"))
        self.assertIn("state_conflict", score.conflicts)

    def test_missing_store_does_not_look_like_exact_branch_match(self):
        result = rank_candidates(query(), [candidate(name="KWIK TRIP")])
        self.assertEqual(result["status"], "needs_review")
        self.assertIsNone(result["coordinate"])
        self.assertIn("insufficient_branch_or_locality_evidence", result["reasons"])

    def test_same_store_far_apart_requires_review(self):
        result = rank_candidates(query(), [candidate(), candidate("two", latitude=45)])
        self.assertEqual(result["status"], "needs_review")
        self.assertIn("competing_location_with_similar_score", result["reasons"])

    def test_nearby_sources_cluster_with_real_representative_point(self):
        first, second = candidate(), candidate("two", latitude=44.0001)
        second.provider = "another"
        result = rank_candidates(query(), [first, second])
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(len(result["clusters"]), 1)
        self.assertIn(result["coordinate"], [c.coordinate.model_dump() for c in [first, second]])

    def test_complete_link_clustering_does_not_chain(self):
        result = rank_candidates(
            query(),
            [
                candidate("a", latitude=44),
                candidate("b", latitude=44.002),
                candidate("c", latitude=44.004),
            ],
        )
        self.assertEqual(len(result["clusters"]), 2)
        self.assertEqual(result["status"], "needs_review")

    def test_source_count_does_not_increase_match_score(self):
        one = rank_candidates(query(), [candidate()])
        many = rank_candidates(query(), [candidate(str(n)) for n in range(10)])
        self.assertEqual(one["score"], many["score"])

    def test_city_centroid_is_never_accepted(self):
        item = candidate()
        item.precision = "locality"
        result = rank_candidates(query(), [item])
        self.assertEqual(result["status"], "needs_review")
        self.assertIsNone(result["coordinate"])

    def test_no_coordinate_is_never_accepted(self):
        item = candidate()
        item.coordinate = None
        result = rank_candidates(query(), [item])
        self.assertEqual(result["status"], "needs_review")

    def test_closed_place_is_ineligible(self):
        item = candidate()
        item.operating_status = "permanently_closed"
        self.assertEqual(rank_candidates(query(), [item])["status"], "unmatched")

    def test_truncated_candidates_cannot_auto_accept(self):
        result = rank_candidates(query(), [candidate()], truncated=True)
        self.assertIn("candidate_limit_reached", result["reasons"])
        self.assertEqual(result["status"], "needs_review")

    def test_missing_fields_have_no_similarity_component(self):
        score = score_addresses(query(), query(name="KWIK TRIP", address=""))
        self.assertNotIn("store_id", score.components)
        self.assertIn("store_id", score.missing)
        self.assertLess(score.coverage, 1)

    def test_poi_existence_confidence_is_not_a_matching_score(self):
        item = candidate()
        first = rank_candidates(query(), [item])
        item.existence_confidence = 0.01
        second = rank_candidates(query(), [item])
        self.assertEqual(first["score"], second["score"])

    def test_coordinate_validation_and_distance(self):
        with self.assertRaises(ValidationError):
            Coordinate(latitude=float("nan"), longitude=1)
        with self.assertRaises(ValidationError):
            Coordinate(latitude=91, longitude=0)
        self.assertEqual(distance_meters(candidate().coordinate, candidate().coordinate), 0)


class AdapterTests(unittest.TestCase):
    def test_atp_opaque_ref_is_not_automatically_store_id(self):
        feature = {
            "id": "abc",
            "properties": {
                "@spider": "kwik_trip",
                "ref": "opaque-id",
                "name": "Kwik Trip",
                "addr:state": "WI",
            },
            "geometry": {"type": "Point", "coordinates": [-90.5, 44]},
        }
        item = alltheplaces(feature)
        self.assertEqual(item.address.store_id, "")
        self.assertEqual(item.coordinate.latitude, 44)
        self.assertEqual(alltheplaces(feature, ref_is_store_id=True).address.store_id, "opaque-id")

    def test_overture_preserves_lineage_and_confidence(self):
        feature = {
            "id": "gers-id",
            "geometry": {"type": "Point", "coordinates": [-90.5, 44]},
            "properties": {
                "names": {"primary": "Kwik Trip #796"},
                "brand": {"names": {"primary": "Kwik Trip"}},
                "addresses": [{"locality": "Tomah", "region": "US-WI", "country": "US"}],
                "confidence": 0.99,
                "sources": [{"dataset": "AllThePlaces", "record_id": "abc"}],
            },
        }
        item = overture(feature, "2026-test")
        self.assertEqual(item.address.store_id, "796")
        self.assertEqual(item.address.state, "WI")
        self.assertEqual(item.existence_confidence, 0.99)
        self.assertEqual(item.provenance[0]["dataset"], "AllThePlaces")

    def test_missing_geometry_is_allowed(self):
        item = alltheplaces(
            {
                "id": "no-point",
                "geometry": None,
                "properties": {"@spider": "test", "name": "Kwik Trip"},
            }
        )
        self.assertIsNone(item.coordinate)

    def test_osm_ways_use_explicit_centers(self):
        item = osm(
            {
                "type": "way",
                "id": 42,
                "center": {"lat": 44, "lon": -90.5},
                "tags": {"amenity": "fuel", "name": "Kwik Trip #796"},
            }
        )
        self.assertEqual((item.provider, item.namespace, item.source_id), ("osm", "way", "42"))
        self.assertEqual(item.precision, "poi_center")

    def test_nominatim_uses_osm_identity_and_rejects_generic_precision(self):
        item = nominatim(
            {
                "place_id": 999,
                "osm_type": "relation",
                "osm_id": 42,
                "lat": "44",
                "lon": "-90.5",
                "type": "city",
                "name": "Tomah",
                "address": {"city": "Tomah", "state": "Wisconsin", "country_code": "us"},
            }
        )
        self.assertEqual(item.source_id, "42")
        self.assertEqual(item.precision, "unknown")

    def test_invalid_coordinate_and_unknown_identity_fail(self):
        with self.assertRaises(ValueError):
            osm({"type": "node", "tags": {}})
        with self.assertRaises(ValueError):
            alltheplaces(
                {
                    "id": "bad",
                    "properties": {"@spider": "test"},
                    "geometry": {"type": "Point", "coordinates": [200, 100]},
                }
            )


if __name__ == "__main__":
    unittest.main()
