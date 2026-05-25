import asyncio
import os
import re
import sys
import random
import logging
import base64
import urllib.parse
import httpx
from datetime import datetime, timedelta
from openai import AsyncOpenAI
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    PhotoSize,
    LabeledPrice,
    PreCheckoutQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

sys.path.insert(0, os.path.dirname(__file__))
from db import (
    init_db,
    upsert_user,
    get_user,
    set_status,
    approve_user,
    is_trial_expired,
    set_daily_goal,
    mark_onboarded,
    activate_subscription,
    check_subscription_expired,
    get_all_users,
    get_active_users,
    record_usage,
    get_daily_usage,
    get_daily_macros,
    get_weekly_stats,
    get_total_stats,
    register_referral,
    mark_referral_paid,
    get_referral_stats,
    update_entry_calories,
    update_streak,
    add_weight_log,
    get_weight_history,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BOT_TOKEN     = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_ID      = int(os.environ["TELEGRAM_CHAT_ID"])
BOT_USERNAME  = os.environ.get("BOT_USERNAME", "")

BETA_DAILY_LIMIT      = 5
SUB_PRICE_STARS       = 150
SUB_DAYS              = 30
REFERRAL_BONUS_DAYS   = 7
REFERRAL_JOIN_BONUS_DAYS = 3

STREAK_MILESTONES = {
    3:  "🥉 3 дня подряд",
    7:  "🥈 7 дней подряд",
    14: "🥇 14 дней подряд",
    30: "🏆 30 дней подряд!",
    60: "👑 60 дней подряд!",
    100:"🌟 100 дней!!",
}

CHEAT_KEYWORDS = [
    "бургер", "гамбургер", "чизбургер", "kfc", "mcdonald", "макдональдс",
    "пицца", "чипсы", "картофель фри", "фри", "шоколад", "конфеты", "торт",
    "пончик", "donut", "мороженое", "fast food", "фастфуд", "нагетсы",
    "хот-дог", "hotdog", "картошка фри", "сникерс", "kit kat",
]
SUGAR_KEYWORDS = ["сахар", "конфеты", "сладкое", "торт", "пирожное", "газировка", "кола"]

openai_client = AsyncOpenAI(
    api_key=os.environ["GROQ_API_KEY"],
    base_url="https://api.groq.com/openai/v1",
    max_retries=3,
    timeout=60,
)

# ── FSM states ───────────────────────────────────────────────────────────────
user_states: dict[int, dict] = {}

STATES = {
    "GOAL_ENTER":             "goal_enter",
    "CALC_AGE":               "calc_age",
    "CALC_WEIGHT":            "calc_weight",
    "CALC_HEIGHT":            "calc_height",
    "MANUAL_ENTRY":           "manual_entry",
    "CORRECT_ENTRY":          "correct_entry",
    "WEIGHT_LOG":             "weight_log",
    # Onboarding
    "ONBOARD_WEIGHT":         "onboard_weight",
    "ONBOARD_HEIGHT":         "onboard_height",
    "ONBOARD_AGE":            "onboard_age",
    "ONBOARD_GOAL_TYPE":      "onboard_goal_type",
    "ONBOARD_WAIT_GENDER":    "onboard_wait_gender",
    "ONBOARD_WAIT_ACTIVITY":  "onboard_wait_activity",
    # Goal-calc flow
    "CALC_GENDER":            "calc_gender",
    "GOAL_ASK":               "goal_ask",
    # Admin
    "ADMIN_GIVE_DAYS":        "admin_give_days",
    "ADMIN_BROADCAST":        "admin_broadcast",
}

# State TTL: auto-expire stale FSM states after 1 hour of inactivity
_STATE_TTL_SECONDS = 3600


def _get_state(uid: int) -> dict:
    """Return user FSM state, evicting entries older than _STATE_TTL_SECONDS."""
    s = user_states.get(uid)
    if not s:
        return {}
    ts = s.get("_ts")
    if ts and (datetime.utcnow() - ts).total_seconds() > _STATE_TTL_SECONDS:
        user_states.pop(uid, None)
        return {}
    return s


def _set_state(uid: int, state: str, data: dict | None = None):
    user_states[uid] = {"state": state, "data": data or {}, "_ts": datetime.utcnow()}

# ── Кнопки меню ──────────────────────────────────────────────────────────────
BTN_PHOTO    = "📸 Анализ фото"
BTN_MANUAL   = "✍️ Ввести вручную"
BTN_PROGRESS = "📊 Мой прогресс"
BTN_SUB      = "⭐ Подписка"
BTN_REF      = "👥 Пригласить друга"
BTN_PROFILE  = "⚙️ Профиль"
BTN_ADMIN    = "🛠 Админка"

MENU_BUTTONS = {
    BTN_PHOTO, BTN_MANUAL, BTN_PROGRESS,
    BTN_SUB, BTN_REF, BTN_PROFILE, BTN_ADMIN,
}


def main_keyboard(is_admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_PHOTO),   KeyboardButton(text=BTN_MANUAL)],
        [KeyboardButton(text=BTN_PROGRESS), KeyboardButton(text=BTN_PROFILE)],
        [KeyboardButton(text=BTN_SUB),     KeyboardButton(text=BTN_REF)],
    ]
    if is_admin:
        rows.append([KeyboardButton(text=BTN_ADMIN)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


# ─────────────────── AI анализ ───────────────────────────────────────────────

VISION_PROMPT = """Ты — эксперт по еде. Опиши фото:
1. Блюдо/продукты (точно)
2. Способ приготовления
3. Примерный вес порции (г)
4. Основные ингредиенты и количество

Если это не еда — напиши только: НЕ ЕДА"""

NUTRITION_PROMPT = """Ты — дружелюбный AI-тренер по питанию с характером. Рассчитай КБЖУ.

Блюдо: {desc}

Ответь СТРОГО в этом формате:
🍽 *{{название}}* (~{{вес}} г)

🔥 {{ккал}} ккал  |  Б {{б}}г  Ж {{ж}}г  У {{у}}г

💬 {{живой комментарий от тренера — как друг, не как справочник. Оцени выбор, дай одну практическую рекомендацию. 1-2 предложения с эмодзи. Будь конкретным.}}

KCAL:{{ккал}}
PROTEIN:{{б}}
FAT:{{ж}}
CARBS:{{у}}
NAME:{{название}}"""

TEXT_NUTRITION_PROMPT = """Ты — дружелюбный AI-тренер по питанию с характером.

Блюдо/продукт: {desc}

Если это не еда — ответь только: НЕ ЕДА

Иначе ответь СТРОГО в этом формате:
🍽 *{{название}}* (~{{вес}} г)

🔥 {{ккал}} ккал  |  Б {{б}}г  Ж {{ж}}г  У {{у}}г

💬 {{живой комментарий — как друг, не как справочник. 1-2 предложения с эмодзи.}}

KCAL:{{ккал}}
PROTEIN:{{б}}
FAT:{{ж}}
CARBS:{{у}}
NAME:{{название}}"""


def _parse_macros(raw: str):
    """Extract kcal/protein/fat/carbs/name from AI response."""
    kcal = protein = fat = carbs = food_name = None
    m = re.search(r"KCAL:(\d+)", raw)
    if m: kcal = int(m.group(1))
    m = re.search(r"PROTEIN:([\d.]+)", raw)
    if m: protein = float(m.group(1))
    m = re.search(r"FAT:([\d.]+)", raw)
    if m: fat = float(m.group(1))
    m = re.search(r"CARBS:([\d.]+)", raw)
    if m: carbs = float(m.group(1))
    m = re.search(r"NAME:(.+)", raw)
    if m: food_name = m.group(1).strip()
    display = re.sub(r"\s*(KCAL|PROTEIN|FAT|CARBS|NAME):[^\n]+", "", raw).strip()
    return display, kcal, protein, fat, carbs, food_name


async def analyze_food_photo(photo_bytes: bytes):
    b64 = base64.b64encode(photo_bytes).decode()
    vision = await openai_client.chat.completions.create(
        model="meta-llama/llama-4-scout-17b-16e-instruct",
        messages=[{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                {"type": "text", "text": VISION_PROMPT},
            ],
        }],
        max_tokens=400,
    )
    desc = vision.choices[0].message.content or ""
    if "НЕ ЕДА" in desc.upper():
        return "🙅 На фото не еда. Пришли фото блюда — посчитаю калории!", None, None, None, None, None

    nutrition = await openai_client.chat.completions.create(
        model="meta-llama/llama-4-scout-17b-16e-instruct",
        messages=[
            {"role": "system", "content": "Точный нутрициолог и дружелюбный тренер. Строго по шаблону."},
            {"role": "user", "content": NUTRITION_PROMPT.format(desc=desc)},
        ],
        max_tokens=500,
    )
    raw = nutrition.choices[0].message.content or ""
    return _parse_macros(raw)


async def analyze_food_text(description: str):
    response = await openai_client.chat.completions.create(
        model="meta-llama/llama-4-scout-17b-16e-instruct",
        messages=[
            {"role": "system", "content": "Точный нутрициолог и дружелюбный тренер. Строго по шаблону."},
            {"role": "user", "content": TEXT_NUTRITION_PROMPT.format(desc=description)},
        ],
        max_tokens=500,
    )
    raw = response.choices[0].message.content or ""
    if "НЕ ЕДА" in raw.upper():
        return "🙅 Это не похоже на еду. Введи название блюда или продукта.", None, None, None, None, None
    return _parse_macros(raw)


# ─────────────────── Хелперы ─────────────────────────────────────────────────


def user_label(row) -> str:
    name = row["first_name"] or ""
    un = f"@{row['username']}" if row["username"] else f"id{row['telegram_id']}"
    return f"{name} ({un})"


def ref_link(uid: int) -> str:
    bot_un = BOT_USERNAME.lstrip("@") or "YOUR_BOT"
    return f"https://t.me/{bot_un}?start=ref_{uid}"


def streak_emoji(streak: int) -> str:
    if streak >= 30: return "🏆"
    if streak >= 14: return "🥇"
    if streak >= 7:  return "🥈"
    if streak >= 3:  return "🥉"
    return "🔥"


def progress_bar(current: int, goal: int, width: int = 10) -> str:
    filled = min(int(width * current / goal), width) if goal else 0
    pct = min(int(100 * current / goal), 100) if goal else 0
    return f"{'█' * filled}{'░' * (width - filled)} {pct}%"


def calc_daily_score(kcal: int, protein: float, fat: float, carbs: float,
                     goal_kcal: int | None, goal_protein: int | None, meals: int) -> int:
    """Calculate nutrition score 0–100."""
    score = 0
    if goal_kcal and goal_kcal > 0:
        ratio = kcal / goal_kcal
        if 0.85 <= ratio <= 1.10:
            score += 35
        elif 0.70 <= ratio < 0.85 or 1.10 < ratio <= 1.20:
            score += 20
        elif ratio <= 1.30:
            score += 10
    else:
        score += 20

    if goal_protein and goal_protein > 0 and protein > 0:
        p_ratio = protein / goal_protein
        if p_ratio >= 0.85:
            score += 30
        elif p_ratio >= 0.65:
            score += 20
        elif p_ratio >= 0.45:
            score += 10
    elif protein > 0:
        score += 15

    if meals >= 3:
        score += 20
    elif meals == 2:
        score += 12
    elif meals == 1:
        score += 5

    total_macros = protein + fat + carbs
    if total_macros > 0:
        balance_ok = (protein / total_macros >= 0.20)
        if balance_ok:
            score += 15

    return min(score, 100)


def score_emoji(score: int) -> str:
    if score >= 85: return "🔥"
    if score >= 70: return "✅"
    if score >= 50: return "📊"
    return "💤"


def detect_fun_reaction(food_name_lower: str, kcal: int | None) -> str | None:
    """Return a fun reaction for specific foods."""
    for kw in CHEAT_KEYWORDS:
        if kw in food_name_lower:
            reactions = [
                "🍗 Чит-мил детектирован! Раз в неделю можно — наслаждайся без вины 😄",
                "🍔 Зафиксировано. Завтра компенсируем лёгким ужином 💪",
                "🍕 Калорийная бомба принята. Главное — не делать из этого систему 😅",
            ]
            return random.choice(reactions)
    for kw in SUGAR_KEYWORDS:
        if kw in food_name_lower:
            return "🍬 Сахарная атака! Сладкое хорошо в меру — запей водой и всё будет ок 😊"
    if kcal and kcal > 900:
        return "😅 Вот это порция! Мощно. Остаток дня — полегче 💪"
    return None


async def notify_admin(bot: Bot, text: str, markup=None):
    try:
        await bot.send_message(ADMIN_ID, text, parse_mode="Markdown", reply_markup=markup)
    except Exception as e:
        log.warning(f"notify_admin: {e}")


def access_check(user_row) -> tuple[bool, str]:
    """Check access using the already-fetched user dict — no extra DB queries."""
    if user_row is None:
        return False, "not_registered"
    s = user_row["status"]
    if s == "blocked":   return False, "blocked"
    if s == "pending":   return False, "pending"
    if s == "paid":
        if check_subscription_expired(user_row):  # pass dict, avoids extra get_user()
            return False, "sub_expired"
        return True, "paid"
    if s == "beta":
        if is_trial_expired(user_row):            # pass dict, avoids extra get_user()
            return False, "trial_expired"
        return True, "beta"
    return False, "unknown"


async def deny(message: Message, reason: str):
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⭐ Оформить подписку", callback_data="buy_sub")
    ]])
    if reason == "pending":
        await message.answer("⏳ Твоя заявка рассматривается. Ожидай одобрения.")
    elif reason == "blocked":
        await message.answer("⛔ Доступ заблокирован.")
    elif reason in ("trial_expired", "sub_expired"):
        await message.answer(
            "⏰ *Доступ закончился*\n\nОформи подписку и продолжай следить за питанием 🚀",
            parse_mode="Markdown",
            reply_markup=kb,
        )
    else:
        await message.answer("Напиши /start для регистрации.")


