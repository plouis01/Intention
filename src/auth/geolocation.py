"""IP geolocation utility using ip-api.com."""

import logging

import httpx

logger = logging.getLogger(__name__)

_LOCAL_IPS = {"127.0.0.1", "::1", "testclient"}


def resolve_location(ip: str | None) -> str | None:
    """Resolve an IP address to 'City, Country'. Returns None on failure."""
    if not ip or ip in _LOCAL_IPS:
        return None
    try:
        resp = httpx.get(
            f"http://ip-api.com/json/{ip}",
            params={"fields": "city,country,status"},
            timeout=3.0,
        )
        data = resp.json()
        if data.get("status") == "success":
            city = data.get("city", "")
            country = data.get("country", "")
            if city and country:
                return f"{city}, {country}"
            return country or city or None
    except Exception:
        logger.warning("Geolocation lookup failed for %s", ip, exc_info=True)
    return None
