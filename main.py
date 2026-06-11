import asyncio
import csv
import io
import json
import logging
import os
import tempfile
from typing import AsyncGenerator

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

load_dotenv()
ORS_API_KEY = os.getenv("ORS_API_KEY", "")

from calculator import calculate_all, minutes_to_hhmm, hhmm_to_minutes
from database import (
    init_db, get_all_trips, add_trip, update_trip, delete_trip,
    get_trip_stops, save_trip_stops, conf_exists,
    create_group, get_all_groups, get_group, rename_group, delete_group,
    set_trip_group, get_group_trips,
)
from html_parser import parse_report
from route_calculator import calculate_route

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

HOME_BASE = "100 Continental Dr, Newark, Delaware 19713, US"

app = FastAPI(title="Transport Analyzer API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

init_db()


# ── Models ────────────────────────────────────────────────────────────────────

class TripIn(BaseModel):
    confirmation_number: str
    bus_number: str = ""
    start_date: str
    end_date: str
    grand_total: float
    days: int
    miles: float = 0.0
    drive_minutes: int = 0
    status: str = "active"
    is_multiday: int = 0

class StopIn(BaseModel):
    stop_type: str
    address: str
    lat: float | None = None
    lon: float | None = None

class DeleteBody(BaseModel):
    ids: list[int]

class GroupIn(BaseModel):
    name: str

class GroupAssignment(BaseModel):
    trip_ids: list[int]   # ordered — index+1 becomes leg_order

class GroupRouteBody(BaseModel):
    use_home_base: bool = True


# ── Trips ─────────────────────────────────────────────────────────────────────

@app.get("/trips")
def list_trips():
    raw = get_all_trips()
    return [calculate_all(t) for t in raw]


@app.post("/trips", status_code=201)
def create_trip(body: TripIn):
    if conf_exists(body.confirmation_number):
        raise HTTPException(409, f"Confirmation #{body.confirmation_number} already exists")
    tid = add_trip(
        body.confirmation_number, body.bus_number,
        body.start_date, body.end_date, body.grand_total,
        body.days, body.miles, body.drive_minutes,
        body.status, body.is_multiday,
    )
    return {"id": tid}


@app.put("/trips/{trip_id}")
def edit_trip(trip_id: int, body: TripIn):
    update_trip(
        trip_id, body.confirmation_number, body.bus_number,
        body.start_date, body.end_date, body.grand_total,
        body.days, body.miles, body.drive_minutes,
        body.status, body.is_multiday,
    )
    return {"ok": True}


@app.delete("/trips")
def remove_trips(body: DeleteBody):
    for tid in body.ids:
        delete_trip(tid)
    return {"deleted": len(body.ids)}


# ── Stops ─────────────────────────────────────────────────────────────────────

@app.get("/trips/{trip_id}/stops")
def get_stops(trip_id: int):
    return get_trip_stops(trip_id)


@app.put("/trips/{trip_id}/stops")
def set_stops(trip_id: int, stops: list[StopIn]):
    save_trip_stops(trip_id, [s.model_dump() for s in stops])
    return {"ok": True}


# ── Route calculation ─────────────────────────────────────────────────────────

class RouteBody(BaseModel):
    use_home_base: bool = True

@app.post("/trips/{trip_id}/route")
def calc_trip_route(trip_id: int, body: RouteBody):
    if not ORS_API_KEY:
        raise HTTPException(500, "ORS_API_KEY not configured")
    stops = get_trip_stops(trip_id)
    middle = [
        s["address"] for s in stops
        if s.get("address", "").strip()
        and s["address"].strip().lower() != HOME_BASE.lower()
    ]
    if not middle:
        raise HTTPException(400, "No stops defined for this trip")
    addresses = ([HOME_BASE] + middle + [HOME_BASE]) if body.use_home_base else middle
    try:
        result = calculate_route(addresses, ORS_API_KEY)
    except ValueError as e:
        raise HTTPException(400, str(e))

    # Fetch current trip to preserve all fields
    trips = get_all_trips()
    trip = next((t for t in trips if t["id"] == trip_id), None)
    if not trip:
        raise HTTPException(404, "Trip not found")
    update_trip(
        trip_id, trip["confirmation_number"], trip["bus_number"],
        trip["start_date"], trip["end_date"], trip["grand_total"],
        trip["days"], result["total_miles"], result["total_minutes"],
        trip["status"], trip["is_multiday"],
    )
    return {
        "miles": result["total_miles"],
        "drive_minutes": result["total_minutes"],
        "failed": result.get("failed", []),
    }


@app.get("/trips/route/bulk")
async def bulk_route_sse():
    """SSE stream — calculates routes for all active trips, emits one event per trip."""
    if not ORS_API_KEY:
        raise HTTPException(500, "ORS_API_KEY not configured")

    async def generate() -> AsyncGenerator[str, None]:
        trips = [t for t in get_all_trips() if t.get("status") != "cancelled"]

        for trip in trips:
            stops = get_trip_stops(trip["id"])
            middle = [
                s["address"] for s in stops
                if s.get("address", "").strip()
                and s["address"].strip().lower() != HOME_BASE.lower()
            ]
            if not middle:
                payload = json.dumps({
                    "conf": trip["confirmation_number"],
                    "status": "skipped",
                    "message": "No stops defined",
                })
                yield f"data: {payload}\n\n"
                await asyncio.sleep(0)
                continue

            addresses = [HOME_BASE] + middle + [HOME_BASE]
            try:
                result = await asyncio.get_event_loop().run_in_executor(
                    None, calculate_route, addresses, ORS_API_KEY
                )
                update_trip(
                    trip["id"], trip["confirmation_number"], trip["bus_number"],
                    trip["start_date"], trip["end_date"], trip["grand_total"],
                    trip["days"], result["total_miles"], result["total_minutes"],
                    trip["status"], trip["is_multiday"],
                )
                payload = json.dumps({
                    "conf": trip["confirmation_number"],
                    "status": "ok",
                    "miles": result["total_miles"],
                    "drive_minutes": result["total_minutes"],
                    "failed": result.get("failed", []),
                })
            except Exception as e:
                payload = json.dumps({
                    "conf": trip["confirmation_number"],
                    "status": "error",
                    "message": str(e),
                })
            yield f"data: {payload}\n\n"
            await asyncio.sleep(0)

        yield "data: {\"status\":\"done\"}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


# ── Stateless route calculation (for dialog pre-save) ────────────────────────

class RouteCalcBody(BaseModel):
    addresses: list[str]

@app.post("/calculate-route")
def calc_route_stateless(body: RouteCalcBody):
    if not ORS_API_KEY:
        raise HTTPException(500, "ORS_API_KEY not configured")
    if len(body.addresses) < 2:
        raise HTTPException(400, "Need at least 2 addresses")
    try:
        result = calculate_route(body.addresses, ORS_API_KEY)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return result


# ── ORS diagnostics ──────────────────────────────────────────────────────────

@app.get("/debug/ors")
def debug_ors():
    """Test ORS API key and geocoding from this server. Useful for diagnosing Render env issues."""
    test_addr = "Newark, DE 19713"
    result = {"ors_key_set": bool(ORS_API_KEY)}

    # Test ORS geocode
    try:
        r = requests.get(
            "https://api.openrouteservice.org/geocode/search",
            params={"api_key": ORS_API_KEY, "text": test_addr, "size": 1},
            timeout=10,
        )
        result["ors_geocode_status"] = r.status_code
        result["ors_geocode_ok"] = r.status_code == 200 and bool(r.json().get("features"))
        if not result["ors_geocode_ok"]:
            result["ors_geocode_body"] = r.text[:300]
    except Exception as e:
        result["ors_geocode_error"] = str(e)

    # Test Nominatim geocode (fallback)
    try:
        rn = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": test_addr, "format": "json", "limit": 1, "countrycodes": "us"},
            headers={"User-Agent": "GrayTech-TransportAnalyzer/1.0"},
            timeout=10,
        )
        result["nominatim_ok"] = rn.status_code == 200 and bool(rn.json())
    except Exception as e:
        result["nominatim_error"] = str(e)

    return result