def daily_progress_text(uid: int, user: dict | None = None,
                        macros: dict | None = None) -> str:
    """Build progress block. Pass pre-fetched user/macros to avoid extra DB queries."""
    if macros is None:
        macros = get_daily_macros(uid)
    if user is None:
        user = get_user(uid)
    total = macros["kcal"]
    goal = user["daily_goal"] if user else None
    goal_protein = user.get("protein_goal") if user else None
    streak = user.get("streak_days", 0) if user else 0
    meals = get_daily_usage(uid)

    streak_line = (
        f"\n{streak_emoji(streak)} Серия: *{streak} {'день' if streak == 1 else 'дней'}*"
        if streak > 0 else ""
    )

    score = calc_daily_score(total, macros["protein"], macros["fat"], macros["carbs"],
                             goal, goal_protein, meals)
    score_line = f"\n{score_emoji(score)} Питание сегодня: *{score}/100*" if total > 0 else ""

    if not goal:
        return f"\n\n📊 *Сегодня:* {total} ккал{score_line}{streak_line}"

    remaining = max(goal - total, 0)
    bar = progress_bar(total, goal)
    over = total - goal
    extra = f"⚠️ Превышение на {over} ккал" if over > 0 else f"Осталось: {remaining} ккал"

    protein_line = ""
    if macros["protein"] > 0:
        protein_line = f"\nБ {macros['protein']}г  Ж {macros['fat']}г  У {macros['carbs']}г"

    return (
        f"\n\n📊 *Сегодня:* {total} / {goal} ккал\n"
        f"{bar}\n"
        f"{extra}{protein_line}"
        f"{score_line}{streak_line}"
    )


# ── Keyboards ─────────────────────────────────────────────────────────────────


def result_keyboard(entry_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✏️ Исправить калории", callback_data=f"correct:{entry_id}")
    ]])


def new_user_keyboard(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Одобрить",      callback_data=f"approve_{uid}"),
        InlineKeyboardButton(text="🚫 Заблокировать", callback_data=f"block_{uid}"),
    ]])


def profile_keyboard(uid: int, has_goal: bool) -> InlineKeyboardMarkup:
    goal_btn_text = "✏️ Изменить цель" if has_goal else "🎯 Установить цель"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=goal_btn_text, callback_data="profile_goal")],
        [InlineKeyboardButton(text="⚖️ Записать вес", callback_data="weight_opt")],
        [InlineKeyboardButton(text="📈 Неделя", callback_data="profile_week")],
        [InlineKeyboardButton(text="ℹ️ Статус подписки", callback_data="profile_status")],
    ])


def premium_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 Оплатить {SUB_PRICE_STARS} ⭐ (30 дней)", callback_data="buy_sub")],
        [InlineKeyboardButton(text="👥 Получить бесплатно (реферал)", callback_data="ref_screen")],
    ])


def goal_ask_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔢 Введу сам",      callback_data="goal_know")],
        [InlineKeyboardButton(text="🧮 Рассчитай мне",  callback_data="goal_calc")],
        [InlineKeyboardButton(text="⏭ Пропустить",     callback_data="goal_skip")],
    ])


def gender_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="♂️ Мужчина", callback_data="gender_m"),
        InlineKeyboardButton(text="♀️ Женщина", callback_data="gender_f"),
    ]])


def activity_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛋 Сидячий образ жизни",          callback_data="act_1.2")],
        [InlineKeyboardButton(text="🚶 Лёгкая активность (1-3 дня)",  callback_data="act_1.375")],
        [InlineKeyboardButton(text="🏃 Средняя активность (3-5 дней)", callback_data="act_1.55")],
        [InlineKeyboardButton(text="🏋 Высокая активность (6-7 дней)", callback_data="act_1.725")],
        [InlineKeyboardButton(text="⚡ Очень высокая / спортсмен",    callback_data="act_1.9")],
    ])


def goal_type_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📉 Похудеть",       callback_data="gtype_lose")],
        [InlineKeyboardButton(text="📈 Набрать массу",  callback_data="gtype_gain")],
        [InlineKeyboardButton(text="⚖️ Поддерживать",   callback_data="gtype_maintain")],
        [InlineKeyboardButton(text="📊 Просто считать", callback_data="gtype_track")],
    ])


def admin_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📊 Статистика",   callback_data="adm_stats"),
            InlineKeyboardButton(text="👥 Все юзеры",    callback_data="adm_users"),
        ],
        [
            InlineKeyboardButton(text="⏳ Ожидают",      callback_data="adm_pending"),
            InlineKeyboardButton(text="💎 Платные",      callback_data="adm_paid"),
        ],
        [
            InlineKeyboardButton(text="📡 Рассылка",     callback_data="adm_broadcast"),
            InlineKeyboardButton(text="🔄 Обновить",     callback_data="adm_refresh"),
        ],
    ])


