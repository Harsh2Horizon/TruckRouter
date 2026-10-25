"""One indexed PostGIS query, aggregating price observations only for corridor POIs."""

import json

from django.db import connection

from .contracts import METERS_PER_MILE, CandidateLimitExceeded, Point, Station
from .geospatial import RouteProjector

# Keep identical to the expression in the spatial-index migration.
GEOGRAPHY = """ST_SetSRID(ST_MakePoint(
    (l.resolved_coordinate->>'longitude')::double precision,
    (l.resolved_coordinate->>'latitude')::double precision),4326)::geography"""


def fetch_candidates(route, *, corridor_miles, detour_multiplier, max_candidates):

    sql = f"""
        WITH corridor AS MATERIALIZED (
          SELECT l.id,l.external_id,l.address,l.resolved_coordinate
          FROM geocoding_fuellocation l
          WHERE l.resolved_coordinate IS NOT NULL
            AND ST_DWithin({GEOGRAPHY},
                ST_SetSRID(ST_GeomFromGeoJSON(%s),4326)::geography,%s)
        )
        SELECT c.id,c.external_id,c.address->>'raw_name',
               (c.resolved_coordinate->>'latitude')::double precision,
               (c.resolved_coordinate->>'longitude')::double precision,
               MAX(o.retail_price)
        FROM corridor c JOIN geocoding_fuelobservation o ON o.location_id=c.id
        WHERE o.retail_price IS NOT NULL AND o.retail_price >= 0
          AND o.retail_price < 'Infinity'::numeric
        GROUP BY c.id,c.external_id,c.address,c.resolved_coordinate
        ORDER BY c.id LIMIT %s  
    """

    with connection.cursor() as cursor:
        cursor.execute(
            sql, [json.dumps(route.geometry), corridor_miles * METERS_PER_MILE, max_candidates + 1]
        )

        records = cursor.fetchall()
    if len(records) > max_candidates:
        raise CandidateLimitExceeded("Too many corridor stations; reduce corridor_miles")

    projector = RouteProjector(route)
    stations = []

    for identity, external, name, latitude, longitude, price in records:
        point = Point(latitude=latitude, longitude=longitude)
        projected = projector.project(point)
        stations.append(
            Station(
                identity,
                external,
                name or "",
                point,
                price,
                projected.route_miles,
                detour_multiplier * projected.separation_miles,
            )
        )
    return stations
