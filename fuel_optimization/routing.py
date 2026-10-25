"""Exactly one OSRM route request: no retries, redirects, tables or station routes."""

import json
import math
from http.client import HTTPException
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from django.conf import settings
from pydantic import ValidationError

from .contracts import METERS_PER_MILE, Point, Route, RoutingError


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_route(origin: Point, destination: Point) -> Route:
    base = settings.FUEL_OSRM_BASE_URL.rstrip("/")
    coordinates = (
        f"{origin.longitude},{origin.latitude};{destination.longitude},{destination.latitude}"
    )
    options = urlencode(
        {"overview": "full", "geometries": "geojson", "steps": "false", "alternatives": "false"}
    )
    request = Request(
        f"{base}/route/v1/driving/{coordinates}?{options}",
        headers={"User-Agent": "truckroute-fuel/1.0", "Accept": "application/json"},
    )
    try:
        # The opener is per call and has no process-global mutable state.
        with build_opener(NoRedirects()).open(
            request, timeout=settings.FUEL_OSRM_TIMEOUT
        ) as response:
            payload = response.read(settings.FUEL_OSRM_MAX_RESPONSE_BYTES + 1)
        if len(payload) > settings.FUEL_OSRM_MAX_RESPONSE_BYTES:
            raise RoutingError("OSRM response exceeded the configured size limit")
        body = json.loads(payload)
        if body.get("code") != "Ok" or not body.get("routes"):
            raise RoutingError("OSRM could not produce a route")

        route = body["routes"][0]

        geometry = route["geometry"]
        coords = geometry["coordinates"]

        if (
            geometry["type"] != "LineString"
            or not 2 <= len(coords) <= settings.FUEL_MAX_ROUTE_POINTS
        ):
            raise ValueError("Unsupported route geometry")
        checked = []
        for coordinate in coords:
            if len(coordinate) != 2:
                raise ValueError("A route position must be longitude, latitude")
            p = Point(longitude=coordinate[0], latitude=coordinate[1])
            checked.append((p.longitude, p.latitude))
        if any(isinstance(route[key], bool) for key in ("distance", "duration")):
            raise ValueError("Boolean route metrics are invalid")
        distance, duration = float(route["distance"]), float(route["duration"])
        if not all(math.isfinite(n) and n >= 0 for n in (distance, duration)):
            raise ValueError("Invalid route metrics")
        return Route(tuple(checked), distance / METERS_PER_MILE, duration)

    except RoutingError:
        raise
    except (
        OSError,
        HTTPException,
        ValueError,
        KeyError,
        TypeError,
        ValidationError,
        AttributeError,
    ) as exc:
        raise RoutingError("Invalid or unavailable OSRM route response") from exc
