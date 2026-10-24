"""Provider-neutral contracts. Coordinates always use WGS84 decimal degrees."""

from enum import StrEnum
from math import isfinite

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RoadNetwork(StrEnum):
    INTERSTATE = "interstate"
    US_HIGHWAY = "us_highway"
    STATE = "state_route"
    COUNTY = "county_route"
    FARM = "farm_to_market"
    PROVINCIAL = "provincial_highway"
    TRANS_CANADA = "trans_canada"
    UNKNOWN = "unknown_highway"


class RouteVariant(StrEnum):
    MAIN = "main"
    BUSINESS = "business"
    ALTERNATE = "alternate"
    BYPASS = "bypass"
    SPUR = "spur"
    LOOP = "loop"


class RoadReference(Contract):
    network: RoadNetwork
    number: str
    jurisdiction: str = ""
    variant: RouteVariant = RouteVariant.MAIN
    direction: str = ""
    raw: str

    @property
    def key(self) -> str:
        return f"{self.jurisdiction}:{self.network}:{self.number}:{self.variant}"


class ExitReference(Contract):
    identifier: str
    highway_key: str | None = None
    association: str = "unknown"
    raw: str


class AddressInput(Contract):
    name: str = Field(min_length=1, max_length=300)
    address: str = Field(default="", max_length=1000)
    city: str = Field(default="", max_length=150)
    state: str = Field(default="", max_length=100)
    country: str = Field(default="", max_length=2)
    postcode: str = Field(default="", max_length=20)


class CanonicalAddress(Contract):
    schema_version: str = "1.0"
    raw_name: str = ""
    raw_address: str = ""
    name: str = ""
    brand: str = ""
    brand_wikidata: str = ""
    store_id: str = ""
    house_number: str = ""
    street: str = ""
    unit: str = ""
    city: str = ""
    state: str = ""
    country: str = ""
    postcode: str = ""
    highways: list[RoadReference] = Field(default_factory=list)
    exits: list[ExitReference] = Field(default_factory=list)
    unparsed_address: str = ""
    warnings: list[str] = Field(default_factory=list)


class Coordinate(Contract):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)

    @model_validator(mode="after")
    def finite(self):
        if not all(isfinite(v) for v in (self.latitude, self.longitude)):
            raise ValueError("Coordinates must be finite")
        return self


class PlaceCandidate(Contract):
    provider: str
    namespace: str
    source_id: str
    release: str = ""
    address: CanonicalAddress
    coordinate: Coordinate | None = None
    precision: str = "unknown"
    operating_status: str = "unknown"
    existence_confidence: float | None = Field(default=None, ge=0, le=1)
    provenance: list[dict] = Field(default_factory=list)
    license: str = ""
    raw: dict = Field(default_factory=dict)
