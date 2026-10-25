from django.db import transaction
from django.db.models import Case, IntegerField, Q, Value, When

from .matching import (
    ALGORITHM_VERSION,
    business_name,
    city_similarity,
    identity_key,
    input_addresses_disagree,
    rank_candidates,
    similarity,
    update_highest_match,
)
from .models import FuelLocation, GeocodingRun, MatchCandidate, SourcePlace
from .normalization import brand_key, city_key, locality_codes, store_key
from .schemas import CanonicalAddress, Coordinate, PlaceCandidate


def place_values(candidate: PlaceCandidate) -> dict:
    address = candidate.address
    return {
        "release": candidate.release,
        "name_key": address.name,
        "brand_key": address.brand,
        "store_id": store_key(address.store_id),
        "country": address.country,
        "state": address.state,
        "city_key": address.city,
        "address": address.model_dump(mode="json"),
        "latitude": candidate.coordinate.latitude if candidate.coordinate else None,
        "longitude": candidate.coordinate.longitude if candidate.coordinate else None,
        "precision": candidate.precision,
        "operating_status": candidate.operating_status,
        "existence_confidence": candidate.existence_confidence,
        "provenance": candidate.provenance,
        "license": candidate.license,
        "raw": candidate.raw,
    }


def bulk_save_places(candidates: list[PlaceCandidate]) -> int:
    records = {}
    for candidate in candidates:
        identity = (candidate.provider, candidate.namespace, candidate.source_id)
        records[identity] = SourcePlace(
            provider=candidate.provider,
            namespace=candidate.namespace,
            source_id=candidate.source_id,
            **place_values(candidate),
        )
    if records:
        SourcePlace.objects.bulk_create(
            list(records.values()),
            batch_size=500,
            update_conflicts=True,
            unique_fields=["provider", "namespace", "source_id"],
            update_fields=[*place_values(candidates[0]), "imported_at"],
        )
    return len(candidates)


def save_place(candidate: PlaceCandidate) -> SourcePlace:
    place, _ = SourcePlace.objects.update_or_create(
        provider=candidate.provider,
        namespace=candidate.namespace,
        source_id=candidate.source_id,
        defaults=place_values(candidate),
    )
    return place


def as_candidate(place: SourcePlace) -> PlaceCandidate:
    return PlaceCandidate(
        provider=place.provider,
        namespace=place.namespace,
        source_id=place.source_id,
        release=place.release,
        address=CanonicalAddress.model_validate(place.address),
        coordinate=(
            Coordinate(latitude=place.latitude, longitude=place.longitude)
            if place.latitude is not None and place.longitude is not None
            else None
        ),
        precision=place.precision,
        operating_status=place.operating_status,
        existence_confidence=place.existence_confidence,
        provenance=place.provenance,
        license=place.license,
        raw=place.raw,
    )


def retrieve_candidates(query: CanonicalAddress, limit=500, *, source_scope=None, key_cache=None):
    records = SourcePlace.objects.all()
    if source_scope is not None:
        scope = Q(pk__in=[])
        for provider, release in source_scope.items():
            scope |= Q(provider=provider, release=release)
        records = records.filter(scope)
    state, country = locality_codes(query.state, query.country)
    scope_key = (
        tuple(sorted(source_scope.items())) if source_scope is not None else None,
        country,
        state,
    )

    def distinct_keys(key, queryset):
        # A batch owns this cache while its imported source releases are immutable.
        # Ordinary API calls do not share it, so provider updates remain visible.
        if key_cache is None:
            return list(queryset)
        if key not in key_cache:
            key_cache[key] = list(queryset)
        return key_cache[key]

    for key, value in (("country", country), ("state", state)):
        if value:
            records = records.filter(Q(**{key: value}) | Q(**{key: ""}))
    # Compare distinct text keys first, then fetch only eligible records and raw payloads.
    # This preserves the fuzzy rules without scanning every JSON payload for each input.
    exact_cities = []
    if query.city:
        cities = distinct_keys(
            ("cities", scope_key), records.order_by().values_list("city_key", flat=True).distinct()
        )
        eligible_cities = [
            city for city in cities if not city or city_similarity(query.city, city) >= 0.90
        ]
        exact_cities = [city for city in eligible_cities if city_key(city) == city_key(query.city)]
        city_block = Q(city_key__in=eligible_cities)
        records = records.filter(city_block)
    if query.brand or query.raw_name:
        keys = distinct_keys(
            ("names", scope_key, city_key(query.city)),
            records.order_by().values_list("brand_key", "name_key").distinct(),
        )
        terms = [identity_key(query.brand), identity_key(business_name(query))]
        eligible_brands, eligible_names = set(), set()
        for brand, name in keys:
            if any(similarity(term, identity_key(brand)) >= 0.5 for term in terms if term):
                eligible_brands.add(brand)
            if any(similarity(term, identity_key(name)) >= 0.5 for term in terms if term):
                eligible_names.add(name)
        records = records.filter(Q(brand_key__in=eligible_brands) | Q(name_key__in=eligible_names))
    priorities = {
        "_city_priority": Case(
            When(city_key__in=exact_cities, then=Value(0)),
            When(city_key="", then=Value(2)),
            default=Value(1),
            output_field=IntegerField(),
        )
        if query.city
        else Value(1),
        "_brand_priority": Case(
            When(brand_key=brand_key(query.brand), then=Value(0)),
            default=Value(1),
            output_field=IntegerField(),
        )
        if query.brand
        else Value(1),
    }
    providers = (
        sorted(source_scope)
        if source_scope is not None
        else list(records.order_by().values_list("provider", flat=True).distinct())
    )
    found, truncated = [], set()
    for provider in providers:
        subset = list(
            records.filter(provider=provider)
            .annotate(**priorities)
            .order_by("_city_priority", "_brand_priority", "pk")[: limit + 1]
        )
        found.extend(subset[:limit])
        if len(subset) > limit:
            truncated.add(provider)
    return found, truncated


