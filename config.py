"""Configuration for the Telegram order bot.

All operational settings are read from environment variables so the same code
runs unchanged in dev and prod. Business constants (fees, split) live here too.

A local ``.env`` file is loaded automatically on import (see ``load_dotenv``).
Real environment variables always win, so ``.env`` is dev convenience only and
never overrides values injected by the deployment/server.
"""
import os

from dotenv import load_dotenv

# Populate os.environ from a local .env if present. ``override=False`` (the
# default) means an already-exported var takes precedence over the file.
load_dotenv()


def _parse_ids(raw: str) -> set[int]:
    """Parse a comma/semicolon separated list of Telegram user IDs."""
    ids: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            # Ignore malformed entries rather than crashing on startup.
            pass
    return ids


# --- Telegram / storage -----------------------------------------------------
BOT_TOKEN: str = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_IDS: set[int] = _parse_ids(os.environ.get("ADMIN_IDS", ""))
# Superadmins may do everything an admin can, plus reset the monthly report.
SUPERADMIN_IDS: set[int] = _parse_ids(os.environ.get("SUPERADMIN_IDS", ""))
DB_PATH: str = os.environ.get("DB_PATH", "bot.db")


def _parse_int(raw: str, default: int) -> int:
    """Parse a single integer env value, falling back to ``default`` if unset/bad."""
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


# Customer-facing group/supergroup where the bot posts device-availability
# announcements. The bot is otherwise SILENT in every group — it never replies
# to anything written there. Stored as an int (Telegram group IDs are negative).
GROUP_CHAT_ID: int = _parse_int(os.environ.get("GROUP_CHAT_ID"), -1004439378633)

# --- Business rules ---------------------------------------------------------
# Cancellation fee (so'm) charged when a *confirmed* order is cancelled.
CANCEL_FEES: dict[str, int] = {
    "kunduzgi": 0,  # daytime
    "tungi": 0,     # nighttime
    "bir_kun": 0,        # full day — no cancellation fee
}

# Monthly revenue is reported split 60 / 40. Integer percentages keep the math
# exact (no floating-point rounding of money).
SPLIT_A_PCT: int = 60
SPLIT_B_PCT: int = 40
SPLIT_A_LABEL: str = os.environ.get("SPLIT_A_LABEL", "Firma (60%)")
SPLIT_B_LABEL: str = os.environ.get("SPLIT_B_LABEL", "Xodim (40%)")


def is_admin(user_id: int) -> bool:
    """Return True for whitelisted admins (superadmins are admins too)."""
    return user_id in ADMIN_IDS or user_id in SUPERADMIN_IDS


def is_superadmin(user_id: int) -> bool:
    """Return True only for whitelisted superadmin Telegram user IDs.

    Superadmins can reset the monthly money report (see ``services.reset_report``).
    """
    return user_id in SUPERADMIN_IDS


def _mask_token(token: str) -> str:
    """Redact the bot token for logs, keeping just enough to identify it."""
    if not token:
        return "(o'rnatilmagan)"
    head = token.split(":", 1)[0]
    return f"{head}:***"


def env_summary() -> str:
    """Human-readable snapshot of the loaded environment, for startup logs.

    Shows exactly which admin/superadmin IDs are active so a mis-set ``.env`` is
    obvious immediately instead of surfacing later as "ruxsat yo'q".
    """
    def _fmt(ids: set[int]) -> str:
        return ", ".join(str(i) for i in sorted(ids)) if ids else "(bo'sh)"

    return (
        "🔧 Environment:\n"
        f"   BOT_TOKEN      : {_mask_token(BOT_TOKEN)}\n"
        f"   ADMIN_IDS      : {_fmt(ADMIN_IDS)}\n"
        f"   SUPERADMIN_IDS : {_fmt(SUPERADMIN_IDS)}\n"
        f"   DB_PATH        : {DB_PATH}\n"
        f"   GROUP_CHAT_ID  : {GROUP_CHAT_ID}\n"
        f"   SPLIT_A_LABEL  : {SPLIT_A_LABEL}\n"
        f"   SPLIT_B_LABEL  : {SPLIT_B_LABEL}"
    )
