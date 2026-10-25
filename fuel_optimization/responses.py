"""A single physical fuel/cost ledger and JSON contract for both optimizers."""

from decimal import Decimal

from .contracts import EPSILON, MAX_RANGE_MILES, MPG, TANK_GALLONS


def number(value, places=6):
    return round(float(value), places)


def build_response(
    *, algorithm, route, plan, elapsed_ms, candidate_count, corridor_miles, detour_multiplier
):
    fuel, previous_mile, previous_detour = TANK_GALLONS, 0.0, 0.0
    detours, purchased, adjustment, cost, stops = 0.0, 0.0, 0.0, Decimal(0), []
    for purchase in plan.purchases:
        station = purchase.station
        leg = station.route_miles - previous_mile + previous_detour + station.detour_miles
        arrival = fuel - leg / MPG
        if leg < -EPSILON or arrival < -1e-7:
            raise RuntimeError("The optimizer returned an infeasible physical fuel plan")
        arrival = max(0.0, arrival)
        # Rounded Dijkstra driving consumption can understate physical remaining fuel.
        # Cap the actual pump quantity instead of overfilling the real tank.
        gallons = max(0.0, min(purchase.gallons, TANK_GALLONS - arrival))
        adjustment += purchase.gallons - gallons
        fuel = arrival + gallons
        line_cost = (Decimal(str(gallons)) * station.price).quantize(Decimal("0.000001"))
        cost += line_cost
        detours += 2 * station.detour_miles
        purchased += gallons
        previous_mile, previous_detour = station.route_miles, station.detour_miles
        if gallons > EPSILON:
            stops.append(
                {
                    "location_id": station.location_id,
                    "external_id": station.external_id,
                    "name": station.name,
                    "latitude": station.point.latitude,
                    "longitude": station.point.longitude,
                    "route_mile": number(station.route_miles),
                    "estimated_detour_miles": number(2 * station.detour_miles),
                    "price_per_gallon": number(station.price, 8),
                    "gallons_purchased": number(gallons),
                    "fuel_cost": number(line_cost),
                    "fuel_on_arrival_gallons": number(arrival),
                    "fuel_on_departure_gallons": number(fuel),
                }
            )
    fuel -= (route.distance_miles - previous_mile + previous_detour) / MPG
    if not -1e-7 <= fuel <= TANK_GALLONS + EPSILON:
        raise RuntimeError("The optimizer returned an infeasible destination fuel balance")
    return {
        "algorithm": algorithm,
        "route": {
            "type": "Feature",
            "geometry": route.geometry,
            "properties": {
                "base_distance_miles": number(route.distance_miles),
                "base_duration_seconds": number(route.duration_seconds),
            },
        },
        "stops": stops,
        "summary": {
            "base_distance_miles": number(route.distance_miles),
            "estimated_detour_miles": number(detours),
            "estimated_total_distance_miles": number(route.distance_miles + detours),
            "total_gallons_purchased": number(purchased),
            "total_fuel_cost": number(cost),
            "remaining_fuel_gallons": number(max(0.0, fuel)),
            "fuel_stop_count": len(stops),
        },
        "algorithm_execution_time_ms": number(elapsed_ms, 3),
        "candidate_count": candidate_count,
        "assumptions": {
            "mpg": MPG,
            "tank_capacity_gallons": TANK_GALLONS,
            "initial_fuel_gallons": TANK_GALLONS,
            "maximum_range_miles": MAX_RANGE_MILES,
            "corridor_miles": corridor_miles,
            "detour_multiplier": detour_multiplier,
            "road_accessibility": "Approximate: geometric out-and-back detours, not routed access",
            "price_basis": "MAX(retail_price) per location; common currency per US gallon assumed",
            "route_scope": "Forward progression along the single OSRM base route",
        },
        "diagnostics": {
            **plan.diagnostics,
            "routing_api_calls": 1,
            "physical_purchase_reduction_gallons": number(adjustment),
        },
    }
