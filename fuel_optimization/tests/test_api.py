import io
import json
from decimal import Decimal
from unittest.mock import patch
from urllib.error import URLError

from django.test import TestCase, override_settings

from fuel_optimization.candidates import fetch_candidates
from fuel_optimization.contracts import CandidateLimitExceeded
from geocoding.models import FuelLocation, FuelObservation

from .factories import osrm_payload, route

ENDPOINTS = ("/api/fuel/greedy/", "/api/fuel/dijkstra/")
QUERY = {"origin": {"latitude": 0, "longitude": 0}, "destination": {"latitude": 0, "longitude": 10}}


class StationFixtures:
    def location(self, external="one", latitude=0, longitude=5, status="accepted", prices=(2, 4)):
        location = FuelLocation.objects.create(
            external_id=external,
            address={"raw_name": "Test Fuel"},
            resolution_status=status,
            resolved_coordinate={"latitude": latitude, "longitude": longitude},
        )
        for index, price in enumerate(prices, start=1):
            FuelObservation.objects.create(
                location=location,
                dataset_sha256=external.ljust(64, "0"),
                row_number=index,
                raw={},
                retail_price=Decimal(str(price)),
            )
        return location


class StationDataTests(StationFixtures, TestCase):
    def test_one_spatial_query_deduplicates_location_and_takes_max_price(self):
        location = self.location(latitude=0.01)
        self.location("outside", latitude=20)
        review = self.location("review", status="needs_review")
        self.location("no-price", prices=())
        with self.assertNumQueries(1):
            selected = fetch_candidates(
                route(), corridor_miles=5, detour_multiplier=2, max_candidates=1000
            )
        self.assertEqual({s.location_id for s in selected}, {location.pk, review.pk})
        selected = sorted(selected, key=lambda s: s.location_id != location.pk)
        self.assertEqual(selected[0].price, Decimal("4"))
        self.assertAlmostEqual(selected[0].route_miles, 400, places=5)
        self.assertGreater(selected[0].detour_miles, 1)

    def test_excess_candidates_raise_instead_of_silently_dropping_cheaper_stations(self):
        self.location()
        self.location("two")
        with self.assertRaises(CandidateLimitExceeded), self.assertNumQueries(1):
            fetch_candidates(route(), corridor_miles=5, detour_multiplier=1, max_candidates=1)


class FuelAPITests(StationFixtures, TestCase):
    def test_both_apis_make_one_osrm_call_and_one_database_query(self):
        self.location()
        outputs = []
        for path in ENDPOINTS:
            with patch("fuel_optimization.routing.build_opener") as build:
                build.return_value.open.side_effect = lambda *a, **k: io.BytesIO(osrm_payload())
                with self.assertNumQueries(1):
                    response = self.client.post(
                        path, data=json.dumps(QUERY), content_type="application/json"
                    )
                self.assertEqual(response.status_code, 200, response.content)
                build.assert_called_once()
                build.return_value.open.assert_called_once()
                request = build.return_value.open.call_args.args[0]
                self.assertIn("0.0,0.0;10.0,0.0?", request.full_url)
                self.assertIn("overview=full", request.full_url)
                self.assertIn("geometries=geojson", request.full_url)
            result = response.json()
            self.assertEqual(result["summary"]["total_fuel_cost"], 120)
            self.assertEqual(result["summary"]["total_gallons_purchased"], 30)
            self.assertEqual(len(result["stops"]), 1)
            self.assertEqual(result["stops"][0]["price_per_gallon"], 4)
            self.assertEqual(result["route"]["geometry"]["type"], "LineString")
            self.assertGreaterEqual(result["algorithm_execution_time_ms"], 0)
            self.assertEqual(result["diagnostics"]["routing_api_calls"], 1)
            outputs.append(result)
        self.assertEqual(set(outputs[0]), set(outputs[1]))
        self.assertEqual(outputs[0]["summary"], outputs[1]["summary"])

    def test_configurable_corridor_and_multiplier_are_shared(self):
        with patch("fuel_optimization.routing.build_opener") as build:
            build.return_value.open.side_effect = lambda *a, **k: io.BytesIO(osrm_payload(100))
            result = self.client.post(
                ENDPOINTS[0],
                data=json.dumps({**QUERY, "corridor_miles": 2, "detour_multiplier": 1.8}),
                content_type="application/json",
            )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["assumptions"]["corridor_miles"], 2)
        self.assertEqual(result.json()["assumptions"]["detour_multiplier"], 1.8)

    def test_unreachable_route_has_consistent_422_json(self):
        for path in ENDPOINTS:
            with patch("fuel_optimization.routing.build_opener") as build:
                build.return_value.open.side_effect = lambda *a, **k: io.BytesIO(osrm_payload())
                result = self.client.post(
                    path, data=json.dumps(QUERY), content_type="application/json"
                )
            self.assertEqual(result.status_code, 422)
            self.assertEqual(result.json()["error"]["code"], "unreachable_route")
            build.return_value.open.assert_called_once()

    def test_routing_failure_is_not_retried_or_replaced_with_another_call(self):
        for path in ENDPOINTS:
            with patch("fuel_optimization.routing.build_opener") as build:
                build.return_value.open.side_effect = URLError("unavailable")
                with self.assertNumQueries(0):
                    result = self.client.post(
                        path, data=json.dumps(QUERY), content_type="application/json"
                    )
            self.assertEqual(result.status_code, 502)
            build.return_value.open.assert_called_once()

    def test_malformed_coordinates_and_options_do_not_call_osrm(self):
        invalid = [
            {**QUERY, "origin": {"latitude": 91, "longitude": 0}},
            {**QUERY, "origin": {"latitude": True, "longitude": 0}},
            {**QUERY, "destination": {"latitude": float("nan"), "longitude": 0}},
            {**QUERY, "corridor_miles": -1},
            {**QUERY, "detour_multiplier": 0.1},
            {**QUERY, "unknown": 1},
            [],
        ]
        for path in ENDPOINTS:
            for query in invalid:
                with patch("fuel_optimization.routing.build_opener") as build:
                    result = self.client.post(
                        path, data=json.dumps(query), content_type="application/json"
                    )
                self.assertEqual(result.status_code, 400)
                build.assert_not_called()

    def test_method_and_content_type_errors_are_json(self):
        for path in ENDPOINTS:
            self.assertEqual(self.client.get(path).json()["error"]["code"], "method_not_allowed")
            self.assertEqual(
                self.client.post(path, data="x", content_type="text/plain").status_code, 415
            )

    @override_settings(FUEL_MAX_SEARCH_STATES=1)
    def test_dijkstra_resource_limit_is_explicit(self):
        self.location()
        with patch("fuel_optimization.routing.build_opener") as build:
            build.return_value.open.side_effect = lambda *a, **k: io.BytesIO(osrm_payload())
            result = self.client.post(
                ENDPOINTS[1], data=json.dumps(QUERY), content_type="application/json"
            )
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.json()["error"]["code"], "search_limit_exceeded")
