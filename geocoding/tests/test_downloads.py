import io
import json
import unittest
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from geocoding.downloads import (
    DownloadClient,
    atp_feature_relevant,
    fetch_alltheplaces,
    json_value,
    overpass_query,
    overture_plan,
    overture_sql,
    parse_bbox,
)
from geocoding.providers import osm


class DownloadTests(unittest.TestCase):
    def test_http_cache_avoids_repeated_downloads_and_records_checksum(self):
        response = io.BytesIO(b'{"features": []}')
        response.url = "https://example.test/resolved"
        with TemporaryDirectory() as directory:
            client = DownloadClient(directory)
            with patch("geocoding.downloads.urlopen", return_value=response) as request:
                first = client.fetch("https://example.test/data")
                second = client.fetch("https://example.test/data")
            self.assertEqual(first, second)
            self.assertEqual(request.call_count, 1)
            metadata = json.loads(first.with_suffix(".json").read_text())
            self.assertEqual(metadata["resolved_url"], "https://example.test/resolved")
            self.assertEqual(len(metadata["sha256"]), 64)

    def test_failed_download_does_not_leave_a_completed_cache_entry(self):
        with TemporaryDirectory() as directory:
            client = DownloadClient(directory)
            with patch("geocoding.downloads.urlopen", side_effect=OSError("offline")):
                with self.assertRaises(OSError):
                    client.fetch("https://example.test/data")
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_atp_filter_keeps_fuel_or_input_brands_and_excludes_other_countries(self):
        self.assertTrue(
            atp_feature_relevant({"properties": {"amenity": "fuel", "addr:country": "US"}}, set())
        )
        self.assertTrue(
            atp_feature_relevant(
                {"properties": {"brand": "Kwik Trip", "addr:state": "WI"}}, {"KWIK TRIP"}
            )
        )
        self.assertFalse(
            atp_feature_relevant({"properties": {"amenity": "fuel", "addr:country": "DE"}}, set())
        )
        self.assertFalse(atp_feature_relevant({"properties": {"brand": "Coffee"}}, {"KWIK TRIP"}))

    def test_overpass_query_is_country_bounded_and_requests_geometry(self):
        query = overpass_query("US")
        self.assertIn('["ISO3166-1"="US"]', query)
        self.assertIn('["amenity"="fuel"]', query)
        self.assertIn("out body center", query)
        with self.assertRaises(ValueError):
            overpass_query('US";out;')

    def test_query_area_country_is_explicit_provenance_not_a_state_guess(self):
        item = osm(
            {
                "type": "node",
                "id": 10,
                "lat": 44,
                "lon": -90.5,
                "tags": {"amenity": "fuel", "name": "Test Fuel"},
                "query_context": {"country": "US"},
            }
        )
        self.assertEqual(item.address.country, "US")
        self.assertEqual(item.address.state, "")
        self.assertEqual(item.provenance[0]["query_context"], {"country": "US"})

    def test_overture_sql_supports_modern_schema_and_parameterizes_brands(self):
        query, params = overture_sql(
            {"id", "geometry", "bbox", "names", "brand", "addresses", "taxonomy", "basic_category"},
            {"PILOT'S"},
        )
        self.assertNotIn("PILOT'S", query)
        self.assertIn("taxonomy.primary", query)
        self.assertEqual(params[0], ["US", "CA"])
        self.assertEqual(params[-1], ["PILOT'S"])
        self.assertEqual(query.count("?"), len(params) + 1)

    def test_overture_sql_supports_old_schema_and_rejects_unknown_schema(self):
        query, _ = overture_sql(
            {"id", "geometry", "bbox", "names", "addresses", "categories"}, set()
        )
        self.assertIn("categories.primary", query)
        with self.assertRaises(ValueError):
            overture_sql({"id"}, set())

    def test_duckdb_timestamp_exports_as_iso8601(self):
        value = datetime(2026, 10, 24, tzinfo=UTC)
        self.assertEqual(
            json.loads(json.dumps({"updated": value}, default=json_value)),
            {"updated": "2026-10-24T00:00:00+00:00"},
        )

    def test_atp_empty_archive_member_does_not_discard_valid_records(self):
        with TemporaryDirectory() as directory:
            archive_path = Path(directory) / "output.zip"
            feature = {
                "type": "Feature",
                "id": "valid",
                "properties": {"amenity": "fuel", "addr:country": "US"},
            }
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("output/empty.geojson", "")
                archive.writestr(
                    "output/fuel.geojson",
                    json.dumps({"type": "FeatureCollection", "features": [feature]}),
                )
            client = DownloadClient(directory)
            with (
                patch.object(
                    client,
                    "json",
                    return_value={
                        "run_id": "test",
                        "output_url": "https://example.test/run/output.zip",
                    },
                ),
                patch.object(client, "fetch", return_value=archive_path),
            ):
                result = fetch_alltheplaces(client, set(), lambda _: None)
            self.assertEqual(result["empty_members_skipped"], 1)
            self.assertEqual(json.loads(Path(result["filename"]).read_text())["id"], "valid")

    def test_targeted_atp_download_pins_each_spider_to_the_same_run(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "fuel.geojson"
            source.write_text(
                json.dumps(
                    {"features": [{"properties": {"amenity": "fuel", "addr:country": "US"}}]}
                )
            )
            client = DownloadClient(directory)
            with (
                patch.object(
                    client,
                    "json",
                    return_value={
                        "run_id": "test",
                        "output_url": "https://example.test/run/output.zip",
                    },
                ),
                patch.object(client, "fetch", return_value=source) as fetch,
            ):
                result = fetch_alltheplaces(client, set(), lambda _: None, spiders=["kwik_trip"])
            fetch.assert_called_once_with("https://example.test/run/output/kwik_trip.geojson")
            self.assertEqual(result["spiders"], ["kwik_trip"])

    def test_spatial_plan_excludes_other_continents_and_keeps_unknown_bounds(self):
        items = [
            {
                "id": "us",
                "bbox": [-125, 24, -66, 50],
                "properties": {"num_rows": 100},
                "assets": {"aws": {"href": "https://example.test/us", "file:size": 1000}},
            },
            {
                "id": "europe",
                "bbox": [0, 30, 30, 60],
                "properties": {"num_rows": 200},
                "assets": {"aws": {"href": "https://example.test/eu", "file:size": 2000}},
            },
            {"id": "unknown", "assets": {"aws": {"href": "https://example.test/unknown"}}},
        ]
        plan = overture_plan({"features": 300}, items, (-180, 15, -50, 85))
        self.assertEqual([part["id"] for part in plan["parts"]], ["us", "unknown"])
        self.assertEqual(plan["global_parquet_bytes"], 3000)
        self.assertEqual(plan["selected_file_count"], 2)

    def test_bbox_validation_and_sql_parameter_order(self):
        bbox = parse_bbox("-125,24,-66,50")
        self.assertEqual(bbox, (-125, 24, -66, 50))
        for invalid in ["1,2,3", "nan,0,10,20", "-190,10,20,30", "10,20,0,30"]:
            with self.assertRaises(ValueError):
                parse_bbox(invalid)
        query, params = overture_sql(
            {"id", "geometry", "names", "addresses", "bbox", "taxonomy"}, set(), bbox=bbox
        )
        self.assertIn("bbox.xmin BETWEEN ? AND ?", query)
        self.assertEqual(params[1:5], [-125, -66, 24, 50])
        self.assertEqual(query.count("?"), len(params) + 1)


if __name__ == "__main__":
    unittest.main()
