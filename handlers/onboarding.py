import logging
from datetime import datetime, timezone

from aiogram import Router, Bot, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

from config import (
    ADMIN_ID, REFERRAL_JOIN_BONUS_DAYS, REFERRAL_BONUS_DAYS,
    SUB_DAYS,
)
from db import (
    upsert_user, get_user, set_status, approve_user,
    activate_subscription, register_referral, mark_onboarded,
    clear_onboard_state, set_user_goals, track_event,
)
from keyboards import main_keyboard
from services.state import user_states, _set_onboard_state, _get_state

log = logging.getLogger(__name__)
router = Router()

_utcnow = lambda: datetime.now(timezone.utc).replace(tzinfo=None)


def _calc_calorie_goal(gender, age, height_cm, weight_kg, goal_type, activity="moderate"):
    PAL = {
        "sedentary":  1.2,
        "light":      1.375,
        "moderate":   1.55,
        "active":     1.725,
        "very_active": 1.9,
    }
    pal = PAL.get(activity, 1.55)
    if gender == "female":
        bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age - 161
    else:
        bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age + 5
    tdee = bmr * pal
    if goal_type == "lose":
        kcal_goal = tdee - 400
    elif goal_type == "gain":
        kcal_goal = tdee + 300
    else:
        kcal_goal = tdee
    protein_goal = round(weight_kg * (2.2 if goal_type == "gain" else 2.0))
    return round(kcal_goal), protein_goal


async def start_onboarding(bot: Bot, uid: int, name: str):
    track_event(uid, "onboarding_started")
    try:
        clear_onboard_state(uid)
    except Exception:
        pass
    user_states.pop(uid, None)
    safe_name = (name or "друг").split()[0]
    await bot.send_message(
        uid,
        f"Привет, {safe_name}! 👋\n\n"
        f"Я — твой *персональный счётчик калорий*.\n"
        f"Отправляй фото еды или описывай что съел — считаю КБЖУ 📸\n\n"
        f"Давай за минуту настроим *дневную норму калорий* — "
        f"это поможет точнее следить за прогрессом ⚡",
        parse_mode="Markdown",
    )
    await bot.send_message(
        uid,
        "🎯 *Какова твоя цель?*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔥 Похудеть",       callback_data="ob_goal:lose")],
            [InlineKeyboardButton(text="⚖️ Удержать вес",   callback_data="ob_goal:maintain")],
            [InlineKeyboardButton(text="💪 Набрать массу",  callback_data="ob_goal:gain")],
        ]),
    )
    _set_onboard_state(uid, "ob_goal", {"name": safe_name})


@router.message(Command("start"))
async def cmd_start(message: Message, bot: Bot):
    uid  = message.from_user.id
    name = message.from_user.first_name or ""
    un   = message.from_user.username or ""
    args  = message.text.split(maxsplit=1)
    param = args[1].strip() if len(args) > 1 else ""

    referrer_id = None
    if param.startswith("ref_"):
        try:
            referrer_id = int(param[4:])
            if referrer_id == uid:
                referrer_id = None
        except ValueError:
            pass

    is_new_user = get_user(uid) is None
    upsert_user(uid, un, name, referred_by=referrer_id)

    if referrer_id:
        register_referral(referrer_id, uid)
        try:
            activate_subscription(referrer_id, REFERRAL_JOIN_BONUS_DAYS)
            ref_user = get_user(referrer_id)
            new_exp = (
                datetime.fromisoformat(ref_user["expires_at"]).strftime("%d.%m.%Y")
                if ref_user and ref_user.get("expires_at") else "—"
            )
            safe_new_name = (name or f"id{uid}").replace("_", "\\_").replace("*", "\\*")
            await bot.send_message(
                referrer_id,
                f"🎉 По твоей ссылке зарегистрировался *{safe_new_name}*!\n"
                f"*+{REFERRAL_JOIN_BONUS_DAYS} дня* начислено → подписка до *{new_exp}*\n\n"
                f"🎁 Когда он оформит подписку — получишь ещё *+{REFERRAL_BONUS_DAYS} дней*!",
                parse_mode="Markdown",
            )
        except Exception as e:
            log.warning(f"referral reg notify: {e}")

    if uid == ADMIN_ID:
        user = get_user(uid)
        if not user or user["status"] in ("pending", "beta", "blocked"):
            approve_user(uid, trial_days=3650)
        elif user["status"] == "paid":
            if not user["expires_at"] or datetime.fromisoformat(user["expires_at"]) < _utcnow():
                activate_subscription(uid, 3650)
    elif is_new_user:
        approve_user(uid, trial_days=7)

    user = get_user(uid)
    if not user or user["status"] == "blocked":
        await message.answer("⛔ Доступ заблокирован.")
        return

    if is_new_user and uid != ADMIN_ID:
        safe_name = (name or "").replace("_", "\\_").replace("*", "\\*")
        safe_un   = (un or "").replace("_", "\\_")
        un_str    = f"@{safe_un}" if safe_un else f"id{uid}"
        ref_info  = f"\n🔗 Реферал от: `{referrer_id}`" if referrer_id else ""
        try:
            await bot.send_message(
                ADMIN_ID,
                f"🆕 *Новый пользователь*\n\n"
                f"👤 *{safe_name}* ({un_str})\n"
                f"🆔 `{uid}`{ref_info}\n\n"
                f"✅ Доступ открыт автоматически на 7 дней",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="🚫 Заблокировать", callback_data=f"block_{uid}"),
                ]]),
            )
        except Exception as e:
            log.warning(f"admin notify: {e}")

    is_admin = uid == ADMIN_ID

    if is_new_user or not user.get("onboarded"):
        await start_onboarding(bot, uid, name)
        return

    # Returning user — check if inactive for 7+ days
    last_active = user.get("last_active_date")
    if last_active:
        try:
            days_since = (_utcnow().date() - datetime.fromisoformat(last_active).date()).days
            if days_since >= 7:
                track_event(uid, "inactive_user_returned", {"days_since": days_since})
        except Exception:
            pass

    await message.answer(
        f"👋 С возвращением, {name}!\n\n"
        f"📸 Отправь фото еды — посчитаю КБЖУ.\n"
        f"Или используй кнопки меню 👇",
        reply_markup=main_keyboard(is_admin),
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message):
    uid = message.from_user.id
    user_states.pop(uid, None)
    from keyboards import main_keyboard as mk
    await message.answer("✅ Отменено.", reply_markup=mk(uid == ADMIN_ID))


