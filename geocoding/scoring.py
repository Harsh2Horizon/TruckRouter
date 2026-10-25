"""Explainable heuristic scores, not calibrated probabilities of correctness."""

import re
from dataclasses import asdict, dataclass, field
from math import asin, cos, radians, sin, sqrt

from rapidfuzz.fuzz import ratio, token_sort_ratio

from .normalization import (
    BOUNDARY_CITY_WARNING,
    NEAREST_CITY_WARNING,
    SUBDIVISIONS,
    brand_key,
    city_key,
    locality_codes,
    text_key,
)
from .schemas import CanonicalAddress, Coordinate, RoadNetwork

ALGORITHM_VERSION = "brand-locality-3.1"
WEIGHTS = {
    "name": 0.60,
    "locality": 0.30,
    "highways": 0.07,
    "exits": 0.03,
}
ACCEPT_THRESHOLD = 85.0
REVIEW_THRESHOLD = 65.0
MIN_MARGIN = 8.0
CLUSTER_RADIUS_METERS = 250.0
CITY_MATCH_THRESHOLD = 0.90
NAME_MATCH_THRESHOLD = 0.70
NAME_ACCEPT_THRESHOLD = 0.85
CROSS_SOURCE_REVIEW_METERS = 1000.0
CROSS_SOURCE_SEVERE_METERS = 5000.0
CLOSED_STATUSES = {"permanently_closed", "closed", "temporarily_closed"}


def similarity(a: str, b: str) -> float:
    return token_sort_ratio(text_key(a), text_key(b)) / 100 if a and b else 0.0


def business_name(address: CanonicalAddress) -> str:
    """Store identifiers are stripped from identity text and never compared."""
    value = (address.raw_name or address.name).partition("#")[0]
    value = re.sub(r"\b(?:STORE|LOCATION)\s*(?:NO\.?\s*)?\d+[A-Z]?\s*$", "", value, flags=re.I)
    return brand_key(value)


def identity_key(value: str) -> str:
    value = brand_key(value.partition("#")[0])
    value = re.sub(r"\b(?:STORE|LOCATION|NO)\s+\d+[A-Z]?\s*$", "", value)
    # Descriptive business suffixes do not identify a different brand.
    value = re.sub(
        r"\b(?:TRAVEL CENTERS?|TRAVEL PLAZA|TRUCK STOPS?|CONVENIENCE STORE|FUEL STOP)\b",
        " ",
        value,
    )
    return " ".join(value.split())


def name_similarity(query: CanonicalAddress, candidate: CanonicalAddress) -> tuple[float, dict]:
    left = [identity_key(query.brand), identity_key(business_name(query))]
    right = [identity_key(candidate.brand), identity_key(business_name(candidate))]
    generic = {"", "UNKNOWN", "FUEL", "GAS STATION", "TRUCK STOP", "EXPRESS", "TRAVEL CENTER"}
    scores = []
    for a in left:
        for b in right:
            if a in generic or b in generic:
                continue
            if {a, b} == {"KWIK TRIP", "QUIKTRIP"}:
                continue
            scores.append((max(ratio(a, b), token_sort_ratio(a, b)) / 100, a, b))
    best = max(scores, default=(0.0, "", ""))
    return best[0], {
        "input_name": best[1],
        "candidate_name": best[2],
        "method": "max normalized brand or business-name fuzzy similarity",
    }


def city_similarity(left: str, right: str) -> float:
    return similarity(city_key(left), city_key(right))


def locality_evidence(query: CanonicalAddress, candidate: CanonicalAddress) -> dict:
    qs, qc = locality_codes(query.state, query.country)
    cs, cc = locality_codes(candidate.state, candidate.country)
    city = city_similarity(query.city, candidate.city)
    state = float(bool(qs and cs and qs == cs))
    country = float(bool(qc and cc and qc == cc))
    missing = [
        key
        for key in ("city", "state", "country")
        if not getattr(query, key) or not getattr(candidate, key)
    ]
    recognized = (
        qc in SUBDIVISIONS
        and cc in SUBDIVISIONS
        and qs in SUBDIVISIONS[qc]
        and cs in SUBDIVISIONS[cc]
    )
    complete = (
        not missing and recognized and state == 1 and country == 1 and city >= CITY_MATCH_THRESHOLD
    )
    return {
        "city": round(city, 6),
        "state": state,
        "country": country,
        "complete": complete,
        "missing": missing,
        "input_city": city_key(query.city),
        "candidate_city": city_key(candidate.city),
        "input_state": qs,
        "candidate_state": cs,
        "input_country": qc,
        "candidate_country": cc,
        "candidate_city_method": "nearest_settlement"
        if NEAREST_CITY_WARNING in candidate.warnings
        else "point_in_polygon"
        if BOUNDARY_CITY_WARNING in candidate.warnings
        else "provider",
    }


