"""Telegram layer (pyTelegramBotAPI) — thin handlers over ``services``.

Every handler is admin-gated. All business logic lives in ``services`` so this
file only deals with UI: messages, keyboards and callback routing.

The whole bot is driven from a persistent reply-keyboard menu (see
``main_menu``); the equivalent slash commands are kept working as aliases.
"""
import html
import logging
from datetime import datetime

import telebot
from telebot import types

import config
import database
import services

logger = logging.getLogger(__name__)

bot = telebot.TeleBot(config.BOT_TOKEN, parse_mode="HTML")

# Shared connection + schema bootstrap (creates the DB on first run).
conn = database.init_db()

# In-memory per-chat draft state during order creation.
_drafts: dict[int, dict] = {}

# In-memory per-chat pending device type while awaiting a new device's name.
_dev_add_type: dict[int, str] = {}

# In-memory per-chat draft while composing a discount announcement (📣 E'lon).
_promos: dict[int, dict] = {}

TYPE_LABELS = {
    "kunduzgi": "🌞 Kunduzgi",
    "tungi": "🌙 Tungi",
    "bir_kun": "🌗 Bir kun to'liq",
}
STATUS_LABELS = {
    "new": "🆕 Yangi",
    "confirmed": "✅ Tasdiqlangan",
    "completed": "☑️ Yakunlangan",
    "cancelled": "❌ Bekor qilingan",
}

# --- menu button labels (also used as the message text handlers match on) ----
BTN_NEW = "➕ Yangi buyurtma"
BTN_LIST = "📋 Buyurtmalar"
BTN_HISTORY = "📚 Tarix"
BTN_REPORT = "📊 Hisobot"
BTN_DEVICES = "🎮 Qurilmalar"
BTN_ANNOUNCE = "📣 E'lon (Aksiya)"
BTN_RESET_HISTORY = "🗂 Hisobot tarixi"
BTN_RESET = "♻️ Hisobotni nollash"

# Values sent to skip the optional phone step.
_PHONE_SKIP = {"-", "", "yo'q", "yoq", "skip", "/skip", "o'tkazish", "otkazish"}

HELP_TEXT = (
    "🤖 <b>Buyurtmalar boti</b>\n\n"
    "Quyidagi tugmalardan foydalaning:\n"
    f"{BTN_NEW} — yangi buyurtma yaratish\n"
    f"{BTN_LIST} — aktiv buyurtmalar\n"
    f"{BTN_HISTORY} — buyurtmalar tarixi\n"
    f"{BTN_DEVICES} — qurilmalar (PlayStation / joystik)\n"
    f"{BTN_ANNOUNCE} — chegirma e'lonini guruhga yuborish\n"
    f"{BTN_REPORT} — shu oylik hisobot\n"
    f"{BTN_RESET_HISTORY} — nollangan hisobotlar tarixi\n"
    f"{BTN_RESET} — hisobotni nollash (faqat SUPERADMIN)"
)


# --- helpers ---------------------------------------------------------------
def money(n: int) -> str:
    """Format an integer amount with space thousands separators."""
    return f"{int(n):,}".replace(",", " ")


def esc(s) -> str:
    return html.escape(str(s)) if s else ""


def fmt_dt(value) -> str:
    """Show a stored 'YYYY-MM-DD HH:MM:SS' timestamp without the seconds."""
    return str(value)[:16] if value else "—"


def _is_private(chat) -> bool:
    """True only for one-to-one private chats. The bot is silent everywhere else
    (groups, supergroups, channels) — it only *pushes* notifications there."""
    return getattr(chat, "type", None) == "private"


def _guard(message) -> bool:
    """Reject non-admin messages. Returns True if allowed.

    In any non-private chat (group/supergroup/channel) the bot stays completely
    silent — no reply, no error — so it never reacts to group traffic.
    """
    if not _is_private(message.chat):
        return False
    if not config.is_admin(message.from_user.id):
        bot.reply_to(message, "⛔ Kechirasiz, sizda ruxsat yo'q.")
        return False
    return True


def _guard_cb(call) -> bool:
    if not _is_private(call.message.chat):
        return False
    if not config.is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "⛔ Ruxsat yo'q", show_alert=True)
        return False
    return True


def _notify_group(text: str) -> None:
    """Best-effort push of a customer-facing announcement to the group.

    Like the superadmin push, a failure (bot not in the group, wrong ID, no
    permission) must never break the order flow — log and move on.
    """
    try:
        bot.send_message(config.GROUP_CHAT_ID, text, parse_mode="HTML")
    except Exception as e:  # noqa: BLE001 - best effort, never fatal
        logger.warning("Group notification failed (chat %s): %s", config.GROUP_CHAT_ID, e)


def _user_label(u) -> str:
    """Readable name for a Telegram user, for notifications."""
    name = " ".join(filter(None, [u.first_name, u.last_name])).strip()
    if u.username:
        return f"{name} (@{u.username})" if name else f"@{u.username}"
    return name or f"ID {u.id}"


