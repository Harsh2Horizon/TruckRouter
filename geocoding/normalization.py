import re
import unicodedata
from functools import lru_cache

NEAREST_CITY_WARNING = "city_from_nearest_settlement_requires_review"
BOUNDARY_CITY_WARNING = "city_from_spatial_boundary"

# Explicit aliases only: Kwik Trip and QuikTrip are different brands.
BRAND_ALIASES = {
    "PILOT TRAVEL CENTER": "PILOT",
    "PILOT TRAVEL CENTERS": "PILOT",
    "PETRO STOPPING CENTER": "PETRO",
    "PETRO STOPPING CENTERS": "PETRO",
    "LOVES TRAVEL STOP": "LOVES",
    "LOVES TRAVEL STOPS": "LOVES",
    "TA TRAVEL CENTER": "TA",
    "TRAVELCENTERS OF AMERICA": "TA",
    "PILOT DEALER": "PILOT",
    "FLYING J TRAVEL PLAZA": "FLYING J",
    "FLYING J FUEL STOP": "FLYING J",
    "FLYING J CARDLOCK": "FLYING J",
    "CASEYS GENERAL STORE": "CASEYS",
    "MAVERIK COUNTRY STORE": "MAVERIK",
    "ALLSUPS CONVENIENCE STORE": "ALLSUPS",
    "QUIK TRIP": "QUIKTRIP",
    "KWIKTRIP": "KWIK TRIP",
}

SUBDIVISIONS = {
    "US": dict(
        zip(
            "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO "
            "MT NE NV "
            "NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split(),
            "Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|"
            "District of Columbia|Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|"
            "Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|Mississippi|"
            "Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|"
            "North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|"
            "South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|"
            "West Virginia|Wisconsin|Wyoming".split("|"),
            strict=True,
        )
    ),
    "CA": dict(
        zip(
            "AB BC MB NB NL NS NT NU ON PE QC SK YT".split(),
            "Alberta|British Columbia|Manitoba|New Brunswick|Newfoundland and Labrador|Nova Scotia|"
            "Northwest Territories|Nunavut|Ontario|Prince Edward Island|"
            "Quebec|Saskatchewan|Yukon".split("|"),
            strict=True,
        )
    ),
}


def text_key(value: str) -> str:
    value = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Z0-9]+", " ", value.upper()).strip()


def brand_key(value: str) -> str:
    key = text_key(value.replace("'", "").replace("’", ""))
    return BRAND_ALIASES.get(key, key)


def country_code(value: str) -> str:
    key = text_key(value)
    return {
        "US": "US",
        "USA": "US",
        "U S": "US",
        "U S A": "US",
        "UNITED STATES": "US",
        "UNITED STATES OF AMERICA": "US",
        "CA": "CA",
        "CAN": "CA",
        "CANADA": "CA",
    }.get(key, value.upper().strip())


def subdivision(state: str, country: str = "") -> tuple[str, str]:
    country = country_code(country)
    state = state.upper().strip()
    if re.match(r"^(US|CA)-", state):
        country, state = state.split("-", 1)
    for nation, regions in SUBDIVISIONS.items():
        if country and nation != country:
            continue
        for code, name in regions.items():
            state_key = text_key(state).replace(" ", "")
            if state_key in (code, text_key(name).replace(" ", "")):
                return code, country or nation
    return state, country


@lru_cache(maxsize=100000)
def city_key(value: str) -> str:
    aliases = {
        "ST": "SAINT",
        "STE": "SAINTE",
        "FT": "FORT",
        "MT": "MOUNT",
        "N": "NORTH",
        "S": "SOUTH",
        "E": "EAST",
        "W": "WEST",
        "NE": "NORTHEAST",
        "NW": "NORTHWEST",
        "SE": "SOUTHEAST",
        "SW": "SOUTHWEST",
    }
    result = " ".join(aliases.get(token, token) for token in text_key(value).split())
    return {
        "NYC": "NEW YORK",
        "N Y C": "NEW YORK",
        "NEW YORK CITY": "NEW YORK",
        "QUEBEC CITY": "QUEBEC",
        "STLOUIS": "SAINT LOUIS",
        "INDIANAPOLIS CITY BALANCE": "INDIANAPOLIS",
        "MILFORD CITY BALANCE": "MILFORD",
        "NASHVILLE DAVIDSON METROPOLITAN GOVERNMENT BALANCE": "NASHVILLE",
        "LOUISVILLE JEFFERSON COUNTY METRO GOVERNMENT BALANCE": "LOUISVILLE",
        "ATHENS CLARKE COUNTY UNIFIED GOVERNMENT BALANCE": "ATHENS",
        "AUGUSTA RICHMOND COUNTY CONSOLIDATED GOVERNMENT BALANCE": "AUGUSTA",
        "BUTTE SILVER BOW BALANCE": "BUTTE",
        "LEXINGTON FAYETTE": "LEXINGTON",
        "LEXINGTON FAYETTE URBAN COUNTY": "LEXINGTON",
        "MACON BIBB COUNTY": "MACON",
    }.get(result, result)


def locality_codes(state: str, country: str) -> tuple[str, str]:
    country = country_code(country)
    state_aliases = {
        "CALIF": "CA",
        "TEX": "TX",
        "PENN": "PA",
        "PENNA": "PA",
        "WISC": "WI",
        "ONT": "ON",
        "QUE": "QC",
        "SASK": "SK",
        "ALTA": "AB",
    }
    state = state_aliases.get(text_key(state), state)
    return subdivision(state, country)


def store_key(value: str) -> str:
    # Internal punctuation can be meaningful in alphanumeric branch identifiers.
    key = re.sub(r"\s+", "", value.upper().strip().lstrip("#"))
    return str(int(key)) if key.isdigit() else key


def street_key(value: str) -> str:
    words = text_key(value).split()
    aliases = {
        "STREET": "ST",
        "ROAD": "RD",
        "AVENUE": "AVE",
        "DRIVE": "DR",
        "BOULEVARD": "BLVD",
        "HIGHWAY": "HWY",
        "NORTH": "N",
        "SOUTH": "S",
        "EAST": "E",
        "WEST": "W",
    }
    return " ".join(aliases.get(word, word) for word in words)
