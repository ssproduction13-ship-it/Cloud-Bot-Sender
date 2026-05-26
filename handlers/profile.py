import logging
from datetime import datetime, timezone

from aiogram import Router, F, Bot
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

from config import ADMIN_ID, BETA_DAILY_LIMIT
from db import (
    get_user, get_daily_usage, get_daily_macros, get_weekly_stats,
    get_referral_stats, get_weight_history, add_weight_log,
    check_subscription_expired, clear_onboard_state, track_event,
)
from keyboards import main_keyboard, profile_keyboard, premium_keyboard
from services.state import user_states, _set_state
from services.ai_service import openai_client
from utils.helpers import streak_emoji
from config import STATES

log = logging.getLogger(__name__)
router = Router()

_utcnow = lambda: datetime.now(timezone.utc).replace(tzinfo=None)


async def _send_status(send_fn, uid: int, user: dict):
    status = user["status"]
    used   = get_daily_usage(uid)
    macros = get_daily_macros(uid)
    total  = macros["kcal"]
    ref_s  = get_referral_stats(uid)
    goal   = user["daily_goal"]
    streak = user.get("streak_days", 0)
    best_streak = user.get("best_streak", 0)
    kcal_str = f"{total}/{goal}" if goal else str(total)

    streak_block = (
        f"\n{streak_emoji(streak)} Серия: *{streak}* дн.  |  Рекорд: *{best_streak}*"
        if streak > 0 or best_streak > 0 else ""
    )

    if status == "paid" and not check_subscription_expired(uid):
        exp_dt = datetime.fromisoformat(user["expires_at"])
        exp    = exp_dt.strftime("%d.%m.%Y")
        dl     = max((exp_dt - _utcnow()).days, 0)
        await send_fn(
            f"💎 *Подписка активна*\n"
            f"📅 До {exp} — осталось *{dl} дн.*\n"
            f"📸 Анализов сегодня: {used}\n"
            f"🔥 Ккал: {kcal_str}\n"
            f"👥 Рефералов: {ref_s['total']} (оплатили: {ref_s['paid']})"
            f"{streak_block}",
            parse_mode="Markdown",
        )
    elif status == "beta" and user.get("trial_expires_at"):
        trial_dt  = datetime.fromisoformat(user["trial_expires_at"])
        trial_exp = trial_dt.strftime("%d.%m.%Y")
        dl = max((trial_dt - _utcnow()).days, 0)
        await send_fn(
            f"🎁 *Пробный период*\n"
            f"📅 До {trial_exp} — осталось *{dl} дн.*\n"
            f"📊 Анализов: {used}/{BETA_DAILY_LIMIT}\n"
            f"🔥 Ккал: {kcal_str}\n"
            f"👥 Рефералов: {ref_s['total']} (оплатили: {ref_s['paid']})"
            f"{streak_block}",
            parse_mode="Markdown",
        )
    else:
        await send_fn(
            f"⏰ *Доступ закончился*\n"
            f"📸 Анализов сегодня: {used}\n"
            f"🔥 Ккал: {kcal_str}\n"
            f"👥 Рефералов: {ref_s['total']} (оплатили: {ref_s['paid']})"
            f"{streak_block}\n\n"
            f"Оформи подписку — ⭐ Premium",
            parse_mode="Markdown",
        )


@router.callback_query(F.data == "weight_opt")
async def cb_weight_opt(callback: CallbackQuery):
    uid  = callback.from_user.id
    await callback.answer()
    user = get_user(uid)
    last_weight = f"  (последний: *{user['weight_kg']} кг*)" if user and user.get("weight_kg") else ""
    await callback.message.answer(
        f"⚖️ *Хочешь записать вес сегодня?*{last_weight}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Да",          callback_data="profile_weight"),
            InlineKeyboardButton(text="⏭ Не сегодня",  callback_data="weight_skip"),
        ]]),
    )


@router.callback_query(F.data == "weight_skip")
async def cb_weight_skip(callback: CallbackQuery):
    await callback.answer("Ок, в следующий раз 👍")
    try:
        await callback.message.delete()
    except Exception:
        pass


@router.callback_query(F.data == "profile_weight")
async def cb_profile_weight(callback: CallbackQuery):
    uid = callback.from_user.id
    await callback.answer()
    _set_state(uid, STATES["WEIGHT_LOG"])
    await callback.message.answer(
        "⚖️ Введи свой текущий вес в кг:\n_(например: 75 или 75.5)_",
        parse_mode="Markdown",
    )


