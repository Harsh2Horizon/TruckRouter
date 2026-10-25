import csv
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import TestCase

from geocoding.ingestion import import_fuel_csv
from geocoding.models import FuelLocation, FuelObservation, GeocodingRun, SourcePlace
from geocoding.pipeline import import_source
from geocoding.services import resolve
from geocoding.tests.factories import candidate, query, save_place


class PipelineTests(TestCase):
    def setUp(self):
        fallback = patch("geocoding.geography.get_exit_locator", return_value=None)
        fallback.start()
        self.addCleanup(fallback.stop)
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.csv = Path(self.temp.name) / "input.csv"
        self.csv.write_text(
            "OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price\n"
            '9,KWIK TRIP #796,"I-94, EXIT 143 & US-12 & SR-21",Tomah,WI,420,3.28733333\n'
            '9,KWIK TRIP #796,"I-94, EXIT 143 & US-12 & SR-21",Tomah,WI,420,3.28733333\n'
        )

    def test_csv_import_preserves_duplicate_observations_and_is_idempotent(self):
        first = import_fuel_csv(str(self.csv))
        second = import_fuel_csv(str(self.csv))
        self.assertEqual(first["rows"], 2)
        self.assertEqual(first["locations_created"], 1)
        self.assertEqual(second["observations_created"], 0)
        self.assertEqual(FuelLocation.objects.count(), 1)
        self.assertEqual(FuelObservation.objects.count(), 2)
        observation = FuelObservation.objects.first()
        self.assertEqual(str(observation.retail_price), "3.28733333")
        self.assertEqual(observation.price_unit, "unknown")

    def test_malformed_price_rolls_back_entire_csv(self):
        self.csv.write_text(self.csv.read_text().replace("3.28733333", "NaN", 1))
        with self.assertRaises(ValueError):
            import_fuel_csv(str(self.csv))
        self.assertFalse(FuelLocation.objects.exists())

    def test_conflicting_input_rows_block_acceptance(self):
        self.csv.write_text(self.csv.read_text().replace("Tomah,WI", "Tomah,MN", 1))
        import_fuel_csv(str(self.csv))
        location = FuelLocation.objects.get()
        save_place(candidate(state="MN"))
        from geocoding.schemas import CanonicalAddress

        run = resolve(CanonicalAddress.model_validate(location.address), location)
        self.assertEqual(run.status, "needs_review")
        self.assertIn("input_records_disagree", run.result["reasons"])

    def test_result_and_evidence_survive_provider_updates(self):
        place = save_place(candidate())
        run = resolve(query())
        self.assertEqual(run.status, "accepted")
        place.raw = {"updated": True}
        place.latitude = 45
        place.save()
        self.assertEqual(run.candidates.get().snapshot["coordinate"]["latitude"], 44)

    def test_partial_coordinate_pair_is_rejected_by_database(self):
        place = save_place(candidate())
        with self.assertRaises(IntegrityError), transaction.atomic():
            SourcePlace.objects.filter(pk=place.pk).update(longitude=None)

    def test_provider_upsert_does_not_duplicate_source_identity(self):
        save_place(candidate())
        save_place(candidate(latitude=44.1))
        self.assertEqual(SourcePlace.objects.count(), 1)
        self.assertEqual(SourcePlace.objects.get().latitude, 44.1)

    def test_parse_api(self):
        response = self.client.post(
            "/api/v1/addresses/parse",
            data=json.dumps(
                {
                    "name": "KWIK TRIP #796",
                    "address": "I-94, EXIT 143 & US-12 & SR-21",
                    "city": "Tomah",
                    "state": "WI",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["store_id"], "796")

    def test_api_rejects_unknown_fields(self):
        response = self.client.post(
            "/api/v1/addresses/parse",
            data=json.dumps(
                {
                    "name": "Kwik Trip",
                    "unknown": True,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_geocode_api_and_audit_lookup(self):
        save_place(candidate())
        response = self.client.post(
            "/api/v1/geocode",
            data=json.dumps(
                {
                    "name": "KWIK TRIP #796",
                    "city": "Tomah",
                    "state": "WI",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "accepted")
        lookup = self.client.get(f"/api/v1/geocode/runs/{response.json()['run_id']}")
        self.assertEqual(response.json(), lookup.json())
        self.assertEqual(GeocodingRun.objects.count(), 1)

    def test_zero_candidates_are_unmatched(self):
        result = resolve(query())
        self.assertEqual(result.status, "unmatched")
        self.assertIsNone(result.result["coordinate"])

    def test_empty_source_scope_cannot_use_old_imports(self):
        save_place(candidate())
        self.assertEqual(resolve(query(), source_scope={}).status, "unmatched")

    def test_scope_excludes_a_different_source_release(self):
        item = candidate()
        item.release = "old"
        save_place(item)
        self.assertEqual(resolve(query(), source_scope={"test": "new"}).status, "unmatched")

    def test_partial_acquisition_blocks_combined_auto_acceptance(self):
        save_place(candidate())
        run = resolve(query(), acquisition_incomplete=True)
        self.assertEqual(run.status, "needs_review")
        self.assertIsNone(run.result["coordinate"])
        self.assertIn("source_acquisition_incomplete", run.result["reasons"])

    def test_rescore_reuses_imports_and_exports_each_source_and_original_price_rows(self):
        imported = import_fuel_csv(str(self.csv))
        manifests, previous = {}, {}
        root = Path(self.temp.name)
        for provider in ("alltheplaces", "overture", "osm"):
            item = candidate(provider)
            item.provider, item.release = provider, "release"
            save_place(item)
            filename = root / f"{provider}.ndjson"
            filename.touch()
            manifest = {
                "provider": provider,
                "release": "release",
                "filename": str(filename),
                "format": "ndjson",
                "records": 1,
            }
            manifests[provider] = {**manifest, "status": "downloaded"}
            previous[provider] = {
                **manifest,
                "status": "imported",
                "imported": 1,
                "rejected": 0,
                "unique_stored_records": 1,
            }
        acquisition, prior = root / "acquisition.json", root / "prior.json"
        acquisition.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "sources": manifests,
                    "dataset_sha256": imported["dataset_sha256"],
                }
            )
        )
        prior.write_text(
            json.dumps({"status": "completed", "input_import": imported, "sources": previous})
        )
        output = root / "rescored"
        with (
            patch("geocoding.pipeline.import_fuel_csv") as importer,
            patch("geocoding.pipeline.bulk_save_places") as saver,
            patch("geocoding.acquisition.subprocess.run") as network,
        ):
            call_command(
                "run_geocoding_pipeline",
                csv=str(self.csv),
                output_dir=str(output),
                acquisition_report=str(acquisition),
                reuse_import_report=str(prior),
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
        importer.assert_not_called()
        saver.assert_not_called()
        network.assert_not_called()
        with (output / "geocodes.csv").open() as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["algorithm_version"], "brand-locality-3.1")
        self.assertEqual(float(row["highest_matching_score"]), 100)
        for provider in manifests:
            self.assertEqual(float(row[f"{provider}_latitude"]), 44)
            self.assertEqual(row[f"{provider}_status"], "accepted")
        with (output / "fuel-prices-geocoded.csv").open() as stream:
            enriched = list(csv.DictReader(stream))
        self.assertEqual(len(enriched), 2)
        self.assertEqual(enriched[0]["Retail Price"], "3.28733333")
        report = json.loads((output / "report.json").read_text())
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["enriched_input_rows"], 2)
        with (output / "source-comparison.csv").open() as stream:
            comparisons = list(csv.DictReader(stream))
        self.assertEqual({r["provider"] for r in comparisons}, set(manifests))
        self.assertEqual(len(comparisons), 3)
        self.assertTrue(all(float(r["name_points"]) == 60 for r in comparisons))
        self.assertTrue(all(float(r["locality_points"]) == 30 for r in comparisons))
        self.assertTrue(all(r["eligible_point"] == "True" for r in comparisons))
        with (output / "final-stops.csv").open() as stream:
            final = list(csv.DictReader(stream))
        self.assertEqual(len(final), 1)
        self.assertEqual(final[0]["prediction_method"], "fuel_poi")
        self.assertEqual(report["coordinate_deviation"]["severity_counts"], {"consistent": 1})

    def test_broken_source_export_rolls_back_even_after_a_batch_has_been_written(self):
        root = Path(self.temp.name)
        export = root / "source.ndjson"
        record = {
            "type": "node",
            "lat": 44,
            "lon": -90.5,
            "tags": {"name": "Fuel", "amenity": "fuel"},
        }
        export.write_text(
            "".join(json.dumps({**record, "id": i}) + "\n" for i in range(500)) + "invalid json\n"
        )
        with self.assertRaises(ValueError):
            import_source(
                "osm", {"filename": str(export), "release": "test", "format": "ndjson"}, root
            )
        self.assertFalse(SourcePlace.objects.exists())

    def test_empty_source_is_rejected_instead_of_recording_a_successful_import(self):
        root = Path(self.temp.name)
        export = root / "source.ndjson"
        export.write_text("")
        with self.assertRaisesRegex(ValueError, "zero usable records"):
            import_source(
                "osm", {"filename": str(export), "release": "test", "format": "ndjson"}, root
            )

    def test_incomplete_acquisition_fails_before_importing_or_matching(self):
        root = Path(self.temp.name)
        acquisition = root / "acquisition.json"
        import hashlib

        acquisition.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "sources": {},
                    "dataset_sha256": hashlib.sha256(self.csv.read_bytes()).hexdigest(),
                }
            )
        )
        with self.assertRaisesRegex(CommandError, "Acquisition is not complete"):
            call_command(
                "run_geocoding_pipeline",
                csv=str(self.csv),
                acquisition_report=str(acquisition),
                output_dir=str(root / "output"),
            )
        self.assertFalse(FuelObservation.objects.exists())
        self.assertFalse(GeocodingRun.objects.exists())

    def test_import_only_stage_creates_reusable_indexes_without_matching(self):
        import hashlib

        root = Path(self.temp.name)
        examples = {
            "alltheplaces": {"id": "one", "properties": {"@spider": "test", "name": "Fuel"}},
            "overture": {"id": "one", "properties": {"names": {"primary": "Fuel"}}},
            "osm": {"type": "node", "id": 1, "tags": {"name": "Fuel"}},
        }
        sources = {}
        for provider, example in examples.items():
            path = root / f"{provider}.ndjson"
            path.write_text(json.dumps(example) + "\n")
            sources[provider] = {
                "status": "downloaded",
                "provider": provider,
                "release": "test",
                "filename": str(path),
                "format": "ndjson",
            }
        acquisition = root / "acquisition.json"
        acquisition.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "sources": sources,
                    "dataset_sha256": hashlib.sha256(self.csv.read_bytes()).hexdigest(),
                }
            )
        )
        output = root / "import-only"
        with patch("geocoding.pipeline.resolve") as resolver:
            call_command(
                "run_geocoding_pipeline",
                csv=str(self.csv),
                acquisition_report=str(acquisition),
                output_dir=str(output),
                import_only=True,
                stdout=io.StringIO(),
            )
        resolver.assert_not_called()
        self.assertEqual(SourcePlace.objects.count(), 3)
        self.assertEqual(FuelObservation.objects.count(), 2)
        self.assertFalse((output / "geocodes.csv").exists())
        report = json.loads((output / "report.json").read_text())
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["phase"], "imported")
        self.assertTrue(all(r["unique_stored_records"] == 1 for r in report["sources"].values()))

    def test_brand_alias_upgrade_does_not_create_an_input_conflict(self):
        self.csv.write_text(
            self.csv.read_text().replace("KWIK TRIP #796", "MAVERIK COUNTRY STORE #796")
        )
        import_fuel_csv(str(self.csv))
        location = FuelLocation.objects.get()
        location.address["brand"] = "MAVERIK COUNTRY STORE"
        location.save()
        import_fuel_csv(str(self.csv))
        location.refresh_from_db()
        self.assertEqual(location.warnings, [])
