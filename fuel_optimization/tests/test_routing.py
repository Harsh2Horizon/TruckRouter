import io
import json
from http.client import IncompleteRead
from unittest.mock import patch
from urllib.error import HTTPError

from django.test import SimpleTestCase, override_settings

from fuel_optimization.contracts import Point, RoutingError
from fuel_optimization.routing import NoRedirects, fetch_route

from .factories import osrm_payload


class RoutingTests(SimpleTestCase):
    def test_only_one_full_geojson_route_is_requested(self):
        with patch("fuel_optimization.routing.build_opener") as build:
            build.return_value.open.return_value = io.BytesIO(osrm_payload())
            route = fetch_route(Point(latitude=0, longitude=0), Point(latitude=0, longitude=10))
        self.assertAlmostEqual(route.distance_miles, 800)
        build.return_value.open.assert_called_once()

    def test_invalid_osrm_data_is_rejected(self):
        payloads = [
            b"not json",
            json.dumps({"code": "NoRoute"}).encode(),
            osrm_payload().replace(b'"LineString"', b'"Polygon"'),
            osrm_payload().replace(b'"distance": 1287475.2', b'"distance": -1'),
        ]
        for payload in payloads:
            with patch("fuel_optimization.routing.build_opener") as build:
                build.return_value.open.return_value = io.BytesIO(payload)
                with self.assertRaises(RoutingError):
                    fetch_route(Point(latitude=0, longitude=0), Point(latitude=0, longitude=10))

    @override_settings(FUEL_OSRM_MAX_RESPONSE_BYTES=3)
    def test_oversized_response_does_not_trigger_another_request(self):
        with patch("fuel_optimization.routing.build_opener") as build:
            build.return_value.open.return_value = io.BytesIO(osrm_payload())
            with self.assertRaises(RoutingError):
                fetch_route(Point(latitude=0, longitude=0), Point(latitude=0, longitude=10))
        build.return_value.open.assert_called_once()

    def test_redirect_handler_refuses_secondary_http_requests(self):
        self.assertIsNone(NoRedirects().redirect_request(None, None, 302, "", {}, "https://other/"))
        with patch("fuel_optimization.routing.build_opener") as build:
            build.return_value.open.side_effect = HTTPError(
                "https://test/", 302, "redirect", {}, None
            )
            with self.assertRaises(RoutingError):
                fetch_route(Point(latitude=0, longitude=0), Point(latitude=0, longitude=10))
        build.return_value.open.assert_called_once()

    def test_interrupted_response_is_a_routing_error_without_retry(self):
        with patch("fuel_optimization.routing.build_opener") as build:
            response = build.return_value.open.return_value.__enter__.return_value
            response.read.side_effect = IncompleteRead(b"partial")
            with self.assertRaises(RoutingError):
                fetch_route(Point(latitude=0, longitude=0), Point(latitude=0, longitude=10))
        build.return_value.open.assert_called_once()
