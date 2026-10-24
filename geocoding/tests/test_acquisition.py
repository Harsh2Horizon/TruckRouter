import hashlib
import json
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import duckdb
from django.test import TestCase

from geocoding.acquisition import FastDownloadClient, native_export, select_atp_members
from geocoding.models import SourcePlace
from geocoding.providers import osm
from geocoding.services import bulk_save_places


class AcquisitionTests(unittest.TestCase):
    def test_completed_partial_is_verified_and_finalized_without_another_network_request(self):
        with TemporaryDirectory() as directory:
            client = FastDownloadClient(directory)
            url = "https://example.test/source.parquet"
            key = hashlib.sha256(url.encode()).hexdigest()
            partial = Path(directory) / f"{key}.part"
            partial.write_bytes(b"content")
            checksum = hashlib.sha256(b"content").hexdigest()
            with patch("geocoding.acquisition.subprocess.run") as request:
                result = client.fetch(url, expected_bytes=7, sha256=checksum)
            request.assert_not_called()
            self.assertEqual(result.read_bytes(), b"content")
            self.assertFalse(partial.exists())

    def test_incorrect_checksum_never_becomes_a_completed_cache_entry(self):
        with TemporaryDirectory() as directory:
            client = FastDownloadClient(directory)
            url = "https://example.test/source.parquet"
            key = hashlib.sha256(url.encode()).hexdigest()
            (Path(directory) / f"{key}.part").write_bytes(b"corrupt")
            with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
                client.fetch(url, expected_bytes=7, sha256="wrong")
            self.assertFalse((Path(directory) / key).exists())

    def test_native_copy_binds_destination_and_values_in_their_correct_places(self):
        with TemporaryDirectory() as directory, duckdb.connect() as connection:
            target = Path(directory) / "places.ndjson"
            count = native_export(connection, "SELECT ? AS name, ? AS id", ["Pilot's", 42], target)
            self.assertEqual(count, 1)
            self.assertEqual(json.loads(target.read_text()), {"name": "Pilot's", "id": 42})

    def test_atp_aliases_and_fuel_sample_include_files_with_unrelated_filenames(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / "source.zip"
            with zipfile.ZipFile(target, "w") as archive:
                for spider, props in {
                    "pilot_flying_j": {"name": "Pilot Center", "addr:country": "US"},
                    "small_independent_us": {"amenity": "fuel", "addr:country": "US"},
                    "restaurant_us": {"amenity": "restaurant", "addr:country": "US"},
                }.items():
                    archive.writestr(
                        f"output/{spider}.geojson",
                        json.dumps({"features": [{"properties": props}]}),
                    )
                archive.writestr("output/empty.geojson", "")
                archive.writestr("output/broken.geojson", "{")
            with zipfile.ZipFile(target) as archive:
                selected, empty, errors = select_atp_members(archive, {"PILOT"})
            self.assertEqual(
                set(selected),
                {"output/pilot_flying_j.geojson", "output/small_independent_us.geojson"},
            )
            self.assertEqual(empty, 1)
            self.assertEqual(errors[0]["member"], "output/broken.geojson")

    def test_osm_polygon_uses_centroid_when_native_export_contains_null_node_coordinates(self):
        candidate = osm(
            {
                "type": "way",
                "id": 12,
                "lat": None,
                "lon": None,
                "center": {"lat": 44.0, "lon": -90.5},
                "tags": {"amenity": "fuel", "name": "Fuel"},
                "query_context": {"country": "US", "state": "US-WI", "mirror": "https://mirror/"},
            },
            "snapshot",
        )
        self.assertEqual(candidate.coordinate.latitude, 44)
        self.assertEqual(candidate.address.country, "US")
        self.assertEqual(candidate.address.state, "WI")
        self.assertEqual(candidate.precision, "poi_center")
        self.assertIn("state_from_source_region", candidate.address.warnings)

    def test_osm_raw_country_alias_is_normalized_and_original_tag_is_preserved(self):
        candidate = osm(
            {
                "type": "node",
                "id": 15,
                "lat": 44,
                "lon": -90,
                "tags": {"amenity": "fuel", "addr:country": "USA"},
            }
        )
        self.assertEqual(candidate.address.country, "US")
        self.assertEqual(candidate.raw["tags"]["addr:country"], "USA")


class BulkImportTests(TestCase):
    def test_bulk_upsert_deduplicates_identity_and_updates_payload_on_rerun(self):
        def candidate(name, release):
            return osm(
                {
                    "type": "node",
                    "id": 10,
                    "lat": 44,
                    "lon": -90,
                    "tags": {"amenity": "fuel", "name": name, "addr:country": "US"},
                },
                release,
            )

        bulk_save_places([candidate("Old", "one"), candidate("New", "one")])
        bulk_save_places([candidate("Latest", "two")])
        self.assertEqual(SourcePlace.objects.count(), 1)
        place = SourcePlace.objects.get()
        self.assertEqual(place.name_key, "LATEST")
        self.assertEqual(place.release, "two")
        self.assertEqual(place.raw["tags"]["name"], "Latest")
