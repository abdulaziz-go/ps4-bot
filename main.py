"""Entry point: bootstrap the database, then start long-polling.

    python main.py

The database is created automatically. BOT_TOKEN and ADMIN_IDS must be set in
the environment (see .env.example / README.md).
"""
import sys

import config
import database


def main() -> None:
    # Show the loaded environment first so a mis-set .env is obvious at a glance.
    print(config.env_summary())

    # Always ensure the DB exists first, even if the token is missing.
    database.init_db()
    print(f"✅ Ma'lumotlar bazasi tayyor: {config.DB_PATH}")

    if not config.BOT_TOKEN:
        print("❌ BOT_TOKEN o'rnatilmagan.")
        print("   export BOT_TOKEN=<telegram-bot-token>")
        print("   export ADMIN_IDS=<sizning-telegram-id>")
        sys.exit(1)

    if not config.ADMIN_IDS and not config.SUPERADMIN_IDS:
        print("⚠️  ADMIN_IDS bo'sh — hech kim botdan foydalana olmaydi.")
        print("   export ADMIN_IDS=<sizning-telegram-id>")
        print("   export SUPERADMIN_IDS=<hisobotni nollash uchun>")

    # Import here so the DB bootstrap above runs before touching Telegram.
    import bot as bot_module

    print("🤖 Bot ishga tushdi. To'xtatish uchun Ctrl+C.")
    bot_module.bot.infinity_polling(skip_pending=True)


if __name__ == "__main__":
    main()
