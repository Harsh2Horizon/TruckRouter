"""Explainable heuristic scores, not calibrated probabilities of correctness."""

import re
from dataclasses import asdict, dataclass, field
from math import asin, cos, radians, sin, sqrt

from rapidfuzz.fuzz import ratio, token_sort_ratio

from .normalization import brand_key, store_key, street_key, text_key
from .schemas import CanonicalAddress, Coordinate, PlaceCandidate

ALGORITHM_VERSION = "evidence-2.0"
WEIGHTS = {
    "brand": 0.20,
    "name": 0.10,
    "store_id": 0.22,
    "city": 0.14,
    "state": 0.07,
    "country": 0.03,
    "postcode": 0.04,
    "street": 0.08,
    "house_number": 0.03,
    "highways": 0.07,
    "exits": 0.02,
}
FULL_EVIDENCE_WEIGHT = 0.60
ACCEPT_THRESHOLD = 90.0
REVIEW_THRESHOLD = 65.0
MIN_MARGIN = 8.0
CLUSTER_RADIUS_METERS = 250.0
CLOSED_STATUSES = {"permanently_closed", "closed", "temporarily_closed"}


def similarity(a: str, b: str) -> float:
    return token_sort_ratio(text_key(a), text_key(b)) / 100 if a and b else 0.0


def business_name(address: CanonicalAddress) -> str:
    """The explicit store token is compared once, in store_id."""
    value = (address.raw_name or address.name).partition("#")[0]
    value = re.sub(r"\b(?:STORE|LOCATION)\s*(?:NO\.?\s*)?\d+[A-Z]?\s*$", "", value, flags=re.I)
    return brand_key(value)


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

    def to_dict(self):
        return asdict(self)


def score_addresses(query: CanonicalAddress, candidate: CanonicalAddress) -> MatchScore:
    components = {}
    conflicts = []
    applicable = []
    for key in WEIGHTS:
        if key in {"highways", "exits"}:
            left, right = getattr(query, key), getattr(candidate, key)
            if key == "exits":
                left = [e for e in left if e.highway_key]
                right = [e for e in right if e.highway_key]
        elif key == "name":
            left, right = business_name(query), business_name(candidate)
        else:
            left, right = getattr(query, key), getattr(candidate, key)
        if left:
            applicable.append(key)
        if not left or not right:
            continue
        if key == "store_id":
            components[key] = float(store_key(left) == store_key(right))
        elif key in {"state", "country", "postcode", "house_number"}:
            components[key] = float(text_key(left) == text_key(right))
        elif key == "brand":
            components[key] = brand_similarity(left, right)
        elif key == "street":
            components[key] = ratio(street_key(left), street_key(right)) / 100
        elif key == "highways":
            left, right = {h.key for h in left}, {h.key for h in right}
            # A street address can name just one of the input's access highways.
            components[key] = len(left & right) / min(len(left), len(right))
        elif key == "exits":
            left = {(e.highway_key, e.identifier) for e in left}
            right = {(e.highway_key, e.identifier) for e in right}
            components[key] = len(left & right) / min(len(left), len(right))
        else:
            components[key] = similarity(left, right)
    if query.brand_wikidata and candidate.brand_wikidata:
        components["brand"] = float(query.brand_wikidata == candidate.brand_wikidata)
        if "brand" not in applicable:
            applicable.append("brand")
    for key in ("country", "state"):
        if key in components and components[key] == 0:
            conflicts.append(f"{key}_conflict")
    if components.get("brand", 1) < 0.55:
        conflicts.append("brand_conflict")
    if components.get("store_id") == 0:
        conflicts.append("store_id_conflict")
    if components.get("house_number") == 0:
        conflicts.append("house_number_conflict")
    applicable_weight = sum(WEIGHTS[key] for key in applicable)
    compared_weight = sum(WEIGHTS[key] for key in components)
    coverage = compared_weight / applicable_weight if applicable_weight else 0.0
    average = (
        sum(WEIGHTS[key] * value for key, value in components.items()) / compared_weight
        if compared_weight
        else 0
    )
    strength = min(1.0, compared_weight / FULL_EVIDENCE_WEIGHT)
    score = 100 * average * sqrt(min(1.0, coverage)) * strength
    return MatchScore(
        score=0.0 if conflicts else round(min(100.0, score), 3),
        similarity=round(average, 4),
        coverage=round(coverage, 4),
        components=components,
        missing=[key for key in applicable if key not in components],
        conflicts=conflicts,
        applicable_weight=round(applicable_weight, 4),
        compared_weight=round(compared_weight, 4),
        evidence_strength=round(strength, 4),
        applicable_fields=applicable,
        missing_in_query=[key for key in WEIGHTS if key not in applicable],
    )


