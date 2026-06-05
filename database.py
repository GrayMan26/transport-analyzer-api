import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "trips.db"


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _connect()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trip_groups (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT    NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trips (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            confirmation_number TEXT    UNIQUE NOT NULL,
            bus_number          TEXT    NOT NULL DEFAULT '',
            start_date          TEXT    NOT NULL DEFAULT '',
            end_date            TEXT    NOT NULL DEFAULT '',
            grand_total         REAL    NOT NULL,
            days                INTEGER NOT NULL,
            miles               REAL    NOT NULL DEFAULT 0,
            drive_minutes       INTEGER NOT NULL DEFAULT 0,
            status              TEXT    NOT NULL DEFAULT 'active',
            is_multiday         INTEGER NOT NULL DEFAULT 0,
            group_id            INTEGER DEFAULT NULL,
            leg_order           INTEGER NOT NULL DEFAULT 0,
            created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trip_stops (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            trip_id     INTEGER NOT NULL,
            stop_order  INTEGER NOT NULL,
            stop_type   TEXT    NOT NULL,
            address     TEXT    NOT NULL,
            lat         REAL,
            lon         REAL
        )
    """)
    # Migrate existing databases
    for col, definition in [
        ("bus_number",    "TEXT NOT NULL DEFAULT ''"),
        ("start_date",    "TEXT NOT NULL DEFAULT ''"),
        ("end_date",      "TEXT NOT NULL DEFAULT ''"),
        ("status",        "TEXT NOT NULL DEFAULT 'active'"),
        ("is_multiday",   "INTEGER NOT NULL DEFAULT 0"),
        ("miles",         "REAL NOT NULL DEFAULT 0"),
        ("drive_minutes", "INTEGER NOT NULL DEFAULT 0"),
        ("group_id",      "INTEGER DEFAULT NULL"),
        ("leg_order",     "INTEGER NOT NULL DEFAULT 0"),
    ]:
        try:
            conn.execute(f"ALTER TABLE trips ADD COLUMN {col} {definition}")
        except Exception:
            pass
    conn.commit()
    conn.close()


# ── Trips ─────────────────────────────────────────────────────────────────────

def get_all_trips() -> list[dict]:
    conn = _connect()
    rows = conn.execute("SELECT * FROM trips ORDER BY confirmation_number").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_trip(confirmation: str, bus_number: str, start_date: str, end_date: str,
             grand_total: float, days: int, miles: float, drive_minutes: int,
             status: str = "active", is_multiday: int = 0) -> int:
    conn = _connect()
    cur = conn.execute(
        "INSERT INTO trips "
        "(confirmation_number, bus_number, start_date, end_date, grand_total, "
        " days, miles, drive_minutes, status, is_multiday) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (confirmation, bus_number, start_date, end_date, grand_total,
         days, miles, drive_minutes, status, is_multiday),
    )
    trip_id = cur.lastrowid
    conn.commit()
    conn.close()
    return trip_id


def update_trip(trip_id: int, confirmation: str, bus_number: str, start_date: str,
                end_date: str, grand_total: float, days: int, miles: float,
                drive_minutes: int, status: str = "active", is_multiday: int = 0):
    conn = _connect()
    conn.execute(
        "UPDATE trips SET confirmation_number=?, bus_number=?, start_date=?, end_date=?, "
        "grand_total=?, days=?, miles=?, drive_minutes=?, status=?, is_multiday=? "
        "WHERE id=?",
        (confirmation, bus_number, start_date, end_date, grand_total,
         days, miles, drive_minutes, status, is_multiday, trip_id),
    )
    conn.commit()
    conn.close()


def delete_trip(trip_id: int):
    conn = _connect()
    conn.execute("DELETE FROM trip_stops WHERE trip_id=?", (trip_id,))
    conn.execute("DELETE FROM trips WHERE id=?", (trip_id,))
    conn.commit()
    conn.close()


def conf_exists(confirmation_number: str) -> bool:
    conn = _connect()
    count = conn.execute(
        "SELECT COUNT(*) FROM trips WHERE confirmation_number=?",
        (confirmation_number,)
    ).fetchone()[0]
    conn.close()
    return count > 0


# ── Stops ─────────────────────────────────────────────────────────────────────

def get_trip_stops(trip_id: int) -> list[dict]:
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM trip_stops WHERE trip_id=? ORDER BY stop_order",
        (trip_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def save_trip_stops(trip_id: int, stops: list[dict]):
    """Replace all stops for a trip."""
    conn = _connect()
    conn.execute("DELETE FROM trip_stops WHERE trip_id=?", (trip_id,))
    for i, s in enumerate(stops):
        conn.execute(
            "INSERT INTO trip_stops (trip_id, stop_order, stop_type, address, lat, lon) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (trip_id, i, s["stop_type"], s["address"],
             s.get("lat"), s.get("lon")),
        )
    conn.commit()
    conn.close()


# ── Trip Groups ───────────────────────────────────────────────────────────────

def create_group(name: str) -> int:
    conn = _connect()
    cur = conn.execute("INSERT INTO trip_groups (name) VALUES (?)", (name,))
    gid = cur.lastrowid
    conn.commit()
    conn.close()
    return gid


def get_all_groups() -> list[dict]:
    conn = _connect()
    rows = conn.execute("SELECT * FROM trip_groups ORDER BY name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_group(group_id: int) -> dict | None:
    conn = _connect()
    row = conn.execute("SELECT * FROM trip_groups WHERE id=?", (group_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def rename_group(group_id: int, name: str):
    conn = _connect()
    conn.execute("UPDATE trip_groups SET name=? WHERE id=?", (name, group_id))
    conn.commit()
    conn.close()


def delete_group(group_id: int):
    conn = _connect()
    conn.execute("UPDATE trips SET group_id=NULL, leg_order=0 WHERE group_id=?", (group_id,))
    conn.execute("DELETE FROM trip_groups WHERE id=?", (group_id,))
    conn.commit()
    conn.close()


def set_trip_group(trip_id: int, group_id: int | None, leg_order: int = 0):
    conn = _connect()
    conn.execute(
        "UPDATE trips SET group_id=?, leg_order=? WHERE id=?",
        (group_id, leg_order, trip_id),
    )
    conn.commit()
    conn.close()


def get_group_trips(group_id: int) -> list[dict]:
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM trips WHERE group_id=? ORDER BY leg_order, start_date",
        (group_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