def _notify_superadmins(text: str, exclude_id: int | None = None) -> None:
    """Best-effort push of ``text`` to every superadmin.

    ``exclude_id`` skips one recipient (e.g. the person who triggered the event
    and already saw the result). A superadmin who never started the bot / blocked
    it must not break the action for everyone else, so send failures are ignored.
    """
    for sid in config.SUPERADMIN_IDS:
        if sid == exclude_id:
            continue
        try:
            bot.send_message(sid, text)
        except Exception:
            pass


def main_menu(user_id: int) -> types.ReplyKeyboardMarkup:
    """Persistent bottom keyboard; the reset button is superadmin-only."""
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(BTN_NEW)
    kb.row(BTN_LIST, BTN_HISTORY)
    kb.row(BTN_DEVICES, BTN_ANNOUNCE)
    kb.row(BTN_REPORT, BTN_RESET_HISTORY)
    if config.is_superadmin(user_id):
        kb.row(BTN_RESET)
    return kb


def format_order(o) -> str:
    lines = [
        f"<b>Buyurtma #{o['id']}</b>",
        f"👤 Mijoz: {esc(o['customer_name'])}",
    ]
    if o["phone"]:
        lines.append(f"📞 Tel: {esc(o['phone'])}")
    if o["address"]:
        lines.append(f"📍 Manzil: {esc(o['address'])}")
    lines.append(f"🕒 Turi: {TYPE_LABELS.get(o['order_type'], o['order_type'])}")
    if o["start_at"] or o["end_at"]:
        lines.append(f"🟢 Boshlanish: {fmt_dt(o['start_at'])}")
        lines.append(f"🔴 Tugash: {fmt_dt(o['end_at'])}")
    lines.append(f"💰 Summa: {money(o['amount'])} so'm")
    # Attached devices (— when the order has none, incl. pre-feature orders).
    devices = services.order_devices(conn, o["id"])
    lines.extend(esc(line) for line in services.order_devices_lines(devices))
    lines.append(f"Holat: {STATUS_LABELS.get(o['status'], o['status'])}")
    if o["status"] == "cancelled" and o["cancel_fee"]:
        lines.append(f"⚠️ Bekor qilish to'lovi: {money(o['cancel_fee'])} so'm")
    return "\n".join(lines)


def order_markup(o) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    if o["status"] == "new":
        kb.add(
            types.InlineKeyboardButton("✅ Tasdiqlash", callback_data=f"confirm:{o['id']}"),
            types.InlineKeyboardButton("❌ Bekor qilish", callback_data=f"cancel:{o['id']}"),
        )
    elif o["status"] == "confirmed":
        kb.add(
            types.InlineKeyboardButton("☑️ Yakunlash", callback_data=f"complete:{o['id']}"),
            types.InlineKeyboardButton("❌ Bekor qilish", callback_data=f"cancel:{o['id']}"),
        )
    return kb


def format_report(r) -> str:
    return (
        f"📊 <b>{r['year']}-{r['month']:02d} oylik hisobot</b>\n\n"
        f"☑️ Yakunlangan: {r['completed_count']} ta — {money(r['completed_amount'])} so'm\n"
        f"❌ Bekor (to'lov): {r['cancelled_count']} ta — {money(r['cancel_fees'])} so'm\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"💰 Jami tushum: <b>{money(r['total_revenue'])} so'm</b>\n\n"
        f"{config.SPLIT_A_LABEL}: {money(r['share_a'])} so'm\n"
        f"{config.SPLIT_B_LABEL}: {money(r['share_b'])} so'm"
    )


def format_overall_report(r) -> str:
    """Cumulative report since the last nollash (all months combined)."""
    since = fmt_dt(r["since"]) if r["since"] else "boshidan beri"
    return (
        f"📊 <b>Umumiy hisobot</b>\n"
        f"<i>(oxirgi nollashdan beri: {since})</i>\n\n"
        f"☑️ Yakunlangan: {r['completed_count']} ta — {money(r['completed_amount'])} so'm\n"
        f"❌ Bekor (to'lov): {r['cancelled_count']} ta — {money(r['cancel_fees'])} so'm\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"💰 Jami tushum: <b>{money(r['total_revenue'])} so'm</b>\n\n"
        f"{config.SPLIT_A_LABEL}: {money(r['share_a'])} so'm\n"
        f"{config.SPLIT_B_LABEL}: {money(r['share_b'])} so'm"
    )


def format_reset_entry(row, index: int) -> str:
    """One saved reset snapshot for the history list.

    Each entry is a numbered, self-contained block. Unlike ``format_report`` it
    has no internal ruler line — a single divider is placed *between* entries by
    the caller, so stacked snapshots read as separate cards instead of merging.
    """
    return (
        f"🗂 <b>#{index}</b> · 🕒 <i>{esc(row['reset_at'])}</i>\n"
        f"📅 Davr: <b>{row['year']}-{row['month']:02d}</b>\n"
        f"☑️ Yakunlangan: {row['completed_count']} ta — {money(row['completed_amount'])} so'm\n"
        f"❌ Bekor (to'lov): {row['cancelled_count']} ta — {money(row['cancel_fees'])} so'm\n"
        f"💰 Jami tushum: <b>{money(row['total_revenue'])} so'm</b>\n"
        f"{config.SPLIT_A_LABEL}: {money(row['share_a'])} so'm\n"
        f"{config.SPLIT_B_LABEL}: {money(row['share_b'])} so'm"
    )


