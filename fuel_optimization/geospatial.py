"""Spherical route projection with a pure-Python segment bounding-volume index."""

from dataclasses import dataclass
from heapq import heappop, heappush
from itertools import count
from math import asin, atan2, cos, degrees, pi, radians, sin, sqrt

from .contracts import Point, Route, RoutingError

EARTH_MILES = 3958.761333810546


def haversine_miles(left: Point, right: Point):
    lat1, lat2 = radians(left.latitude), radians(right.latitude)
    value = (
        sin((lat2 - lat1) / 2) ** 2
        + cos(lat1) * cos(lat2) * sin(radians(right.longitude - left.longitude) / 2) ** 2
    )
    return 2 * EARTH_MILES * asin(sqrt(min(1.0, max(0.0, value))))


def vector(longitude, latitude):
    lon, lat = radians(longitude), radians(latitude)
    return (cos(lat) * cos(lon), cos(lat) * sin(lon), sin(lat))


def dot(a, b):
    return sum(x * y for x, y in zip(a, b, strict=True))


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def squared_distance(a, b):
    return sum((x - y) ** 2 for x, y in zip(a, b, strict=True))


@dataclass(frozen=True, slots=True)
class Projection:
    route_miles: float
    separation_miles: float


class Segment:
    def __init__(self, start, end, cumulative):
        self.start, self.end, self.cumulative = start, end, cumulative
        normal = cross(start, end)
        length = sqrt(dot(normal, normal))
        self.angle = atan2(length, dot(start, end))
        if self.angle >= pi - 1e-10:
            raise RoutingError("Antipodal consecutive route points cannot be projected uniquely")
        self.normal = tuple(n / length for n in normal)
        self.tangent = cross(self.normal, start)
        # Include interior extrema of the GREAT-CIRCLE ARC, not just its endpoints.
        # This makes bounding-box pruning valid at poles and across the date line.
        bounds = []
        for axis in range(3):
            values = [start[axis], end[axis]]
            extremum = atan2(self.tangent[axis], start[axis])
            for t in (extremum, extremum + pi, extremum - pi):
                if 0 < t < self.angle:
                    values.append(start[axis] * cos(t) + self.tangent[axis] * sin(t))
            bounds.extend((min(values) - 1e-15, max(values) + 1e-15))
        self.bounds = tuple(bounds)

    def nearest(self, point):
        height = dot(point, self.normal)
        projected = tuple(point[i] - height * self.normal[i] for i in range(3))
        norm = sqrt(dot(projected, projected))
        if norm > 1e-15:
            projected = tuple(n / norm for n in projected)
            angle = atan2(dot(projected, self.tangent), dot(projected, self.start))
            if 0 <= angle <= self.angle:
                return projected, angle
        if squared_distance(point, self.start) <= squared_distance(point, self.end):
            return self.start, 0.0
        return self.end, self.angle


class SegmentNode:
    def __init__(self, segments):
        self.bounds = tuple(
            (min if i % 2 == 0 else max)(s.bounds[i] for s in segments) for i in range(6)
        )
        self.children = ()
        self.segments = ()
        if len(segments) <= 8:
            self.segments = tuple(segments)
        else:
            axis = max(range(3), key=lambda i: self.bounds[2 * i + 1] - self.bounds[2 * i])
            ordered = sorted(segments, key=lambda s: s.bounds[2 * axis] + s.bounds[2 * axis + 1])
            middle = len(ordered) // 2
            self.children = (SegmentNode(ordered[:middle]), SegmentNode(ordered[middle:]))

    def lower_bound(self, point):
        return sum(
            max(self.bounds[2 * i] - point[i], 0, point[i] - self.bounds[2 * i + 1]) ** 2
            for i in range(3)
        )


class RouteProjector:
    def __init__(self, route: Route):
        self.route = route
        cumulative, segments = 0.0, []
        vectors = [vector(*p) for p in route.coordinates]
        for start, end in zip(vectors, vectors[1:], strict=False):
            if squared_distance(start, end) < 1e-28:
                continue  # Repeated OSRM geometry vertices carry no distance.
            segment = Segment(start, end, cumulative)
            segments.append(segment)
            cumulative += segment.angle * EARTH_MILES
        if not segments and route.distance_miles > 1e-9:
            raise RoutingError("Positive driving distance has no usable route geometry")
        self.length = cumulative
        self.root = SegmentNode(segments) if segments else None

    def project(self, point: Point) -> Projection:
        if self.root is None:
            lon, lat = self.route.coordinates[0]
            return Projection(0.0, haversine_miles(point, Point(longitude=lon, latitude=lat)))
        p = vector(point.longitude, point.latitude)
        serial, queue = count(), []
        heappush(queue, (self.root.lower_bound(p), next(serial), self.root))
        best, along, nearest = float("inf"), float("inf"), None
        while queue:
            bound, _, node = heappop(queue)
            if bound > best + 1e-15:
                continue
            for segment in node.segments:
                q, angle = segment.nearest(p)
                distance = squared_distance(p, q)
                position = segment.cumulative + angle * EARTH_MILES
                if distance < best - 1e-15 or (abs(distance - best) <= 1e-15 and position < along):
                    best, along, nearest = distance, position, q
            for child in node.children:
                lower = child.lower_bound(p)
                if lower <= best + 1e-15:
                    heappush(queue, (lower, next(serial), child))
        projected = Point(
            longitude=degrees(atan2(nearest[1], nearest[0])),
            latitude=degrees(atan2(nearest[2], sqrt(nearest[0] ** 2 + nearest[1] ** 2))),
        )
        # Calibrate cumulative spherical geometry length to OSRM's DRIVING distance.
        miles = min(
            self.route.distance_miles, max(0.0, along / self.length * self.route.distance_miles)
        )
        return Projection(miles, haversine_miles(point, projected))