# ── Address suggestions ───────────────────────────────────────────────────────

@app.get("/geocode/suggest")
def geocode_suggest(text: str = ""):
    """Return up to 5 address suggestions via ORS (preferred) or Nominatim (fallback)."""
    if len(text.strip()) < 2:
        return {"suggestions": []}

    # Try ORS first if key is available
    if ORS_API_KEY:
        try:
            r = requests.get(
                "https://api.openrouteservice.org/geocode/search",
                params={"api_key": ORS_API_KEY, "text": text, "size": 6},
                timeout=8,
            )
            r.raise_for_status()
            features = r.json().get("features", [])
            suggestions, seen = [], set()
            for f in features:
                props    = f.get("properties", {})
                name     = props.get("name", "").strip()
                region_a = props.get("region_a", "").strip()
                postal   = props.get("postalcode", "").strip()
                if not name:
                    continue
                label = f"{name}, {region_a} {postal}".strip(", ") if region_a else props.get("label", name)
                if region_a and postal:
                    label = f"{name}, {region_a} {postal}"
                elif region_a:
                    label = f"{name}, {region_a}"
                if label not in seen:
                    seen.add(label)
                    suggestions.append(label)
                if len(suggestions) == 5:
                    break
            if suggestions:
                return {"suggestions": suggestions}
        except Exception as e:
            log.warning("ORS suggest failed (%s) — falling back to Nominatim", e)

    # Nominatim fallback
    try:
        import time
        time.sleep(1.1)   # respect Nominatim rate limit
        rn = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": text, "format": "json", "limit": 5, "countrycodes": "us",
                    "addressdetails": "1"},
            headers={"User-Agent": "GrayTech-TransportAnalyzer/1.0"},
            timeout=10,
        )
        rn.raise_for_status()
        results = rn.json()
        suggestions, seen = [], set()
        for res in results:
            addr = res.get("address", {})
            city    = addr.get("city") or addr.get("town") or addr.get("village") or addr.get("county", "")
            state   = addr.get("state_code") or addr.get("state", "")
            postal  = addr.get("postcode", "")
            if not city:
                continue
            if state and postal:
                label = f"{city}, {state.upper()} {postal}"
            elif state:
                label = f"{city}, {state.upper()}"
            else:
                label = city
            if label not in seen:
                seen.add(label)
                suggestions.append(label)
        return {"suggestions": suggestions[:5]}
    except Exception as e:
        log.warning("Nominatim suggest failed: %s", e)
        return {"suggestions": []}


