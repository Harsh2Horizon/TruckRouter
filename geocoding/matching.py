"""Source rankings, branch clustering and cross-source coordinate decisions."""

from .normalization import NEAREST_CITY_WARNING
from .schemas import CanonicalAddress, Coordinate, PlaceCandidate
from .scoring import (
    ACCEPT_THRESHOLD,
    ALGORITHM_VERSION,
    CITY_MATCH_THRESHOLD,
    CLOSED_STATUSES,
    CLUSTER_RADIUS_METERS,
    CROSS_SOURCE_REVIEW_METERS,
    CROSS_SOURCE_SEVERE_METERS,
    MIN_MARGIN,
    NAME_ACCEPT_THRESHOLD,
    NAME_MATCH_THRESHOLD,
    REVIEW_THRESHOLD,
    WEIGHTS,
    distance_meters,
    has_acceptance_evidence,
    score_addresses,
)


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


def _rank_scored(ranked, *, truncated=False):
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
        if not has_acceptance_evidence(best["evidence"]):
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
    result = _rank_scored(ranked, truncated=truncated)
    providers = sorted(set(providers or ()) | {c.provider for c in candidates})
    per_source = {}
    for provider in providers:
        limited = provider in truncated_providers if truncated_providers is not None else truncated
        source_rows = [row for row in ranked if row["candidate"].provider == provider]
        decision = _rank_scored(
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
