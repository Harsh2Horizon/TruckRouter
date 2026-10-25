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
from .schemas import CanonicalAddress, Coordinate, PlaceCandidate, RoadNetwork

ALGORITHM_VERSION = "brand-locality-3.1"
WEIGHTS = {
    "name": 0.60,
    "locality": 0.30,
    "highways": 0.07,
    "exits": 0.03,
}
FULL_EVIDENCE_WEIGHT = 1.0
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


def brand_similarity(left: str, right: str) -> float:
    left, right = brand_key(left), brand_key(right)
    if {left, right} == {"KWIK TRIP", "QUIKTRIP"}:
        return 0.0
    return similarity(left, right)


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


def branch_evidence(
    query: CanonicalAddress, candidate: CanonicalAddress, score: MatchScore
) -> bool:
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


def same_place(a: PlaceCandidate, b: PlaceCandidate) -> bool:
    if a.coordinate is None or b.coordinate is None:
        return False
    distance = distance_meters(a.coordinate, b.coordinate)
    if distance > CLUSTER_RADIUS_METERS:
        return False
    score = score_addresses(a.address, b.address)
    if score.conflicts:
        return False
    name_ok = score.components.get("name", 0) >= 0.90 and score.locality["complete"]
    road_ok = score.components.get("highways", 0) > 0 and score.components.get("exits", 0) > 0
    return name_ok and (distance <= 100 or road_ok)


def _rank_scored(query: CanonicalAddress, ranked, *, truncated=False):
    clusters = []
    for row in ranked:
        if row["evidence"].conflicts or row["evidence"].score < REVIEW_THRESHOLD:
            continue
        candidate = row["candidate"]
        if candidate.operating_status in CLOSED_STATUSES:
            continue
        if candidate.existence_confidence == 0:
            continue
        # Complete-link clustering prevents a chain of nearby records joining distant stations.
        for cluster in clusters:
            if all(same_place(candidate, member["candidate"]) for member in cluster):
                cluster.append(row)
                break
        else:
            clusters.append([row])

    reasons = []
    status, coordinate, best_identity = "unmatched", None, None
    best_score = clusters[0][0]["evidence"].score if clusters else 0.0
    margin = best_score - clusters[1][0]["evidence"].score if len(clusters) > 1 else best_score
    representative = None
    if clusters:
        status = "needs_review"
        best = clusters[0][0]
        # Select a real provider coordinate (medoid), never an averaged point between stations.
        located = [
            r
            for r in clusters[0]
            if r["candidate"].coordinate and r["candidate"].precision in {"poi", "poi_center"}
        ]
        if located:
            representative = min(
                located,
                key=lambda row: sum(
                    distance_meters(row["candidate"].coordinate, other["candidate"].coordinate)
                    for other in located
                ),
            )
        if best_score < ACCEPT_THRESHOLD:
            reasons.append("score_below_accept_threshold")
        if margin < MIN_MARGIN:
            reasons.append("competing_location_with_similar_score")
        if not branch_evidence(query, best["candidate"].address, best["evidence"]):
            reasons.append("insufficient_name_or_locality_evidence")
        if NEAREST_CITY_WARNING in best["candidate"].address.warnings:
            reasons.append("approximate_locality_requires_review")
        if representative is None:
            reasons.append("no_poi_coordinate")
        if truncated:
            reasons.append("candidate_limit_reached")
        if not reasons:
            status = "accepted"
            coordinate = representative["candidate"].coordinate.model_dump()
        chosen = representative or best
        best_identity = identity(chosen["candidate"])
    else:
        reasons.append("no_eligible_candidate")
        if truncated:
            reasons.append("candidate_limit_reached")

    return {
        "algorithm_version": ALGORITHM_VERSION,
        "status": status,
        "score": best_score,
        "margin": round(margin, 3),
        "coordinate": coordinate,
        "selected_source": best_identity if status == "accepted" else None,
        "proposed_source": best_identity,
        "reasons": reasons,
        "thresholds": {
            "accept": ACCEPT_THRESHOLD,
            "review": REVIEW_THRESHOLD,
            "margin": MIN_MARGIN,
            "cluster_radius_m": CLUSTER_RADIUS_METERS,
        },
        "scoring": {
            "weights": WEIGHTS,
            "formula": (
                "100 * sum(weight * similarity) / applicable_input_weight; locality and name gates"
            ),
            "city_match_threshold": CITY_MATCH_THRESHOLD,
            "name_match_threshold": NAME_MATCH_THRESHOLD,
            "name_accept_threshold": NAME_ACCEPT_THRESHOLD,
            "store_ids_used": False,
            "calibrated_probability": False,
        },
        "best_candidate": _best_located(ranked),
        "clusters": [
            {
                "score": cluster[0]["evidence"].score,
                "members": [identity(row["candidate"]) for row in cluster],
                "lineage": sorted(
                    {
                        str(p.get("dataset", "unknown"))
                        for row in cluster
                        for p in row["candidate"].provenance
                    }
                ),
            }
            for cluster in clusters
        ],
        "candidates": [
            {
                "source": identity(row["candidate"]),
                "coordinate": (
                    row["candidate"].coordinate.model_dump()
                    if row["candidate"].coordinate
                    else None
                ),
                "address": row["candidate"].address.model_dump(mode="json"),
                "precision": row["candidate"].precision,
                "operating_status": row["candidate"].operating_status,
                "existence_confidence": row["candidate"].existence_confidence,
                "provenance": row["candidate"].provenance,
                "license": row["candidate"].license,
                "evidence": row["evidence"].to_dict(),
            }
            for row in ranked
        ],
    }