@transaction.atomic
def resolve(
    query: CanonicalAddress,
    location: FuelLocation | None = None,
    *,
    source_scope=None,
    acquisition_incomplete=False,
    candidate_key_cache=None,
) -> GeocodingRun:
    records, truncated = retrieve_candidates(
        query, source_scope=source_scope, key_cache=candidate_key_cache
    )
    candidates = [as_candidate(record) for record in records]
    result = rank_candidates(
        query,
        candidates,
        truncated=bool(truncated),
        providers=source_scope,
        truncated_providers=truncated,
    )
    result["candidate_retrieval"] = {
        "limit_per_source": 500,
        "truncated_sources": sorted(truncated),
        "counts": {
            provider: sum(c.provider == provider for c in candidates)
            for provider in result["per_source"]
        },
    }
    input_conflict = bool(location and location.warnings)
    if input_conflict and location.observations.exists():
        from .parsing import parse_address

        input_conflict = any(
            input_addresses_disagree(
                query,
                parse_address(
                    {
                        "name": raw["Truckstop Name"],
                        "address": raw["Address"],
                        "city": raw["City"],
                        "state": raw["State"],
                    }
                ),
            )
            for raw in location.observations.values_list("raw", flat=True)
        )
    if source_scope is not None:
        result["source_scope"] = source_scope
    if acquisition_incomplete:
        result["reasons"].append("source_acquisition_incomplete")
        if result["status"] == "accepted":
            result.update(status="needs_review", coordinate=None, selected_source=None)
    if input_conflict:
        result["reasons"].append("input_records_disagree")
        result["status"] = "needs_review"
        result["coordinate"] = None
        result["selected_source"] = None
        for entry in result["per_source"].values():
            entry["reasons"].append("input_records_disagree")
            if entry["status"] == "accepted":
                entry.update(status="needs_review", coordinate=None, selected_source=None)
        update_highest_match(result)
    from .geography import apply_geographic_fallback

    apply_geographic_fallback(result, query, input_conflict=input_conflict)
    run = GeocodingRun.objects.create(
        location=location,
        query=query.model_dump(mode="json"),
        algorithm_version=ALGORITHM_VERSION,
        status=result["status"],
        result=result,
    )
    snapshots = {
        (c.provider, c.namespace, c.source_id): c.model_dump(mode="json") for c in candidates
    }
    by_identity = {(p.provider, p.namespace, p.source_id): p for p in records}
    MatchCandidate.objects.bulk_create(
        [
            MatchCandidate(
                run=run,
                source_place=by_identity[
                    (
                        row["source"]["provider"],
                        row["source"]["namespace"],
                        row["source"]["source_id"],
                    )
                ],
                score=row["evidence"]["score"],
                evidence=row["evidence"],
                snapshot=snapshots[
                    (
                        row["source"]["provider"],
                        row["source"]["namespace"],
                        row["source"]["source_id"],
                    )
                ],
            )
            for row in result["candidates"]
        ]
    )
    if location:
        location.resolution_status = result["status"]
        location.resolved_coordinate = result["coordinate"]
        location.save(update_fields=["resolution_status", "resolved_coordinate", "updated_at"])
    return run
