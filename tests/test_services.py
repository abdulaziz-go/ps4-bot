"""Unit tests covering every acceptance criterion at the logic level.

These run without Telegram or a network — they exercise the service/DB layer
directly against an in-memory SQLite database.
"""
from datetime import datetime

import pytest

import config
import database
import services


@pytest.fixture
def conn():
    c = database.init_db(":memory:")
    yield c
    c.close()


# --- Criterion 1: DB auto-creation -----------------------------------------
def test_init_creates_orders_table(conn):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='orders'"
    ).fetchone()
    assert row is not None


def test_init_db_creates_file(tmp_path):
    db = tmp_path / "auto.db"
    assert not db.exists()
    c = database.init_db(str(db))
    c.close()
    assert db.exists()


# --- Criterion 2: happy path + 60/40 split ---------------------------------
def test_happy_path_completed_appears_in_report_with_split(conn):
    now = datetime(2026, 8, 15, 10, 0, 0)
    oid = services.create_order(
        conn, customer_name="Ali", order_type="kunduzgi", amount=150_000, now=now
    )

    o = services.confirm_order(conn, oid, now=now)
    assert o["status"] == "confirmed"

    o = services.complete_order(conn, oid, now=now)
    assert o["status"] == "completed"

    r = services.monthly_report(conn, 2026, 8)
    assert r["completed_count"] == 1
    assert r["completed_amount"] == 150_000
    assert r["total_revenue"] == 150_000
    assert r["share_a"] == 90_000   # 60%
    assert r["share_b"] == 60_000   # 40%
    assert r["share_a"] + r["share_b"] == r["total_revenue"]


# --- Criterion 3: cancellation fees ----------------------------------------
def test_cancel_confirmed_kunduzgi_adds_15000(conn):
    now = datetime(2026, 8, 10, 9, 0, 0)
    oid = services.create_order(
        conn, customer_name="Vali", order_type="kunduzgi", amount=100_000, now=now
    )
    services.confirm_order(conn, oid, now=now)
    fee = services.cancel_order(conn, oid, now=now)

    assert fee == 15_000
    r = services.monthly_report(conn, 2026, 8)
    assert r["cancel_fees"] == 15_000
    assert r["total_revenue"] == 15_000


def test_cancel_confirmed_tungi_adds_20000(conn):
    now = datetime(2026, 8, 10, 23, 0, 0)
    oid = services.create_order(
        conn, customer_name="Guli", order_type="tungi", amount=100_000, now=now
    )
    services.confirm_order(conn, oid, now=now)
    fee = services.cancel_order(conn, oid, now=now)

    assert fee == 20_000
    r = services.monthly_report(conn, 2026, 8)
    assert r["cancel_fees"] == 20_000


def test_cancel_new_order_is_free(conn):
    now = datetime(2026, 8, 1)
    oid = services.create_order(
        conn, customer_name="X", order_type="kunduzgi", amount=50_000, now=now
    )
    fee = services.cancel_order(conn, oid, now=now)
    assert fee == 0
    r = services.monthly_report(conn, 2026, 8)
    assert r["total_revenue"] == 0