# ── Import ────────────────────────────────────────────────────────────────────

@app.post("/import/parse")
async def import_parse(file: UploadFile = File(...)):
    """Upload an HTML report → return preview list (does not save to DB)."""
    content = await file.read()
    with tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="wb") as f:
        f.write(content)
        tmp_path = f.name
    try:
        trips = parse_report(tmp_path)
    finally:
        os.unlink(tmp_path)

    for t in trips:
        t["already_exists"] = conf_exists(t["confirmation_number"])
    return trips


class ImportConfirmBody(BaseModel):
    trips: list[dict]

@app.post("/import/confirm", status_code=201)
def import_confirm(body: ImportConfirmBody):
    imported = skipped = 0
    for t in body.trips:
        if conf_exists(t["confirmation_number"]):
            skipped += 1
            continue
        try:
            tid = add_trip(
                t["confirmation_number"], t.get("bus_number", ""),
                t["start_date"], t["end_date"], t["grand_total"],
                t["days"], t.get("miles", 0.0), t.get("drive_minutes", 0),
                t["status"], t.get("is_multiday", 0),
            )
            if t.get("stops"):
                save_trip_stops(tid, t["stops"])
            imported += 1
        except Exception as e:
            log.error("Import error for %s: %s", t.get("confirmation_number"), e)
            skipped += 1
    return {"imported": imported, "skipped": skipped}


