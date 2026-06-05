import logging
import requests

log = logging.getLogger(__name__)

ORS_BASE = "https://api.openrouteservice.org"


def geocode_address(address: str, api_key: str) -> tuple[float, float] | None:
    """Return (lon, lat) for an address, or None if not found."""
    try:
        r = requests.get(
            f"{ORS_BASE}/geocode/search",
            params={"api_key": api_key, "text": address, "size": 1},
            timeout=10,
        )
        r.raise_for_status()
        features = r.json().get("features", [])
        if not features:
            log.warning("No geocode result for: %s", address)
            return None
        lon, lat = features[0]["geometry"]["coordinates"]
        return (lon, lat)
    except Exception as e:
        log.warning("Geocode failed for '%s': %s", address, e)
        return None


def calculate_route(addresses: list[str], api_key: str) -> dict:
    """
    Geocode each address in order and calculate the driving route.
    Returns:
        total_miles    float
        total_minutes  int
        failed         list[str]  — addresses that could not be geocoded
    Raises ValueError if fewer than 2 addresses geocoded successfully.
    """
    coords   = []
    failed   = []
    geocoded = []

    for addr in addresses:
        coord = geocode_address(addr, api_key)
        if coord:
            coords.append(list(coord))   # ORS wants [lon, lat]
            geocoded.append(addr)
        else:
            failed.append(addr)

    if len(coords) < 2:
        raise ValueError(
            f"Need at least 2 geocodable addresses (got {len(coords)}).\n"
            f"Could not geocode: {failed}"
        )

    r = requests.post(
        f"{ORS_BASE}/v2/directions/driving-hgv",
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
    r.raise_for_status()

    summary = r.json()["routes"][0]["summary"]
    return {
        "total_miles":   round(summary["distance"], 1),
        "total_minutes": int(summary["duration"] / 60),
        "failed":        failed,
    }
