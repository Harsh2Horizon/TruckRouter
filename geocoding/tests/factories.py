"""Shared synthetic test inputs; coordinates do not represent actual stations."""

from geocoding.parsing import parse_address
from geocoding.schemas import Coordinate, PlaceCandidate


def query(**changes):
    return parse_address(
        {
            "name": "KWIK TRIP #796",
            "address": "I-94, EXIT 143 & US-12 & SR-21",
            "city": "Tomah",
            "state": "WI",
            **changes,
        }
    )


def candidate(source_id="one", *, latitude=44.0, longitude=-90.5, **changes):
    # These are deliberately synthetic points, not verified coordinates of store 796.
    return PlaceCandidate(
        provider="test",
        namespace="synthetic",
        source_id=source_id,
        address=query(**changes),
        coordinate=Coordinate(latitude=latitude, longitude=longitude),
        precision="poi",
    )


def save_place(item):
    from geocoding.models import SourcePlace
    from geocoding.services import bulk_save_places

    bulk_save_places([item])
    return SourcePlace.objects.get(
        provider=item.provider, namespace=item.namespace, source_id=item.source_id
    )