def user_action_keyboard(target_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Одобрить",     callback_data=f"approve_{target_id}"),
            InlineKeyboardButton(text="🚫 Блокировать",  callback_data=f"block_{target_id}"),
        ],
        [
            InlineKeyboardButton(text="💎 +7 дней",      callback_data=f"give7_{target_id}"),
            InlineKeyboardButton(text="💎 +30 дней",     callback_data=f"give30_{target_id}"),
        ],
        [
            InlineKeyboardButton(text="⚡ Активировать", callback_data=f"activate_{target_id}"),
        ],
    ])


# ── TDEE calc ─────────────────────────────────────────────────────────────────


def calc_tdee(gender: str, age: int, weight: float, height: float,
              activity: float, goal_type: str = "track") -> tuple[int, int]:
    bmr = 10 * weight + 6.25 * height - 5 * age + (5 if gender == "m" else -161)
    tdee = round(bmr * activity)
    if goal_type == "lose":
        tdee = round(tdee * 0.85)
    elif goal_type == "gain":
        tdee = round(tdee * 1.10)
    protein = round(weight * 1.8 if goal_type == "gain" else weight * 1.6)
    return tdee, protein


# ── Onboarding ────────────────────────────────────────────────────────────────


async def start_onboarding(bot: Bot, uid: int, name: str):
    _set_state(uid, STATES["ONBOARD_GOAL_TYPE"])
    await bot.send_message(
        uid,
        f"Привет, {name}! 👋\n\n"
        f"Я помогу *считать калории по фото еды* и следить за питанием.\n\n"
        f"Буквально 30 секунд — и твой личный трекер готов 🚀\n\n"
        f"*Какая у тебя цель?*",
        parse_mode="Markdown",
        reply_markup=goal_type_keyboard(),
    )


# ── Общий хелпер доставки результата анализа еды ─────────────────────────────


async def _deliver_analysis(
    message: Message,
    uid: int,
    user: dict,
    display: str,
    kcal, protein, fat, carbs,
    food_name: str | None,
    thinking_msg,
):
    """Record, streak, send result, fun reaction, milestone. DRY for all 3 entry points."""
    entry_id = record_usage(uid, kcal, protein, fat, carbs, food_name)
    streak, milestone = update_streak(uid, user=user) if kcal else (0, False)

    # Re-fetch macros after recording the new entry
    macros = get_daily_macros(uid)
    # Refresh user for up-to-date streak
    fresh_user = get_user(uid)
    progress = daily_progress_text(uid, user=fresh_user, macros=macros)
    hint = "\n\n_Установи норму в ⚙️ Профиль_" if not (user.get("daily_goal")) else ""

    try:
        await thinking_msg.delete()
    except Exception:
        pass

    user_states.pop(uid, None)
    await message.answer(
        display + progress + hint,
        parse_mode="Markdown",
        reply_markup=main_keyboard(uid == ADMIN_ID),
    )
    if kcal:
        await message.answer("Неточно? Можно исправить:", reply_markup=result_keyboard(entry_id))

    if food_name:
        fun = detect_fun_reaction(food_name.lower(), kcal)
        if fun:
            await message.answer(fun)

    if milestone and streak in STREAK_MILESTONES:
        await message.answer(
            f"🎉 *Ачивка разблокирована!*\n"
            f"_{STREAK_MILESTONES[streak]}_\n\nПродолжай! 💪",
            parse_mode="Markdown",
        )


# ── Планировщик ───────────────────────────────────────────────────────────────


async def send_morning_checkins(bot: Bot):
    users = get_active_users()
    for user in users:
        uid = user["telegram_id"]
        goal = user.get("daily_goal")
        streak = user.get("streak_days", 0)
        name = user.get("first_name") or "Привет"
        try:
            goal_line = f"🎯 Цель: *{goal} ккал*" if goal else "🎯 Цель не задана — настрой в профиле"
            streak_line = (
                f"\n{streak_emoji(streak)} Серия *{streak} дней* — не прерывай!"
                if streak > 1 else ""
            )
            await bot.send_message(
                uid,
                f"☀️ *Доброе утро, {name}!*\n\n"
                f"{goal_line}{streak_line}\n\n"
                f"📸 Начни день — сфотографируй завтрак!",
                parse_mode="Markdown",
            )
        except Exception as e:
            log.debug(f"morning {uid}: {e}")
        await asyncio.sleep(0.05)  # stay under Telegram 30 msg/sec limit


async def send_evening_summaries(bot: Bot):
    users = get_active_users()
    for user in users:
        uid = user["telegram_id"]
        goal = user.get("daily_goal")
        goal_protein = user.get("protein_goal")
        streak = user.get("streak_days", 0)
        try:
            macros = get_daily_macros(uid)
            total = macros["kcal"]
            if total == 0:
                continue

            meals = get_daily_usage(uid)
            score = calc_daily_score(total, macros["protein"], macros["fat"],
                                     macros["carbs"], goal, goal_protein, meals)

            if goal:
                pct = round(total / goal * 100)
                result_line = (
                    f"✅ Отличный день — {pct}% нормы" if 85 <= pct <= 115
                    else f"⚠️ Перебор на {total - goal} ккал" if pct > 115
                    else f"📉 Недобор — {goal - total} ккал осталось"
                )
            else:
                result_line = ""

            protein_line = f"\n💪 Белок: {macros['protein']}г" if macros["protein"] > 0 else ""
            streak_line = f"\n🔥 Серия: *{streak} {'день' if streak == 1 else 'дней'}*!" if streak > 0 else ""
            score_line = f"\n{score_emoji(score)} Оценка дня: *{score}/100*"

            await bot.send_message(
                uid,
                f"🌙 *Итоги дня*\n\n"
                f"🔥 Калории: *{total}*"
                f"{f' / {goal}' if goal else ''} ккал\n"
                f"{protein_line}\n"
                f"{result_line}"
                f"{score_line}{streak_line}",
                parse_mode="Markdown",
            )
        except Exception as e:
            log.debug(f"evening {uid}: {e}")
        await asyncio.sleep(0.05)  # Telegram rate limit


async def send_weekly_reports(bot: Bot):
    users = get_active_users()
    for user in users:
        uid = user["telegram_id"]
        try:
            stats = get_weekly_stats(uid)
            if stats["logged_days"] < 2:
                continue

            consistency_icon = (
                "🔥" if stats["consistency"] >= 80
                else "📊" if stats["consistency"] >= 50
                else "💤"
            )
            goal = user.get("daily_goal", 0)
            avg_vs_goal = ""
            if goal and stats["avg_kcal"]:
                diff = stats["avg_kcal"] - goal
                avg_vs_goal = f" ({'➕' if diff > 0 else '➖'}{abs(diff)} от нормы)"

            await bot.send_message(
                uid,
                f"📊 *Итоги недели*\n\n"
                f"🍽 Дней с записями: *{stats['logged_days']}/7*\n"
                f"🔥 Среднее: *{stats['avg_kcal']} ккал*{avg_vs_goal}\n"
                f"💪 Средний белок: *{stats['avg_protein']}г*\n"
                f"🏆 Лучший день: *{stats['best_day_kcal']} ккал*\n"
                f"{consistency_icon} Постоянство: *{stats['consistency']}%*\n\n"
                f"{'🔥 Отличная неделя — так держать!' if stats['consistency'] >= 80 else '💪 Логируй каждый день — это ключ к результату!'}",
                parse_mode="Markdown",
            )
        except Exception as e:
            log.debug(f"weekly {uid}: {e}")
        await asyncio.sleep(0.05)  # Telegram rate limit


# ─────────────────── Бот ─────────────────────────────────────────────────────


