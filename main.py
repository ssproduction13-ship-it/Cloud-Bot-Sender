import asyncio
import os
import sys
import logging
from aiohttp import web

from aiogram import Bot, Dispatcher
from apscheduler.schedulers.asyncio import AsyncIOScheduler

sys.path.insert(0, os.path.dirname(__file__))

from config import BOT_TOKEN
from db import init_db
from services.notifications import (
    send_morning_checkins,
    send_evening_summaries,
    send_weekly_reports,
    send_expiry_reminders,
    send_winback_messages,
    send_streak_reminders,
)
from handlers import onboarding, nutrition, premium, referrals, profile, admin, progress

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


async def health(request):
    return web.Response(text="OK")


async def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info(f"Health server running on port {port}")


async def main():
    init_db()
    bot = Bot(token=BOT_TOKEN)
    dp  = Dispatcher()

    # Router registration order matters:
    # - Specific command/callback routers first
    # - F.text catch-all (nutrition) MUST be last
    dp.include_router(onboarding.router)
    dp.include_router(admin.router)
    dp.include_router(profile.router)
    dp.include_router(premium.router)
    dp.include_router(referrals.router)
    dp.include_router(progress.router)
    dp.include_router(nutrition.router)   # F.photo + F.text catch-all — LAST

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(send_morning_checkins,  "cron", hour=3,  minute=0,  args=[bot])
    scheduler.add_job(send_evening_summaries, "cron", hour=17, minute=0,  args=[bot])
    scheduler.add_job(send_weekly_reports,    "cron", day_of_week="mon", hour=4, minute=0, args=[bot])
    scheduler.add_job(send_expiry_reminders,  "cron", hour=4,  minute=30, args=[bot])
    scheduler.add_job(send_winback_messages,  "cron", hour=4,  minute=45, args=[bot])
    scheduler.add_job(send_streak_reminders,  "cron", hour=16, minute=30, args=[bot])
    scheduler.start()

    await run_health_server()

    log.info("Bot started. Polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
