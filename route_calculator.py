import logging
import re
import time
import requests

log = logging.getLogger(__name__)

ORS_BASE  = "https://api.openrouteservice.org"
OSRM_BASE = "https://router.project-osrm.org"

# Per-call cache — HOME_BASE appears twice per route; geocode it once
_geocode_cache: dict[str, tuple[float, float] | None] = {}
_nominatim_last: float = 0.0

# Two-char US state abbreviation map for normalizing spelled-out state names
_STATE_ABBR = {
    "alabama":"AL","alaska":"AK","arizona":"AZ","arkansas":"AR","california":"CA",
    "colorado":"CO","connecticut":"CT","delaware":"DE","florida":"FL","georgia":"GA",
    "hawaii":"HI","idaho":"ID","illinois":"IL","indiana":"IN","iowa":"IA","kansas":"KS",
    "kentucky":"KY","louisiana":"LA","maine":"ME","maryland":"MD","massachusetts":"MA",
    "michigan":"MI","minnesota":"MN","mississippi":"MS","missouri":"MO","montana":"MT",
    "nebraska":"NE","nevada":"NV","new hampshire":"NH","new jersey":"NJ","new mexico":"NM",
    "new york":"NY","north carolina":"NC","north dakota":"ND","ohio":"OH","oklahoma":"OK",
    "oregon":"OR","pennsylvania":"PA","rhode island":"RI","south carolina":"SC",
    "south dakota":"SD","tennessee":"TN","texas":"TX","utah":"UT","vermont":"VT",
    "virginia":"VA","washington":"WA","west virginia":"WV","wisconsin":"WI","wyoming":"WY",
    "district of columbia":"DC",
}


def _address_variants(address: str) -> list[str]:
    """
    Return progressively simpler geocodable forms of a full street address.
    "100 Continental Dr, Newark, Delaware 19713, US"
      → "Newark, Delaware 19713"
      → "Newark, DE 19713"
      → "Newark, DE"
    Already-simple "City, ST ZIP" addresses pass through with just state normalization.
    """
    parts = [p.strip() for p in address.split(",")]
    # Strip trailing country codes (US, USA)
    while parts and parts[-1].upper() in ("US", "USA", "UNITED STATES"):
        parts.pop()

    variants: list[str] = []

    # Try dropping leading street components one at a time
    for start in range(1, len(parts)):
        candidate = ", ".join(parts[start:]).strip(", ")
        if candidate:
            variants.append(candidate)

    # Normalize any spelled-out state name → abbreviation in each variant
    normalized = []
    for v in variants:
        v_lower = v.lower()
        for full, abbr in _STATE_ABBR.items():
            if full in v_lower:
                v = re.sub(re.escape(full), abbr, v, flags=re.IGNORECASE)
                break
        normalized.append(v)

    # Deduplicate while preserving order
    seen: set[str] = set()
    result: list[str] = []
    for v in normalized:
        if v not in seen and v.lower() != address.lower():
            seen.add(v)
            result.append(v)

    return result


