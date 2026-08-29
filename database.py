"""SQLite storage: connection helper + schema bootstrap.

The database and its tables are created automatically on first use, so
`python main.py` works against a fresh machine with no manual setup.
"""
import sqlite3

from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_name TEXT    NOT NULL,
    phone         TEXT,
    address       TEXT,
    order_type    TEXT    NOT NULL CHECK (order_type IN ('kunduzgi', 'tungi', 'bir_kun')),
    amount        INTEGER NOT NULL DEFAULT 0,
    status        TEXT    NOT NULL DEFAULT 'new'
                          CHECK (status IN ('new', 'confirmed', 'completed', 'cancelled')),
    cancel_fee    INTEGER NOT NULL DEFAULT 0,
    -- Scheduled service window (the order's slot in the queue). Stored as
    -- 'YYYY-MM-DD HH:MM:SS' so string comparison == chronological order.
    -- Nullable so pre-scheduling rows still load.
    start_at      TEXT,
    end_at        TEXT,
    created_by    INTEGER,
    created_at    TEXT    NOT NULL,
    confirmed_at  TEXT,
    completed_at  TEXT,
    cancelled_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_orders_status ON orders (status);
-- NB: the start_at index is created in _migrate(), after that column is
-- guaranteed to exist — this SCHEMA also runs against pre-scheduling databases.

-- Each row is an immutable snapshot of the monthly report captured the moment
-- a superadmin reset it. Resetting never deletes orders — it just records the
-- closed period's income and counts here and starts a fresh tally afterwards.
CREATE TABLE IF NOT EXISTS resets (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    reset_at         TEXT    NOT NULL,
    reset_by         INTEGER,
    year             INTEGER NOT NULL,
    month            INTEGER NOT NULL,
    completed_count  INTEGER NOT NULL DEFAULT 0,
    completed_amount INTEGER NOT NULL DEFAULT 0,
    cancelled_count  INTEGER NOT NULL DEFAULT 0,
    cancel_fees      INTEGER NOT NULL DEFAULT 0,
    total_revenue    INTEGER NOT NULL DEFAULT 0,
    share_a          INTEGER NOT NULL DEFAULT 0,
    share_b          INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_resets_period ON resets (year, month);

-- Physical inventory: each PlayStation / joystick is one row, added by name.
-- A device has NO status column on purpose — "busy" is derived from whether it
-- is linked to an active (new/confirmed) order (see services.list_devices),
-- which avoids the classic status-out-of-sync bug.
CREATE TABLE IF NOT EXISTS devices (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL,
    dtype      TEXT    NOT NULL CHECK (dtype IN ('playstation', 'joystick')),
    created_at TEXT
);

-- Which devices were handed out for which order. Link rows are kept even after
-- the order finishes, for history; uniqueness stops a device being attached
-- twice to the same order.
CREATE TABLE IF NOT EXISTS order_devices (
    order_id  INTEGER NOT NULL REFERENCES orders (id),
    device_id INTEGER NOT NULL REFERENCES devices (id),
    UNIQUE (order_id, device_id)
);

CREATE INDEX IF NOT EXISTS idx_order_devices_order  ON order_devices (order_id);
CREATE INDEX IF NOT EXISTS idx_order_devices_device ON order_devices (device_id);
"""


def get_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    """Open a connection with dict-like rows.

    ``check_same_thread=False`` lets the connection be shared across
    pyTelegramBotAPI's worker threads. Access is effectively serialized by the
    bot's low request volume.
    """
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


# Rebuilds the `orders` table with the current schema. Used by the migration to
# widen the `order_type` CHECK constraint, which SQLite cannot ALTER in place.
# Columns are copied by name, so it is safe regardless of the old column order.
_REBUILD_ORDERS = """
BEGIN;
CREATE TABLE orders_new (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_name TEXT    NOT NULL,
    phone         TEXT,
    address       TEXT,
    order_type    TEXT    NOT NULL CHECK (order_type IN ('kunduzgi', 'tungi', 'bir_kun')),
    amount        INTEGER NOT NULL DEFAULT 0,
    status        TEXT    NOT NULL DEFAULT 'new'
                          CHECK (status IN ('new', 'confirmed', 'completed', 'cancelled')),
    cancel_fee    INTEGER NOT NULL DEFAULT 0,
    start_at      TEXT,
    end_at        TEXT,
    created_by    INTEGER,
    created_at    TEXT    NOT NULL,
    confirmed_at  TEXT,
    completed_at  TEXT,
    cancelled_at  TEXT
);
INSERT INTO orders_new
    (id, customer_name, phone, address, order_type, amount, status, cancel_fee,
     start_at, end_at, created_by, created_at, confirmed_at, completed_at, cancelled_at)
SELECT
     id, customer_name, phone, address, order_type, amount, status, cancel_fee,
     start_at, end_at, created_by, created_at, confirmed_at, completed_at, cancelled_at
  FROM orders;
DROP TABLE orders;
ALTER TABLE orders_new RENAME TO orders;
COMMIT;
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders (status);
CREATE INDEX IF NOT EXISTS idx_orders_start ON orders (start_at);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Upgrade an existing DB in place: add scheduling columns and the new type.

    Idempotent — safe to run on every startup and on a freshly-created DB (where
    it is a no-op because ``SCHEMA`` already produced the current shape).
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(orders)")}
    if "start_at" not in cols:
        conn.execute("ALTER TABLE orders ADD COLUMN start_at TEXT")
    if "end_at" not in cols:
        conn.execute("ALTER TABLE orders ADD COLUMN end_at TEXT")
    conn.commit()

    # Widen the order_type CHECK to allow 'bir_kun'. The constraint lives in the
    # table's CREATE SQL, so detect the old shape by its absence and rebuild.
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='orders'"
    ).fetchone()
    if row and "bir_kun" not in row["sql"]:
        conn.executescript(_REBUILD_ORDERS)
        conn.commit()

    # Safe on every path now that start_at is guaranteed to exist.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_start ON orders (start_at)")
    conn.commit()


def init_db(db_path: str = DB_PATH) -> sqlite3.Connection:
    """Create the schema if missing, migrate older DBs, and return the connection."""
    conn = get_connection(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    _migrate(conn)
    return conn