@router.callback_query(F.data.startswith("ob_goal:"))
async def cb_ob_goal(callback: CallbackQuery):
    uid       = callback.from_user.id
    goal_type = callback.data.split(":")[1]
    await callback.answer()
    current    = _get_state(uid)
    state_data = current.get("data", {})
    state_data["goal_type"] = goal_type
    goal_labels = {
        "lose":     "🔥 Похудеть",
        "maintain": "⚖️ Удержать вес",
        "gain":     "💪 Набрать массу",
    }
    try:
        await callback.message.edit_text(
            f"🎯 *Цель:* {goal_labels.get(goal_type, goal_type)} ✅",
            parse_mode="Markdown",
        )
    except Exception:
        pass
    await callback.message.answer(
        "👤 *Укажи свой пол:*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="👨 Мужской", callback_data="ob_gender:male"),
            InlineKeyboardButton(text="👩 Женский", callback_data="ob_gender:female"),
        ]]),
    )
    _set_onboard_state(uid, "ob_gender", state_data)


@router.callback_query(F.data.startswith("ob_gender:"))
async def cb_ob_gender(callback: CallbackQuery):
    uid    = callback.from_user.id
    gender = callback.data.split(":")[1]
    await callback.answer()
    current    = _get_state(uid)
    state_data = current.get("data", {})
    state_data["gender"] = gender
    gender_label = "👨 Мужской" if gender == "male" else "👩 Женский"
    try:
        await callback.message.edit_text(
            f"👤 *Пол:* {gender_label} ✅",
            parse_mode="Markdown",
        )
    except Exception:
        pass
    await callback.message.answer(
        "🎂 *Сколько тебе лет?*\n\n_Введи число (например: 25)_",
        parse_mode="Markdown",
    )
    _set_onboard_state(uid, "ob_age", state_data)


@router.callback_query(F.data.startswith("ob_activity:"))
async def cb_ob_activity(callback: CallbackQuery):
    uid      = callback.from_user.id
    activity = callback.data.split(":")[1]
    await callback.answer()
    activity_labels = {
        "sedentary":   "🛋 Сидячий",
        "light":       "🚶 Лёгкая",
        "moderate":    "🏃 Средняя",
        "active":      "💪 Высокая",
        "very_active": "🔥 Очень высокая",
    }
    act_label = activity_labels.get(activity, activity)
    try:
        await callback.message.edit_text(
            f"⚡️ *Активность:* {act_label} ✅",
            parse_mode="Markdown",
        )
    except Exception:
        pass
    ob_data = user_states.get(uid, {}).get("data", {})
    ob_data["activity"] = activity
    weight = ob_data.get("weight", 70.0)
    goal_kcal, protein_goal = _calc_calorie_goal(
        ob_data.get("gender", "male"),
        ob_data.get("age", 25),
        ob_data.get("height", 170),
        weight,
        ob_data.get("goal_type", "maintain"),
        activity,
    )
    set_user_goals(
        uid,
        daily_goal=goal_kcal,
        protein_goal=protein_goal,
        weight_kg=weight,
        height_cm=ob_data.get("height", 170),
        age=ob_data.get("age", 25),
        gender=ob_data.get("gender", "male"),
        goal_type=ob_data.get("goal_type", "maintain"),
        activity=activity,
    )
    mark_onboarded(uid)
    track_event(uid, "onboarding_completed")
    try:
        clear_onboard_state(uid)
    except Exception:
        pass
    user_states.pop(uid, None)

    goal_labels = {
        "lose":     "похудение",
        "maintain": "поддержание веса",
        "gain":     "набор массы",
    }
    goal_label = goal_labels.get(ob_data.get("goal_type", "maintain"), "поддержание веса")
    goal_type_local = ob_data.get("goal_type", "maintain")
    forecast_line = ""
    if goal_type_local == "lose":
        kg_month = round(400 * 30 / 7700, 1)
        forecast_line = f"\n\n📉 *Прогноз:* при соблюдении нормы — минус ~*{kg_month} кг/мес*"
    elif goal_type_local == "gain":
        kg_month = round(300 * 30 / 7700, 1)
        forecast_line = f"\n\n📈 *Прогноз:* при профиците — плюс ~*{kg_month} кг/мес*"
    else:
        forecast_line = "\n\n⚖️ *Цель — поддержание веса.* Отслеживай КБЖУ каждый день!"

    await callback.message.answer(
        f"🎉 *Профиль настроен!*\n\n"
        f"🎯 Цель: *{goal_label}*\n"
        f"⚡️ Активность: *{act_label}*\n"
        f"🔥 Норма калорий: *{goal_kcal} ккал/день*\n"
        f"🥩 Норма белка: *{protein_goal} г/день*"
        f"{forecast_line}\n\n"
        "Отправляй фото еды или описывай что съел — буду следить за прогрессом! 📸",
        parse_mode="Markdown",
        reply_markup=main_keyboard(uid == ADMIN_ID),
    )
