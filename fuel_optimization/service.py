"""Shared I/O boundary; optimizers themselves never make network/database calls."""

from time import perf_counter

from django.conf import settings

from . import candidates, routing
from .graph import RouteGraph
from .responses import build_response


def execute(request, algorithm, optimizer):
    corridor = request.corridor_miles or settings.FUEL_CORRIDOR_MILES
    multiplier = request.detour_multiplier or settings.FUEL_DETOUR_MULTIPLIER
    route = routing.fetch_route(request.origin, request.destination)
    stations = candidates.fetch_candidates(
        route,
        corridor_miles=corridor,
        detour_multiplier=multiplier,
        max_candidates=settings.FUEL_MAX_CANDIDATES,
    )
    started = perf_counter()
    graph = RouteGraph(route.distance_miles, stations)
    if algorithm == "dijkstra":
        plan = optimizer(graph, max_states=settings.FUEL_MAX_SEARCH_STATES)
    else:
        plan = optimizer(graph)
    elapsed_ms = (perf_counter() - started) * 1000
    return build_response(
        algorithm=algorithm,
        route=route,
        plan=plan,
        elapsed_ms=elapsed_ms,
        candidate_count=len(stations),
        corridor_miles=corridor,
        detour_multiplier=multiplier,
    )