# --- Criterion 4: admin lockout --------------------------------------------
def test_admin_lockout(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_IDS", {111})
    monkeypatch.setattr(config, "SUPERADMIN_IDS", set())
    assert config.is_admin(111) is True
    assert config.is_admin(999) is False


# --- SUPERADMIN role --------------------------------------------------------
def test_superadmin_is_also_admin(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_IDS", set())
    monkeypatch.setattr(config, "SUPERADMIN_IDS", {555})
    assert config.is_superadmin(555) is True
    assert config.is_admin(555) is True        # superadmin implies admin
    assert config.is_superadmin(111) is False
    assert config.is_admin(111) is False


# --- Optional phone field ---------------------------------------------------
def test_create_order_stores_optional_phone(conn):
    oid = services.create_order(
        conn, customer_name="Ali", order_type="kunduzgi", amount=1000,
        phone="+998901234567",
    )
    assert services.get_order(conn, oid)["phone"] == "+998901234567"


def test_create_order_without_phone(conn):
    oid = services.create_order(conn, customer_name="Ali", order_type="kunduzgi", amount=1000)
    assert services.get_order(conn, oid)["phone"] is None


# --- State machine guards ---------------------------------------------------
def test_cannot_complete_new_order(conn):
    oid = services.create_order(conn, customer_name="X", order_type="kunduzgi", amount=1)
    with pytest.raises(services.OrderError):
        services.complete_order(conn, oid)


def test_cannot_confirm_completed_order(conn):
    oid = services.create_order(conn, customer_name="X", order_type="kunduzgi", amount=1)
    services.confirm_order(conn, oid)
    services.complete_order(conn, oid)
    with pytest.raises(services.OrderError):
        services.confirm_order(conn, oid)


def test_cannot_cancel_completed_order(conn):
    oid = services.create_order(conn, customer_name="X", order_type="kunduzgi", amount=1)
    services.confirm_order(conn, oid)
    services.complete_order(conn, oid)
    with pytest.raises(services.OrderError):
        services.cancel_order(conn, oid)


def test_invalid_order_type_rejected(conn):
    with pytest.raises(services.OrderError):
        services.create_order(conn, customer_name="X", order_type="ertalabki", amount=1)


def test_empty_customer_name_rejected(conn):
    with pytest.raises(services.OrderError):
        services.create_order(conn, customer_name="  ", order_type="tungi", amount=1)


# --- Reporting edge cases ---------------------------------------------------
def test_report_scoped_to_month(conn):
    july = datetime(2026, 7, 15)
    august = datetime(2026, 8, 15)

    oid_july = services.create_order(conn, customer_name="A", order_type="kunduzgi", amount=100_000, now=july)
    services.confirm_order(conn, oid_july, now=july)
    services.complete_order(conn, oid_july, now=july)

    oid_aug = services.create_order(conn, customer_name="B", order_type="kunduzgi", amount=200_000, now=august)
    services.confirm_order(conn, oid_aug, now=august)
    services.complete_order(conn, oid_aug, now=august)

    assert services.monthly_report(conn, 2026, 7)["completed_amount"] == 100_000
    assert services.monthly_report(conn, 2026, 8)["completed_amount"] == 200_000


def test_split_sums_exactly_with_odd_total(conn):
    now = datetime(2026, 8, 15)
    oid = services.create_order(conn, customer_name="X", order_type="kunduzgi", amount=100_001, now=now)
    services.confirm_order(conn, oid, now=now)
    services.complete_order(conn, oid, now=now)

    r = services.monthly_report(conn, 2026, 8)
    assert r["total_revenue"] == 100_001
    assert r["share_a"] + r["share_b"] == 100_001


def test_completed_and_cancelled_combined_revenue(conn):
    now = datetime(2026, 8, 15)
    # One completed kunduzgi (120 000) + one cancelled tungi (fee 20 000) = 140 000.
    o1 = services.create_order(conn, customer_name="A", order_type="kunduzgi", amount=120_000, now=now)
    services.confirm_order(conn, o1, now=now)
    services.complete_order(conn, o1, now=now)

    o2 = services.create_order(conn, customer_name="B", order_type="tungi", amount=999_999, now=now)
    services.confirm_order(conn, o2, now=now)
    services.cancel_order(conn, o2, now=now)

    r = services.monthly_report(conn, 2026, 8)
    assert r["total_revenue"] == 140_000
    assert r["share_a"] == 84_000   # 60% of 140 000
    assert r["share_b"] == 56_000   # 40% of 140 000


# --- Reset money report + saved history -------------------------------------
def _complete(conn, amount, now, order_type="kunduzgi"):
    oid = services.create_order(conn, customer_name="X", order_type=order_type, amount=amount, now=now)
    services.confirm_order(conn, oid, now=now)
    services.complete_order(conn, oid, now=now)
    return oid


def test_reset_zeroes_current_report_and_saves_snapshot(conn):
    now = datetime(2026, 8, 15, 10, 0, 0)
    _complete(conn, 50_000, now)
    assert services.monthly_report(conn, 2026, 8)["total_revenue"] == 50_000

    reset_now = datetime(2026, 8, 15, 12, 0, 0)
    snap = services.reset_report(conn, 2026, 8, reset_by=7, now=reset_now)

    # The returned snapshot preserves the closed period's income and numbers.
    assert snap["total_revenue"] == 50_000
    assert snap["completed_count"] == 1
    assert snap["share_a"] == 30_000
    assert snap["share_b"] == 20_000

    # ...and the live report is now zero.
    after = services.monthly_report(conn, 2026, 8)
    assert after["total_revenue"] == 0
    assert after["completed_count"] == 0


def test_orders_after_reset_count_again(conn):
    services.reset_report(conn, 2026, 8, now=datetime(2026, 8, 10, 9, 0, 0))
    _complete(conn, 70_000, datetime(2026, 8, 10, 10, 0, 0))
    assert services.monthly_report(conn, 2026, 8)["total_revenue"] == 70_000


def test_reset_is_scoped_to_its_month(conn):
    _complete(conn, 100_000, datetime(2026, 7, 15))          # July revenue
    services.reset_report(conn, 2026, 8, now=datetime(2026, 8, 1, 0, 0, 0))  # reset empty August
    # Resetting August must not disturb July's historical report.
    assert services.monthly_report(conn, 2026, 7)["completed_amount"] == 100_000


def test_history_saves_every_reset(conn):
    _complete(conn, 50_000, datetime(2026, 8, 5, 9, 0, 0))
    services.reset_report(conn, 2026, 8, now=datetime(2026, 8, 5, 12, 0, 0))
    _complete(conn, 30_000, datetime(2026, 8, 6, 9, 0, 0))
    services.reset_report(conn, 2026, 8, now=datetime(2026, 8, 6, 12, 0, 0))

    rows = services.list_resets(conn)
    assert len(rows) == 2
    # Newest first; each snapshot records only its own period's income.
    assert rows[0]["reset_at"] >= rows[1]["reset_at"]
    assert rows[0]["total_revenue"] == 30_000
    assert rows[1]["total_revenue"] == 50_000


def test_last_reset_at_none_when_never_reset(conn):
    assert services.last_reset_at(conn) is None
    assert services.last_reset_at(conn, 2026, 8) is None
