import logging
import time
import requests

log = logging.getLogger(__name__)

ORS_BASE = "https://api.openrouteservice.org"

# Simple in-call cache so HOME_BASE (which appears twice) is only geocoded once
_geocode_cache: dict[str, tuple[float, float] | None] = {}
_nominatim_last: float = 0.0


def _ors_geocode(address: str, api_key: str) -> tuple[float, float] | None:
    """Try ORS Pelias geocoder. Returns (lon, lat) or None."""
    try:
        r = requests.get(
            f"{ORS_BASE}/geocode/search",
            params={
                "api_key": api_key,
                "text": address,
                "size": 1,
                "boundary.country": "US",
            },
            timeout=10,
        )
        r.raise_for_status()
        features = r.json().get("features", [])
        if features:
            lon, lat = features[0]["geometry"]["coordinates"]
            return (lon, lat)
        return None
    except Exception as e:
        log.warning("ORS geocode failed for '%s': %s", address, e)
        return None


def _nominatim_geocode(address: str) -> tuple[float, float] | None:
    """Fallback geocoder using Nominatim / OpenStreetMap (no API key needed)."""
    global _nominatim_last
    # Respect Nominatim's 1 req/sec policy
    wait = 1.1 - (time.time() - _nominatim_last)
    if wait > 0:
        time.sleep(wait)
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": address, "format": "json", "limit": 1, "countrycodes": "us"},
            headers={"User-Agent": "GrayTech-TransportAnalyzer/1.0"},
            timeout=12,
        )
        _nominatim_last = time.time()
        r.raise_for_status()
        results = r.json()
        if results:
            return (float(results[0]["lon"]), float(results[0]["lat"]))
        log.warning("Nominatim: no result for '%s'", address)
        return None
    except Exception as e:
        log.warning("Nominatim geocode failed for '%s': %s", address, e)
        return None


def geocode_address(address: str, api_key: str) -> tuple[float, float] | None:
    """
    Geocode an address to (lon, lat).
    Tries ORS Pelias first; falls back to Nominatim if ORS returns nothing.
    Results are cached within the process to avoid re-geocoding duplicates.
    """
    if address in _geocode_cache:
        return _geocode_cache[address]

    coord = None
    if api_key:
        coord = _ors_geocode(address, api_key)

    if coord is None:
        log.info("ORS returned no result for '%s' — trying Nominatim", address)
        coord = _nominatim_geocode(address)

    _geocode_cache[address] = coord
    return coord


def calculate_route(addresses: list[str], api_key: str) -> dict:
    """
    Geocode each address and calculate the driving route via ORS.
    Returns: { total_miles, total_minutes, failed }
    Raises ValueError if fewer than 2 addresses can be geocoded.
    """
    # Clear per-call cache (keeps duplicate addresses fast within one request)
    _geocode_cache.clear()

    coords: list[list[float]] = []
    failed: list[str]         = []

    for addr in addresses:
        coord = geocode_address(addr, api_key)
        if coord:
            coords.append(list(coord))
        else:
            failed.append(addr)

    if len(coords) < 2:
        raise ValueError(
            f"Could not locate enough addresses to build a route "
            f"({len(coords)} of {len(addresses)} resolved). "
            f"Could not locate: {list(dict.fromkeys(failed))}"
        )

    r = requests.post(
        f"{ORS_BASE}/v2/directions/driving-car",
        headers={
            "Authorization": api_key,
            "Content-Type":  "application/json",
        },
        json={
            "coordinates": coords,
            "units":        "mi",
            "instructions": False,
        },
        timeout=30,
    )
    try:
        r.raise_for_status()
    except Exception:
        try:
            detail = r.json().get("error", {}).get("message", r.text[:200])
        except Exception:
            detail = r.text[:200]
        raise ValueError(f"ORS routing failed: {detail}")

    summary = r.json()["routes"][0]["summary"]
    return {
        "total_miles":   round(summary["distance"], 1),
        "total_minutes": int(summary["duration"] / 60),
        "failed":        failed,
    }