# --- menu actions ----------------------------------------------------------
# Each `_do_*` holds the logic for one menu button; command/button/interrupt
# handlers all funnel into these so there is a single source of truth.
def _do_new(message):
    _drafts[message.chat.id] = {}
    msg = bot.send_message(message.chat.id, "👤 Mijoz ismini kiriting:")
    bot.register_next_step_handler(msg, _step_name)


def _step_name(message):
    if not _guard(message):
        return
    if _intercept(message):
        return
    _drafts.setdefault(message.chat.id, {})["customer_name"] = message.text.strip()
    msg = bot.send_message(
        message.chat.id,
        "📞 Mijoz telefon raqami (ixtiyoriy).\n"
        "O'tkazib yuborish uchun «-» yuboring:",
    )
    bot.register_next_step_handler(msg, _step_phone)


def _step_phone(message):
    if not _guard(message):
        return
    if _intercept(message):
        return
    raw = message.text.strip()
    phone = None if raw.lower() in _PHONE_SKIP else raw
    _drafts.setdefault(message.chat.id, {})["phone"] = phone
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("🌞 Kunduzgi", callback_data="type:kunduzgi"),
        types.InlineKeyboardButton("🌙 Tungi", callback_data="type:tungi"),
    )
    kb.add(types.InlineKeyboardButton("🌗 Bir kun to'liq", callback_data="type:bir_kun"))
    bot.send_message(message.chat.id, "Buyurtma turini tanlang:", reply_markup=kb)


def _step_start(message):
    if not _guard(message):
        return
    if _intercept(message):
        return
    try:
        start_at = services.parse_dt(message.text)
    except services.OrderError as e:
        msg = bot.reply_to(message, f"❌ {e}")
        bot.register_next_step_handler(msg, _step_start)
        return
    _drafts.setdefault(message.chat.id, {})["start_at"] = start_at
    msg = bot.send_message(
        message.chat.id,
        "🔴 Tugash sana va vaqtini kiriting.\nMasalan: 2026-08-25 18:00",
    )
    bot.register_next_step_handler(msg, _step_end)


def _step_end(message):
    if not _guard(message):
        return
    if _intercept(message):
        return
    try:
        end_at = services.parse_dt(message.text)
    except services.OrderError as e:
        msg = bot.reply_to(message, f"❌ {e}")
        bot.register_next_step_handler(msg, _step_end)
        return
    draft = _drafts.setdefault(message.chat.id, {})
    if draft.get("start_at") and end_at <= draft["start_at"]:
        msg = bot.reply_to(
            message,
            "❌ Tugash vaqti boshlanish vaqtidan keyin bo'lishi kerak. "
            "Qaytadan kiriting:",
        )
        bot.register_next_step_handler(msg, _step_end)
        return
    draft["end_at"] = end_at
    msg = bot.send_message(message.chat.id, "💰 Summani kiriting (so'm):")
    bot.register_next_step_handler(msg, _step_amount)


def _step_amount(message):
    if not _guard(message):
        return
    if _intercept(message):
        return
    draft = _drafts.get(message.chat.id, {})
    raw = message.text.strip().replace(" ", "").replace(",", "")
    try:
        amount = int(raw)
    except ValueError:
        msg = bot.reply_to(message, "❌ Summa noto'g'ri. Butun son kiriting (masalan 150000):")
        bot.register_next_step_handler(msg, _step_amount)
        return

    draft["amount"] = amount
    # Next: pick the devices handed out for this order (PlayStation, then joysticks).
    draft["js_ids"] = []
    _ask_playstation(message.chat.id)


# --- device selection during order creation --------------------------------
def _ask_playstation(chat_id: int) -> None:
    """Step 1 of device selection: choose one free PlayStation (or skip)."""
    free_ps = services.list_devices(conn, "playstation", only_free=True)
    kb = types.InlineKeyboardMarkup()
    for d in free_ps:
        kb.add(types.InlineKeyboardButton(f"🎮 {d['name']}", callback_data=f"opsel:{d['id']}"))
    kb.add(types.InlineKeyboardButton("⏭ O'tkazib yuborish", callback_data="opsel:skip"))
    if free_ps:
        text = "🎮 <b>PlayStation tanlang</b> (yoki o'tkazib yuboring):"
    else:
        text = "🎮 Hozircha bo'sh PlayStation yo'q. «O'tkazib yuborish»ni bosing:"
    bot.send_message(chat_id, text, reply_markup=kb)


def _joystick_markup(chat_id: int) -> types.InlineKeyboardMarkup:
    """Multi-select keyboard of free joysticks; selected ones get a ✅ mark."""
    selected = set(_drafts.get(chat_id, {}).get("js_ids", []))
    kb = types.InlineKeyboardMarkup()
    for d in services.list_devices(conn, "joystick", only_free=True):
        mark = "✅ " if d["id"] in selected else "🕹 "
        kb.add(types.InlineKeyboardButton(f"{mark}{d['name']}", callback_data=f"ojtog:{d['id']}"))
    kb.add(types.InlineKeyboardButton("✅ Tayyor", callback_data="ojdone"))
    return kb


def _ask_joysticks(chat_id: int) -> None:
    """Step 2 of device selection: toggle any number of free joysticks."""
    free_js = services.list_devices(conn, "joystick", only_free=True)
    if free_js:
        text = "🕹 <b>Joystik tanlang</b> (bir nechta bo'lishi mumkin), so'ng «Tayyor»:"
    else:
        text = "🕹 Hozircha bo'sh joystik yo'q. «Tayyor»ni bosing:"
    bot.send_message(chat_id, text, reply_markup=_joystick_markup(chat_id))


def _finalize_order(chat_id: int, from_user) -> tuple:
    """Create the order from the draft (with its devices) and return (order, clashes)."""
    draft = _drafts.get(chat_id, {})
    start_at = draft.get("start_at")
    end_at = draft.get("end_at")
    # "Warn but allow": surface any queue clash but still create the order.
    clashes = services.find_overlaps(conn, start_at, end_at) if start_at and end_at else []

    device_ids = []
    if draft.get("ps_id"):
        device_ids.append(draft["ps_id"])
    device_ids.extend(draft.get("js_ids", []))

    oid = services.create_order(
        conn,
        customer_name=draft.get("customer_name", ""),
        order_type=draft.get("order_type", "kunduzgi"),
        amount=draft.get("amount", 0),
        phone=draft.get("phone"),
        start_at=start_at,
        end_at=end_at,
        created_by=from_user.id,
        device_ids=device_ids,
    )
    _drafts.pop(chat_id, None)
    return services.get_order(conn, oid), clashes


def _publish_new_order(chat_id: int, from_user, o, clashes) -> None:
    """Show the created order card, push to superadmins, and announce to the group."""
    text = format_order(o)
    if clashes:
        conflict_lines = "\n".join(
            f"• #{c['id']} {fmt_dt(c['start_at'])} — {fmt_dt(c['end_at'])} "
            f"({esc(c['customer_name'])})"
            for c in clashes
        )
        text += (
            f"\n\n⚠️ <b>Diqqat:</b> bu vaqt oralig'ida {len(clashes)} ta "
            f"buyurtma bor:\n{conflict_lines}"
        )
    bot.send_message(chat_id, text, reply_markup=order_markup(o))

    # Notify every superadmin about the new order (skip the creator, who just
    # saw the card above). Reuses `text`, so clashes are surfaced to them too.
    _notify_superadmins(
        f"🔔 <b>Yangi buyurtma yaratildi</b>\n"
        f"👨‍💼 Kim: {esc(_user_label(from_user))}\n\n{text}",
        exclude_id=from_user.id,
    )
    # No group announcement on creation — the group is only notified when the
    # order is confirmed or finished (completed / cancelled).


def _do_list(message):
    # Ordered as a queue: by scheduled start time, earliest first.
    rows = services.list_orders(conn, statuses=["new", "confirmed"], order_by="queue")
    if not rows:
        bot.reply_to(message, "Aktiv buyurtmalar yo'q.")
        return
    bot.send_message(message.chat.id, f"📋 <b>Navbat</b> ({len(rows)} ta):")
    for o in rows:
        bot.send_message(message.chat.id, format_order(o), reply_markup=order_markup(o))


def _do_history(message):
    rows = services.list_orders(conn, statuses=["completed", "cancelled"], limit=20)
    if not rows:
        bot.reply_to(message, "📚 Tarix bo'sh.")
        return
    lines = ["📚 <b>Buyurtmalar tarixi</b> (oxirgi 20):\n"]
    for o in rows:
        line = (
            f"#{o['id']} {STATUS_LABELS.get(o['status'], o['status'])} — "
            f"{esc(o['customer_name'])} — {money(o['amount'])} so'm"
        )
        if o["start_at"]:
            line += f" — 🕒 {fmt_dt(o['start_at'])}"
        if o["status"] == "cancelled" and o["cancel_fee"]:
            line += f" (jarima {money(o['cancel_fee'])})"
        devices = services.order_devices(conn, o["id"])
        line += "\n   " + esc(" · ".join(services.order_devices_lines(devices)))
        lines.append(line)
    bot.send_message(message.chat.id, "\n".join(lines))


def _do_report(message):
    # Cumulative total since the last nollash (spans all months), not one month.
    r = services.overall_report(conn)
    bot.reply_to(message, format_overall_report(r))


def _do_reset_history(message):
    rows = services.list_resets(conn, limit=10)
    if not rows:
        bot.reply_to(message, "🗂 Hisobot tarixi bo'sh. Hali nollash amalga oshirilmagan.")
        return
    header = "🗂 <b>Nollangan hisobotlar tarixi</b> (oxirgi 10):"
    divider = "\n➖➖➖➖➖➖➖➖➖➖➖➖\n"
    entries = [format_reset_entry(row, i) for i, row in enumerate(rows, start=1)]
    bot.send_message(message.chat.id, header + divider + divider.join(entries))


def _do_reset(message):
    """Superadmin-only: show the current report and ask for Ha/Yo'q first."""
    if not config.is_superadmin(message.from_user.id):
        bot.reply_to(message, "⛔ Bu amal faqat SUPERADMIN uchun.")
        return
    r = services.overall_report(conn)
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("Ha, nollash ✅", callback_data="reset_yes_all"),
        types.InlineKeyboardButton("Yo'q ❌", callback_data="reset_no"),
    )
    bot.send_message(
        message.chat.id,
        "♻️ <b>Hisobotni nollash</b>\n\n"
        "Joriy umumiy hisobot tarixga saqlanadi va nolga tushiriladi.\n"
        "Buyurtmalar o'chirilmaydi.\n\n"
        f"{format_overall_report(r)}\n\n"
        "Davom etamizmi?",
        reply_markup=kb,
    )


def _devices_overview_text() -> str:
    """Full inventory grouped by type with 🟢/🔴 status and per-type totals."""
    lines = ["🎮 <b>Qurilmalar</b>\n"]
    for dtype, title in (("playstation", "🎮 PlayStationlar"), ("joystick", "🕹 Joystiklar")):
        rows = services.list_devices(conn, dtype)
        free = sum(1 for r in rows if not r["is_busy"])
        lines.append(f"<b>{title}</b> — {len(rows)} tadan {free} tasi bo'sh")
        for r in rows:
            badge = "🔴 Band" if r["is_busy"] else "🟢 Bo'sh"
            lines.append(f"  • {esc(r['name'])} — {badge}")
        if not rows:
            lines.append("  <i>(bo'sh)</i>")
        lines.append("")
    return "\n".join(lines).rstrip()


def _devices_markup() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("➕ Qurilma qo'shish", callback_data="devadd"),
        types.InlineKeyboardButton("🗑 O'chirish", callback_data="devdel"),
    )
    return kb


def _do_devices(message):
    bot.send_message(message.chat.id, _devices_overview_text(), reply_markup=_devices_markup())


# --- announcement / discount promo (📣 E'lon) ------------------------------
# Collects a period (start/end), a discounted fixed price, and a free-text body,
# then shows a preview with a "send to group" button. Nothing is posted until
# the admin taps that button.
def _do_announce(message):
    _promos[message.chat.id] = {}
    msg = bot.send_message(
        message.chat.id,
        "📣 <b>Chegirma e'loni</b>\n\n"
        "🗓 Aksiya <b>muddatini</b> yozing (ixtiyoriy matn).\n"
        "Masalan: <i>Dushanba va Seshanba kunlari</i> yoki <i>Shu hafta oxirigacha</i>",
    )
    bot.register_next_step_handler(msg, _promo_step_period)


def _promo_intercepted(message) -> bool:
    """Abort the promo draft if a menu button / command is tapped mid-flow."""
    if _intercept(message):
        _promos.pop(message.chat.id, None)
        return True
    return False


def _promo_step_period(message):
    if not _guard(message):
        return
    if _promo_intercepted(message):
        return
    period = (message.text or "").strip()
    if not period:
        msg = bot.reply_to(message, "❌ Muddat bo'sh bo'lishi mumkin emas. Qaytadan kiriting:")
        bot.register_next_step_handler(msg, _promo_step_period)
        return
    _promos.setdefault(message.chat.id, {})["period"] = period
    msg = bot.send_message(message.chat.id, "💰 Chegirmali (belgilangan) narxni kiriting (so'm):")
    bot.register_next_step_handler(msg, _promo_step_price)


def _promo_step_price(message):
    if not _guard(message):
        return
    if _promo_intercepted(message):
        return
    raw = message.text.strip().replace(" ", "").replace(",", "")
    try:
        price = int(raw)
    except ValueError:
        msg = bot.reply_to(message, "❌ Narx noto'g'ri. Butun son kiriting (masalan 25000):")
        bot.register_next_step_handler(msg, _promo_step_price)
        return
    _promos.setdefault(message.chat.id, {})["price"] = price
    msg = bot.send_message(
        message.chat.id,
        "✍️ E'lon matnini kiriting (o'z so'zlaringiz bilan).\n"
        "Matnsiz yuborish uchun «-» yuboring:",
    )
    bot.register_next_step_handler(msg, _promo_step_body)


def _promo_step_body(message):
    if not _guard(message):
        return
    if _promo_intercepted(message):
        return
    promo = _promos.setdefault(message.chat.id, {})
    raw = (message.text or "").strip()
    promo["body"] = "" if raw in {"-", ""} else raw
    text = services.build_promo_announcement_message(
        promo.get("period", ""), promo.get("price", 0), promo.get("body", ""),
    )
    promo["message"] = text
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("✅ Guruhga yuborish", callback_data="promo_send"),
        types.InlineKeyboardButton("❌ Bekor qilish", callback_data="promo_cancel"),
    )
    bot.send_message(
        message.chat.id,
        f"👀 <b>Ko'rib chiqing — guruhga shu ko'rinishda boradi:</b>\n\n{text}",
        reply_markup=kb,
    )


# Maps a menu button label to its action. Defined after the `_do_*` functions.
MENU_ROUTES = {
    BTN_NEW: _do_new,
    BTN_LIST: _do_list,
    BTN_HISTORY: _do_history,
    BTN_DEVICES: _do_devices,
    BTN_ANNOUNCE: _do_announce,
    BTN_REPORT: _do_report,
    BTN_RESET_HISTORY: _do_reset_history,
    BTN_RESET: _do_reset,
}


def _intercept(message) -> bool:
    """Abort an in-progress order draft if the user taps a menu button / command.

    Returns True (and dispatches the tapped action) when a step handler should
    stop, so half-finished drafts don't swallow a menu press.
    """
    text = message.text or ""
    if text in MENU_ROUTES:
        _drafts.pop(message.chat.id, None)
        MENU_ROUTES[text](message)
        return True
    if text.startswith("/"):
        _drafts.pop(message.chat.id, None)
        bot.send_message(
            message.chat.id,
            "❌ Buyurtma yaratish bekor qilindi.",
            reply_markup=main_menu(message.from_user.id),
        )
        return True
    return False


# --- commands (kept as aliases for the buttons) ----------------------------
@bot.message_handler(commands=["start", "help"])
def cmd_start(message):
    if not _guard(message):
        return
    bot.send_message(message.chat.id, HELP_TEXT, reply_markup=main_menu(message.from_user.id))


@bot.message_handler(commands=["yangi"])
def cmd_new(message):
    if not _guard(message):
        return
    _do_new(message)


@bot.message_handler(commands=["buyurtmalar"])
def cmd_list(message):
    if not _guard(message):
        return
    _do_list(message)


@bot.message_handler(commands=["tarix"])
def cmd_history(message):
    if not _guard(message):
        return
    _do_history(message)


@bot.message_handler(commands=["hisobot"])
def cmd_report(message):
    if not _guard(message):
        return
    _do_report(message)


@bot.message_handler(commands=["qurilmalar"])
def cmd_devices(message):
    if not _guard(message):
        return
    _do_devices(message)


@bot.message_handler(commands=["elon"])
def cmd_announce(message):
    if not _guard(message):
        return
    _do_announce(message)


@bot.message_handler(commands=["nollash"])
def cmd_reset(message):
    if not _guard(message):
        return
    _do_reset(message)


# One handler for every menu button (registered before the catch-all fallback).
@bot.message_handler(func=lambda m: m.text in MENU_ROUTES)
def on_menu_button(message):
    if not _guard(message):
        return
    MENU_ROUTES[message.text](message)


# --- callbacks -------------------------------------------------------------
@bot.callback_query_handler(func=lambda c: c.data.startswith("type:"))
def cb_type(call):
    if not _guard_cb(call):
        return
    order_type = call.data.split(":", 1)[1]
    _drafts.setdefault(call.message.chat.id, {})["order_type"] = order_type
    bot.edit_message_text(
        f"Tur: {TYPE_LABELS[order_type]}\n\n"
        "🟢 Boshlanish sana va vaqtini kiriting.\nMasalan: 2026-08-25 09:00",
        call.message.chat.id,
        call.message.message_id,
    )
    bot.register_next_step_handler(call.message, _step_start)


@bot.callback_query_handler(func=lambda c: c.data.startswith("opsel:"))
def cb_order_pick_ps(call):
    """PlayStation chosen (or skipped) during order creation -> ask joysticks."""
    if not _guard_cb(call):
        return
    value = call.data.split(":", 1)[1]
    draft = _drafts.setdefault(call.message.chat.id, {})
    if value == "skip":
        draft["ps_id"] = None
        label = "—"
    else:
        dev = services.get_device(conn, int(value))
        if dev is None or dev["is_busy"]:
            bot.answer_callback_query(call.id, "Bu PlayStation endi bo'sh emas.", show_alert=True)
            return
        draft["ps_id"] = dev["id"]
        label = dev["name"]
    bot.answer_callback_query(call.id)
    bot.edit_message_text(f"🎮 PlayStation: {esc(label)}", call.message.chat.id, call.message.message_id)
    _ask_joysticks(call.message.chat.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("ojtog:"))
def cb_order_toggle_joystick(call):
    """Toggle a joystick in the current order draft and re-render the keyboard."""
    if not _guard_cb(call):
        return
    did = int(call.data.split(":", 1)[1])
    draft = _drafts.setdefault(call.message.chat.id, {})
    js_ids = draft.setdefault("js_ids", [])
    if did in js_ids:
        js_ids.remove(did)
    else:
        js_ids.append(did)
    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_reply_markup(
            call.message.chat.id, call.message.message_id,
            reply_markup=_joystick_markup(call.message.chat.id),
        )
    except Exception:
        pass


@bot.callback_query_handler(func=lambda c: c.data == "ojdone")
def cb_order_joysticks_done(call):
    """Finish device selection and actually create the order."""
    if not _guard_cb(call):
        return
    bot.answer_callback_query(call.id)
    js = _drafts.get(call.message.chat.id, {}).get("js_ids", [])
    bot.edit_message_text(f"🕹 Tanlangan joystiklar: {len(js)} ta", call.message.chat.id, call.message.message_id)
    try:
        o, clashes = _finalize_order(call.message.chat.id, call.from_user)
    except services.OrderError as e:
        bot.send_message(call.message.chat.id, f"❌ {e}")
        return
    _publish_new_order(call.message.chat.id, call.from_user, o, clashes)


@bot.callback_query_handler(func=lambda c: c.data.startswith("confirm:"))
def cb_confirm(call):
    if not _guard_cb(call):
        return
    oid = int(call.data.split(":", 1)[1])
    try:
        o = services.confirm_order(conn, oid)
    except services.OrderError as e:
        bot.answer_callback_query(call.id, str(e), show_alert=True)
        return
    bot.answer_callback_query(call.id, "Tasdiqlandi ✅")
    bot.edit_message_text(format_order(o), call.message.chat.id, call.message.message_id,
                          reply_markup=order_markup(o))
    # Group: snapshot of devices still free, with the "confirmed" header.
    snap = services.free_devices_snapshot(conn)
    _notify_group(services.build_devices_snapshot_message(
        snap["playstations"], snap["joysticks"], confirmed=True))


@bot.callback_query_handler(func=lambda c: c.data.startswith("complete:"))
def cb_complete(call):
    if not _guard_cb(call):
        return
    oid = int(call.data.split(":", 1)[1])
    try:
        o = services.complete_order(conn, oid)
    except services.OrderError as e:
        bot.answer_callback_query(call.id, str(e), show_alert=True)
        return
    bot.answer_callback_query(call.id, "Yakunlandi ☑️")
    bot.edit_message_text(format_order(o), call.message.chat.id, call.message.message_id,
                          reply_markup=order_markup(o))
    # Devices freed up -> announce availability to the group.
    snap = services.free_devices_snapshot(conn)
    _notify_group(services.build_devices_freed_message(snap["playstations"], snap["joysticks"]))


@bot.callback_query_handler(func=lambda c: c.data.startswith("cancel:"))
def cb_cancel(call):
    """Ask for Ha/Yo'q confirmation before cancelling."""
    if not _guard_cb(call):
        return
    oid = int(call.data.split(":", 1)[1])
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("Ha ✅", callback_data=f"cyes:{oid}"),
        types.InlineKeyboardButton("Yo'q ❌", callback_data=f"cno:{oid}"),
    )
    bot.edit_message_text(
        f"Buyurtma #{oid} ni rostdan bekor qilasizmi?",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=kb,
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("cyes:"))
def cb_cancel_yes(call):
    if not _guard_cb(call):
        return
    oid = int(call.data.split(":", 1)[1])
    try:
        fee = services.cancel_order(conn, oid)
    except services.OrderError as e:
        bot.answer_callback_query(call.id, str(e), show_alert=True)
        return
    o = services.get_order(conn, oid)
    bot.answer_callback_query(call.id, "Bekor qilindi")
    text = format_order(o)
    if fee:
        text += f"\n\n➕ Oylik tushumga qo'shildi: {money(fee)} so'm"
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id)
    # Cancelling an active order frees its devices -> announce availability.
    snap = services.free_devices_snapshot(conn)
    _notify_group(services.build_devices_freed_message(snap["playstations"], snap["joysticks"]))


@bot.callback_query_handler(func=lambda c: c.data.startswith("cno:"))
def cb_cancel_no(call):
    if not _guard_cb(call):
        return
    oid = int(call.data.split(":", 1)[1])
    o = services.get_order(conn, oid)
    bot.answer_callback_query(call.id, "Bekor qilinmadi")
    bot.edit_message_text(format_order(o), call.message.chat.id, call.message.message_id,
                          reply_markup=order_markup(o))


@bot.callback_query_handler(func=lambda c: c.data == "reset_yes_all")
def cb_reset_yes(call):
    """Confirmed reset — superadmin only. Saves the snapshot, then zeroes."""
    if not _guard_cb(call):
        return
    if not config.is_superadmin(call.from_user.id):
        bot.answer_callback_query(call.id, "⛔ Faqat SUPERADMIN", show_alert=True)
        return
    snap = services.reset_overall(conn, reset_by=call.from_user.id)
    bot.answer_callback_query(call.id, "Nollandi ♻️")
    bot.edit_message_text(
        "✅ Hisobot nollandi va tarixga saqlandi.\n\n"
        f"<b>Saqlangan hisobot:</b>\n{format_overall_report(snap)}",
        call.message.chat.id,
        call.message.message_id,
    )


@bot.callback_query_handler(func=lambda c: c.data == "reset_no")
def cb_reset_no(call):
    if not _guard_cb(call):
        return
    bot.answer_callback_query(call.id, "Bekor qilindi")
    bot.edit_message_text("♻️ Nollash bekor qilindi.", call.message.chat.id, call.message.message_id)


# --- device management (add / delete) --------------------------------------
@bot.callback_query_handler(func=lambda c: c.data == "devadd")
def cb_device_add(call):
    """Ask which type of device to add."""
    if not _guard_cb(call):
        return
    bot.answer_callback_query(call.id)
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("🎮 PlayStation", callback_data="devaddt:playstation"),
        types.InlineKeyboardButton("🕹 Joystik", callback_data="devaddt:joystick"),
    )
    bot.send_message(call.message.chat.id, "Qanday qurilma qo'shamiz?", reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("devaddt:"))
def cb_device_add_type(call):
    if not _guard_cb(call):
        return
    dtype = call.data.split(":", 1)[1]
    _dev_add_type[call.message.chat.id] = dtype
    bot.answer_callback_query(call.id)
    label = services.DEVICE_TYPE_LABELS.get(dtype, dtype)
    msg = bot.send_message(
        call.message.chat.id,
        f"{label} nomini kiriting (masalan: PS5 №1):",
    )
    bot.register_next_step_handler(msg, _step_device_name)


def _step_device_name(message):
    if not _guard(message):
        return
    if _intercept(message):
        _dev_add_type.pop(message.chat.id, None)
        return
    dtype = _dev_add_type.get(message.chat.id)
    if dtype is None:  # lost state (e.g. after a restart) — just show the menu
        _do_devices(message)
        return
    try:
        services.add_device(conn, name=message.text, dtype=dtype)
    except services.OrderError as e:
        msg = bot.reply_to(message, f"❌ {e}\nQaytadan nom kiriting:")
        bot.register_next_step_handler(msg, _step_device_name)
        return
    _dev_add_type.pop(message.chat.id, None)
    bot.send_message(message.chat.id, "✅ Qurilma qo'shildi.")
    _do_devices(message)


@bot.callback_query_handler(func=lambda c: c.data == "devdel")
def cb_device_delete(call):
    """Show the list of devices to delete."""
    if not _guard_cb(call):
        return
    bot.answer_callback_query(call.id)
    rows = services.list_devices(conn)
    if not rows:
        bot.send_message(call.message.chat.id, "O'chirish uchun qurilma yo'q.")
        return
    kb = types.InlineKeyboardMarkup()
    for r in rows:
        icon = "🎮" if r["dtype"] == "playstation" else "🕹"
        badge = " (🔴 band)" if r["is_busy"] else ""
        kb.add(types.InlineKeyboardButton(f"{icon} {r['name']}{badge}", callback_data=f"devdel:{r['id']}"))
    bot.send_message(call.message.chat.id, "🗑 Qaysi qurilmani o'chiramiz?", reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("devdel:"))
def cb_device_delete_pick(call):
    if not _guard_cb(call):
        return
    did = int(call.data.split(":", 1)[1])
    dev = services.get_device(conn, did)
    if dev is None:
        bot.answer_callback_query(call.id, "Qurilma topilmadi.", show_alert=True)
        return
    busy = services.device_busy_order(conn, did)
    if busy is not None:
        bot.answer_callback_query(call.id)
        bot.edit_message_text(
            f"⛔ «{esc(dev['name'])}» hozir #{busy['id']}-buyurtmada band. "
            "Uni o'chirib bo'lmaydi.",
            call.message.chat.id, call.message.message_id,
        )
        return
    bot.answer_callback_query(call.id)
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("Ha ✅", callback_data=f"devdelyes:{did}"),
        types.InlineKeyboardButton("Yo'q ❌", callback_data="devdelno"),
    )
    bot.edit_message_text(
        f"«{esc(dev['name'])}» qurilmasini o'chirasizmi?",
        call.message.chat.id, call.message.message_id, reply_markup=kb,
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("devdelyes:"))
def cb_device_delete_yes(call):
    if not _guard_cb(call):
        return
    did = int(call.data.split(":", 1)[1])
    try:
        services.delete_device(conn, did)
    except services.OrderError as e:
        bot.answer_callback_query(call.id, str(e), show_alert=True)
        return
    bot.answer_callback_query(call.id, "O'chirildi 🗑")
    bot.edit_message_text("✅ Qurilma o'chirildi.", call.message.chat.id, call.message.message_id)
    _do_devices(call.message)


@bot.callback_query_handler(func=lambda c: c.data == "devdelno")
def cb_device_delete_no(call):
    if not _guard_cb(call):
        return
    bot.answer_callback_query(call.id, "Bekor qilindi")
    bot.edit_message_text("O'chirish bekor qilindi.", call.message.chat.id, call.message.message_id)


# --- announcement send / cancel --------------------------------------------
@bot.callback_query_handler(func=lambda c: c.data == "promo_send")
def cb_promo_send(call):
    if not _guard_cb(call):
        return
    promo = _promos.get(call.message.chat.id)
    if not promo or not promo.get("message"):
        bot.answer_callback_query(call.id, "E'lon topilmadi, qaytadan boshlang.", show_alert=True)
        return
    _notify_group(promo["message"])
    _promos.pop(call.message.chat.id, None)
    bot.answer_callback_query(call.id, "Yuborildi ✅")
    bot.edit_message_text(
        "✅ E'lon guruhga yuborildi.",
        call.message.chat.id, call.message.message_id,
    )


@bot.callback_query_handler(func=lambda c: c.data == "promo_cancel")
def cb_promo_cancel(call):
    if not _guard_cb(call):
        return
    _promos.pop(call.message.chat.id, None)
    bot.answer_callback_query(call.id, "Bekor qilindi")
    bot.edit_message_text("❌ E'lon bekor qilindi.", call.message.chat.id, call.message.message_id)


# Catch-all: anything else from a non-admin is rejected; admins get the menu.
@bot.message_handler(func=lambda m: True)
def fallback(message):
    if not _guard(message):
        return
    bot.send_message(
        message.chat.id,
        "Menyudan tanlang yoki /help ni bosing.",
        reply_markup=main_menu(message.from_user.id),
    )
