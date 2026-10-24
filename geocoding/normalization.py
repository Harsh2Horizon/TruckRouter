import re
import unicodedata

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
    country = country.upper()
    state = state.upper().strip()
    if re.match(r"^(US|CA)-", state):
        country, state = state.split("-", 1)
    for nation, regions in SUBDIVISIONS.items():
        if country and nation != country:
            continue
        for code, name in regions.items():
            if text_key(state) in (code, text_key(name)):
                return code, country or nation
    return state, country


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
