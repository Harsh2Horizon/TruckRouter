import csv
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from django.test import TestCase as DatabaseTestCase

from geocoding.exports import comparison_row, enrich_input_csv
from geocoding.matching import WEIGHTS, rank_candidates, score_addresses
from geocoding.services import resolve, retrieve_candidates, save_place
from geocoding.tests.test_domain import candidate, query


class EvidenceScoreTests(TestCase):
    def test_weights_and_score_range(self):
        self.assertAlmostEqual(sum(WEIGHTS.values()), 1)
        full = score_addresses(query(), query())
        self.assertEqual(full.score, 100)
        self.assertEqual(full.coverage, 1)
        self.assertEqual(full.evidence_strength, 1)
        self.assertNotIn("postcode", full.missing_in_query)
        self.assertNotIn("postcode", full.missing)

    def test_missing_store_is_not_counted_as_a_name_disagreement(self):
        score = score_addresses(query(), query(name="KWIK TRIP"))
        self.assertEqual(score.components["name"], 1)
        self.assertNotIn("store_id", score.components)
        self.assertNotIn("store_id", score.missing)
        self.assertEqual(score.coverage, 1)
        self.assertEqual(score.score, 100)

    def test_sparse_exact_fields_cannot_score_like_a_full_address(self):
        partial = query(name="KWIK TRIP", address="", city="")
        score = score_addresses(query(), partial)
        full = score_addresses(query(), query())
        self.assertLess(score.score, 65)
        self.assertLess(score.score, full.score)
        self.assertIn("locality_incomplete", score.conflicts)

    def test_brand_aliases_apply_to_existing_canonical_records(self):
        left = query(name="FLYING J TRAVEL PLAZA #10")
        right = query(name="FLYING J #10")
        left.brand = "FLYING J TRAVEL PLAZA"  # A record imported before v2.
        score = score_addresses(left, right)
        self.assertEqual(score.components["name"], 1)

    def test_kwik_trip_and_quiktrip_are_distinct_brands(self):
        score = score_addresses(query(), query(name="QUIKTRIP #796"))
        self.assertEqual(score.score, 0)
        self.assertIn("name_not_matched", score.conflicts)

    def test_one_shared_access_highway_is_positive_evidence(self):
        score = score_addresses(query(), query(address="I-94"))
        self.assertEqual(score.components["highways"], 1)
        self.assertIn("exits", score.missing)

    def test_source_scores_and_points_belong_to_the_same_record(self):
        atp = candidate("atp", latitude=44.01)
        atp.provider = "alltheplaces"
        osm = candidate("osm", name="KWIK TRIP", latitude=44.02)
        osm.provider = "osm"
        result = rank_candidates(query(), [osm, atp], providers=["alltheplaces", "overture", "osm"])
        self.assertEqual(
            result["per_source"]["osm"]["matched_coordinate"], osm.coordinate.model_dump()
        )
        self.assertEqual(result["per_source"]["overture"]["status"], "unmatched")
        self.assertIsNone(result["per_source"]["overture"]["matched_coordinate"])
        self.assertEqual(result["highest_matching_source"], "alltheplaces")
        self.assertEqual(result["highest_matching_coordinate"], atp.coordinate.model_dump())
        self.assertEqual(
            result["highest_matching_score"],
            max(row["score"] for row in result["per_source"].values()),
        )

    def test_equal_scores_are_explicit_ties_with_a_deterministic_source(self):
        first, second = candidate("first"), candidate("second")
        first.provider, second.provider = "osm", "overture"
        a = rank_candidates(query(), [first, second])
        b = rank_candidates(query(), [second, first])
        self.assertEqual(a["highest_matching_source"], "osm")
        self.assertEqual(a["highest_matching_source"], b["highest_matching_source"])
        self.assertEqual(a["highest_matching_tied_sources"], ["osm", "overture"])

    def test_closed_conflicting_or_non_poi_points_are_not_exported_as_matches(self):
        for changes in ("closed", "conflict", "centroid"):
            item = candidate()
            if changes == "closed":
                item.operating_status = "permanently_closed"
            elif changes == "conflict":
                item.address.state = "MN"
            else:
                item.precision = "locality"
            result = rank_candidates(query(), [item])
            self.assertIsNone(result["highest_matching_coordinate"])
            self.assertIsNone(result["per_source"]["test"]["matched_coordinate"])

    def test_enriched_csv_preserves_original_duplicate_price_rows(self):
        item = candidate()
        item.provider = "osm"
        result = rank_candidates(query(), [item], providers=["alltheplaces", "overture", "osm"])
        comparison = comparison_row("9", query(), result, "run")
        with TemporaryDirectory() as directory:
            source, target = Path(directory) / "input.csv", Path(directory) / "output.csv"
            source.write_text(
                "OPIS Truckstop ID,Truckstop Name,Retail Price\n"
                "9,KWIK TRIP #796,3.28000000\n9,KWIK TRIP #796,3.29000000\n"
            )
            count = enrich_input_csv(source, target, {"9": comparison})
            with target.open() as stream:
                rows = list(csv.DictReader(stream))
        self.assertEqual(count, 2)
        self.assertEqual([row["Retail Price"] for row in rows], ["3.28000000", "3.29000000"])
        self.assertEqual(float(rows[0]["osm_latitude"]), item.coordinate.latitude)
        self.assertEqual(float(rows[0]["highest_matching_score"]), 100)
        self.assertEqual(rows[0]["alltheplaces_latitude"], "")


class SourceRetrievalTests(DatabaseTestCase):
    def test_each_source_gets_its_own_limit_and_truncation_flag(self):
        for provider, ids in (("alltheplaces", ["a", "b"]), ("osm", ["c"])):
            for source_id in ids:
                item = candidate(source_id)
                item.provider, item.release = provider, "release"
                save_place(item)
        records, truncated = retrieve_candidates(
            query(), limit=1, source_scope={"alltheplaces": "release", "osm": "release"}
        )
        self.assertEqual({row.provider for row in records}, {"alltheplaces", "osm"})
        self.assertEqual(truncated, {"alltheplaces"})

    def test_a_source_limit_does_not_mark_another_source_as_truncated(self):
        first, second = candidate("a"), candidate("b")
        first.provider, second.provider = "alltheplaces", "osm"
        from geocoding.matching import rank_candidates

        result = rank_candidates(
            query(), [first, second], truncated=True, truncated_providers={"alltheplaces"}
        )
        self.assertEqual(result["per_source"]["alltheplaces"]["status"], "needs_review")
        self.assertEqual(result["per_source"]["osm"]["status"], "accepted")

    def test_input_conflicts_downgrade_source_and_highest_status(self):
        from geocoding.models import FuelLocation

        item = candidate()
        save_place(item)
        location = FuelLocation.objects.create(
            external_id="9",
            address=query().model_dump(mode="json"),
            warnings=["conflicting_input_rows"],
        )
        run = resolve(query(), location)
        self.assertEqual(run.result["per_source"]["test"]["status"], "needs_review")
        self.assertEqual(run.result["highest_matching_status"], "needs_review")
        self.assertIsNone(run.result["per_source"]["test"]["coordinate"])
