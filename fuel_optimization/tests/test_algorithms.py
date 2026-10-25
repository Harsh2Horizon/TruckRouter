import random
from decimal import Decimal
from unittest import TestCase

from fuel_optimization import dijkstra, greedy
from fuel_optimization.contracts import SearchLimitExceeded, UnreachableRoute
from fuel_optimization.graph import RouteGraph
from fuel_optimization.responses import build_response

from .factories import route, station


def response(algorithm, miles, stations):
    optimizer = {"greedy": greedy.optimize, "dijkstra": dijkstra.optimize}[algorithm]
    plan = optimizer(RouteGraph(miles, stations))
    return build_response(
        algorithm=algorithm,
        route=route(miles),
        plan=plan,
        elapsed_ms=0,
        candidate_count=len(stations),
        corridor_miles=5,
        detour_multiplier=1.4,
    )


def exhaustive_grid_cost(graph):
    """Independent dynamic program on the node/fuel DAG; no heap/search-code reuse."""
    costs = [[Decimal("Infinity")] * 101 for _ in graph.positions]
    costs[0][100] = Decimal(0)
    for node in range(graph.destination):
        for fuel in range(101):
            current = costs[node][fuel]
            if not current.is_finite():
                continue
            if node and fuel < 100:
                costs[node][fuel + 1] = min(
                    costs[node][fuel + 1], current + graph.station(node).price * Decimal("0.5")
                )
            for edge in graph.edges[node]:
                if edge.fuel_units <= fuel:
                    remaining = fuel - edge.fuel_units
                    costs[edge.target][remaining] = min(costs[edge.target][remaining], current)
    return min(costs[-1])


class AlgorithmTests(TestCase):
    def test_zero_refuel_trips_and_exact_500_mile_boundary(self):
        for algorithm in ("greedy", "dijkstra"):
            for miles in (0, 450, 500):
                with self.subTest(algorithm=algorithm, miles=miles):
                    for stations in ([], [station(1, miles / 2, 0)]):
                        result = response(algorithm, miles, stations)
                        self.assertEqual(result["stops"], [])
                        self.assertEqual(result["summary"]["total_fuel_cost"], 0)
                        self.assertEqual(
                            result["summary"]["remaining_fuel_gallons"], 50 - miles / 10
                        )

    def test_single_station_purchase_and_balance(self):
        for algorithm in ("greedy", "dijkstra"):
            result = response(algorithm, 800, [station(1, 400, 4)])
            self.assertEqual(result["summary"]["total_gallons_purchased"], 30)
            self.assertEqual(result["summary"]["total_fuel_cost"], 120)
            self.assertEqual(result["summary"]["remaining_fuel_gallons"], 0)

    def test_next_cheaper_station_requires_only_enough_fuel_to_reach_it(self):
        for algorithm in ("greedy", "dijkstra"):
            result = response(algorithm, 900, [station(1, 350, 4), station(2, 650, 3)])
            self.assertEqual([r["gallons_purchased"] for r in result["stops"]], [15, 25])
            self.assertEqual(result["summary"]["total_fuel_cost"], 135)

    def test_unreachable_gaps_and_stations_only_reachable_without_detours(self):
        for algorithm in ("greedy", "dijkstra"):
            for stations in ([], [station(1, 501, 3)], [station(1, 490, 2, 20)]):
                with self.subTest(algorithm=algorithm, stations=stations):
                    with self.assertRaises(UnreachableRoute):
                        response(algorithm, 1000, stations)

    def test_cheaper_station_with_long_detour_can_cost_more_than_nearby_station(self):
        stations = [station(1, 300, 3, 50), station(2, 450, 3.5)]
        for algorithm in ("greedy", "dijkstra"):
            result = response(algorithm, 800, stations)
            self.assertEqual([r["location_id"] for r in result["stops"]], [2])
            self.assertEqual(result["summary"]["total_fuel_cost"], 105)
            self.assertEqual(result["summary"]["estimated_detour_miles"], 0)

    def test_sufficiently_cheaper_detour_is_worthwhile(self):
        result = response("dijkstra", 800, [station(1, 300, 1, 50), station(2, 450, 3.5)])
        self.assertEqual([r["location_id"] for r in result["stops"]], [1, 2])
        self.assertEqual(result["summary"]["estimated_detour_miles"], 100)
        self.assertEqual(result["summary"]["total_fuel_cost"], 52.5)

    def test_conservative_grid_plan_never_overfills_a_physical_tank(self):
        result = response(
            "dijkstra", 1300, [station(1, 250.1, 1), station(2, 650.3, 5), station(3, 1000, 5)]
        )
        self.assertGreater(result["diagnostics"]["physical_purchase_reduction_gallons"], 0)
        for stop in result["stops"]:
            self.assertLessEqual(stop["fuel_on_departure_gallons"], 50)
            self.assertGreaterEqual(stop["fuel_on_arrival_gallons"], 0)
        summary = result["summary"]
        self.assertAlmostEqual(
            50
            + summary["total_gallons_purchased"]
            - summary["estimated_total_distance_miles"] / 10,
            summary["remaining_fuel_gallons"],
            places=5,
        )

    def test_grid_optimum_matches_exhaustive_reference_on_varied_routes(self):
        rng = random.Random(92)
        for _ in range(35):
            miles = rng.uniform(600, 1800)
            stations = [
                station(i, x, rng.choice([0, 2, 3.25, 4, 7]), rng.uniform(0, 5))
                for i, x in enumerate(range(150, int(miles), 150), start=1)
            ]
            graph = RouteGraph(miles, stations)
            plan = dijkstra.optimize(graph)
            self.assertEqual(
                Decimal(plan.diagnostics["discrete_objective_cost"]), exhaustive_grid_cost(graph)
            )
            self.assertGreaterEqual(
                response("dijkstra", miles, stations)["summary"]["remaining_fuel_gallons"], 0
            )

    def test_improved_exact_states_skip_outdated_heap_entries(self):
        graph = RouteGraph(
            1200, [station(1, 250, 4), station(2, 450, 3), station(3, 650, 5), station(4, 850, 3.5)]
        )
        plan = dijkstra.optimize(graph)
        self.assertGreater(plan.diagnostics["heap_stale_pops"], 0)
        self.assertGreater(
            plan.diagnostics["distance_updates"], plan.diagnostics["states_discovered"]
        )
        self.assertLessEqual(
            plan.diagnostics["states_expanded"], plan.diagnostics["states_discovered"]
        )
        self.assertLessEqual(plan.diagnostics["states_discovered"], len(graph.positions) * 101)
        self.assertEqual(Decimal(plan.diagnostics["discrete_objective_cost"]), Decimal("222.50"))

    def test_search_limit_fails_explicitly(self):
        with self.assertRaises(SearchLimitExceeded):
            dijkstra.optimize(RouteGraph(800, [station(1, 400, 3)]), max_states=1)

    def test_different_fuel_states_remain_independent_even_at_equal_cost(self):
        graph = RouteGraph(1000, [station(1, 250, 0), station(2, 750, 4)])
        plan = dijkstra.optimize(graph)
        self.assertEqual(Decimal(plan.diagnostics["discrete_objective_cost"]), Decimal("100"))
        self.assertEqual(plan.diagnostics["discrete_remaining_fuel_gallons"], 0)
        self.assertEqual(
            [(p.station.location_id, p.gallons) for p in plan.purchases], [(1, 25), (2, 25)]
        )
        # The free station's fuel levels and the paid station's fuel levels
        # are stored as distinct states, including equal-cost free-fuel states.
        self.assertGreater(plan.diagnostics["states_discovered"], 100)
