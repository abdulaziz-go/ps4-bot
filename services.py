"""Business logic for orders — pure functions over a sqlite3 connection.

This layer has zero Telegram dependencies, so every rule in the acceptance
criteria (state machine, cancellation fees, monthly 60/40 split) is unit
testable in isolation. All timestamps are injectable via ``now`` for
deterministic tests.
"""
import html
import sqlite3
from datetime import datetime

from config import CANCEL_FEES, SPLIT_A_PCT

VALID_TYPES = ("kunduzgi", "tungi", "bir_kun")

# Device types stored in the `devices.dtype` column.
DEVICE_TYPES = ("playstation", "joystick")

# Active statuses that occupy a slot in the schedule (used for overlap checks
# and the queue listing). Completed / cancelled orders no longer hold a slot.
ACTIVE_STATUSES = ("new", "confirmed")

# Accepted input formats for start/end date-times, tried in order. All normalise
# to '%Y-%m-%d %H:%M:%S' so stored strings sort chronologically.
_DT_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%d.%m.%Y %H:%M:%S",
    "%d.%m.%Y %H:%M",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
)


class OrderError(Exception):
    """Raised for invalid input or illegal state transitions."""


def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


def parse_dt(raw: str) -> str:
    """Parse a user-supplied date-time into canonical 'YYYY-MM-DD HH:MM:SS'.

    Accepts a few common orderings (ISO ``2026-08-25 09:00`` and dotted/slashed
    ``25.08.2026 09:00``). Raises ``OrderError`` on anything unrecognised so the
    caller can re-prompt.
    """
    text = (raw or "").strip()
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    raise OrderError(
        "Sana/vaqt formati noto'g'ri. Masalan: 2026-08-25 09:00 "
        "yoki 25.08.2026 09:00"
    )


def create_order(
    conn: sqlite3.Connection,
    *,
    customer_name: str,
    order_type: str,
    amount: int,
    phone: str | None = None,
    address: str | None = None,
    start_at: str | None = None,
    end_at: str | None = None,
    created_by: int | None = None,
    device_ids: list[int] | None = None,
    now: datetime | None = None,
) -> int:
    """Insert a new order (status='new') and return its id.

    ``start_at`` / ``end_at`` are the scheduled service window (canonical
    'YYYY-MM-DD HH:MM:SS' strings — see ``parse_dt``). They must be supplied
    together, with the end strictly after the start.
    """
    if order_type not in VALID_TYPES:
        raise OrderError(f"Noto'g'ri buyurtma turi: {order_type!r}")
    if not customer_name or not customer_name.strip():
        raise OrderError("Mijoz ismi bo'sh bo'lishi mumkin emas")
    amount = int(amount)
    if amount < 0:
        raise OrderError("Summa manfiy bo'lishi mumkin emas")
    if (start_at is None) != (end_at is None):
        raise OrderError("Boshlanish va tugash vaqti birga kiritilishi kerak")
    if start_at is not None and end_at <= start_at:
        raise OrderError("Tugash vaqti boshlanish vaqtidan keyin bo'lishi kerak")

    cur = conn.execute(
        """INSERT INTO orders
               (customer_name, phone, address, order_type, amount, status,
                start_at, end_at, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, 'new', ?, ?, ?, ?)""",
        (customer_name.strip(), phone, address, order_type, amount,
         start_at, end_at, created_by, _now_iso(now)),
    )
    order_id = cur.lastrowid
    for device_id in dict.fromkeys(device_ids or []):  # de-dupe, keep order
        conn.execute(
            "INSERT OR IGNORE INTO order_devices (order_id, device_id) VALUES (?, ?)",
            (order_id, device_id),
        )
    conn.commit()
    return order_id


def find_overlaps(
    conn: sqlite3.Connection,
    start_at: str,
    end_at: str,
    exclude_id: int | None = None,
    statuses: tuple[str, ...] = ACTIVE_STATUSES,
) -> list[sqlite3.Row]:
    """Return active orders whose scheduled window overlaps ``[start_at, end_at)``.

    Two windows overlap when ``start_at < other.end_at AND end_at > other.start_at``.
    Used to warn (not block) when a new order lands on top of one already queued.
    """
    placeholders = ",".join("?" * len(statuses))
    return conn.execute(
        f"""SELECT * FROM orders
             WHERE status IN ({placeholders})
               AND start_at IS NOT NULL AND end_at IS NOT NULL
               AND ? < end_at AND ? > start_at
               AND (? IS NULL OR id != ?)
             ORDER BY start_at ASC, id ASC""",
        (*statuses, start_at, end_at, exclude_id, exclude_id),
    ).fetchall()