async def main():
    init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()

    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(send_morning_checkins, "cron", hour=8,  minute=0, args=[bot])
    scheduler.add_job(send_evening_summaries, "cron", hour=21, minute=0, args=[bot])
    scheduler.add_job(send_weekly_reports, "cron", day_of_week="mon", hour=9, minute=0, args=[bot])
    scheduler.start()

    # ── /start ────────────────────────────────────────────────────────────────
    @dp.message(Command("start"))
    async def cmd_start(message: Message):
        uid  = message.from_user.id
        name = message.from_user.first_name or ""
        un   = message.from_user.username or ""
        args = message.text.split(maxsplit=1)
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
                if not user["expires_at"] or datetime.fromisoformat(user["expires_at"]) < datetime.utcnow():
                    activate_subscription(uid, 3650)

        user = get_user(uid)
        if not user or user["status"] == "blocked":
            await message.answer("⛔ Доступ заблокирован.")
            return

        # Notify admin about new users
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
                    f"🆔 `{uid}`{ref_info}",
                    parse_mode="Markdown",
                    reply_markup=new_user_keyboard(uid),
                )
            except Exception as e:
                log.warning(f"admin notify: {e}")

        status = user["status"]
        is_admin = uid == ADMIN_ID

        # Trigger onboarding for new/non-onboarded users
        if is_new_user or not user.get("onboarded"):
            if status not in ("pending", "blocked"):
                await start_onboarding(bot, uid, name)
                return

        if status == "pending":
            await message.answer(
                "⏳ Заявка на рассмотрении.\n\nАдмин скоро одобрит доступ!",
                reply_markup=main_keyboard(is_admin),
            )
            return

        await message.answer(
            f"👋 С возвращением, {name}!\n\n"
            f"📸 Отправь фото еды — посчитаю КБЖУ.\n"
            f"Или используй кнопки меню 👇",
            reply_markup=main_keyboard(is_admin),
        )

    # ── /cancel ───────────────────────────────────────────────────────────────
    @dp.message(Command("cancel"))
    async def cmd_cancel(message: Message):
        uid = message.from_user.id
        if uid in user_states:
            del user_states[uid]
        await message.answer(
            "✅ Отменено.",
            reply_markup=main_keyboard(uid == ADMIN_ID),
        )

    # ── /admin ────────────────────────────────────────────────────────────────
    @dp.message(Command("admin"))
    async def cmd_admin(message: Message):
        if message.from_user.id != ADMIN_ID:
            return
        await show_admin_panel(message.answer)

    async def show_admin_panel(send_fn):
        s = get_total_stats()
        text = (
            f"🛡 *Admin Panel*\n\n"
            f"👥 Всего: *{s['total_users']}*  "
            f"(⏳{s['pending']} ✅{s['beta']} 💎{s['paid']} 🚫{s['blocked']})\n"
            f"🆕 Новых сегодня: *{s['new_today']}*\n"
            f"📸 Анализов сегодня: *{s['analyses_today']}*  |  всего: {s['analyses_total']}\n"
            f"👁 DAU: *{s['dau']}*  |  WAU: *{s['wau']}*\n"
            f"📈 D1 retention: *{s['d1_retention']}%*  |  D7: *{s['d7_retention']}%*\n"
            f"🔥 Средний стрик: *{s['avg_streak']} дн.*\n"
            f"🔗 Реф. оплат: *{s['referrals_paid']}*\n\n"
            f"Команды: `/user ID` `/approve ID` `/activate ID`\n"
            f"`/give ID [дней]` `/block ID` `/broadcast ТЕКСТ`"
        )
        await send_fn(text, parse_mode="Markdown", reply_markup=admin_panel_keyboard())

    # ── Admin text commands ───────────────────────────────────────────────────
    @dp.message(Command("user"))
    async def cmd_user(message: Message):
        if message.from_user.id != ADMIN_ID:
            return
        parts = message.text.split()
        if len(parts) < 2:
            await message.answer("Использование: /user ID")
            return
        try:
            target_id = int(parts[1])
        except ValueError:
            await message.answer("ID должен быть числом.")
            return
        u = get_user(target_id)
        if not u:
            await message.answer("Пользователь не найден.")
            return
        await message.answer(_fmt_user_card(u), parse_mode="Markdown",
                             reply_markup=user_action_keyboard(target_id))

    @dp.message(Command("approve"))
    async def cmd_approve(message: Message):
        if message.from_user.id != ADMIN_ID:
            return
        parts = message.text.split()
        if len(parts) < 2:
            await message.answer("Использование: /approve ID")
            return
        try:
            target_id = int(parts[1])
        except ValueError:
            await message.answer("ID должен быть числом.")
            return
        approve_user(target_id, trial_days=3)
        await message.answer(f"✅ Пользователь {target_id} одобрен (3 дня).")
        try:
            u = get_user(target_id)
            if u and not u.get("onboarded"):
                user_name = u.get("first_name") or "друг"
                await start_onboarding(bot, target_id, user_name)
            else:
                await bot.send_message(target_id,
                    "✅ *Доступ открыт!*\n\nОтправляй фото еды — считаю калории 📸",
                    parse_mode="Markdown", reply_markup=main_keyboard(False))
        except Exception:
            pass

    @dp.message(Command("activate"))
    async def cmd_activate(message: Message):
        if message.from_user.id != ADMIN_ID:
            return
        parts = message.text.split()
        if len(parts) < 2:
            await message.answer("Использование: /activate ID")
            return
        try:
            target_id = int(parts[1])
        except ValueError:
            await message.answer("ID должен быть числом.")
            return
        activate_subscription(target_id, SUB_DAYS)
        u = get_user(target_id)
        exp = datetime.fromisoformat(u["expires_at"]).strftime("%d.%m.%Y") if u and u.get("expires_at") else "?"
        await message.answer(f"💎 Подписка активирована. {target_id} → до {exp}")
        try:
            await bot.send_message(target_id,
                f"🎉 *Подписка активирована* до *{exp}*!\n\nОтправляй фото без ограничений 🚀",
                parse_mode="Markdown")
        except Exception:
            pass

    @dp.message(Command("give"))
    async def cmd_give(message: Message):
        if message.from_user.id != ADMIN_ID:
            return
        parts = message.text.split()
        if len(parts) < 2:
            await message.answer("Использование: /give ID [дней]")
            return
        try:
            target_id = int(parts[1])
            days = int(parts[2]) if len(parts) > 2 else 7
        except ValueError:
            await message.answer("Ошибка парсинга.")
            return
        activate_subscription(target_id, days)
        u = get_user(target_id)
        exp = datetime.fromisoformat(u["expires_at"]).strftime("%d.%m.%Y") if u and u.get("expires_at") else "?"
        await message.answer(f"✅ +{days} дней → {target_id} до {exp}")
        try:
            await bot.send_message(target_id,
                f"🎁 *+{days} дней* добавлено к подписке → до *{exp}*!",
                parse_mode="Markdown")
        except Exception:
            pass

    @dp.message(Command("block"))
    async def cmd_block(message: Message):
        if message.from_user.id != ADMIN_ID:
            return
        parts = message.text.split()
        if len(parts) < 2:
            await message.answer("Использование: /block ID")
            return
        try:
            target_id = int(parts[1])
        except ValueError:
            await message.answer("ID должен быть числом.")
            return
        set_status(target_id, "blocked")
        await message.answer(f"🚫 Пользователь {target_id} заблокирован.")

    @dp.message(Command("users"))
    async def cmd_users(message: Message):
        if message.from_user.id != ADMIN_ID:
            return
        parts = message.text.split()
        sf = parts[1] if len(parts) > 1 else None
        users = get_all_users(sf)
        if not users:
            await message.answer("Нет пользователей.")
            return
        icons = {"pending": "⏳", "beta": "✅", "paid": "💎", "blocked": "🚫"}
        lines = [f"*{'Все' if not sf else sf.upper()} ({len(users)}):*\n"]
        for u in users[:30]:
            nm = (u["first_name"] or "").replace("_", " ")[:15]
            un = u["username"] or ""
            label = f"{nm} (@{un})" if un else f"{nm} (id{u['telegram_id']})"
            streak = u.get("streak_days", 0)
            s_icon = f" 🔥{streak}" if streak > 1 else ""
            lines.append(f"{icons.get(u['status'],'❓')} {label} — `{u['telegram_id']}`{s_icon}")
        await message.answer("\n".join(lines), parse_mode="Markdown")

    @dp.message(Command("stats"))
    async def cmd_stats(message: Message):
        if message.from_user.id != ADMIN_ID:
            return
        s = get_total_stats()
        await message.answer(
            f"📈 *Статистика*\n\n"
            f"👥 Всего: {s['total_users']}  (⏳{s['pending']} ✅{s['beta']} 💎{s['paid']} 🚫{s['blocked']})\n"
            f"🆕 Новых сегодня: {s['new_today']}\n"
            f"📸 Сегодня: {s['analyses_today']}  |  Всего: {s['analyses_total']}\n"
            f"👁 DAU: {s['dau']}  |  WAU: {s['wau']}\n"
            f"📈 D1 retention: {s['d1_retention']}%  |  D7: {s['d7_retention']}%\n"
            f"🔥 Средний стрик: {s['avg_streak']} дн.\n"
            f"🔗 Реф. оплат: {s['referrals_paid']}",
            parse_mode="Markdown",
        )

    @dp.message(Command("broadcast"))
    async def cmd_broadcast(message: Message):
        if message.from_user.id != ADMIN_ID:
            return
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.answer("Использование: /broadcast ТЕКСТ")
            return
        text = parts[1]
        users = get_active_users()
        sent = failed = 0
        for u in users:
            try:
                await bot.send_message(u["telegram_id"], text)
                sent += 1
            except Exception:
                failed += 1
        await message.answer(f"📡 Рассылка завершена.\n✅ Отправлено: {sent}\n❌ Ошибок: {failed}")

    def _fmt_user_card(u: dict) -> str:
        icons = {"pending": "⏳", "beta": "✅", "paid": "💎", "blocked": "🚫"}
        safe_name = (u.get("first_name") or "").replace("_", "\\_").replace("*", "\\*")
        un = (u.get("username") or "").replace("_", "\\_")
        un_str = f"@{un}" if un else f"id{u['telegram_id']}"
        status_icon = icons.get(u.get("status", ""), "❓")

        lines = [
            f"{status_icon} *{safe_name}* ({un_str})",
            f"🆔 `{u['telegram_id']}`",
            f"📌 Статус: *{u.get('status', '—')}*",
        ]
        if u.get("trial_expires_at"):
            try:
                dt = datetime.fromisoformat(u["trial_expires_at"])
                dl = max((dt - datetime.utcnow()).days, 0)
                lines.append(f"🎁 Триал: {dt.strftime('%d.%m.%Y')} ({dl} дн.)")
            except Exception:
                pass
        if u.get("expires_at"):
            try:
                dt = datetime.fromisoformat(u["expires_at"])
                dl = max((dt - datetime.utcnow()).days, 0)
                lines.append(f"💎 Подписка: {dt.strftime('%d.%m.%Y')} ({dl} дн.)")
            except Exception:
                pass
        if u.get("daily_goal"):
            lines.append(f"🎯 Норма: {u['daily_goal']} ккал")
        streak = u.get("streak_days", 0)
        best   = u.get("best_streak", 0)
        if streak or best:
            lines.append(f"🔥 Серия: {streak} дн.  |  Рекорд: {best} дн.")
        if u.get("created_at"):
            lines.append(f"📅 Зарег.: {u['created_at'][:10]}")
        ref = u.get("referred_by")
        if ref:
            lines.append(f"🔗 Реферал от: `{ref}`")
        ref_s = get_referral_stats(u["telegram_id"])
        if ref_s["total"] > 0:
            lines.append(f"👥 Рефералов: {ref_s['total']} (оплатили: {ref_s['paid']})")
        return "\n".join(lines)

    # ── Admin inline callbacks ─────────────────────────────────────────────────
    @dp.callback_query(F.data.in_({"adm_stats", "adm_users", "adm_pending",
                                    "adm_paid", "adm_refresh", "adm_broadcast"}))
    async def cb_admin_panel(callback: CallbackQuery):
        if callback.from_user.id != ADMIN_ID:
            await callback.answer("Нет доступа.", show_alert=True)
            return
        await callback.answer()
        data = callback.data

        if data in ("adm_stats", "adm_refresh"):
            s = get_total_stats()
            text = (
                f"🛡 *Admin Panel*\n\n"
                f"👥 Всего: *{s['total_users']}*  "
                f"(⏳{s['pending']} ✅{s['beta']} 💎{s['paid']} 🚫{s['blocked']})\n"
                f"🆕 Новых сегодня: *{s['new_today']}*\n"
                f"📸 Анализов сегодня: *{s['analyses_today']}*  |  всего: {s['analyses_total']}\n"
                f"👁 DAU: *{s['dau']}*  |  WAU: *{s['wau']}*\n"
                f"📈 D1: *{s['d1_retention']}%*  |  D7: *{s['d7_retention']}%*\n"
                f"🔥 Средний стрик: *{s['avg_streak']} дн.*\n"
                f"🔗 Реф. оплат: *{s['referrals_paid']}*"
            )
            await callback.message.edit_text(text, parse_mode="Markdown",
                                             reply_markup=admin_panel_keyboard())
            return

        if data == "adm_broadcast":
            user_states[callback.from_user.id] = {"state": STATES["ADMIN_BROADCAST"], "data": {}}
            await callback.message.answer("📡 Введи текст рассылки:")
            return

        status_filter = "pending" if data == "adm_pending" else "paid"
        if data == "adm_users":
            status_filter = None
        users = get_all_users(status_filter)
        icons = {"pending": "⏳", "beta": "✅", "paid": "💎", "blocked": "🚫"}
        if not users:
            await callback.message.answer("Нет пользователей.")
            return
        label = status_filter.upper() if status_filter else "ВСЕ"
        lines = [f"*{label} ({min(len(users),25)}):*\n"]
        for u in users[:25]:
            nm = (u["first_name"] or "").replace("_", " ")[:15]
            un = u["username"] or ""
            lbl = f"{nm} (@{un})" if un else f"{nm} (id{u['telegram_id']})"
            lines.append(f"{icons.get(u['status'],'❓')} {lbl} — `{u['telegram_id']}`")
        await callback.message.answer("\n".join(lines), parse_mode="Markdown")

    @dp.callback_query(F.data.startswith("approve_"))
    async def cb_approve(callback: CallbackQuery):
        if callback.from_user.id != ADMIN_ID:
            await callback.answer("Нет доступа.", show_alert=True)
            return
        target_id = int(callback.data.split("_")[1])
        approve_user(target_id, trial_days=3)
        await callback.answer("✅ Одобрено")
        try:
            u = get_user(target_id)
            # Trigger onboarding if not yet completed
            if u and not u.get("onboarded"):
                user_name = u.get("first_name") or "друг"
                await start_onboarding(bot, target_id, user_name)
            else:
                await bot.send_message(
                    target_id,
                    "✅ *Доступ открыт! Добро пожаловать!* 🎉\n\n"
                    "Отправь фото еды — и я посчитаю калории за секунды 📸",
                    parse_mode="Markdown",
                    reply_markup=main_keyboard(False),
                )
        except Exception as e:
            log.warning(f"cb_approve send: {e}")
        try:
            u = get_user(target_id)
            await callback.message.edit_text(
                f"✅ Одобрен:\n{_fmt_user_card(u)}", parse_mode="Markdown"
            )
        except Exception:
            pass

    @dp.callback_query(F.data.startswith("block_"))
    async def cb_block(callback: CallbackQuery):
        if callback.from_user.id != ADMIN_ID:
            await callback.answer("Нет доступа.", show_alert=True)
            return
        target_id = int(callback.data.split("_")[1])
        set_status(target_id, "blocked")
        await callback.answer("🚫 Заблокировано")
        try:
            await callback.message.edit_text(f"🚫 Заблокирован: `{target_id}`",
                                             parse_mode="Markdown")
        except Exception:
            pass

    @dp.callback_query(F.data.startswith("give7_"))
    async def cb_give7(callback: CallbackQuery):
        if callback.from_user.id != ADMIN_ID:
            await callback.answer("Нет доступа.", show_alert=True)
            return
        target_id = int(callback.data.split("_")[1])
        activate_subscription(target_id, 7)
        u = get_user(target_id)
        exp = datetime.fromisoformat(u["expires_at"]).strftime("%d.%m.%Y") if u and u.get("expires_at") else "?"
        await callback.answer(f"✅ +7 дней → до {exp}")
        try:
            await bot.send_message(target_id, f"🎁 *+7 дней* к подписке → до *{exp}*!",
                                   parse_mode="Markdown")
        except Exception:
            pass

    @dp.callback_query(F.data.startswith("give30_"))
    async def cb_give30(callback: CallbackQuery):
        if callback.from_user.id != ADMIN_ID:
            await callback.answer("Нет доступа.", show_alert=True)
            return
        target_id = int(callback.data.split("_")[1])
        activate_subscription(target_id, 30)
        u = get_user(target_id)
        exp = datetime.fromisoformat(u["expires_at"]).strftime("%d.%m.%Y") if u and u.get("expires_at") else "?"
        await callback.answer(f"✅ +30 дней → до {exp}")
        try:
            await bot.send_message(target_id, f"🎁 *+30 дней* к подписке → до *{exp}*!",
                                   parse_mode="Markdown")
        except Exception:
            pass

    @dp.callback_query(F.data.startswith("activate_"))
    async def cb_activate(callback: CallbackQuery):
        if callback.from_user.id != ADMIN_ID:
            await callback.answer("Нет доступа.", show_alert=True)
            return
        target_id = int(callback.data.split("_")[1])
        activate_subscription(target_id, SUB_DAYS)
        u = get_user(target_id)
        exp = datetime.fromisoformat(u["expires_at"]).strftime("%d.%m.%Y") if u and u.get("expires_at") else "?"
        await callback.answer(f"⚡ Активировано до {exp}")
        try:
            await bot.send_message(target_id,
                f"⚡ *Подписка активирована* до *{exp}*!",
                parse_mode="Markdown", reply_markup=main_keyboard(False))
        except Exception:
            pass

    # ── Onboarding callbacks ───────────────────────────────────────────────────
    @dp.callback_query(F.data.startswith("gtype_"))
    async def cb_goal_type(callback: CallbackQuery):
        uid = callback.from_user.id
        await callback.answer()
        state = user_states.get(uid, {})
        goal_type = callback.data.split("_")[1]
        state.setdefault("data", {})["goal_type"] = goal_type

        labels = {"lose": "📉 Похудеть", "gain": "📈 Набрать массу",
                  "maintain": "⚖️ Поддерживать", "track": "📊 Просто считать"}
        label = labels.get(goal_type, goal_type)

        if goal_type == "track":
            # Skip detailed calculation — mark onboarded and go straight to menu
            mark_onboarded(uid)
            user_states.pop(uid, None)
            is_admin = uid == ADMIN_ID
            await callback.message.edit_text(
                f"✅ *{label}* — запомнил!\n\n"
                f"Отправляй фото еды — начнём считать 📸",
                parse_mode="Markdown",
            )
            await bot.send_message(uid,
                "Главное меню готово 👇",
                reply_markup=main_keyboard(is_admin))
            return

        # Ask for weight
        user_states[uid] = {"state": STATES["ONBOARD_WEIGHT"], "data": {"goal_type": goal_type}}
        await callback.message.edit_text(
            f"*{label}* — отличный выбор! 💪\n\n"
            f"Введи свой текущий вес в кг:\n_(например: 75 или 75.5)_",
            parse_mode="Markdown",
        )

    # ── Goal setup callbacks ───────────────────────────────────────────────────
    @dp.callback_query(F.data.in_({"goal_know", "goal_calc", "goal_skip"}))
    async def cb_goal_ask(callback: CallbackQuery):
        uid = callback.from_user.id
        await callback.answer()
        data = callback.data

        if data == "goal_skip":
            user_states.pop(uid, None)
            await callback.message.edit_text("Ок, норму можно задать позже — кнопка ⚙️ Профиль.")
            return

        if data == "goal_know":
            user_states[uid] = {"state": STATES["GOAL_ENTER"], "data": {}}
            await callback.message.edit_text(
                "Введи свою дневную норму в ккал (например: 2000):"
            )
            return

        # goal_calc → ask gender
        user_states[uid] = {"state": "calc_gender", "data": {}}
        await callback.message.edit_text(
            "🧮 *Рассчитаю норму по формуле Mifflin-St Jeor*\n\nВыбери пол:",
            parse_mode="Markdown",
            reply_markup=gender_keyboard(),
        )

    @dp.callback_query(F.data.in_({"gender_m", "gender_f"}))
    async def cb_gender(callback: CallbackQuery):
        uid = callback.from_user.id
        await callback.answer()
        state_obj = user_states.get(uid, {})
        gender = "m" if callback.data == "gender_m" else "f"
        state_obj.setdefault("data", {})["gender"] = gender

        cur_state = state_obj.get("state", "")

        # Onboarding flow: after age entry we wait for gender
        if cur_state == "onboard_wait_gender":
            state_obj["state"] = "onboard_wait_activity"
            user_states[uid] = state_obj
            await callback.message.edit_text(
                f"{'♂️' if gender == 'm' else '♀️'} Записал!\n\nУровень активности:",
                parse_mode="Markdown",
                reply_markup=activity_keyboard(),
            )
            return

        # Regular goal-calc flow
        user_states[uid] = {"state": STATES["CALC_WEIGHT"], "data": state_obj["data"]}
        await callback.message.edit_text(
            "Введи свой вес в кг:\n_(например: 75 или 75.5)_",
            parse_mode="Markdown",
        )

    @dp.callback_query(F.data.startswith("act_"))
    async def cb_activity(callback: CallbackQuery):
        uid = callback.from_user.id
        await callback.answer()
        state_obj = user_states.get(uid, {})
        activity = float(callback.data.split("_")[1])
        d = state_obj.get("data", {})
        d["activity"] = activity

        gender    = d.get("gender", "m")
        age       = d.get("age", 25)
        weight    = d.get("weight", 70.0)
        height    = d.get("height", 170.0)
        goal_type = d.get("goal_type", "track")

        tdee, protein = calc_tdee(gender, age, weight, height, activity, goal_type)

        set_daily_goal(
            uid, tdee,
            protein_goal=protein,
            goal_type=goal_type,
            weight_kg=weight,
            height_cm=height,
            age=age,
            gender=gender,
        )

        is_onboarding = d.get("_from_onboard") or state_obj.get("state") == "onboard_wait_activity"
        user_states.pop(uid, None)

        if is_onboarding:
            mark_onboarded(uid)
            is_admin = uid == ADMIN_ID
            await callback.message.edit_text(
                f"🔥 *Готово!*\n\n"
                f"Твоя цель:\n"
                f"🎯 *{tdee} ккал* в день\n"
                f"💪 *{protein} г белка*\n\n"
                f"Теперь просто отправляй фото еды 📸",
                parse_mode="Markdown",
            )
            await bot.send_message(uid, "Главное меню 👇", reply_markup=main_keyboard(is_admin))
        else:
            await callback.message.edit_text(
                f"✅ *Норма установлена!*\n\n"
                f"🎯 {tdee} ккал/день\n"
                f"💪 Белок: {protein}г",
                parse_mode="Markdown",
            )

    # ── Profile inline callbacks ───────────────────────────────────────────────
    @dp.callback_query(F.data == "profile_goal")
    async def cb_profile_goal(callback: CallbackQuery):
        uid = callback.from_user.id
        await callback.answer()
        user = get_user(uid)
        current = user["daily_goal"] if user else None
        prefix = f"Текущая норма: *{current} ккал*\n\n" if current else ""
        user_states[uid] = {"state": "goal_ask", "data": {}}
        await callback.message.answer(
            f"{prefix}🎯 *Изменить норму калорий*\n\nВыбери способ:",
            parse_mode="Markdown",
            reply_markup=goal_ask_keyboard(),
        )

    @dp.callback_query(F.data == "weight_opt")
    async def cb_weight_opt(callback: CallbackQuery):
        uid = callback.from_user.id
        await callback.answer()
        user = get_user(uid)
        last_weight = f"  (последний: *{user['weight_kg']} кг*)" if user and user.get("weight_kg") else ""
        await callback.message.answer(
            f"⚖️ *Хочешь записать вес сегодня?*{last_weight}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Да", callback_data="profile_weight"),
                InlineKeyboardButton(text="⏭ Не сегодня", callback_data="weight_skip"),
            ]]),
        )

    @dp.callback_query(F.data == "weight_skip")
    async def cb_weight_skip(callback: CallbackQuery):
        await callback.answer("Ок, в следующий раз 👍")
        try:
            await callback.message.delete()
        except Exception:
            pass

    @dp.callback_query(F.data == "profile_weight")
    async def cb_profile_weight(callback: CallbackQuery):
        uid = callback.from_user.id
        await callback.answer()
        user_states[uid] = {"state": STATES["WEIGHT_LOG"], "data": {}}
        await callback.message.answer(
            "⚖️ Введи свой текущий вес в кг:\n_(например: 75 или 75.5)_",
            parse_mode="Markdown",
        )

    @dp.callback_query(F.data == "profile_week")
    async def cb_profile_week(callback: CallbackQuery):
        uid = callback.from_user.id
        await callback.answer()
        stats = get_weekly_stats(uid)
        if stats["logged_days"] == 0:
            await callback.message.answer("📊 Записей за неделю пока нет. Начни сегодня 💪")
            return

        lines = ["📅 *Последние 7 дней:*\n"]
        day_names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
        for i, (d, data) in enumerate(zip(stats["dates"], stats["daily"])):
            dt = datetime.strptime(d, "%Y-%m-%d")
            dn = day_names[dt.weekday()]
            if data["kcal"] > 0:
                bar = "█" * min(int(data["kcal"] / 200), 10)
                lines.append(f"{dn} {d[5:]} {bar} {data['kcal']} ккал")
            else:
                lines.append(f"{dn} {d[5:]} ░░░░░░░░░░ —")

        ci = "🔥" if stats["consistency"] >= 80 else "📊" if stats["consistency"] >= 50 else "💤"
        lines.append(f"\n{ci} Постоянство: *{stats['consistency']}%*")
        lines.append(f"🔥 Среднее: *{stats['avg_kcal']} ккал*")
        lines.append(f"💪 Средний белок: *{stats['avg_protein']}г*")
        await callback.message.answer("\n".join(lines), parse_mode="Markdown")

    @dp.callback_query(F.data == "profile_status")
    async def cb_profile_status(callback: CallbackQuery):
        uid = callback.from_user.id
        await callback.answer()
        user = get_user(uid)
        if not user:
            await callback.message.answer("Напиши /start для регистрации.")
            return
        await _send_status(callback.message.answer, uid, user)

    async def _send_status(send_fn, uid: int, user: dict):
        status = user["status"]
        used = get_daily_usage(uid)
        macros = get_daily_macros(uid)
        total = macros["kcal"]
        ref_s = get_referral_stats(uid)
        goal = user["daily_goal"]
        streak = user.get("streak_days", 0)
        best_streak = user.get("best_streak", 0)
        kcal_str = f"{total}/{goal}" if goal else str(total)

        streak_block = (
            f"\n{streak_emoji(streak)} Серия: *{streak}* дн.  |  Рекорд: *{best_streak}*"
            if streak > 0 or best_streak > 0 else ""
        )

        if status == "paid" and not check_subscription_expired(uid):
            exp_dt = datetime.fromisoformat(user["expires_at"])
            exp = exp_dt.strftime("%d.%m.%Y")
            dl = max((exp_dt - datetime.utcnow()).days, 0)
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
            trial_dt = datetime.fromisoformat(user["trial_expires_at"])
            trial_exp = trial_dt.strftime("%d.%m.%Y")
            dl = max((trial_dt - datetime.utcnow()).days, 0)
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
                f"Оформи подписку — ⭐ Подписка",
                parse_mode="Markdown",
            )

    # ── buy_sub callback ───────────────────────────────────────────────────────
    @dp.callback_query(F.data == "buy_sub")
    async def cb_buy_sub(callback: CallbackQuery):
        uid = callback.from_user.id
        await callback.answer()
        await bot.send_invoice(
            chat_id=uid,
            title="CalorieBot Premium — 30 дней",
            description="Безлимит · Трекер калорий · КБЖУ · Стрики · Недельные отчёты",
            payload=f"sub_30d_{uid}",
            currency="XTR",
            prices=[LabeledPrice(label="Premium 30 дней", amount=SUB_PRICE_STARS)],
        )

    @dp.callback_query(F.data == "ref_screen")
    async def cb_ref_screen(callback: CallbackQuery):
        uid = callback.from_user.id
        await callback.answer()
        await _show_referral(callback.message.answer, uid)

    # ── correct entry callback ─────────────────────────────────────────────────
    @dp.callback_query(F.data.startswith("correct:"))
    async def cb_correct_entry(callback: CallbackQuery):
        uid = callback.from_user.id
        await callback.answer()
        entry_id = int(callback.data.split(":")[1])
        user_states[uid] = {"state": STATES["CORRECT_ENTRY"], "data": {"entry_id": entry_id}}
        await callback.message.answer(
            "✏️ Введи правильное количество калорий (целое число):"
        )

    # ── Photo handler ──────────────────────────────────────────────────────────
    @dp.message(F.photo)
    async def handle_photo(message: Message):
        uid = message.from_user.id
        upsert_user(uid, message.from_user.username or "", message.from_user.first_name or "")
        user = get_user(uid)

        ok, reason = access_check(user)
        if not ok:
            await deny(message, reason)
            return

        if user["status"] == "beta":
            used = get_daily_usage(uid)
            if used >= BETA_DAILY_LIMIT:
                await message.answer(
                    f"📊 *Лимит {BETA_DAILY_LIMIT} анализов в день*\n\n"
                    f"Оформи подписку — безлимит 🚀",
                    parse_mode="Markdown",
                    reply_markup=premium_keyboard(),
                )
                return

        thinking_msg = await message.answer(
            "🔍 *Анализирую...*\n\n_Держи телефон в 10–15 см от еды_",
            parse_mode="Markdown",
        )
        try:
            photo: PhotoSize = message.photo[-1]
            file = await bot.get_file(photo.file_id)
            url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file.file_path}"
            async with httpx.AsyncClient() as client:
                photo_bytes = (await client.get(url, timeout=15)).content

            display, kcal, protein, fat, carbs, food_name = await analyze_food_photo(photo_bytes)
            await _deliver_analysis(message, uid, user, display, kcal, protein,
                                    fat, carbs, food_name, thinking_msg)

        except Exception as e:
            log.error(f"photo analysis error: {e}")
            try:
                await thinking_msg.delete()
            except Exception:
                pass
            await message.answer("⚠️ Не удалось проанализировать. Попробуй ещё раз.")

    # ── Payment ────────────────────────────────────────────────────────────────
    @dp.pre_checkout_query()
    async def pre_checkout(query: PreCheckoutQuery):
        await query.answer(ok=True)

    @dp.message(F.successful_payment)
    async def payment_done(message: Message):
        uid = message.from_user.id
        activate_subscription(uid, SUB_DAYS)
        exp = (datetime.utcnow() + timedelta(days=SUB_DAYS)).strftime("%d.%m.%Y")
        await message.answer(
            f"🎉 *Оплата прошла! Добро пожаловать в Premium!*\n\n"
            f"📅 Подписка до *{exp}*\n"
            f"📸 Безлимитные анализы разблокированы\n\n"
            f"Отправляй фото еды — я всегда рядом 🚀",
            parse_mode="Markdown",
            reply_markup=main_keyboard(uid == ADMIN_ID),
        )
        referrer_id = mark_referral_paid(uid)
        if referrer_id:
            activate_subscription(referrer_id, REFERRAL_BONUS_DAYS)
            try:
                ref_user = get_user(referrer_id)
                new_exp = (
                    datetime.fromisoformat(ref_user["expires_at"]).strftime("%d.%m.%Y")
                    if ref_user and ref_user.get("expires_at") else "—"
                )
                await bot.send_message(
                    referrer_id,
                    f"🎁 Твой реферал оплатил! *+{REFERRAL_BONUS_DAYS} дней* → до *{new_exp}*",
                    parse_mode="Markdown",
                )
            except Exception as e:
                log.warning(f"referral notify: {e}")
        user = get_user(uid)
        await notify_admin(bot, f"💰 Оплата: {user_label(user)} → до {exp}")

    # ── Menu text handlers ─────────────────────────────────────────────────────
    @dp.message(F.text.in_(MENU_BUTTONS))
    async def handle_menu(message: Message):
        uid = message.from_user.id
        text = message.text
        upsert_user(uid, message.from_user.username or "", message.from_user.first_name or "")

        if text == BTN_ADMIN:
            if uid != ADMIN_ID:
                return
            await show_admin_panel(message.answer)
            return

        user = get_user(uid)

        if text == BTN_PHOTO:
            ok, reason = access_check(user)
            if not ok:
                await deny(message, reason)
                return
            await message.answer(
                "📸 *Отправь фото еды*\n\n"
                "_Держи телефон в 10–15 см от блюда для точного результата_",
                parse_mode="Markdown",
            )
            return

        if text == BTN_MANUAL:
            ok, reason = access_check(user)
            if not ok:
                await deny(message, reason)
                return
            _set_state(uid, STATES["MANUAL_ENTRY"])
            await message.answer(
                "✍️ Напиши, что съел:\n\n"
                "_Например: «борщ 300г», «яблоко», «овсянка 100г с молоком»_\n\n"
                "Или /cancel для отмены",
                parse_mode="Markdown",
            )
            return

        if text == BTN_PROGRESS:
            ok, reason = access_check(user)
            if not ok:
                await deny(message, reason)
                return
            macros = get_daily_macros(uid)
            total = macros["kcal"]
            meals = get_daily_usage(uid)
            goal = user["daily_goal"]
            goal_protein = user.get("protein_goal")
            streak = user.get("streak_days", 0)
            streak_line = (
                f"\n{streak_emoji(streak)} Серия: *{streak} {'день' if streak == 1 else 'дней'}*"
                if streak > 0 else ""
            )
            score = calc_daily_score(total, macros["protein"], macros["fat"],
                                     macros["carbs"], goal, goal_protein, meals)
            score_line = f"\n{score_emoji(score)} Питание сегодня: *{score}/100*" if total > 0 else ""

            macros_line = ""
            if macros["protein"] > 0:
                macros_line = f"\n💪 Б: {macros['protein']}г  Ж: {macros['fat']}г  У: {macros['carbs']}г"

            if goal:
                remaining = max(goal - total, 0)
                bar = progress_bar(total, goal)
                over = total - goal
                extra = f"⚠️ Превышение на {over} ккал" if over > 0 else f"Осталось: {remaining} ккал"
                text_out = (
                    f"📊 *Сегодня*\n\n"
                    f"🔥 {total} / {goal} ккал\n"
                    f"{bar}\n"
                    f"{extra}{macros_line}\n"
                    f"🍽 Приёмов: {meals}"
                    f"{score_line}{streak_line}"
                )
            else:
                text_out = (
                    f"📊 *Сегодня*\n\n"
                    f"🔥 Съедено: {total} ккал{macros_line}\n"
                    f"🍽 Приёмов: {meals}"
                    f"{score_line}{streak_line}\n\n"
                    f"_Установи норму в ⚙️ Профиль_"
                )
            await message.answer(text_out, parse_mode="Markdown")
            return

        if text == BTN_PROFILE:
            ok, reason = access_check(user)
            if ok or reason in ("trial_expired", "sub_expired"):
                has_goal = bool(user and user.get("daily_goal"))
                name = (user.get("first_name") or "").split()[0] if user else ""
                streak = user.get("streak_days", 0) if user else 0
                best = user.get("best_streak", 0) if user else 0
                goal_line = (
                    f"🎯 Норма: *{user['daily_goal']} ккал*"
                    + (f"  💪 Белок: *{user['protein_goal']}г*" if user.get("protein_goal") else "")
                    if user and user.get("daily_goal") else
                    "🎯 Норма не задана"
                )
                weight_line = f"⚖️ Вес: *{user['weight_kg']} кг*" if user and user.get("weight_kg") else "⚖️ Вес не указан"
                streak_line = f"🔥 Серия: *{streak} дн.*  |  Рекорд: *{best} дн.*" if streak or best else ""

                await message.answer(
                    f"⚙️ *Профиль*\n\n"
                    f"{goal_line}\n"
                    f"{weight_line}\n"
                    f"{streak_line}",
                    parse_mode="Markdown",
                    reply_markup=profile_keyboard(uid, has_goal),
                )
            else:
                await deny(message, reason)
            return

        if text == BTN_SUB:
            await _show_premium_screen(message.answer, uid, user)
            return

        if text == BTN_REF:
            if not user or user.get("status") == "blocked":
                await message.answer("⛔ Доступ заблокирован.")
                return
            await _show_referral(message.answer, uid)
            return

    async def _show_premium_screen(send_fn, uid: int, user):
        status = user["status"] if user else "pending"
        is_premium = status == "paid" and not check_subscription_expired(user)  # pass dict

        if is_premium:
            exp_dt = datetime.fromisoformat(user["expires_at"])
            exp = exp_dt.strftime("%d.%m.%Y")
            dl = max((exp_dt - datetime.utcnow()).days, 0)
            await send_fn(
                f"💎 *Подписка активна*\n\n"
                f"📅 До *{exp}* — осталось *{dl} дн.*\n\n"
                f"Все возможности разблокированы 🚀",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text=f"🔄 Продлить ({SUB_PRICE_STARS} ⭐)",
                                        callback_data="buy_sub")
                ]]),
            )
            return

        await send_fn(
            "⭐ *Premium подписка*\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "🆓 *Бесплатно*\n"
            "• 5 анализов в день\n"
            "• Базовое КБЖУ\n"
            "• Дневной трекер\n\n"
            "💎 *Premium — 150 ⭐ / месяц*\n"
            "• Безлимитные анализы 📸\n"
            "• Умный AI-комментарий тренера\n"
            "• Ежедневные утро/вечер итоги ☀️🌙\n"
            "• Недельные отчёты 📊\n"
            "• Трекер веса и прогресс\n"
            "• Стрики и ачивки 🔥\n"
            "• Оценка питания /100\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "1 месяц = примерно *5 ₽/день*",
            parse_mode="Markdown",
            reply_markup=premium_keyboard(),
        )

    async def _show_referral(send_fn, uid: int):
        stats = get_referral_stats(uid)
        link = ref_link(uid)
        share_text = "Считаю калории по фото 📸 Попробуй бесплатно:"
        share_url = f"https://t.me/share/url?url={urllib.parse.quote(link)}&text={urllib.parse.quote(share_text)}"

        earned_days = stats["total"] * REFERRAL_JOIN_BONUS_DAYS + stats["paid"] * REFERRAL_BONUS_DAYS

        await send_fn(
            f"👥 *Приглашай друзей — получай дни бесплатно!*\n\n"
            f"🎁 За регистрацию друга: *+{REFERRAL_JOIN_BONUS_DAYS} дня*\n"
            f"💰 За его оплату подписки: *+{REFERRAL_BONUS_DAYS} дней*\n\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"📊 Приглашено: *{stats['total']}*\n"
            f"💎 Оплатили: *{stats['paid']}*\n"
            f"🎁 Заработано: *≈{earned_days} дней*\n"
            f"━━━━━━━━━━━━━━━━\n\n"
            f"🔗 Твоя ссылка:\n`{link}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📤 Поделиться с другом", url=share_url)],
                [InlineKeyboardButton(text="🔗 Открыть мою ссылку", url=link)],
            ]),
        )

    # ── FSM text input handler ─────────────────────────────────────────────────
    @dp.message(F.text)
    async def handle_text(message: Message):
        uid = message.from_user.id
        text = message.text.strip()

        # Global cancel
        if text.lower() in ("/cancel", "отмена", "cancel"):
            user_states.pop(uid, None)
            await message.answer("✅ Отменено.", reply_markup=main_keyboard(uid == ADMIN_ID))
            return

        upsert_user(uid, message.from_user.username or "", message.from_user.first_name or "")
        user = get_user(uid)
        state_data = user_states.get(uid, {})
        state = state_data.get("state")

        # ── Admin broadcast state ──────────────────────────────────────────────
        if state == STATES["ADMIN_BROADCAST"] and uid == ADMIN_ID:
            users = get_active_users()
            sent = failed = 0
            for u in users:
                try:
                    await bot.send_message(u["telegram_id"], text)
                    sent += 1
                except Exception:
                    failed += 1
                await asyncio.sleep(0.05)  # Telegram rate limit
            user_states.pop(uid, None)
            await message.answer(f"📡 Рассылка: ✅{sent} ❌{failed}")
            return

        # ── Onboarding weight ──────────────────────────────────────────────────
        if state == STATES["ONBOARD_WEIGHT"]:
            try:
                weight = float(text.replace(",", "."))
                if not (20 <= weight <= 400):
                    raise ValueError
            except ValueError:
                await message.answer("⚠️ Введи корректный вес от 20 до 400 кг:")
                return
            state_data["data"]["weight"] = weight
            state_data["state"] = STATES["ONBOARD_HEIGHT"]
            user_states[uid] = state_data
            await message.answer(
                f"Вес *{weight} кг* — записал ✅\n\nТеперь рост в см:\n_(например: 175)_",
                parse_mode="Markdown",
            )
            return

        # ── Onboarding height ──────────────────────────────────────────────────
        if state == STATES["ONBOARD_HEIGHT"]:
            try:
                height = float(text.replace(",", "."))
                if not (100 <= height <= 250):
                    raise ValueError
            except ValueError:
                await message.answer("⚠️ Введи корректный рост от 100 до 250 см:")
                return
            state_data["data"]["height"] = height
            state_data["state"] = STATES["ONBOARD_AGE"]
            user_states[uid] = state_data
            await message.answer(
                f"Рост *{int(height)} см* — отлично ✅\n\nСколько тебе лет?",
                parse_mode="Markdown",
            )
            return

        # ── Onboarding age ─────────────────────────────────────────────────────
        if state == STATES["ONBOARD_AGE"]:
            try:
                age = int(text)
                if not (10 <= age <= 100):
                    raise ValueError
            except ValueError:
                await message.answer("⚠️ Введи возраст от 10 до 100:")
                return
            state_data["data"]["age"] = age
            state_data["data"]["_from_onboard"] = True
            state_data["state"] = "onboard_wait_gender"
            user_states[uid] = state_data
            await message.answer(
                f"*{age} лет* — отлично! ✅\n\nПол (для точного расчёта нормы):",
                parse_mode="Markdown",
                reply_markup=gender_keyboard(),
            )
            return

        # ── Goal manual enter ──────────────────────────────────────────────────
        if state == STATES["GOAL_ENTER"]:
            try:
                kcal = int(text)
                if not (500 <= kcal <= 10000):
                    raise ValueError
            except ValueError:
                await message.answer("⚠️ Введи число от 500 до 10000:")
                return
            set_daily_goal(uid, kcal)
            user_states.pop(uid, None)
            await message.answer(
                f"✅ *Норма установлена: {kcal} ккал*",
                parse_mode="Markdown",
                reply_markup=main_keyboard(uid == ADMIN_ID),
            )
            return

        # ── Calc weight ────────────────────────────────────────────────────────
        if state == STATES["CALC_WEIGHT"]:
            try:
                weight = float(text.replace(",", "."))
                if not (20 <= weight <= 400):
                    raise ValueError
            except ValueError:
                await message.answer("⚠️ Введи вес от 20 до 400 кг:")
                return
            state_data["data"]["weight"] = weight
            state_data["state"] = STATES["CALC_HEIGHT"]
            user_states[uid] = state_data
            await message.answer("Рост в см (например: 175):")
            return

        # ── Calc height ────────────────────────────────────────────────────────
        if state == STATES["CALC_HEIGHT"]:
            try:
                height = float(text.replace(",", "."))
                if not (100 <= height <= 250):
                    raise ValueError
            except ValueError:
                await message.answer("⚠️ Введи рост от 100 до 250 см:")
                return
            state_data["data"]["height"] = height
            state_data["state"] = STATES["CALC_AGE"]
            user_states[uid] = state_data
            await message.answer("Возраст (лет):")
            return

        # ── Calc age ───────────────────────────────────────────────────────────
        if state == STATES["CALC_AGE"]:
            try:
                age = int(text)
                if not (10 <= age <= 100):
                    raise ValueError
            except ValueError:
                await message.answer("⚠️ Введи возраст от 10 до 100:")
                return
            state_data["data"]["age"] = age
            user_states[uid] = state_data
            await message.answer("Уровень активности:", reply_markup=activity_keyboard())
            return

        # ── Correct entry ──────────────────────────────────────────────────────
        if state == STATES["CORRECT_ENTRY"]:
            try:
                new_kcal = int(text)
                if not (1 <= new_kcal <= 99999):
                    raise ValueError
            except ValueError:
                await message.answer("⚠️ Введи число от 1 до 99999 или /cancel для отмены:")
                return
            entry_id = state_data["data"].get("entry_id")
            if entry_id:
                update_entry_calories(entry_id, new_kcal)
            user_states.pop(uid, None)
            progress = daily_progress_text(uid)
            await message.answer(
                f"✅ *Исправлено: {new_kcal} ккал*{progress}",
                parse_mode="Markdown",
                reply_markup=main_keyboard(uid == ADMIN_ID),
            )
            return

        # ── Weight log ─────────────────────────────────────────────────────────
        if state == STATES["WEIGHT_LOG"]:
            try:
                weight = float(text.replace(",", "."))
                if not (20 <= weight <= 400):
                    raise ValueError
            except ValueError:
                await message.answer("⚠️ Введи вес от 20 до 400 кг или /cancel:")
                return
            add_weight_log(uid, weight)
            user_states.pop(uid, None)

            history = get_weight_history(uid, days=30)
            diff_line = ""
            if len(history) >= 2:
                diff = round(weight - history[-2][1], 1)
                sign = "+" if diff > 0 else ""
                diff_line = f"\nИзменение: *{sign}{diff} кг*"

            await message.answer(
                f"✅ *Вес записан: {weight} кг*{diff_line}\n\n"
                f"Так держать — следить за весом важно! 💪",
                parse_mode="Markdown",
                reply_markup=main_keyboard(uid == ADMIN_ID),
            )
            return

        # ── Manual food entry ──────────────────────────────────────────────────
        if state == STATES["MANUAL_ENTRY"]:
            ok, reason = access_check(user)
            if not ok:
                user_states.pop(uid, None)
                await deny(message, reason)
                return

            if user["status"] == "beta":
                used = get_daily_usage(uid)
                if used >= BETA_DAILY_LIMIT:
                    user_states.pop(uid, None)
                    await message.answer(
                        f"📊 *Лимит {BETA_DAILY_LIMIT} анализов в день*\n\nОформи подписку 🚀",
                        parse_mode="Markdown",
                        reply_markup=premium_keyboard(),
                    )
                    return

            thinking_msg = await message.answer("🤔 *Считаю...*", parse_mode="Markdown")
            try:
                display, kcal, protein, fat, carbs, food_name = await analyze_food_text(text)
                await _deliver_analysis(message, uid, user, display, kcal, protein,
                                        fat, carbs, food_name, thinking_msg)
            except Exception as e:
                log.error(f"manual entry analysis error: {e}")
                try:
                    await thinking_msg.delete()
                except Exception:
                    pass
                user_states.pop(uid, None)
                await message.answer("⚠️ Не удалось. Попробуй ещё раз или /cancel")
            return

        # ── Default: treat as food description ────────────────────────────────
        ok, reason = access_check(user)
        if not ok:
            await deny(message, reason)
            return

        if user["status"] == "beta":
            used = get_daily_usage(uid)
            if used >= BETA_DAILY_LIMIT:
                await message.answer(
                    f"📊 *Лимит {BETA_DAILY_LIMIT} анализов в день*\n\nОформи подписку 🚀",
                    parse_mode="Markdown",
                    reply_markup=premium_keyboard(),
                )
                return

        thinking_msg = await message.answer("🤔 *Считаю...*", parse_mode="Markdown")
        try:
            display, kcal, protein, fat, carbs, food_name = await analyze_food_text(text)
            await _deliver_analysis(message, uid, user, display, kcal, protein,
                                    fat, carbs, food_name, thinking_msg)
        except Exception as e:
            log.error(f"default text error: {e}")
            try:
                await thinking_msg.delete()
            except Exception:
                pass
            await message.answer(
                "Не понял. Попробуй:\n"
                "• отправить *фото* блюда 📸\n"
                "• или написать, что съел (например: «гречка 200г»)\n"
                "• или нажать кнопку ✍️ Ввести вручную",
                parse_mode="Markdown",
                reply_markup=main_keyboard(uid == ADMIN_ID),
            )

    # ── Start polling ─────────────────────────────────────────────────────────
    log.info("Bot started. Polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
