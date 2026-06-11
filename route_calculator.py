import logging
import time
import requests

log = logging.getLogger(__name__)

ORS_BASE  = "https://api.openrouteservice.org"
OSRM_BASE = "https://router.project-osrm.org"

# Per-call geocode cache — prevents re-geocoding HOME_BASE twice per request
_geocode_cache: dict[str, tuple[float, float] | None] = {}
_nominatim_last: float = 0.0


# ── Geocoding ─────────────────────────────────────────────────────────────────

def _nominatim_geocode(address: str) -> tuple[float, float] | None:
    """Geocode via Nominatim/OpenStreetMap. Free, no API key needed."""
    global _nominatim_last
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


def _ors_geocode(address: str, api_key: str) -> tuple[float, float] | None:
    """Try ORS Pelias geocoder as secondary option."""
    try:
        r = requests.get(
            f"{ORS_BASE}/geocode/search",
            params={"api_key": api_key, "text": address, "size": 1, "boundary.country": "US"},
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


def geocode_address(address: str, api_key: str) -> tuple[float, float] | None:
    """Geocode an address. Uses Nominatim first; falls back to ORS if key is available."""
    if address in _geocode_cache:
        return _geocode_cache[address]

    coord = _nominatim_geocode(address)
    if coord is None and api_key:
        log.info("Nominatim returned nothing for '%s' — trying ORS", address)
        coord = _ors_geocode(address, api_key)

    _geocode_cache[address] = coord
    return coord


# ── Routing ───────────────────────────────────────────────────────────────────

def _osrm_route(coords: list[list[float]]) -> tuple[float, int]:
    """Route via OSRM public server. Free, no API key. Returns (miles, minutes)."""
    coord_str = ";".join(f"{c[0]},{c[1]}" for c in coords)
    r = requests.get(
        f"{OSRM_BASE}/route/v1/driving/{coord_str}",
        params={"overview": "false", "annotations": "false"},
        timeout=30,
    )
    r.raise_for_status()
    route = r.json()["routes"][0]
    miles   = round(route["distance"] / 1609.344, 1)
    minutes = int(route["duration"] / 60)
    return miles, minutes


def _ors_route(coords: list[list[float]], api_key: str) -> tuple[float, int]:
    """Route via ORS directions API. Returns (miles, minutes) or raises."""
    r = requests.post(
        f"{ORS_BASE}/v2/directions/driving-car",
        headers={"Authorization": api_key, "Content-Type": "application/json"},
        json={"coordinates": coords, "units": "mi", "instructions": False},
        timeout=30,
    )
    try:
        r.raise_for_status()
    except Exception:
        try:
            detail = r.json().get("error", {}).get("message", r.text[:200])
        except Exception:
            detail = r.text[:200]
        raise ValueError(f"ORS routing failed ({r.status_code}): {detail}")
    summary = r.json()["routes"][0]["summary"]
    return round(summary["distance"], 1), int(summary["duration"] / 60)


# ── Main entry point ──────────────────────────────────────────────────────────

def calculate_route(addresses: list[str], api_key: str) -> dict:
    """
    Geocode addresses with Nominatim, then calculate driving route via OSRM
    (with ORS as fallback for routing if key is present and OSRM fails).
    Returns: { total_miles, total_minutes, failed }
    """
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
            f"({len(coords)} of {len(addresses)} found). "
            f"Could not locate: {list(dict.fromkeys(failed))}"
        )

    # Try OSRM first (free, no key), then fall back to ORS
    miles, minutes = None, None
    try:
        miles, minutes = _osrm_route(coords)
    except Exception as e:
        log.warning("OSRM routing failed (%s) — trying ORS", e)
        if api_key:
            try:
                miles, minutes = _ors_route(coords, api_key)
            except Exception as e2:
                raise ValueError(f"Both OSRM and ORS routing failed. OSRM: {e}. ORS: {e2}")
        else:
            raise ValueError(f"OSRM routing failed and no ORS key is set: {e}")

    return {
        "total_miles":   miles,
        "total_minutes": minutes,
        "failed":        failed,
    }