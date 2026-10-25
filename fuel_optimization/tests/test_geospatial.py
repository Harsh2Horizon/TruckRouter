import random
from math import atan2, degrees, sqrt
from unittest import TestCase

from fuel_optimization.contracts import Point, Route
from fuel_optimization.geospatial import (
    EARTH_MILES,
    RouteProjector,
    Segment,
    haversine_miles,
    squared_distance,
    vector,
)


class ProjectionTests(TestCase):
    def test_cumulative_miles_use_osrm_distance_and_closest_segment_not_vertex(self):
        projector = RouteProjector(Route(((0, 0), (4, 0), (10, 0)), 800, 0))
        projected = projector.project(Point(latitude=1, longitude=5))
        self.assertAlmostEqual(projected.route_miles, 400, places=6)
        self.assertAlmostEqual(
            projected.separation_miles,
            haversine_miles(Point(latitude=1, longitude=5), Point(latitude=0, longitude=5)),
            places=6,
        )

    def test_date_line_and_high_latitude_arcs(self):
        projected = RouteProjector(Route(((179, 0), (-179, 0)), 200, 0)).project(
            Point(latitude=1, longitude=180)
        )
        self.assertAlmostEqual(projected.route_miles, 100, places=6)
        north = RouteProjector(Route(((-45, 80), (45, 80)), 1000, 0)).project(
            Point(latitude=85, longitude=0)
        )
        self.assertAlmostEqual(north.route_miles, 500, places=6)
        self.assertLess(north.separation_miles, 200)

    def test_duplicate_vertices_and_zero_distance_route(self):
        projected = RouteProjector(Route(((1, 1), (1, 1)), 0, 0)).project(
            Point(latitude=1, longitude=1)
        )
        self.assertEqual(projected.route_miles, 0)
        self.assertEqual(projected.separation_miles, 0)

    def test_bounding_volume_search_matches_exhaustive_segment_search(self):
        rng = random.Random(104)
        coordinates = tuple((x, rng.uniform(50, 75)) for x in range(60))
        route = Route(coordinates, 5000, 0)
        projector = RouteProjector(route)
        segments, cumulative = [], 0
        for a, b in zip(coordinates, coordinates[1:], strict=False):
            arc = Segment(vector(*a), vector(*b), cumulative)
            segments.append(arc)
            cumulative += arc.angle * EARTH_MILES
        for _ in range(40):
            point = Point(longitude=rng.uniform(-1, 61), latitude=rng.uniform(45, 80))
            p = vector(point.longitude, point.latitude)
            choices = []
            for segment in segments:
                q, angle = segment.nearest(p)
                choices.append(
                    (squared_distance(p, q), segment.cumulative + angle * EARTH_MILES, q)
                )
            _, position, q = min(choices)
            expected = Point(
                longitude=degrees(atan2(q[1], q[0])),
                latitude=degrees(atan2(q[2], sqrt(q[0] ** 2 + q[1] ** 2))),
            )
            result = projector.project(point)
            self.assertAlmostEqual(result.route_miles, position / cumulative * 5000, places=6)
            self.assertAlmostEqual(
                result.separation_miles, haversine_miles(point, expected), places=6
            )