def _best_located(ranked):
    eligible = [
        row
        for row in ranked
        if row["evidence"].score > 0
        and not row["evidence"].conflicts
        and row["candidate"].coordinate is not None
        and row["candidate"].precision in {"poi", "poi_center"}
        and row["candidate"].operating_status not in CLOSED_STATUSES
        and row["candidate"].existence_confidence != 0
    ]
    if not eligible:
        return None
    best = eligible[0]
    other = next(
        (row for row in eligible[1:] if not same_place(best["candidate"], row["candidate"])), None
    )
    candidate = best["candidate"]
    return {
        "source": identity(candidate),
        "score": best["evidence"].score,
        "coordinate": candidate.coordinate.model_dump(),
        "evidence_coverage": best["evidence"].coverage,
        "similarity": best["evidence"].similarity,
        "margin": round(best["evidence"].score - other["evidence"].score, 3)
        if other
        else best["evidence"].score,
        "name": candidate.address.raw_name,
        "address": candidate.address.raw_address,
        "city": candidate.address.city,
        "state": candidate.address.state,
        "precision": candidate.precision,
        "country": candidate.address.country,
        "evidence": best["evidence"].to_dict(),
    }


def rank_candidates(
    query: CanonicalAddress,
    candidates: list[PlaceCandidate],
    *,
    truncated=False,
    providers=None,
    truncated_providers=None,
):
    ranked = [{"candidate": c, "evidence": score_addresses(query, c.address)} for c in candidates]
    ranked.sort(
        key=lambda row: (
            -row["evidence"].score,
            -row["evidence"].components.get("name", 0),
            row["candidate"].provider,
            row["candidate"].namespace,
            row["candidate"].source_id,
        )
    )
    result = _rank_scored(query, ranked, truncated=truncated)
    providers = sorted(set(providers or ()) | {c.provider for c in candidates})
    per_source = {}
    for provider in providers:
        limited = provider in truncated_providers if truncated_providers is not None else truncated
        source_rows = [row for row in ranked if row["candidate"].provider == provider]
        decision = _rank_scored(
            query,
            source_rows,
            truncated=limited,
        )
        best = decision["best_candidate"]
        diagnostic = source_rows[0] if source_rows else None
        evidence = (
            best["evidence"] if best else diagnostic["evidence"].to_dict() if diagnostic else None
        )
        per_source[provider] = {
            "status": decision["status"],
            "score": best["score"] if best else 0.0,
            "matched_coordinate": best["coordinate"] if best else None,
            "matched_source": best["source"] if best else None,
            "matched_name": best["name"] if best else "",
            "matched_address": best["address"] if best else "",
            "matched_city": best["city"] if best else "",
            "matched_state": best["state"] if best else "",
            "matched_country": best["country"] if best else "",
            "precision": best["precision"] if best else "unknown",
            "criteria": evidence,
            "evidence_coverage": best["evidence_coverage"] if best else 0.0,
            "margin": best["margin"] if best else 0.0,
            "coordinate": decision["coordinate"],
            "selected_source": decision["selected_source"],
            "decision_score": decision["score"],
            "reasons": decision["reasons"],
            "candidate_search_truncated": limited,
            "diagnostic_candidate": {
                "source": identity(diagnostic["candidate"]),
                "name": diagnostic["candidate"].address.raw_name,
                "city": diagnostic["candidate"].address.city,
                "state": diagnostic["candidate"].address.state,
                "country": diagnostic["candidate"].address.country,
                "coordinate": diagnostic["candidate"].coordinate.model_dump()
                if diagnostic["candidate"].coordinate
                else None,
            }
            if diagnostic and not best
            else None,
        }
    result["per_source"] = per_source
    update_highest_match(result)
    result["coordinate_disagreement"] = coordinate_disagreement(per_source)
    disagreement = result["coordinate_disagreement"]
    if disagreement["credible_max_distance_m"] > CROSS_SOURCE_REVIEW_METERS:
        result["reasons"].append("cross_source_coordinates_diverge")
        if result["status"] == "accepted":
            result.update(status="needs_review", coordinate=None, selected_source=None)
    return result


