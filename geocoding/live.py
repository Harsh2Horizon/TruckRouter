"""Optional self-hosted Nominatim retrieval. Public OSMF geocoding is not a bulk backend."""

import time
from urllib.parse import urlparse

import httpx
from django.conf import settings

from .providers import nominatim
from .schemas import CanonicalAddress
from .services import save_place


class NominatimClient:
    def __init__(self, base_url=None, user_agent=None, *, client=None, interval=1.1):
        self.base_url = (base_url or settings.NOMINATIM_BASE_URL).rstrip("/")
        agent = user_agent or settings.GEOCODER_USER_AGENT
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Configure NOMINATIM_BASE_URL for a self-hosted Nominatim instance")
        if parsed.hostname.lower().rstrip(".") == "nominatim.openstreetmap.org":
            raise ValueError("Public OSMF Nominatim is not supported by this bulk pipeline")
        if not agent:
            raise ValueError("Configure an identifying GEOCODER_USER_AGENT")
        self.client = client or httpx.Client(
            timeout=20, headers={"User-Agent": agent}, follow_redirects=False
        )
        self._owns_client = client is None
        self.interval = max(interval, 1.1)
        self.last_request = 0.0
        self.cache = {}

    def search(self, query: CanonicalAddress):
        # Search the business/locality first; highway prose is not a postal street address.
        business = f"{query.brand} {query.store_id}".strip()
        if query.house_number and query.street:
            business += f" {query.house_number} {query.street}"
        terms = ", ".join(v for v in (business, query.city, query.state, query.country) if v)
        if terms in self.cache:
            return self.cache[terms]
        time.sleep(max(0, self.interval - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        response = self.client.get(
            f"{self.base_url}/search",
            params={
                "q": terms,
                "format": "jsonv2",
                "addressdetails": 1,
                "extratags": 1,
                "namedetails": 1,
                "limit": 20,
                **({"countrycodes": query.country.lower()} if query.country else {}),
            },
        )
        response.raise_for_status()
        self.cache[terms] = [nominatim(record) for record in response.json()]
        return self.cache[terms]

    def import_search(self, query: CanonicalAddress):
        return [save_place(candidate) for candidate in self.search(query)]

    def close(self):
        if self._owns_client:
            self.client.close()
