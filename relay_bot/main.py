import asyncio
import logging

from aiohttp import web
from aiogram import Bot, Dispatcher

from .api import create_app
from .settings import load_settings
from .store import RelayStore
from .telegram import delivery_loop, router


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = load_settings()
    store = RelayStore(settings.database_path)
    store.initialize()

    bot = Bot(token=settings.bot_token)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    dispatcher["settings"] = settings
    dispatcher["store"] = store

    app = create_app(settings, store, bot)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, settings.api_host, settings.api_port)
    await site.start()
    logging.getLogger(__name__).info(
        "Relay API listening on %s:%s; keep it behind an HTTPS reverse proxy",
        settings.api_host,
        settings.api_port,
    )

    delivery_task = asyncio.create_task(delivery_loop(bot, settings, store))
    try:
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        delivery_task.cancel()
        await asyncio.gather(delivery_task, return_exceptions=True)
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
