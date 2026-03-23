"""Одноразовая настройка Telethon-сессии для автоотправки в Буквицу.

Запусти один раз:
    python setup_telegram.py

Введи номер телефона → код из SMS → готово.
Сессия сохранится в файл zoomhub.session.
"""

import asyncio
from telethon import TelegramClient
from dotenv import load_dotenv
import os

load_dotenv()

API_ID = int(os.environ.get("TELEGRAM_API_ID") or "0")
API_HASH = os.environ.get("TELEGRAM_API_HASH", "")
SESSION_FILE = "zoomhub"


async def main():
    if not API_ID or not API_HASH:
        print("❌ Заполни TELEGRAM_API_ID и TELEGRAM_API_HASH в .env")
        return

    print("🔐 Настройка Telegram-сессии для ZoomHub\n")

    client = TelegramClient(SESSION_FILE, API_ID, API_HASH)
    await client.start()

    me = await client.get_me()
    print(f"\n✅ Авторизован как: {me.first_name} ({me.phone})")

    # Проверяем что бот Буквицы доступен
    bot_username = os.environ.get("BUKVITSA_BOT_USERNAME", "bykvitsa")
    try:
        bot = await client.get_entity(bot_username)
        print(f"✅ Бот @{bot_username} найден: {bot.first_name}")
    except Exception as e:
        print(f"⚠️  Бот @{bot_username} не найден: {e}")
        print("   Проверь BUKVITSA_BOT_USERNAME в .env")

    print(f"\n📁 Сессия сохранена: {SESSION_FILE}.session")
    print("   Теперь ZoomHub может автоматически отправлять файлы в Буквицу.")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
