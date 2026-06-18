import asyncio
import logging
from datetime import datetime

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from db import (
    get_active_users, get_daily_macros, get_daily_usage, get_weekly_stats,
    get_expiring_users, get_winback_users, get_streak_users_no_log_today,
    track_event, get_user_best_daily_protein_excl_today, get_user,
    get_users_for_notifications,
)
from utils.formatting import calc_daily_score, format_score, ai_score_comment
from utils.helpers import streak_emoji, days_ru
from keyboards import premium_keyboard
from config import STREAK_MILESTONES

log = logging.getLogger(__name__)


async def send_morning_checkins(bot: Bot):
    users = get_users_for_notifications()
    for user in users:
        uid  = user["telegram_id"]
        goal = user.get("daily_goal")
        goal_protein = user.get("protein_goal")
        streak = user.get("streak_days", 0)
        name   = (user.get("first_name") or "").split()[0] or "Привет"
        try:
            track_event(uid, "daily_active_user")
            if goal:
                goal_block = f"Цель сегодня:\n*{goal} ккал*"
                if goal_protein:
                    goal_block += f" · *{goal_protein}г белка*"
            else:
                goal_block = "Цель не задана — настрой в профиле"
            streak_line = (
                f"\n{streak_emoji(streak)} Серия *{streak} {days_ru(streak)}* подряд — держи темп!"
                if streak > 1 else ""
            )
            await bot.send_message(
                uid,
                f"☀️ *Доброе утро, {name}*\n\n"
                f"{goal_block}"
                f"{streak_line}\n\n"
                f"Отправь фото завтрака — начнём день правильно 📸",
                parse_mode="Markdown",
            )
        except Exception as e:
            log.warning(f"morning uid={uid}: {e}")
        await asyncio.sleep(0.05)


async def send_evening_summaries(bot: Bot):
    users = get_users_for_notifications()
    for user in users:
        uid  = user["telegram_id"]
        goal = user.get("daily_goal")
        goal_protein = user.get("protein_goal")
        streak = user.get("streak_days", 0)
        try:
            macros = get_daily_macros(uid)
            total  = macros["kcal"]
            if total == 0:
                name = (user.get("first_name") or "").split()[0] or "Привет"
                streak_warn = (
                    f"\n\n⚠️ Сегодня ты ещё не логировал — стрик *{streak} {days_ru(streak)}* под угрозой!"
                    if streak > 1 else ""
                )
                await bot.send_message(
                    uid,
                    f"🌙 *{name}, как прошёл день?*\n\n"
                    f"Ты ещё не добавил ни одного приёма пищи сегодня.{streak_warn}\n\n"
                    f"Отправь фото еды или напиши что ел — займёт 10 секунд 📸",
                    parse_mode="Markdown",
                )
                continue

            meals  = get_daily_usage(uid)
            score  = calc_daily_score(total, macros["protein"], macros["fat"],
                                      macros["carbs"], goal, goal_protein, meals)
            fs      = format_score(score)
            comment = ai_score_comment(score, macros["protein"], macros["carbs"], total, goal, None)

            kcal_line = f"🍽 *{total} ккал*" + (f" из {goal}" if goal else "")
            prot_line = (
                f"\n🥩 *{round(macros['protein'])}г белка*"
                + (f" из {goal_protein}г" if goal_protein else "")
                if macros["protein"] > 0 else ""
            )
            s_line = (
                f"\n🔥 Серия: *{streak} {days_ru(streak)}* подряд"
                if streak > 0 else ""
            )
            comment_line = f"\n\n{comment}" if comment else ""
            await bot.send_message(
                uid,
                f"📊 *Итоги дня*\n\n"
                f"{kcal_line}"
                f"{prot_line}"
                f"{s_line}"
                f"{comment_line}",
                parse_mode="Markdown",
            )
            # Protein daily record — only in evening report
            if macros["protein"] >= 80:
                prev_best = get_user_best_daily_protein_excl_today(uid)
                if macros["protein"] > prev_best:
                    await bot.send_message(
                        uid,
                        f"🥩 *Рекорд по белку за день: {round(macros['protein'])}г!*\n\nЛучши�� результат — так держать! 💪",
                        parse_mode="Markdown",
                    )
        except Exception as e:
            log.warning(f"evening uid={uid}: {e}")
        await asyncio.sleep(0.05)


