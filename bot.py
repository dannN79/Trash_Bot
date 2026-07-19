import asyncio
import logging
import os
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from config import BOT_TOKEN
from db.database import engine, AsyncSessionLocal
from db.models import Base, Ban, Channel

from handlers import start, posts, admin, errors, notifications

logging.basicConfig(level=logging.INFO)

from sqlalchemy import text, select
from utils import utcnow, get_channel_title


async def on_startup():
    # Шаг 1: сброс (если RESET_DB=1) в отдельной транзакции
    if os.getenv("RESET_DB") == "1":
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        print("⚠️  RESET_DB=1: все таблицы удалены.")

    # Шаг 2: создание всех таблиц в отдельной транзакции
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Шаг 3: каждая миграция в своей транзакции — ошибка одной не откатывает остальные
    migrations = [
        "ALTER TABLE channels ADD COLUMN timezone VARCHAR(50) DEFAULT 'Europe/Moscow'",
        "ALTER TABLE channel_users ADD COLUMN edits_today INTEGER DEFAULT 0",
        "ALTER TABLE channel_users ADD COLUMN edits_today_date DATETIME",
        "ALTER TABLE posts ADD COLUMN media_type VARCHAR(20) DEFAULT 'text'",
        "ALTER TABLE notifications ADD COLUMN accepted_by BIGINT",
        "ALTER TABLE bans ADD COLUMN notified_at DATETIME",
    ]
    for sql in migrations:
        try:
            async with engine.begin() as conn:
                await conn.execute(text(sql))
        except Exception:
            pass

    print("База данных инициализирована.")


async def on_shutdown():
    await engine.dispose()
    print("Бот остановлен.")


async def ban_expiry_notifier(bot: Bot):
    """
    Фоновая задача: каждую минуту ищет истёкшие баны без уведомления
    и отправляет пользователям сообщение о разблокировке.
    """
    await asyncio.sleep(10)
    while True:
        try:
            now = utcnow()
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(Ban).where(
                        Ban.ban_until <= now,
                        Ban.notified_at.is_(None),
                    )
                )
                expired_bans = result.scalars().all()

                for ban in expired_bans:
                    channel_title = await get_channel_title(session, ban.channel_id)
                    ban.notified_at = now
                    try:
                        await bot.send_message(
                            chat_id=ban.user_id,
                            text=(
                                f"🔓 <b>Ваш бан в канале «{channel_title}» истёк.</b>\n\n"
                                "Вы снова можете публиковать сообщения."
                            ),
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass

                if expired_bans:
                    await session.commit()
        except Exception as e:
            logging.warning(f"ban_expiry_notifier error: {e}")

        await asyncio.sleep(60)


from aiogram.types import BotCommand


async def set_commands(bot: Bot):
    commands = [
        BotCommand(command="start", description="Запустить бота / Сбросить состояние"),
        BotCommand(command="menu", description="Показать главное меню"),
        BotCommand(command="channels", description="Управление каналами"),
    ]
    await bot.set_my_commands(commands)


async def health_handler(request):
    return web.Response(text="OK")


async def run_health_server():
    port = int(os.getenv("PORT", 80))
    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"Health server listening on port {port}")


async def main():
    bot = Bot(token=BOT_TOKEN)
    await set_commands(bot)
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    dp.include_router(start.router)
    dp.include_router(posts.router)
    dp.include_router(admin.router)
    dp.include_router(notifications.router)
    dp.include_router(errors.router)

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    await run_health_server()

    print("Бот запущен...")
    asyncio.create_task(ban_expiry_notifier(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
