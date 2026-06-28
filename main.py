import asyncio
  import os
  import sys
  import logging
  from datetime import datetime, timezone
  from aiohttp import web

  from aiogram import Bot, Dispatcher
  from apscheduler.schedulers.asyncio import AsyncIOScheduler

  sys.path.insert(0, os.path.dirname(__file__))

  from config import BOT_TOKEN
  from db import init_db, reset_stale_streaks
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


  async def _nightly_streak_reset():
      """Nightly job: reset streak_days to 0 for users who missed yesterday.

      Ensures the profile shows the correct (broken) streak immediately —
      without waiting for the user to log something.
      """
      count = reset_stale_streaks()
      log.info("Nightly streak reset: %d users reset to 0", count)


  async def _fire_missed_notifications(bot: Bot):
      """Send notifications that APScheduler can't recover after a fresh start.

      APScheduler uses an in-memory job store — on every fresh start it computes
      the NEXT fire time, so jobs whose window already passed today are skipped
      until tomorrow.  We compensate by checking the current UTC hour at startup
      and firing the missed job once immediately.

      Windows (UTC):
        morning  05:00-10:59  →  send_morning_checkins
        streak   16:00-21:59  →  send_streak_reminders  (get_daily_usage guard prevents duplicates)
        evening  17:00-21:59  →  send_evening_summaries
      """
      now_utc = datetime.now(timezone.utc)
      h = now_utc.hour
      if 5 <= h < 11:
          log.info("Startup: morning window (UTC %02d:%02d) — firing missed morning notifications", h, now_utc.minute)
          await send_morning_checkins(bot)
      elif 16 <= h < 22:
          log.info("Startup: streak/evening window (UTC %02d:%02d) — firing missed streak reminders", h, now_utc.minute)
          await send_streak_reminders(bot)
          if h >= 17:
              await send_evening_summaries(bot)
      else:
          log.info("Startup at UTC %02d:%02d — no missed notification window", h, now_utc.minute)


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

      # misfire_grace_time: if bot restarts after scheduled time,
      # the job fires immediately if within the grace window (seconds)
      scheduler = AsyncIOScheduler(timezone="UTC")
      scheduler.add_job(send_morning_checkins,  "cron", hour=5,  minute=0,  args=[bot], misfire_grace_time=7200, coalesce=True)
      scheduler.add_job(send_evening_summaries, "cron", hour=17, minute=0,  args=[bot], misfire_grace_time=7200, coalesce=True)
      scheduler.add_job(send_weekly_reports,    "cron", day_of_week="mon", hour=4, minute=0, args=[bot], misfire_grace_time=7200, coalesce=True)
      scheduler.add_job(send_expiry_reminders,  "cron", hour=4,  minute=30, args=[bot], misfire_grace_time=3600, coalesce=True)
      scheduler.add_job(send_winback_messages,  "cron", hour=4,  minute=45, args=[bot], misfire_grace_time=3600, coalesce=True)
      # Streak reminders: primary at 16:30 UTC (19:30 MSK), backup at 19:00 UTC (22:00 MSK).
      # get_daily_usage guard inside send_streak_reminders prevents duplicates.
      scheduler.add_job(send_streak_reminders,  "cron", hour=16, minute=30, args=[bot], misfire_grace_time=7200, coalesce=True)
      scheduler.add_job(send_streak_reminders,  "cron", hour=19, minute=0,  args=[bot], misfire_grace_time=7200, coalesce=True)
      # Nightly streak reset at 02:30 UTC: sets streak_days=0 for users who missed yesterday,
      # so profiles show the correct value without waiting for the next log.
      scheduler.add_job(_nightly_streak_reset,  "cron", hour=2,  minute=30, misfire_grace_time=7200, coalesce=True)
      scheduler.start()

      await run_health_server()

      # Fire any notification whose daily window was already open when bot started
      asyncio.ensure_future(_fire_missed_notifications(bot))

      log.info("Bot started. Polling...")
      await dp.start_polling(bot, drop_pending_updates=True)


  if __name__ == "__main__":
      asyncio.run(main())
  