"""
Parse an HTML reservations report into a list of trip dicts.

Expected HTML table structure (8 columns, 3 rows per trip):
  Row 1: Conf# | PU Date | Billing Contact | Company | Routing Detail | Driver | Vehicle Type | Trip Total
  Row 2: Type  | Times   | Passenger       | Group   | (blank)        | Car    | Status       | Pmt Method
  Row 3: Type  | End time| (blank)         | Ref#    | (blank)        | (blank)| (blank)      | (blank)
"""
import re
import logging
from datetime import datetime
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)


def _text(cell) -> str:
    return cell.get_text(separator="\n", strip=True) if cell else ""


def _parse_stops(routing_text: str) -> list[dict]:
    stops = []
    for line in routing_text.splitlines():
        line = line.strip()
        for prefix, stype in [("PU:", "PU"), ("WT:", "WT"), ("ST:", "ST"), ("DO:", "DO")]:
            if line.upper().startswith(prefix):
                addr = line[len(prefix):].strip()
                if addr:
                    stops.append({"stop_type": stype, "address": addr})
                break
    return stops


def _parse_amount(text: str) -> float:
    try:
        cleaned = re.sub(r"[^\d.]", "", text.split("\n")[0])
        return float(cleaned) if cleaned else 0.0
    except Exception:
        return 0.0


def _parse_date(text: str) -> str:
    """Convert MM/DD/YYYY → YYYY-MM-DD for storage."""
    raw = text.split("\n")[0].strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return ""


def _extract_bus_number(car_text: str) -> str:
    """Pull the vehicle ID from 'MCI J-4500 Motor Coach – CL118875 (BUS-MC56-316)'."""
    # Try 'WORD (WORD)' pattern — grab the word before the parenthesis
    match = re.search(r"[–\-]\s*(\w+)\s*\(", car_text)
    if match:
        return match.group(1)
    # Fallback: content inside parentheses
    match = re.search(r"\(([^)]+)\)", car_text)
    if match:
        return match.group(1)
    return car_text.strip()


def _is_conf_number(text: str) -> bool:
    return bool(re.match(r"^\d{5,7}$", text.strip()))


def parse_report(filepath: str) -> list[dict]:
    """
    Parse the HTML file and return a list of trip dicts ready for database import.
    Each dict has: confirmation_number, start_date, end_date, grand_total,
                   status, bus_number, stops (list), is_multiday, days, miles, drive_minutes.
    """
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            html = f.read()
    except Exception as e:
        log.error("Could not read file: %s", e)
        return []

    soup = BeautifulSoup(html, "lxml")
    all_rows = soup.find_all("tr")

    trips = []
    i = 0

    while i < len(all_rows):
        row = all_rows[i]
        cells = row.find_all(["td", "th"])

        if not cells:
            i += 1
            continue

        # Get the text of the first cell — check for conf# as plain text or link
        first_text = cells[0].get_text(strip=True)
        link = cells[0].find("a")
        if link:
            first_text = link.get_text(strip=True)

        if not _is_conf_number(first_text):
            i += 1
            continue

        # ── Found a trip header row ──
        conf_num = first_text

        # Collect all sub-rows until the next conf# row
        sub_rows = [cells]
        j = i + 1
        while j < len(all_rows):
            sub_cells = all_rows[j].find_all(["td", "th"])
            if not sub_cells:
                j += 1
                continue
            sub_first = sub_cells[0].get_text(strip=True)
            sub_link  = sub_cells[0].find("a")
            if sub_link:
                sub_first = sub_link.get_text(strip=True)
            if _is_conf_number(sub_first):
                break
            sub_rows.append(sub_cells)
            j += 1

        # ── Extract fields from row 0 (trip header) ──
        r0 = sub_rows[0]
        date_text    = _text(r0[1]) if len(r0) > 1 else ""
        routing_text = _text(r0[4]) if len(r0) > 4 else ""
        total_text   = _text(r0[7]) if len(r0) > 7 else ""

        # ── Extract fields from row 1 (first sub-row) ──
        # Row 2 columns: Type(0) | Times(1) | Passenger(2) | Group(3) | Car(4) | Status(5) | Pmt Method(6)
        status     = "active"
        bus_number = ""
        if len(sub_rows) > 1:
            r1 = sub_rows[1]
            if len(r1) > 4:
                bus_number = _extract_bus_number(_text(r1[4]))
            if len(r1) > 5:
                if "cancel" in _text(r1[5]).lower():
                    status = "cancelled"

        stops      = _parse_stops(routing_text)
        start_date = _parse_date(date_text)
        amount     = _parse_amount(total_text)

        if amount == 0.0:
            status = "cancelled"

        if not start_date:
            log.warning("Could not parse date for conf# %s — skipping", conf_num)
            i = j
            continue

        trips.append({
            "confirmation_number": conf_num,
            "bus_number":          bus_number,
            "start_date":          start_date,
            "end_date":            start_date,   # user reviews multi-day trips
            "grand_total":         amount,
            "status":              status,
            "stops":               stops,
            "is_multiday":         0,
            "days":                1,
            "miles":               0.0,
            "drive_minutes":       0,
        })

        i = j

    log.info("Parsed %d trips from %s", len(trips), filepath)
    return trips
