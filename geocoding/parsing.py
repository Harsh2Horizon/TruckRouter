"""Conservative North American highway/address parser; raw input is always retained."""

import re

from .normalization import brand_key, subdivision, text_key
from .schemas import AddressInput, CanonicalAddress, ExitReference, RoadNetwork, RoadReference

ROAD_RE = re.compile(
    r"\b(?P<prefix>INTERSTATE|I|U\.?S\.?|STATE\s+(?:ROUTE|HIGHWAY)|SR|ST|"
    r"COUNTY\s+(?:ROAD|ROUTE)|CR|FM|RM|PR|TCH|HIGHWAY|HWY|LOOP)"
    r"[\s-]*(?P<number>\d+(?:[A-Z](?![A-Z]))?|[A-Z]{1,2}\b)"
    r"(?P<shared>(?:/\d+[A-Z]?)*)(?:\s*(?P<variant>BUS(?:INESS)?|ALT(?:ERNATE)?|"
    r"BYPASS|SPUR|LOOP)\b)?(?:\s+(?P<direction>NORTH|SOUTH|EAST|WEST|NB|SB|EB|WB)\b)?",
    re.I,
)
EXIT_RE = re.compile(r"\bEXIT\s*(?P<identifier>\d+[A-Z]?(?:[/-]\d*[A-Z])?)\b", re.I)


def parse_address(data: AddressInput | dict) -> CanonicalAddress:
    data = AddressInput.model_validate(data)
    state, country = subdivision(data.state, data.country)
    raw_name = data.name.strip()
    brand_part, marker, store_id = raw_name.partition("#")
    # Store identifiers are strings, including leading zeroes and alphabetic suffixes.
    brand = brand_key(brand_part)
    result = CanonicalAddress(
        raw_name=raw_name,
        raw_address=data.address,
        name=text_key(raw_name),
        brand=brand,
        store_id=store_id.strip() if marker else "",
        city=text_key(data.city),
        state=state,
        country=country,
        postcode=data.postcode.upper().replace(" ", ""),
    )

    if not marker:
        store_match = re.search(
            r"\b(?:STORE|LOCATION)\s*(?:NO\.?\s*)?(\d+[A-Z]?)\s*$", raw_name, re.I
        )
        if store_match:
            result.store_id = store_match[1]
            result.brand = brand_key(raw_name[: store_match.start()])
    if not result.country:
        result.warnings.append("country_unknown")
    street = re.match(r"^(\d+[A-Z]?(?:-\d+)?)\s+(.+)$", data.address.strip(), re.I)
    if street:
        result.house_number, result.street = street.groups()

    road_spans = []
    seen = set()
    for match in ROAD_RE.finditer(data.address):
        prefix = re.sub(r"[.\s]", "", match["prefix"].upper())
        network = {
            "I": RoadNetwork.INTERSTATE,
            "INTERSTATE": RoadNetwork.INTERSTATE,
            "US": RoadNetwork.US_HIGHWAY,
            "SR": RoadNetwork.STATE,
            "ST": RoadNetwork.STATE,
            "STATEROUTE": RoadNetwork.STATE,
            "STATEHIGHWAY": RoadNetwork.STATE,
            "CR": RoadNetwork.COUNTY,
            "COUNTYROAD": RoadNetwork.COUNTY,
            "COUNTYROUTE": RoadNetwork.COUNTY,
            "FM": RoadNetwork.FARM,
            "RM": RoadNetwork.FARM,
            "PR": RoadNetwork.PROVINCIAL,
            "TCH": RoadNetwork.TRANS_CANADA,
            "LOOP": RoadNetwork.STATE,
        }.get(prefix, RoadNetwork.PROVINCIAL if country == "CA" else RoadNetwork.UNKNOWN)
        variant = (match["variant"] or ("loop" if prefix == "LOOP" else "main")).lower()
        variant = {"bus": "business", "alt": "alternate"}.get(variant, variant)
        jurisdiction = country
        if network in {
            RoadNetwork.STATE,
            RoadNetwork.COUNTY,
            RoadNetwork.FARM,
            RoadNetwork.PROVINCIAL,
            RoadNetwork.UNKNOWN,
        }:
            jurisdiction = f"{country}-{state}" if country and state else state or country
        directions = {
            "NORTH": "N",
            "SOUTH": "S",
            "EAST": "E",
            "WEST": "W",
            "NB": "N",
            "SB": "S",
            "EB": "E",
            "WB": "W",
        }
        roads = []
        for number in [match["number"], *match["shared"].strip("/").split("/")]:
            if not number:
                continue
            road = RoadReference(
                network=network,
                number=number.upper(),
                jurisdiction=jurisdiction,
                variant=variant,
                direction=directions.get((match["direction"] or "").upper(), ""),
                raw=match[0],
            )
            if road.key not in seen:
                result.highways.append(road)
                seen.add(road.key)
            roads.append(road)
        road_spans.append((match.start(), match.end(), roads))

    for match in EXIT_RE.finditer(data.address):
        preceding = [span for span in road_spans if span[1] <= match.start()]
        # Shared routes (I-80/94) do not establish a unique exit numbering system.
        linked = preceding[-1][2] if preceding else []
        result.exits.append(
            ExitReference(
                identifier=match["identifier"].upper(),
                highway_key=linked[0].key if len(linked) == 1 else None,
                association="inferred_preceding_route" if len(linked) == 1 else "unknown",
                raw=match[0],
            )
        )

    if road_spans or result.exits:
        residual = EXIT_RE.sub(" ", ROAD_RE.sub(" ", data.address))
        result.unparsed_address = re.sub(r"[\s,&/]+", " ", residual).strip()
        if result.unparsed_address:
            result.warnings.append("address_contains_unparsed_context")
        if result.exits:
            result.warnings.append("exit_associations_require_road_network_verification")
    else:
        if not street:
            result.street = data.address.strip()
    return result