@router.callback_query(F.data == "show_meal_plan")
async def cb_show_meal_plan(callback: CallbackQuery):
    uid  = callback.from_user.id
    user = get_user(uid)
    await callback.answer()
    if not user:
        return
    if user.get("status") != "paid" or check_subscription_expired(user):
        await callback.message.answer(
            "🍽 *AI-план питания* — Premium функция\n\nОформи подписку, чтобы получать персональный план.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⭐ Открыть Premium", callback_data="show_premium"),
            ]]),
        )
        return
    if not user.get("daily_goal"):
        await callback.message.answer(
            "⚙️ Сначала настрой профиль — нажми *Пересчитать норму*.",
            parse_mode="Markdown",
        )
        return

    thinking_msg = await callback.message.answer("🤔 *Составляю план питания...*", parse_mode="Markdown")
    try:
        goal_kcal   = user.get("daily_goal", 2000)
        protein_g   = user.get("protein_goal", 150)
        goal_type   = user.get("goal_type", "maintain")
        gender      = user.get("gender", "male")
        goal_labels = {"lose": "похудение", "maintain": "поддержание", "gain": "набор массы"}
        plan_prompt = (
            f"Составь персональный план питания на один день.\n"
            f"Параметры: цель={goal_labels.get(goal_type,'поддержание')}, "
            f"норма={goal_kcal} ккал, белок={protein_g}г, "
            f"пол={'мужской' if gender=='male' else 'женский'}.\n"
            f"Формат: 4 приёма пищи (завтрак, обед, перекус, ужин).\n"
            f"Для каждого: название + калории + КБЖУ. В конце итого. "
            f"Кратко, конкретно. Только реальные блюда."
        )
        resp = await openai_client.chat.completions.create(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            messages=[
                {"role": "system", "content": "Профессиональный нутрициолог. Практичные планы питания."},
                {"role": "user",   "content": plan_prompt},
            ],
            max_tokens=700,
        )
        plan_text = resp.choices[0].message.content or ""
        try:
            await thinking_msg.delete()
        except Exception:
            pass
        await callback.message.answer(
            f"🍽 *Твой план питания на сегодня*\n\n{plan_text}",
            parse_mode="Markdown",
        )
    except Exception as plan_e:
        log.error(f"meal plan error: {plan_e}")
        try:
            await thinking_msg.delete()
        except Exception:
            pass
        await callback.message.answer("Ошибка при составлении плана. Попробуй позже.")


@router.callback_query(F.data == "profile_week")
async def cb_profile_week(callback: CallbackQuery):
    uid = callback.from_user.id
    await callback.answer()
    stats = get_weekly_stats(uid)
    if stats["logged_days"] == 0:
        await callback.message.answer(
            "📊 *Ещё нет данных за неделю*\n\nНачни сегодня — и через 7 дней увидишь прогресс 💪",
            parse_mode="Markdown",
        )
        return

    user  = get_user(uid)
    goal  = user.get("daily_goal") if user else None
    day_names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    day_lines = []
    for d, data in zip(stats["dates"], stats["daily"]):
        dt = datetime.strptime(d, "%Y-%m-%d")
        dn = day_names[dt.weekday()]
        day_lines.append(
            f"  {dn} — {data['kcal']} ккал" if data["kcal"] > 0 else f"  {dn} — нет записей"
        )

    avg_kcal    = stats["avg_kcal"] or 0
    avg_protein = stats["avg_protein"] or 0
    logged      = stats["logged_days"]

    forecast_line = ""
    if goal and avg_kcal > 0:
        diff = avg_kcal - goal
        if abs(diff) > 50:
            kg_week = round(diff * 7 / 7700, 1)
            sign = "+" if kg_week > 0 else ""
            forecast_line = f"\n📉 Прогноз: *{sign}{kg_week} кг/нед.*"

    insight = (
        "🔥 Стабильная неделя — отличная работа!" if logged >= 5
        else "💪 Хороший старт — логируй каждый день."  if logged >= 3
        else "🌱 Ещё немного практики — и привычка закрепится!"
    )
    await callback.message.answer(
        f"📊 *Последние 7 дней*\n\n"
        + "\n".join(day_lines)
        + f"\n\n🍽 Среднее: *{avg_kcal} ккал*\n"
        f"🥩 Белок: *{avg_protein} г/день*"
        f"{forecast_line}\n\n"
        f"💡 {insight}",
        parse_mode="Markdown",
    )


@router.callback_query(F.data == "profile_status")
async def cb_profile_status(callback: CallbackQuery):
    uid = callback.from_user.id
    await callback.answer()
    user = get_user(uid)
    if not user:
        await callback.message.answer("Напиши /start для регистрации.")
        return
    await _send_status(callback.message.answer, uid, user)


@router.callback_query(F.data == "recalc_norm")
async def cb_recalc_norm(callback: CallbackQuery):
    uid = callback.from_user.id
    await callback.answer()
    try:
        clear_onboard_state(uid)
    except Exception:
        pass
    user_states.pop(uid, None)
    await callback.message.answer(
        "🔄 *Пересчитаем норму!*\n\nОтвечай на несколько вопросов — займёт меньше минуты ⚡",
        parse_mode="Markdown",
    )
    await callback.message.answer(
        "🎯 *Какова твоя цель?*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔥 Похудеть",       callback_data="ob_goal:lose")],
            [InlineKeyboardButton(text="⚖️ Удержать вес",   callback_data="ob_goal:maintain")],
            [InlineKeyboardButton(text="💪 Набрать массу",  callback_data="ob_goal:gain")],
        ]),
    )
    from services.state import _set_onboard_state
    _set_onboard_state(uid, "ob_goal", {})