def get_suggestions(address: str, max_results: int = 5) -> list[str]:
    """Return up to max_results geocodable address alternatives for a failed address."""
    global _nominatim_last
    variants = _address_variants(address)

    # Build search candidates: simplified variants first (most reliable)
    candidates: list[str] = list(variants)

    # Strip stray special characters (e.g. "Townshi[ NJ" → "Townshi NJ")
    cleaned = re.sub(r'[^\w\s,.\-]', ' ', address)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    if cleaned.lower() != address.lower() and cleaned not in candidates:
        candidates.insert(0, cleaned)

    # If there are no variants the address has no commas — try to detect a trailing
    # state abbreviation and reformat as "city portion, ST"
    if not variants:
        tokens = address.split()
        _state_values = set(_STATE_ABBR.values())
        if tokens and tokens[-1].upper() in _state_values:
            city_part = ' '.join(tokens[:-1])
            candidates.append(f"{city_part}, {tokens[-1].upper()}")

    if not candidates:
        candidates = [address]

    seen: set[str] = set()
    suggestions: list[str] = []

    for search_query in candidates[:3]:
        wait = 1.1 - (time.time() - _nominatim_last)
        if wait > 0:
            time.sleep(wait)
        try:
            r = requests.get(
                "https://nominatim.openstreetmap.org/search",
                params={
                    "q": search_query, "format": "json", "limit": max_results,
                    "countrycodes": "us", "addressdetails": "1",
                },
                headers={"User-Agent": "GrayTech-TransportAnalyzer/1.0"},
                timeout=12,
            )
            _nominatim_last = time.time()
            r.raise_for_status()
            for res in r.json():
                addr = res.get("address", {})
                city = (
                    addr.get("city") or addr.get("town") or
                    addr.get("village") or addr.get("county", "")
                )
                state = addr.get("state_code") or addr.get("state", "")
                postal = addr.get("postcode", "")
                if not city:
                    continue
                state_abbr = _STATE_ABBR.get(state.lower(), state.upper()[:2])
                label = f"{city}, {state_abbr} {postal}".strip() if postal else f"{city}, {state_abbr}"
                if label not in seen:
                    seen.add(label)
                    suggestions.append(label)
            if suggestions:
                return suggestions[:max_results]
        except Exception as e:
            log.warning("Suggestion fetch failed for '%s': %s", search_query, e)

    return suggestions[:max_results]


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
        return None
    except Exception as e:
        log.warning("Nominatim geocode failed for '%s': %s", address, e)
        return None


def _ors_geocode(address: str, api_key: str) -> tuple[float, float] | None:
    """ORS Pelias geocoder — last resort if Nominatim fails."""
    try:
        r = requests.get(
            f"{ORS_BASE}/geocode/search",
            params={"api_key": api_key, "text": address, "size": 1,
                    "boundary.country": "US"},
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
    """
    Geocode an address to (lon, lat).
    1. Try Nominatim with the full address
    2. If that fails, retry with progressively simplified forms (strip street, normalize state)
    3. Fall back to ORS if key is available
    Results are cached to avoid re-geocoding duplicates within the same route.
    """
    if address in _geocode_cache:
        return _geocode_cache[address]

    # Attempt 1: full address
    coord = _nominatim_geocode(address)

    # Attempt 2: simplified variants (e.g. drop street, normalize state abbreviation)
    if coord is None:
        for variant in _address_variants(address):
            log.info("Retrying geocode for '%s' with simplified form '%s'", address, variant)
            coord = _nominatim_geocode(variant)
            if coord:
                break

    # Attempt 3: ORS as last resort
    if coord is None and api_key:
        coord = _ors_geocode(address, api_key)

    if coord is None:
        log.warning("All geocoding attempts failed for '%s'", address)

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
    data = r.json()
    if data.get("code") != "Ok" or not data.get("routes"):
        raise ValueError(f"OSRM returned no route (code={data.get('code', 'unknown')})")
    route   = data["routes"][0]
    miles   = round(route["distance"] / 1609.344, 1)
    minutes = int(route["duration"] / 60)
    return miles, minutes


def _ors_route(coords: list[list[float]], api_key: str) -> tuple[float, int]:
    """Route via ORS directions API — fallback if OSRM fails."""
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
    Geocode each address (with simplification retry) then calculate driving route.
    Routing: OSRM first (free, no key), ORS as fallback.
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
        unique_failed = list(dict.fromkeys(failed))
        raise ValueError(
            f"Could not locate enough addresses ({len(coords)} of {len(addresses)} resolved). "
            f"Could not locate: {unique_failed}"
        )

    try:
        miles, minutes = _osrm_route(coords)
    except Exception as e:
        log.warning("OSRM routing failed (%s) — trying ORS", e)
        if api_key:
            try:
                miles, minutes = _ors_route(coords, api_key)
            except Exception as e2:
                raise ValueError(
                    f"Route calculation failed. OSRM: {e}  |  ORS: {e2}"
                )
        else:
            raise ValueError(f"Route calculation failed (OSRM): {e}")

    unique_failed = list(dict.fromkeys(failed))
    suggestions: dict[str, list[str]] = {}
    for addr in unique_failed:
        sugg = get_suggestions(addr)
        if sugg:
            suggestions[addr] = sugg

    return {
        "total_miles":   miles,
        "total_minutes": minutes,
        "failed":        failed,
        "suggestions":   suggestions,
    }