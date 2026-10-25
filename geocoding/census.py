"""Optional cached Census address-range geocoding; no POI/brand claim."""

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlencode

from .acquisition import DownloadClient, write_json
from .normalization import city_key, locality_codes
from .schemas import Coordinate
from .scoring import city_similarity


def address_key(query):
    return hashlib.sha256(
        json.dumps(
            [query.country, query.state, city_key(query.city), query.raw_address.upper().strip()]
        ).encode()
    ).hexdigest()


def eligible_address(query):
    return bool(
        query.country == "US"
        and query.house_number
        and query.street
        and not query.highways
        and not re.search(r"\b(?:MILES?|EXIT|MM)\b", query.raw_address, re.I)
    )


def fetch_addresses(queries, directory="data/reference/coverage"):
    directory = Path(directory)
    client = DownloadClient(directory / "census-downloads")
    index_path = directory / "census-addresses.json"
    index = json.loads(index_path.read_text()) if index_path.exists() else {}
    unique = {address_key(query): query for query in queries if eligible_address(query)}

    def fetch(item):
        key, query = item
        params = {
            "street": query.raw_address,
            "city": query.city,
            "state": query.state,
            "benchmark": "Public_AR_Current",
            "format": "json",
        }
        url = "https://geocoding.geo.census.gov/geocoder/locations/address?" + urlencode(params)
        try:
            filename = client.fetch(url, timeout=25)
            response = json.loads(filename.read_text())
            result = {"status": "not_matched", "url": url, "response_file": str(filename)}
            for match in response.get("result", {}).get("addressMatches", []):
                parts = match.get("addressComponents") or {}
                state, country = locality_codes(parts.get("state", ""), "US")
                city = parts.get("city", "")
                matched = match.get("matchedAddress", "")
                if (
                    state != query.state
                    or country != query.country
                    or city_similarity(query.city, city) < 0.90
                    or matched.split()[0].upper() != query.house_number.upper()
                ):
                    continue
                point = Coordinate(
                    latitude=match["coordinates"]["y"], longitude=match["coordinates"]["x"]
                )
                result.update(
                    status="matched",
                    coordinate=point.model_dump(),
                    matched_address=matched,
                    city=city,
                    state=state,
                    country=country,
                    benchmark=response["result"].get("input", {}).get("benchmark"),
                    tiger_line=match.get("tigerLine"),
                )
                break
            return key, result
        except Exception as exc:
            return key, {"status": "error", "url": url, "error": str(exc)}

    with ThreadPoolExecutor(max_workers=2) as pool:
        index.update(dict(pool.map(fetch, unique.items())))
    write_json(index_path, index)
    return {
        "queries": len(unique),
        "matched": sum(index[key]["status"] == "matched" for key in unique),
        "errors": sum(index[key]["status"] == "error" for key in unique),
    }
