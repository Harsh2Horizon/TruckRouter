import json
from decimal import Decimal

from fuel_optimization.contracts import METERS_PER_MILE, Point, Route, Station


def station(identity, mile, price, detour=0):
    return Station(
        identity,
        str(identity),
        f"Fuel {identity}",
        Point(latitude=0, longitude=0),
        Decimal(str(price)),
        float(mile),
        float(detour),
    )


def route(miles=800):
    return Route(((0.0, 0.0), (10.0, 0.0)), float(miles), 36000.0)


def osrm_payload(miles=800):
    return json.dumps(
        {
            "code": "Ok",
            "routes": [
                {
                    "distance": miles * METERS_PER_MILE,
                    "duration": 36000,
                    "geometry": {"type": "LineString", "coordinates": [[0, 0], [10, 0]]},
                }
            ],
        }
    ).encode()
