"""Standard Dijkstra on independent (station, fuel-unit) states."""

from decimal import Decimal
from heapq import heappop, heappush
from itertools import count

from .contracts import FUEL_STEP, TANK_UNITS, Plan, Purchase, SearchLimitExceeded, UnreachableRoute
from .graph import RouteGraph

STEP_PRICE = Decimal("0.5")


def optimize(graph: RouteGraph, *, max_states=250_000) -> Plan:
    
    distances, parents, settled = {}, {}, set()
    queue, sequence = [], count()
    stats = {
        "states_discovered": 0,
        "states_expanded": 0,
        "distance_updates": 0,
        "heap_stale_pops": 0,
        "fuel_step_gallons": FUEL_STEP,
    }
    direct = next((e for e in graph.edges[0] if e.target == graph.destination), None)
    if direct is not None:
        # Zero expenditure is a global lower bound. Avoid gratuitous free-fuel stops.
        return Plan(
            (),
            {
                **stats,
                "discrete_objective_cost": "0",
                "discrete_remaining_fuel_gallons": (TANK_UNITS - direct.fuel_units) * FUEL_STEP,
                "short_circuit": "initial_tank_suffices",
            },
        )

    def relax(state, cost, parent=None, purchase=False):
        # Compare costs only for this EXACT (node, fuel) state.
        previous_cost = distances.get(state)
        if state in settled or (previous_cost is not None and cost >= previous_cost):
            return
        if state not in distances:
            if len(distances) >= max_states:
                raise SearchLimitExceeded("Dijkstra reached its configured state limit")
            stats["states_discovered"] += 1
        distances[state] = cost
        if parent is not None:
            parents[state] = (parent, purchase)
        heappush(queue, (cost, next(sequence), state))
        stats["distance_updates"] += 1

    relax((0, TANK_UNITS), Decimal(0))
    while queue:
        cost, _, state = heappop(queue)
        if state in settled or cost != distances[state]:
            stats["heap_stale_pops"] += 1
            continue
        settled.add(state)
        stats["states_expanded"] += 1
        node, fuel = state
        if node == graph.destination:
            purchases = {}
            cursor = state
            while cursor in parents:
                parent, purchase = parents[cursor]
                if purchase:
                    purchases[cursor[0]] = purchases.get(cursor[0], 0.0) + FUEL_STEP
                cursor = parent
            return Plan(
                tuple(
                    Purchase(graph.station(node), gallons)
                    for node, gallons in sorted(purchases.items())
                ),
                {
                    **stats,
                    "discrete_objective_cost": str(cost),
                    "discrete_remaining_fuel_gallons": fuel * FUEL_STEP,
                },
            )
        if node and fuel < TANK_UNITS:
            price = graph.station(node).price
            relax((node, fuel + 1), cost + STEP_PRICE * price, state, True)
        for edge in graph.edges[node]:
            consumption = edge.fuel_units
            if consumption <= fuel:
                relax((edge.target, fuel - consumption), cost, state)
    raise UnreachableRoute("No feasible fuel state reaches the destination")