# ── Export ────────────────────────────────────────────────────────────────────

@app.get("/trips/export.csv")
def export_csv():
    trips = [calculate_all(t) for t in get_all_trips()]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "Confirmation #", "Bus #", "Status", "Start Date", "End Date",
        "Grand Total", "Days", "Miles", "Drive Time",
        "Maint. Cost", "Labor Cost", "Fuel Cost", "Vehicle Cost", "Trip Profit", "Multi-day",
    ])
    for t in trips:
        w.writerow([
            t["confirmation_number"], t.get("bus_number", ""),
            t.get("status", "active").capitalize(),
            t.get("start_date", ""), t.get("end_date", ""),
            t["grand_total"], t["days"], t["miles"],
            minutes_to_hhmm(t["drive_minutes"]) if t["drive_minutes"] else "",
            t["maint_cost"], t["labor_cost"], t["fuel_cost"],
            t["vehicle_cost"], t["trip_profit"],
            "Yes" if t.get("is_multiday") else "No",
        ])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=trips_export.csv"},
    )


# ── Buses ─────────────────────────────────────────────────────────────────────

@app.get("/buses")
def list_buses():
    trips = get_all_trips()
    buses = sorted({t.get("bus_number") or "" for t in trips})
    return buses


# ── Groups ────────────────────────────────────────────────────────────────────

@app.get("/groups")
def list_groups():
    groups = get_all_groups()
    for g in groups:
        g["trips"] = get_group_trips(g["id"])
    return groups


@app.post("/groups", status_code=201)
def new_group(body: GroupIn):
    gid = create_group(body.name)
    return {"id": gid}


@app.put("/groups/{group_id}")
def edit_group(group_id: int, body: GroupIn):
    rename_group(group_id, body.name)
    return {"ok": True}


@app.delete("/groups/{group_id}")
def remove_group(group_id: int):
    delete_group(group_id)
    return {"ok": True}


@app.put("/groups/{group_id}/assignments")
def assign_group_trips(group_id: int, body: GroupAssignment):
    # Unlink all current members
    for t in get_group_trips(group_id):
        set_trip_group(t["id"], None, 0)
    # Link new ordered list
    for order, tid in enumerate(body.trip_ids, 1):
        set_trip_group(tid, group_id, order)
    return {"ok": True}


@app.post("/groups/{group_id}/route")
def calc_group_route(group_id: int, body: GroupRouteBody):
    if not ORS_API_KEY:
        raise HTTPException(500, "ORS_API_KEY not configured")
    group_trips = get_group_trips(group_id)
    if not group_trips:
        raise HTTPException(400, "No trips in this group")

    all_addresses: list[str] = []
    for t in group_trips:
        stops = get_trip_stops(t["id"])
        middle = [
            s["address"] for s in stops
            if s.get("address", "").strip()
            and s["address"].strip().lower() != HOME_BASE.lower()
        ]
        all_addresses.extend(middle)

    if body.use_home_base:
        all_addresses = [HOME_BASE] + all_addresses + [HOME_BASE]

    if len(all_addresses) < 2:
        raise HTTPException(400, "Not enough stops to calculate a route")

    try:
        result = calculate_route(all_addresses, ORS_API_KEY)
    except ValueError as e:
        raise HTTPException(400, str(e))

    return {
        "miles": result["total_miles"],
        "drive_minutes": result["total_minutes"],
        "failed": result.get("failed", []),
    }
