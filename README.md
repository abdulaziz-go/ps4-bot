# Buyurtmalar boti (Telegram order bot)

Admin-only Telegram bot for tracking service orders
(kunduzgi / tungi / bir kun to'liq), each with a scheduled start/end time so
many orders can be managed as a queue — confirming and completing them,
cancelling with a fee, and producing a monthly revenue report split **60 / 40**.

## Features

- **Admin-only.** Only whitelisted Telegram user IDs (`ADMIN_IDS` /
  `SUPERADMIN_IDS`) may interact; everyone else is fully locked out.
- **Button menu.** Everything is driven from a persistent bottom keyboard
  (`➕ Yangi buyurtma`, `📋 Buyurtmalar`, `📚 Tarix`, `📊 Hisobot`,
  `🗂 Hisobot tarixi`, and `♻️ Hisobotni nollash` for superadmins). The old
  slash commands still work as aliases.
- **Order creation** collects a **client name**, an **optional phone number**
  (send `-` to skip), the **order type** (🌞 Kunduzgi / 🌙 Tungi /
  🌗 Bir kun to'liq), the **scheduled start and end date-time**, and the amount.
- **Scheduling & queue.** Every order has an exact start/end date-time (e.g.
  `2026-08-25 09:00`; `25.08.2026 09:00` also accepted). The active list
  (`📋 Buyurtmalar`) is shown as a **queue ordered by start time**. When a new
  order's window overlaps an existing active one, the bot **warns but still
  creates it** (it lists the clashing orders).
- **Superadmin notifications.** Whenever anyone creates a new order, every
  `SUPERADMIN_IDS` user is pushed a `🔔 Yangi buyurtma yaratildi` message with
  who created it and the full order details (including any queue clash). Delivery
  is best-effort — a superadmin who hasn't started the bot is skipped silently.
- **Order lifecycle** via inline buttons:
  `🆕 Yangi → ✅ Tasdiqlash → ☑️ Yakunlash` (with `❌ Bekor qilish` and a
  `Ha / Yo'q` confirmation step).
- **Device inventory** (`🎮 Qurilmalar`). PlayStations and joysticks are added by
  name and shown grouped by type with `🟢 Bo'sh` / `🔴 Band` status and per-type
  totals. A device is **busy** whenever it is linked to an active (`new` /
  `confirmed`) order and frees automatically when that order is completed or
  cancelled (status is always derived, never stored). Busy devices can't be
  deleted. Order creation now also asks which **PlayStation** and which
  **joysticks** were handed out, and every order card / history line shows them.
- **Group announcements.** The bot is **silent in every group / supergroup /
  channel** — it never replies to group messages. It only *pushes* customer-facing
  device-availability announcements (HTML-formatted, Uzbek, no client data) to
  `GROUP_CHAT_ID`: a snapshot of still-free devices when an order is created and
  when it is confirmed, and a "devices freed up" notice when an order is completed
  or an active order is cancelled. Group sends are best-effort — a failure never
  breaks the order flow.
- **Discount announcements** (`📣 E'lon (Aksiya)`). Compose a customer-facing
  promo: the bot asks for the aksiya **period as free text** (e.g. "Dushanba va
  Seshanba kunlari"), a discounted **fixed price**, and your **own message
  text**, then shows a preview with a `✅ Guruhga yuborish` button. Nothing is
  posted until you tap it — then the HTML-formatted announcement goes to
  `GROUP_CHAT_ID`.
- **Order history** (`📚 Tarix`): the last 20 completed / cancelled orders.
- **Cancellation fee** added to monthly revenue when a *confirmed* order is
  cancelled. The per-type fee is configured in `config.CANCEL_FEES`
  (`bir kun to'liq` has **no** cancellation fee).
- **Monthly report** (`📊 Hisobot`): completed amounts + cancellation fees, with a
  60 / 40 split (exact integer math).
- **Reset report (superadmin).** `♻️ Hisobotni nollash` shows the current report
  and asks for `Ha / Yo'q` confirmation. On confirm, the current income and
  counts are **saved to history** and the running total is reset to **0** —
  **no orders are deleted**. Past snapshots are viewable via `🗂 Hisobot tarixi`,
  where each reset is shown as a **separate numbered card** with a divider
  between entries (so stacked snapshots no longer blur together).
- **SQLite storage**, created automatically on first run.

## Setup & run

```bash
cd /Users/abdulaziz/GolandProjects/tg-bot

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export BOT_TOKEN="123456:your-token-from-BotFather"
export ADMIN_IDS="123456789"        # your Telegram user id (comma-separated for several)
export SUPERADMIN_IDS="123456789"   # who may reset the report (comma-separated)

python main.py
```

The database (`bot.db`) is created automatically. Stop the bot with `Ctrl+C`.

## Menu & commands (in Telegram)

Press `/start`, then use the buttons. Each button has a slash-command alias:

| Button                 | Command        | Description                          |
| ---------------------- | -------------- | ------------------------------------ |
| `➕ Yangi buyurtma`     | `/yangi`       | Create a new order (name + phone)    |
| `📋 Buyurtmalar`       | `/buyurtmalar` | List active orders                   |
| `📚 Tarix`             | `/tarix`       | Order history (last 20)              |
| `🎮 Qurilmalar`        | `/qurilmalar`  | Device inventory (PlayStation/joystik) |
| `📣 E'lon (Aksiya)`    | `/elon`        | Compose & send a discount announcement |
| `📊 Hisobot`           | `/hisobot`     | Current month's report               |
| `🗂 Hisobot tarixi`    | —              | Saved report snapshots (resets)      |
| `♻️ Hisobotni nollash` | `/nollash`     | Reset the report (superadmin only)   |
| —                      | `/help`        | Help                                 |

## Tests

```bash
source .venv/bin/activate
pip install -r requirements.txt   # for pytest
pytest -q
```

Tests exercise the service/DB layer directly (no Telegram, no network) and
cover DB auto-creation and migration of an older DB, the full happy path with
the 60/40 split, the configured cancellation fees, all three order types,
date-time parsing, start/end scheduling with overlap (queue-clash) detection,
admin/superadmin roles, the optional phone field, the state-machine guards, and
the report reset + saved history.

## Project layout

```
tg-bot/
├── main.py              # entry point: init DB, start polling
├── config.py            # env config + business constants (fees, split, admins)
├── database.py          # SQLite connection + schema bootstrap
├── services.py          # business logic (create/confirm/complete/cancel/report)
├── bot.py               # pyTelegramBotAPI handlers (thin UI layer)
├── conftest.py          # test import path
├── tests/
│   └── test_services.py # unit tests for all acceptance criteria
├── requirements.txt
├── .env.example
└── README.md
```

## Configuration reference

| Env var           | Default        | Meaning                                     |
| ----------------- | -------------- | ------------------------------------------- |
| `BOT_TOKEN`       | *(required)*   | Telegram bot token from @BotFather          |
| `ADMIN_IDS`       | *(empty)*      | Comma-separated allowed Telegram user IDs   |
| `SUPERADMIN_IDS`  | *(empty)*      | IDs allowed to reset the report (also admins) |
| `GROUP_CHAT_ID`   | `-1004439378633` | Group/supergroup ID for device-availability announcements (int) |
| `DB_PATH`         | `bot.db`       | SQLite file path                            |
| `SPLIT_A_LABEL`   | `Firma (60%)`  | Label for the 60% share in reports          |
| `SPLIT_B_LABEL`   | `Xodim (40%)`  | Label for the 40% share in reports          |
```
