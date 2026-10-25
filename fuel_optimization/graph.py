"""Shared forward route graph; detours are paid on entering and leaving a station."""

from bisect import bisect_right
from dataclasses import dataclass
from math import ceil, isfinite

from .contracts import EPSILON, FUEL_STEP, MAX_RANGE_MILES, MPG, Station, UnreachableRoute


@dataclass(frozen=True, slots=True)
class Edge:
    target: int
    miles: float

    @property
    def gallons(self):
        return self.miles / MPG

    @property
    def fuel_units(self):
        # Round up driving consumption; never authorize an under-fuelled leg.
        return max(0, ceil(self.gallons / FUEL_STEP - EPSILON))


class RouteGraph:
    def __init__(self, distance_miles: float, stations: list[Station]):
        if not isfinite(distance_miles) or distance_miles < 0:
            raise ValueError("Route distance must be finite and non-negative")
        if len({s.location_id for s in stations}) != len(stations):
            raise ValueError("Stations must be deduplicated by location ID")
        if any(
            not s.price.is_finite()
            or s.price < 0
            or not isfinite(s.route_miles)
            or not 0 <= s.route_miles <= distance_miles
            or not isfinite(s.detour_miles)
            or s.detour_miles < 0
            for s in stations
        ):
            raise ValueError("Invalid station price, route position or detour")

        self.stations = tuple(sorted(stations, key=lambda s: (s.route_miles, s.location_id)))
        self.positions = (0.0, *(s.route_miles for s in self.stations), distance_miles)
        self.detours = (0.0, *(s.detour_miles for s in self.stations), 0.0)
        self.destination = len(self.positions) - 1
        self.edges = []

        for i, position in enumerate(self.positions):
            end = bisect_right(self.positions, position + MAX_RANGE_MILES + EPSILON)
            self.edges.append(
                tuple(
                    Edge(j, self.distance(i, j))
                    for j in range(i + 1, end)
                    if self.distance(i, j) <= MAX_RANGE_MILES + EPSILON
                )
            )
        viable = {self.destination}
        for i in range(self.destination - 1, -1, -1):
            if any(edge.target in viable for edge in self.edges[i]):
                viable.add(i)
        if 0 not in viable:
            raise UnreachableRoute("No forward station sequence is reachable with a 50-gallon tank")
        self.edges = tuple(tuple(e for e in edges if e.target in viable) for edges in self.edges)

    def distance(self, source, target):
        return (
            self.positions[target]
            - self.positions[source]
            + self.detours[source]
            + self.detours[target]
        )

    def station(self, node):
        return self.stations[node - 1]
