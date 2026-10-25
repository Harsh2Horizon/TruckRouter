"""Immutable domain objects and the API's validated input contract."""

from dataclasses import dataclass, field
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

METERS_PER_MILE = 1609.344
MPG = 10.0
TANK_GALLONS = 50.0
MAX_RANGE_MILES = MPG * TANK_GALLONS
FUEL_STEP = 0.5
TANK_UNITS = int(TANK_GALLONS / FUEL_STEP)
EPSILON = 1e-9


class Point(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False, strict=True)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False, strict=True)


class OptimizationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    origin: Point
    destination: Point
    corridor_miles: float | None = Field(
        default=None, gt=0, le=25, allow_inf_nan=False, strict=True
    )
    detour_multiplier: float | None = Field(
        default=None, ge=1, le=5, allow_inf_nan=False, strict=True
    )


@dataclass(frozen=True, slots=True)
class Route:
    coordinates: tuple[tuple[float, float], ...]  # GeoJSON longitude, latitude
    distance_miles: float
    duration_seconds: float

    @property
    def geometry(self):
        return {"type": "LineString", "coordinates": self.coordinates}


@dataclass(frozen=True, slots=True)
class Station:
    location_id: int
    external_id: str
    name: str
    point: Point
    price: Decimal
    route_miles: float
    detour_miles: float  # ONE WAY, after multiplying geometric separation


@dataclass(frozen=True, slots=True)
class Purchase:
    station: Station
    gallons: float


@dataclass(frozen=True, slots=True)
class Plan:
    purchases: tuple[Purchase, ...]
    diagnostics: dict = field(default_factory=dict)


class OptimizationError(Exception):
    code = "optimization_error"
    status = 422


class UnreachableRoute(OptimizationError):
    code = "unreachable_route"


class SearchLimitExceeded(OptimizationError):
    code = "search_limit_exceeded"
    status = 503


class CandidateLimitExceeded(OptimizationError):
    code = "candidate_limit_exceeded"


class RoutingError(OptimizationError):
    code = "routing_unavailable"
    status = 502
