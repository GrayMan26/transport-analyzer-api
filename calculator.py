GAS_PRICE        = 5.74
MPG              = 6.9
LABOR_RATE       = 21.00   # $ per hour
MAINTENANCE_RATE = 0.40    # $ per mile
VEHICLE_DAY_RATE = 330.00  # $ per day


def maintenance_cost(miles: float) -> float:
    return round(miles * MAINTENANCE_RATE, 2)


def labor_cost(drive_minutes: int) -> float:
    return round((drive_minutes / 60) * LABOR_RATE, 2)


def fuel_cost(miles: float) -> float:
    return round((miles / MPG) * GAS_PRICE, 2)


def vehicle_cost(days: int) -> float:
    return round(days * VEHICLE_DAY_RATE, 2)


def trip_profit(grand_total: float, miles: float, drive_minutes: int, days: int) -> float:
    return round(
        grand_total
        - maintenance_cost(miles)
        - labor_cost(drive_minutes)
        - fuel_cost(miles)
        - vehicle_cost(days),
        2,
    )


def calculate_all(trip: dict) -> dict:
    miles      = trip["miles"]
    dm         = trip["drive_minutes"]
    days       = trip["days"]
    gt         = trip["grand_total"]
    is_active  = trip.get("status", "active") != "cancelled"
    has_miles  = miles > 0

    mc = maintenance_cost(miles)
    lc = labor_cost(dm)
    fc = fuel_cost(miles)
    vc = vehicle_cost(days) if (is_active and has_miles) else 0.0
    profit = round(gt - mc - lc - fc - vc, 2)

    return {
        **trip,
        "maint_cost":    mc,
        "labor_cost":    lc,
        "fuel_cost":     fc,
        "vehicle_cost":  vc,
        "trip_profit":   profit,
        "profit_per_day": round(profit / days, 2) if days else 0,
    }


def minutes_to_hhmm(minutes: int) -> str:
    return f"{minutes // 60}:{minutes % 60:02d}"


def hhmm_to_minutes(s: str) -> int:
    """Parse HH:MM into total minutes. Raises ValueError if invalid."""
    parts = s.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Expected HH:MM, got: {s}")
    h, m = int(parts[0]), int(parts[1])
    if m < 0 or m > 59:
        raise ValueError(f"Minutes must be 0-59, got: {m}")
    return h * 60 + m