def road_keys(address):
    state, country = locality_codes(address.state, address.country)
    result = {}
    for road in address.highways:
        jurisdiction = road.jurisdiction
        if road.network in {
            RoadNetwork.INTERSTATE,
            RoadNetwork.US_HIGHWAY,
            RoadNetwork.TRANS_CANADA,
        }:
            jurisdiction = jurisdiction or country
        elif not jurisdiction or jurisdiction == country:
            jurisdiction = f"{country}-{state}" if country and state else jurisdiction
        number = str(int(road.number)) if road.number.isdigit() else road.number.upper()
        result[road.key] = f"{jurisdiction}:{road.network}:{number}:{road.variant}"
    return result


def distance_meters(a: Coordinate, b: Coordinate) -> float:
    lat1, lat2 = radians(a.latitude), radians(b.latitude)
    dlat = lat2 - lat1
    dlon = radians(b.longitude - a.longitude)
    hav = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 6371008.8 * 2 * asin(min(1, sqrt(hav)))


@dataclass
class MatchScore:
    score: float
    similarity: float
    coverage: float
    components: dict[str, float]
    missing: list[str]
    conflicts: list[str] = field(default_factory=list)
    applicable_weight: float = 0.0
    compared_weight: float = 0.0
    evidence_strength: float = 0.0
    applicable_fields: list[str] = field(default_factory=list)
    missing_in_query: list[str] = field(default_factory=list)
    locality: dict = field(default_factory=dict)
    name_match: dict = field(default_factory=dict)
    contributions: dict[str, float] = field(default_factory=dict)
    ungated_score: float = 0.0

    def to_dict(self):
        return asdict(self)


def score_addresses(query: CanonicalAddress, candidate: CanonicalAddress) -> MatchScore:
    name, name_match = name_similarity(query, candidate)
    locality = locality_evidence(query, candidate)
    components = {"name": name, "locality": locality["city"] if locality["complete"] else 0.0}
    applicable, missing = ["name", "locality"], []
    conflicts = []
    if not locality["complete"]:
        conflicts.append("locality_incomplete" if locality["missing"] else "locality_conflict")
    if name < NAME_MATCH_THRESHOLD:
        conflicts.append("name_not_matched")
    for key in ("highways", "exits"):
        left, right = getattr(query, key), getattr(candidate, key)
        if key == "highways":
            left, right = set(road_keys(query).values()), set(road_keys(candidate).values())
        else:
            qroads, croads = road_keys(query), road_keys(candidate)
            left = {
                (qroads.get(e.highway_key, e.highway_key), e.identifier.upper())
                for e in left
                if e.highway_key
            }
            right = {
                (croads.get(e.highway_key, e.highway_key), e.identifier.upper())
                for e in right
                if e.highway_key
            }
        if left:
            applicable.append(key)
            components[key] = len(left & right) / min(len(left), len(right)) if right else 0.0
            if not right:
                missing.append(key)
    if not locality["complete"]:
        missing.extend(f"locality.{key}" for key in locality["missing"])
    applicable_weight = sum(WEIGHTS[key] for key in applicable)
    contributions = {
        key: 100 * WEIGHTS[key] * components[key] / applicable_weight for key in applicable
    }
    score = sum(contributions.values())
    available_weight = sum(WEIGHTS[key] for key in applicable if key not in missing)
    if not locality["complete"]:
        available_weight -= WEIGHTS["locality"]
    coverage = max(0.0, available_weight / applicable_weight)
    return MatchScore(
        score=0.0 if conflicts else round(min(100.0, score), 3),
        similarity=round(score / 100, 6),
        coverage=round(coverage, 4),
        components=components,
        missing=missing,
        conflicts=conflicts,
        applicable_weight=round(applicable_weight, 4),
        compared_weight=round(max(0.0, available_weight), 4),
        evidence_strength=1.0,
        applicable_fields=applicable,
        missing_in_query=[key for key in WEIGHTS if key not in applicable],
        locality=locality,
        name_match=name_match,
        contributions={key: round(value, 6) for key, value in contributions.items()},
        ungated_score=round(score, 3),
    )


def has_acceptance_evidence(score: MatchScore) -> bool:
    return score.components.get("name", 0) >= NAME_ACCEPT_THRESHOLD and score.locality["complete"]


def input_addresses_disagree(left, right):
    """Input quality overrides use only the same four allowed evidence groups."""
    if not locality_evidence(left, right)["complete"]:
        return True
    if name_similarity(left, right)[0] < NAME_MATCH_THRESHOLD:
        return True
    lroads, rroads = set(road_keys(left).values()), set(road_keys(right).values())
    if lroads and rroads and not lroads & rroads:
        return True
    lexits = {(e.highway_key, e.identifier) for e in left.exits if e.highway_key}
    rexits = {(e.highway_key, e.identifier) for e in right.exits if e.highway_key}
    return bool(lexits and rexits and not lexits & rexits)
