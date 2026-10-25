"""Next-cheaper reachable stop with a detour-adjusted price heuristic."""

from .contracts import EPSILON, MPG, TANK_GALLONS, Plan, Purchase, UnreachableRoute
from .graph import RouteGraph


def adjusted_price(station):
    # Amortize estimated round-trip access fuel over one full tank of purchases.
    return float(station.price) * (1 + 2 * station.detour_miles / (MPG * TANK_GALLONS))


def optimize(graph: RouteGraph) -> Plan:

    current, fuel, purchases = 0, TANK_GALLONS, []

    while current != graph.destination:
        reachable = graph.edges[current]
        if current == 0:
            reachable = tuple(e for e in reachable if e.gallons <= fuel + EPSILON)
        if not reachable:
            raise UnreachableRoute("The greedy strategy cannot reach the next viable stop")
        destination = next((e for e in reachable if e.target == graph.destination), None)
        if destination and destination.gallons <= fuel + EPSILON:
            break  # No additional purchase can improve on zero remaining expenditure.
        price = adjusted_price(graph.station(current)) if current else 0.0
        cheaper = next(
            (
                e
                for e in reachable
                if e.target == graph.destination or adjusted_price(graph.station(e.target)) < price
            ),
            None,
        )
        target = cheaper or min(
            reachable,
            key=lambda e: (
                adjusted_price(graph.station(e.target)),
                graph.detours[e.target],
                e.target,
            ),
        )
        if current:
            desired = target.gallons if cheaper else TANK_GALLONS
            gallons = max(0.0, desired - fuel)
            if gallons > EPSILON:
                purchases.append(Purchase(graph.station(current), gallons))
                fuel += gallons
        fuel -= target.gallons
        if fuel < -EPSILON:
            raise UnreachableRoute("Insufficient fuel for the chosen detour")
        current = target.target
    return Plan(tuple(purchases), {"strategy": "next_cheaper_detour_adjusted"})