def coordinate_disagreement(per_source):
    from itertools import combinations

    located = [(p, r) for p, r in per_source.items() if r.get("matched_coordinate")]
    pairs, credible = {}, []
    for (p, left), (q, right) in combinations(located, 2):
        distance = distance_meters(
            Coordinate(**left["matched_coordinate"]), Coordinate(**right["matched_coordinate"])
        )
        pairs[f"{p}__{q}"] = round(distance, 3)
        if left["score"] >= ACCEPT_THRESHOLD and right["score"] >= ACCEPT_THRESHOLD:
            credible.append(distance)
    maximum = max(pairs.values(), default=0.0)
    if len(located) < 2:
        severity = "single_source" if located else "no_coordinates"
    elif maximum >= CROSS_SOURCE_SEVERE_METERS:
        severity = "severe"
    elif maximum >= CROSS_SOURCE_REVIEW_METERS:
        severity = "divergent"
    elif maximum > CLUSTER_RADIUS_METERS:
        severity = "moderate"
    else:
        severity = "consistent"
    names = {r.get("matched_name") for _, r in located}
    note = ""
    if maximum >= CROSS_SOURCE_REVIEW_METERS:
        note = (
            "Possible different branches in the same locality; inspect road and exit evidence"
            if len(names) <= 1
            else (
                "Possible branch or fuzzy-name disagreement; inspect source names and road evidence"
            )
        )
    return {
        "pairwise_meters": pairs,
        "max_distance_m": maximum,
        "credible_max_distance_m": round(max(credible, default=0.0), 3),
        "severity": severity,
        "source_count": len(located),
        "possible_cause": note,
        "review_threshold_m": CROSS_SOURCE_REVIEW_METERS,
        "severe_threshold_m": CROSS_SOURCE_SEVERE_METERS,
    }


def update_highest_match(result):
    """The reported score and point must come from the same provider record."""
    eligible = sorted(
        (
            (provider, row)
            for provider, row in result["per_source"].items()
            if row.get("matched_coordinate") is not None
        ),
        key=lambda item: (-item[1]["score"], item[0]),
    )
    highest = eligible[0] if eligible else None
    result["highest_matching_score"] = highest[1]["score"] if highest else 0.0
    result["highest_matching_source"] = highest[0] if highest else None
    result["highest_matching_coordinate"] = highest[1]["matched_coordinate"] if highest else None
    result["highest_matching_status"] = highest[1]["status"] if highest else "unmatched"
    result["highest_matching_source_id"] = (
        highest[1]["matched_source"]["source_id"] if highest else None
    )
    result["highest_matching_tied_sources"] = [
        provider for provider, row in eligible if row["score"] == result["highest_matching_score"]
    ]


def identity(candidate: PlaceCandidate) -> dict:
    return {
        "provider": candidate.provider,
        "namespace": candidate.namespace,
        "source_id": candidate.source_id,
        "release": candidate.release,
    }