def get_order(conn: sqlite3.Connection, order_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()


# Whitelisted ORDER BY clauses. Kept as a fixed map (never interpolate caller
# input into SQL): history reads newest-first, the active queue reads by slot.
_ORDER_BY = {
    "id_desc": "id DESC",
    # Chronological queue; scheduled orders first, undated legacy rows last.
    "queue": "start_at IS NULL, start_at ASC, id ASC",
}


def list_orders(
    conn: sqlite3.Connection,
    statuses: list[str] | None = None,
    limit: int | None = None,
    order_by: str = "id_desc",
) -> list[sqlite3.Row]:
    order_clause = _ORDER_BY.get(order_by, _ORDER_BY["id_desc"])
    limit_clause = f" LIMIT {int(limit)}" if limit else ""
    if statuses:
        placeholders = ",".join("?" * len(statuses))
        return conn.execute(
            f"SELECT * FROM orders WHERE status IN ({placeholders}) "
            f"ORDER BY {order_clause}{limit_clause}",
            tuple(statuses),
        ).fetchall()
    return conn.execute(
        f"SELECT * FROM orders ORDER BY {order_clause}{limit_clause}"
    ).fetchall()


def confirm_order(conn: sqlite3.Connection, order_id: int, now: datetime | None = None) -> sqlite3.Row:
    """new -> confirmed."""
    order = get_order(conn, order_id)
    if order is None:
        raise OrderError("Buyurtma topilmadi")
    if order["status"] != "new":
        raise OrderError("Faqat yangi buyurtmani tasdiqlash mumkin")
    conn.execute(
        "UPDATE orders SET status='confirmed', confirmed_at=? WHERE id=?",
        (_now_iso(now), order_id),
    )
    conn.commit()
    return get_order(conn, order_id)


def complete_order(conn: sqlite3.Connection, order_id: int, now: datetime | None = None) -> sqlite3.Row:
    """confirmed -> completed. The amount now counts toward monthly revenue."""
    order = get_order(conn, order_id)
    if order is None:
        raise OrderError("Buyurtma topilmadi")
    if order["status"] != "confirmed":
        raise OrderError("Faqat tasdiqlangan buyurtmani yakunlash mumkin")
    conn.execute(
        "UPDATE orders SET status='completed', completed_at=? WHERE id=?",
        (_now_iso(now), order_id),
    )
    conn.commit()
    return get_order(conn, order_id)


def cancel_order(conn: sqlite3.Connection, order_id: int, now: datetime | None = None) -> int:
    """Cancel an order and return the fee added to revenue.

    Cancelling a *confirmed* order charges a fee (kunduzgi=15 000, tungi=20 000).
    Cancelling a *new* (not-yet-confirmed) order is free. Completed / already
    cancelled orders cannot be cancelled.
    """
    order = get_order(conn, order_id)
    if order is None:
        raise OrderError("Buyurtma topilmadi")
    if order["status"] in ("completed", "cancelled"):
        raise OrderError("Bu buyurtmani bekor qilib bo'lmaydi")

    fee = CANCEL_FEES.get(order["order_type"], 0) if order["status"] == "confirmed" else 0
    conn.execute(
        "UPDATE orders SET status='cancelled', cancel_fee=?, cancelled_at=? WHERE id=?",
        (fee, _now_iso(now), order_id),
    )
    conn.commit()
    return fee


def last_reset_at(
    conn: sqlite3.Connection, year: int | None = None, month: int | None = None
) -> str | None:
    """Return the timestamp of the most recent reset (optionally for one month).

    A ``monthly_report`` only counts orders finalised *after* this checkpoint, so
    resetting starts a fresh tally without deleting any order.
    """
    if year is not None and month is not None:
        row = conn.execute(
            "SELECT MAX(reset_at) AS m FROM resets WHERE year = ? AND month = ?",
            (year, month),
        ).fetchone()
    else:
        row = conn.execute("SELECT MAX(reset_at) AS m FROM resets").fetchone()
    return row["m"] if row else None


def monthly_report(conn: sqlite3.Connection, year: int, month: int) -> dict:
    """Aggregate revenue for a calendar month and split it 60 / 40.

    Revenue = completed order amounts (by completed_at) + cancellation fees
    (by cancelled_at). Only orders finalised *after* that month's last reset are
    counted, so a reset zeroes the running total. Integer math guarantees
    share_a + share_b == total.
    """
    prefix = f"{year:04d}-{month:02d}"
    reset_at = last_reset_at(conn, year, month)

    # `? IS NULL OR ...` makes the reset filter a no-op when nothing was reset.
    completed = conn.execute(
        """SELECT COUNT(*) AS c, COALESCE(SUM(amount), 0) AS s
             FROM orders
            WHERE status='completed'
              AND substr(completed_at, 1, 7) = ?
              AND (? IS NULL OR completed_at > ?)""",
        (prefix, reset_at, reset_at),
    ).fetchone()
    cancelled = conn.execute(
        """SELECT COUNT(*) AS c, COALESCE(SUM(cancel_fee), 0) AS s
             FROM orders
            WHERE status='cancelled'
              AND substr(cancelled_at, 1, 7) = ?
              AND (? IS NULL OR cancelled_at > ?)""",
        (prefix, reset_at, reset_at),
    ).fetchone()

    completed_amount = completed["s"]
    cancel_fees = cancelled["s"]
    total = completed_amount + cancel_fees

    share_a = total * SPLIT_A_PCT // 100
    share_b = total - share_a  # remainder -> guarantees exact sum

    return {
        "year": year,
        "month": month,
        "completed_count": completed["c"],
        "completed_amount": completed_amount,
        "cancelled_count": cancelled["c"],
        "cancel_fees": cancel_fees,
        "total_revenue": total,
        "share_a": share_a,
        "share_b": share_b,
    }


def reset_report(
    conn: sqlite3.Connection,
    year: int,
    month: int,
    reset_by: int | None = None,
    now: datetime | None = None,
) -> dict:
    """Snapshot the current month's report into ``resets`` and zero the tally.

    The snapshot preserves the closed period's income and counts; after this the
    same month's ``monthly_report`` returns zero until new orders are finalised.
    Returns the snapshot that was saved (same shape as ``monthly_report``).
    """
    snapshot = monthly_report(conn, year, month)
    conn.execute(
        """INSERT INTO resets
               (reset_at, reset_by, year, month,
                completed_count, completed_amount, cancelled_count, cancel_fees,
                total_revenue, share_a, share_b)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            _now_iso(now), reset_by, year, month,
            snapshot["completed_count"], snapshot["completed_amount"],
            snapshot["cancelled_count"], snapshot["cancel_fees"],
            snapshot["total_revenue"], snapshot["share_a"], snapshot["share_b"],
        ),
    )
    conn.commit()
    return snapshot


def list_resets(conn: sqlite3.Connection, limit: int | None = None) -> list[sqlite3.Row]:
    """Return saved reset snapshots, newest first."""
    limit_clause = f" LIMIT {int(limit)}" if limit else ""
    return conn.execute(
        f"SELECT * FROM resets ORDER BY reset_at DESC, id DESC{limit_clause}"
    ).fetchall()


# --- Devices (PlayStations + joysticks) ------------------------------------
# A device is "busy" iff it is linked to an order whose status is one of
# ACTIVE_STATUSES ('new' / 'confirmed'). No status is stored on the device row —
# it is always derived, so completing/cancelling an order frees its devices
# automatically with no extra bookkeeping.
DEVICE_TYPE_LABELS = {
    "playstation": "PlayStation",
    "joystick": "Joystik",
}

_BUSY_EXPR = (
    "EXISTS (SELECT 1 FROM order_devices od JOIN orders o ON o.id = od.order_id "
    "WHERE od.device_id = d.id AND o.status IN ('new', 'confirmed'))"
)


def add_device(
    conn: sqlite3.Connection,
    *,
    name: str,
    dtype: str,
    now: datetime | None = None,
) -> int:
    """Add a device by name. Rejects empty and case-insensitive duplicate names."""
    if dtype not in DEVICE_TYPES:
        raise OrderError(f"Noto'g'ri qurilma turi: {dtype!r}")
    name = (name or "").strip()
    if not name:
        raise OrderError("Qurilma nomi bo'sh bo'lishi mumkin emas.")
    dup = conn.execute(
        "SELECT 1 FROM devices WHERE lower(name) = lower(?)", (name,)
    ).fetchone()
    if dup:
        raise OrderError(f"«{name}» nomli qurilma allaqachon mavjud.")
    cur = conn.execute(
        "INSERT INTO devices (name, dtype, created_at) VALUES (?, ?, ?)",
        (name, dtype, _now_iso(now)),
    )
    conn.commit()
    return cur.lastrowid


def get_device(conn: sqlite3.Connection, device_id: int) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT *, {_BUSY_EXPR} AS is_busy FROM devices d WHERE d.id = ?",
        (device_id,),
    ).fetchone()


def list_devices(
    conn: sqlite3.Connection,
    dtype: str | None = None,
    only_free: bool = False,
) -> list[sqlite3.Row]:
    """List devices with a derived ``is_busy`` flag (0/1), ordered by type then id."""
    where = []
    params: list = []
    if dtype is not None:
        where.append("d.dtype = ?")
        params.append(dtype)
    if only_free:
        where.append(f"NOT {_BUSY_EXPR}")
    where_clause = (" WHERE " + " AND ".join(where)) if where else ""
    return conn.execute(
        f"SELECT *, {_BUSY_EXPR} AS is_busy FROM devices d{where_clause} "
        "ORDER BY d.dtype ASC, d.id ASC",
        tuple(params),
    ).fetchall()


def device_busy_order(conn: sqlite3.Connection, device_id: int) -> sqlite3.Row | None:
    """Return the active order currently holding this device, or None if free."""
    return conn.execute(
        """SELECT o.* FROM orders o
             JOIN order_devices od ON od.order_id = o.id
            WHERE od.device_id = ? AND o.status IN ('new', 'confirmed')
            ORDER BY o.id ASC LIMIT 1""",
        (device_id,),
    ).fetchone()


def delete_device(conn: sqlite3.Connection, device_id: int) -> None:
    """Delete a device. Refuses if it is currently busy (held by an active order)."""
    device = get_device(conn, device_id)
    if device is None:
        raise OrderError("Qurilma topilmadi.")
    busy = device_busy_order(conn, device_id)
    if busy is not None:
        raise OrderError(
            f"«{device['name']}» hozir #{busy['id']}-buyurtmada band. "
            "Avval o'sha buyurtmani yakunlang yoki bekor qiling."
        )
    conn.execute("DELETE FROM order_devices WHERE device_id = ?", (device_id,))
    conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))
    conn.commit()


def order_devices(conn: sqlite3.Connection, order_id: int) -> list[sqlite3.Row]:
    """Devices attached to an order (any status), ordered by type then id."""
    return conn.execute(
        """SELECT dev.* FROM devices dev
             JOIN order_devices od ON od.device_id = dev.id
            WHERE od.order_id = ?
            ORDER BY dev.dtype ASC, dev.id ASC""",
        (order_id,),
    ).fetchall()


def free_devices_snapshot(conn: sqlite3.Connection) -> dict:
    """Return currently-free devices grouped by type: names lists + counts."""
    ps = [r["name"] for r in list_devices(conn, "playstation", only_free=True)]
    js = [r["name"] for r in list_devices(conn, "joystick", only_free=True)]
    return {"playstations": ps, "joysticks": js}


# --- Order device rendering (plain text, used inside order cards) -----------
def order_devices_lines(devices: list[sqlite3.Row]) -> list[str]:
    """Render an order's attached devices as display lines.

    ``devices`` is the output of ``order_devices``. Renders ``—`` for both types
    when the order has none (old orders created before this feature).
    """
    ps = [d["name"] for d in devices if d["dtype"] == "playstation"]
    js = [d["name"] for d in devices if d["dtype"] == "joystick"]
    ps_line = ", ".join(ps) if ps else "—"
    if js:
        js_line = f"{len(js)} ta ({', '.join(js)})"
    else:
        js_line = "—"
    return [f"🎮 PlayStation: {ps_line}", f"🕹 Joystiklar: {js_line}"]


# --- Group notification message builders (Telegram HTML) -------------------
# Pure functions: they take plain data and return an HTML string, so they are
# unit-testable and never touch Telegram. Every dynamic value (device name) is
# HTML-escaped. NEVER include client name / phone / amount / schedule here — the
# group is customer-facing.
def _esc(s: str) -> str:
    return html.escape(str(s))


def _free_body(playstations: list[str], joysticks: list[str]) -> str:
    """Shared body listing what is free, or the 'all busy' fallback."""
    if not playstations and not joysticks:
        return (
            "😔 Hozircha barcha qurilmalar band. "
            "Bo'shashi bilan shu yerda e'lon qilamiz!"
        )
    parts: list[str] = []
    if playstations:
        parts.append("🎮 <b>Bo'sh PlayStationlar:</b>")
        parts.extend(f" • {_esc(n)}" for n in playstations)
    if joysticks:
        if parts:
            parts.append("")
        parts.append(f"🕹 <b>Bo'sh joystiklar:</b> {len(joysticks)} ta")
        parts.append(" • " + ", ".join(_esc(n) for n in joysticks))
    return "\n".join(parts)


def build_devices_snapshot_message(
    playstations: list[str],
    joysticks: list[str],
    *,
    confirmed: bool = False,
) -> str:
    """Snapshot of still-free devices, sent on order creation / confirmation.

    Both variants make it clear a new order came in **but** these devices are
    still free — so nobody in the group thinks the bot mis-posted.
    """
    all_busy = not playstations and not joysticks
    header = (
        "✅ <b>Yangi buyurtma tushdi!</b>"
        if confirmed
        else "🆕 <b>Yangi buyurtma tushdi!</b>"
    )
    if all_busy:
        intro = "Ammo hozircha barcha qurilmalar band:"
    else:
        intro = "Ammo bizda hali ham quyidagi qurilmalar bo'sh 👇"
    return (
        f"{header}\n\n"
        f"{intro}\n\n"
        f"{_free_body(playstations, joysticks)}\n\n"
        "📞 <i>Buyurtma berish uchun biz bilan bog'laning!</i>"
    )


def _fmt_money(n: int) -> str:
    """Format an integer amount with space thousands separators (12 000)."""
    return f"{int(n):,}".replace(",", " ")


def build_promo_announcement_message(
    period: str,
    price: int,
    body: str = "",
) -> str:
    """Customer-facing discount announcement for a free-text period + fixed price.

    ``period`` is admin-written free text describing when the aksiya runs
    (e.g. "Dushanba va Seshanba kunlari"). ``body`` is the free-text the admin
    wrote and is inserted verbatim. All dynamic values are HTML-escaped.
    No client data — group-facing only.
    """
    parts = [
        "🔥 <b>AKSIYA! Chegirma!</b>",
        "",
        f"📅 <b>Muddat:</b> {_esc(period)}",
        f"🎮 <b>Chegirmali narx:</b> {_fmt_money(price)} so'm",
    ]
    body = (body or "").strip()
    if body:
        parts.append("")
        parts.append(_esc(body))
    parts.append("")
    parts.append("📞 <i>Buyurtma berish uchun biz bilan bog'laning!</i>")
    return "\n".join(parts)


def build_devices_freed_message(
    playstations: list[str],
    joysticks: list[str],
) -> str:
    """Announcement that devices have freed up (order completed / cancelled)."""
    if not playstations and not joysticks:
        return (
            "🎉 <b>Qurilmalar bo'shadi!</b>\n\n"
            "😔 Hozircha barcha qurilmalar band. "
            "Bo'shashi bilan shu yerda e'lon qilamiz!\n\n"
            "📞 <i>Buyurtma berish uchun yozing!</i>"
        )
    parts = [
        "🎉 <b>Qurilmalar bo'shadi!</b>",
        "",
        "Hozir quyidagilar bo'sh — buyurtma qilishingiz mumkin:",
        "",
    ]
    if playstations:
        parts.append("🎮 <b>PlayStationlar:</b>")
        parts.extend(f" • {_esc(n)}" for n in playstations)
        parts.append("")
    if joysticks:
        parts.append(f"🕹 <b>Joystiklar:</b> {len(joysticks)} ta")
        parts.append("")
    parts.append("📞 <i>Buyurtma berish uchun yozing!</i>")
    return "\n".join(parts)
