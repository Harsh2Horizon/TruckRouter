import unittest

from geocoding.providers import osm
from geocoding.source_queries import (
    atp_feature_relevant,
    overture_plan,
    overture_sql,
    parse_bbox,
)


class SourceQueryTests(unittest.TestCase):
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

    def test_source_region_country_is_explicit_provenance_not_a_state_guess(self):
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