async def send_weekly_reports(bot: Bot):
    users = get_active_users()
    for user in users:
        uid  = user["telegram_id"]
        try:
            stats = get_weekly_stats(uid)
            if stats["logged_days"] < 2:
                continue

            goal     = user.get("daily_goal", 0)
            avg_kcal    = stats["avg_kcal"] or 0
            avg_protein = stats["avg_protein"] or 0
            logged   = stats["logged_days"]
            streak   = user.get("streak_days", 0)
            name     = (user.get("first_name") or "").split()[0] or "Привет"

            forecast = ""
            if goal and avg_kcal > 0:
                diff = avg_kcal - goal
                if abs(diff) > 50:
                    kg = round(diff * 7 / 7700, 1)
                    sign = "+" if kg > 0 else ""
                    forecast = f"\n📉 Прогноз: *{sign}{kg} кг/нед.*"

            insight = (
                "🔥 Стабильная неделя — отличная работа!"
                if logged >= 5
                else "💪 Хороший старт — логируй каждый день."
                if logged >= 3
                else "🌱 Ещё немного практики — привычка закрепится!"
            )
            streak_line = (
                f"\n🔥 Серия: *{streak} {days_ru(streak)}* — не останавливайся!"
                if streak > 1 else ""
            )
            await bot.send_message(
                uid,
                f"📊 *Итоги недели, {name}*\n\n"
                f"🍽 Среднее: *{avg_kcal} ккал/день*\n"
                f"🥩 Белок: *{avg_protein} г/день*\n"
                f"📅 Залогировано: *{logged}/7 дней*"
                f"{forecast}"
                f"{streak_line}\n\n"
                f"💡 {insight}",
                parse_mode="Markdown",
            )
        except Exception as e:
            log.warning(f"weekly uid={uid}: {e}")
        await asyncio.sleep(0.05)


async def send_expiry_reminders(bot: Bot):
    for days_left in (3, 1):
        for user in get_expiring_users(days_left):
            uid  = user["telegram_id"]
            name = (user.get("first_name") or "").split()[0] or "Привет"
            exp  = user["expires_at"][:10]
            try:
                exp_fmt = datetime.strptime(exp, "%Y-%m-%d").strftime("%d.%m.%Y")
            except Exception:
                exp_fmt = exp
            when = "завтра" if days_left == 1 else "через 3 дня"
            msg = (
                f"*{name}, подписка истекает {when}*\n\n"
                f"Дата окончания: *{exp_fmt}*\n\n"
                f"Продли сейчас — стрик и история сохранятся.\n\n"
                f"Тарифы:\n"
                f"• 1 мес — 150 ⭐ (~7 ₽/день)\n"
                f"• 3 мес — 360 ⭐ (~6 ₽/день)\n"
                f"• 12 мес — 990 ⭐ (~4 ₽/день)"
            )
            try:
                await bot.send_message(uid, msg, parse_mode="Markdown",
                                       reply_markup=premium_keyboard())
            except Exception as e:
                log.warning(f"expiry reminder uid={uid}: {e}")
            await asyncio.sleep(0.05)


async def send_winback_messages(bot: Bot):
    for user in get_winback_users():
        uid    = user["telegram_id"]
        name   = (user.get("first_name") or "").split()[0] or "Привет"
        streak = user.get("streak_days", 0)
        streak_line = (
            f"\n🔥 У тебя был стрик *{streak} {days_ru(streak)}* — не дай ему пропасть!"
            if streak > 2 else ""
        )
        try:
            await bot.send_message(
                uid,
                f"���� *{name}, скучаем по тебе!*\n\n"
                f"Прошло 3 дня с окончания подписки."
                f"{streak_line}\n\n"
                f"Возвращайся — продолжи следить за питанием и прогрессом! 💪",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="⭐ Возобновить подписку",
                                         callback_data="show_premium"),
                ]]),
            )
        except Exception as e:
            log.warning(f"winback uid={uid}: {e}")
        await asyncio.sleep(0.05)


async def send_streak_reminders(bot: Bot):
    for user in get_streak_users_no_log_today():
        uid  = user["telegram_id"]

        # Real-time guard: the batch query may have run before the user
        # scanned, or the bot may have restarted and re-fired the job.
        # get_daily_usage always hits the DB fresh — skip if already logged.
        if get_daily_usage(uid) > 0:
            log.debug(f"streak reminder skipped {uid}: already has logs today")
            continue

        # Fetch fresh user row — same source as the Profile button —
        # so streak_days is never stale from the batch query.
        fresh  = get_user(uid) or user
        streak = fresh.get("streak_days", 0)
        name   = (fresh.get("first_name") or "").split()[0] or "Привет"
        try:
            await bot.send_message(
                uid,
                f"🔥 *{name}, не прерывай серию!*\n\n"
                f"Ты на *{streak} {days_ru(streak)}* подряд — "
                f"сегодня ещё нет записей.\n\n"
                f"📸 Сфотографируй ужин или введи что ел — займёт 10 секунд!",
                parse_mode="Markdown",
            )
        except Exception as e:
            log.warning(f"streak reminder uid={uid}: {e}")
        await asyncio.sleep(0.05)
