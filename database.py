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
    order_type    TEXT    NOT NULL CHECK (order_type IN ('kunduzgi', 'tungi')),
    amount        INTEGER NOT NULL DEFAULT 0,
    status        TEXT    NOT NULL DEFAULT 'new'
                          CHECK (status IN ('new', 'confirmed', 'completed', 'cancelled')),
    cancel_fee    INTEGER NOT NULL DEFAULT 0,
    created_by    INTEGER,
    created_at    TEXT    NOT NULL,
    confirmed_at  TEXT,
    completed_at  TEXT,
    cancelled_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_orders_status ON orders (status);

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


def init_db(db_path: str = DB_PATH) -> sqlite3.Connection:
    """Create the schema if missing and return an open connection."""
    conn = get_connection(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn
