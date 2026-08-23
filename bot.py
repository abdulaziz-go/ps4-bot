"""Telegram layer (pyTelegramBotAPI) — thin handlers over ``services``.

Every handler is admin-gated. All business logic lives in ``services`` so this
file only deals with UI: messages, keyboards and callback routing.

The whole bot is driven from a persistent reply-keyboard menu (see
``main_menu``); the equivalent slash commands are kept working as aliases.
"""
import html
from datetime import datetime

import telebot
from telebot import types

import config
import database
import services

bot = telebot.TeleBot(config.BOT_TOKEN, parse_mode="HTML")

# Shared connection + schema bootstrap (creates the DB on first run).
conn = database.init_db()

# In-memory per-chat draft state during order creation.
_drafts: dict[int, dict] = {}

TYPE_LABELS = {"kunduzgi": "🌞 Kunduzgi", "tungi": "🌙 Tungi"}
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


def _guard(message) -> bool:
    """Reject non-admin messages. Returns True if allowed."""
    if not config.is_admin(message.from_user.id):
        bot.reply_to(message, "⛔ Kechirasiz, sizda ruxsat yo'q.")
        return False
    return True


def _guard_cb(call) -> bool:
    if not config.is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "⛔ Ruxsat yo'q", show_alert=True)
        return False
    return True


def main_menu(user_id: int) -> types.ReplyKeyboardMarkup:
    """Persistent bottom keyboard; the reset button is superadmin-only."""
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(BTN_NEW)
    kb.row(BTN_LIST, BTN_HISTORY)
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
    lines.append(f"💰 Summa: {money(o['amount'])} so'm")
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
    bot.send_message(message.chat.id, "Buyurtma turini tanlang:", reply_markup=kb)


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

    try:
        oid = services.create_order(
            conn,
            customer_name=draft.get("customer_name", ""),
            order_type=draft.get("order_type", "kunduzgi"),
            amount=amount,
            phone=draft.get("phone"),
            created_by=message.from_user.id,
        )
    except services.OrderError as e:
        bot.reply_to(message, f"❌ {e}")
        return

    _drafts.pop(message.chat.id, None)
    o = services.get_order(conn, oid)
    bot.send_message(message.chat.id, format_order(o), reply_markup=order_markup(o))


def _do_list(message):
    rows = services.list_orders(conn, statuses=["new", "confirmed"])
    if not rows:
        bot.reply_to(message, "Aktiv buyurtmalar yo'q.")
        return
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
        if o["status"] == "cancelled" and o["cancel_fee"]:
            line += f" (jarima {money(o['cancel_fee'])})"
        lines.append(line)
    bot.send_message(message.chat.id, "\n".join(lines))


def _do_report(message):
    now = datetime.now()
    r = services.monthly_report(conn, now.year, now.month)
    bot.reply_to(message, format_report(r))


def _do_reset_history(message):
    rows = services.list_resets(conn, limit=10)
    if not rows:
        bot.reply_to(message, "🗂 Hisobot tarixi bo'sh. Hali nollash amalga oshirilmagan.")
        return
    parts = ["🗂 <b>Nollangan hisobotlar tarixi</b> (oxirgi 10):"]
    for row in rows:
        parts.append(f"\n🕒 <i>{esc(row['reset_at'])}</i>\n{format_report(row)}")
    bot.send_message(message.chat.id, "\n".join(parts))


def _do_reset(message):
    """Superadmin-only: show the current report and ask for Ha/Yo'q first."""
    if not config.is_superadmin(message.from_user.id):
        bot.reply_to(message, "⛔ Bu amal faqat SUPERADMIN uchun.")
        return
    now = datetime.now()
    r = services.monthly_report(conn, now.year, now.month)
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("Ha, nollash ✅", callback_data=f"reset_yes:{now.year}:{now.month}"),
        types.InlineKeyboardButton("Yo'q ❌", callback_data="reset_no"),
    )
    bot.send_message(
        message.chat.id,
        "♻️ <b>Hisobotni nollash</b>\n\n"
        "Joriy hisobot tarixga saqlanadi va nolga tushiriladi.\n"
        "Buyurtmalar o'chirilmaydi.\n\n"
        f"{format_report(r)}\n\n"
        "Davom etamizmi?",
        reply_markup=kb,
    )


# Maps a menu button label to its action. Defined after the `_do_*` functions.
MENU_ROUTES = {
    BTN_NEW: _do_new,
    BTN_LIST: _do_list,
    BTN_HISTORY: _do_history,
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
        f"Tur: {TYPE_LABELS[order_type]}\n💰 Summani kiriting (so'm):",
        call.message.chat.id,
        call.message.message_id,
    )
    bot.register_next_step_handler(call.message, _step_amount)


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


@bot.callback_query_handler(func=lambda c: c.data.startswith("cno:"))
def cb_cancel_no(call):
    if not _guard_cb(call):
        return
    oid = int(call.data.split(":", 1)[1])
    o = services.get_order(conn, oid)
    bot.answer_callback_query(call.id, "Bekor qilinmadi")
    bot.edit_message_text(format_order(o), call.message.chat.id, call.message.message_id,
                          reply_markup=order_markup(o))


@bot.callback_query_handler(func=lambda c: c.data.startswith("reset_yes:"))
def cb_reset_yes(call):
    """Confirmed reset — superadmin only. Saves the snapshot, then zeroes."""
    if not _guard_cb(call):
        return
    if not config.is_superadmin(call.from_user.id):
        bot.answer_callback_query(call.id, "⛔ Faqat SUPERADMIN", show_alert=True)
        return
    _, year, month = call.data.split(":")
    snap = services.reset_report(conn, int(year), int(month), reset_by=call.from_user.id)
    bot.answer_callback_query(call.id, "Nollandi ♻️")
    bot.edit_message_text(
        "✅ Hisobot nollandi va tarixga saqlandi.\n\n"
        f"<b>Saqlangan hisobot:</b>\n{format_report(snap)}",
        call.message.chat.id,
        call.message.message_id,
    )


@bot.callback_query_handler(func=lambda c: c.data == "reset_no")
def cb_reset_no(call):
    if not _guard_cb(call):
        return
    bot.answer_callback_query(call.id, "Bekor qilindi")
    bot.edit_message_text("♻️ Nollash bekor qilindi.", call.message.chat.id, call.message.message_id)


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