def branch_evidence(
    query: CanonicalAddress, candidate: CanonicalAddress, score: MatchScore
) -> bool:
    brand_ok = score.components.get("brand", 0) >= 0.9
    store_ok = bool(
        query.store_id
        and candidate.store_id
        and store_key(query.store_id) == store_key(candidate.store_id)
    )
    street_ok = bool(
        query.house_number
        and candidate.house_number
        and query.street
        and candidate.street
        and text_key(query.house_number) == text_key(candidate.house_number)
        and score.components.get("street", 0) >= 0.95
    )
    locality_ok = (
        score.components.get("city", 0) >= 0.85 or score.components.get("postcode", 0) == 1
    )
    # A brand/store identifier alone can recur across nations and administrative regions.
    scope_ok = score.components.get("country") == 1 and score.components.get("state") == 1
    return brand_ok and (store_ok or street_ok) and locality_ok and scope_ok


def same_place(a: PlaceCandidate, b: PlaceCandidate) -> bool:
    if a.coordinate is None or b.coordinate is None:
        return False
    distance = distance_meters(a.coordinate, b.coordinate)
    if distance > CLUSTER_RADIUS_METERS:
        return False
    score = score_addresses(a.address, b.address)
    if score.conflicts:
        return False
    store_ok = bool(
        a.address.store_id
        and b.address.store_id
        and store_key(a.address.store_id) == store_key(b.address.store_id)
    )
    street_ok = bool(
        a.address.house_number
        and b.address.house_number
        and a.address.street
        and b.address.street
        and text_key(a.address.house_number) == text_key(b.address.house_number)
        and score.components.get("street", 0) >= 0.95
    )
    name_ok = bool(
        business_name(a.address) not in {"", "UNKNOWN", "FUEL", "GAS STATION", "TRUCK STOP"}
        and score.components.get("name", 0) >= 0.95
        and score.components.get("brand", 1) >= 0.9
    )
    return store_ok or street_ok or (distance <= 35 and name_ok)


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
            reasons.append("insufficient_branch_or_locality_evidence")
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
            "formula": "100 * weighted_similarity * sqrt(coverage) * evidence_strength",
            "full_evidence_weight": FULL_EVIDENCE_WEIGHT,
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
        decision = _rank_scored(
            query,
            [row for row in ranked if row["candidate"].provider == provider],
            truncated=limited,
        )
        best = decision["best_candidate"]
        per_source[provider] = {
            "status": decision["status"],
            "score": best["score"] if best else 0.0,
            "matched_coordinate": best["coordinate"] if best else None,
            "matched_source": best["source"] if best else None,
            "matched_name": best["name"] if best else "",
            "matched_address": best["address"] if best else "",
            "matched_city": best["city"] if best else "",
            "matched_state": best["state"] if best else "",
            "evidence_coverage": best["evidence_coverage"] if best else 0.0,
            "margin": best["margin"] if best else 0.0,
            "coordinate": decision["coordinate"],
            "selected_source": decision["selected_source"],
            "decision_score": decision["score"],
            "reasons": decision["reasons"],
            "candidate_search_truncated": limited,
        }
    result["per_source"] = per_source
    update_highest_match(result)
    return result


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
