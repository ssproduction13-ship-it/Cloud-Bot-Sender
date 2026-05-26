import asyncio
import os
import re
import sys
import random
import logging
import base64
import urllib.parse
import httpx
from datetime import datetime, timedelta, timezone
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
    clear_all_goals,
    mark_onboarded,
    save_onboard_state,
    load_onboard_state,
    clear_onboard_state,
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
    get_entries_today,
    delete_entry,
    reset_today_entries,
    set_user_goals,
    get_expiring_users,
    get_winback_users,
    get_streak_users_no_log_today,
    add_water_log,
    get_water_today,
    reset_water_today,
    get_users_by_segment,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

_utcnow = lambda: datetime.now(timezone.utc).replace(tzinfo=None)

BOT_TOKEN     = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_ID      = int(os.environ["TELEGRAM_CHAT_ID"])
BOT_USERNAME  = os.environ.get("BOT_USERNAME", "")

BETA_DAILY_LIMIT      = 5
SUB_PRICE_STARS       = 150
SUB_DAYS              = 30
REFERRAL_BONUS_DAYS   = 7
REFERRAL_JOIN_BONUS_DAYS = 3
SUB_PRICE_3M          = 360   # 3 months (−20%)
SUB_PRICE_12M         = 990   # 12 months (−45%)
SUB_DAYS_3M           = 90
SUB_DAYS_12M          = 365
WATER_GOAL            = 8     # glasses per day

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
    "MANUAL_ENTRY":           "manual_entry",
    "CORRECT_ENTRY":          "correct_entry",
    "WEIGHT_LOG":             "weight_log",
    # Admin
    "ADMIN_GIVE_DAYS":        "admin_give_days",
    "ADMIN_BROADCAST":        "admin_broadcast",
}

# State TTL: auto-expire stale FSM states after 1 hour of inactivity
_STATE_TTL_SECONDS = 3600

ONBOARD_STATES: set = {"ob_goal", "ob_gender", "ob_age", "ob_height", "ob_weight", "ob_activity"}


def _get_state(uid: int) -> dict:
    """Return user FSM state, evicting entries older than _STATE_TTL_SECONDS."""
    s = user_states.get(uid)
    if not s:
        return {}
    ts = s.get("_ts")
    if ts and (_utcnow() - ts).total_seconds() > _STATE_TTL_SECONDS:
        user_states.pop(uid, None)
        return {}
    return s


def _set_state(uid: int, state: str, data: dict | None = None):
    user_states[uid] = {"state": state, "data": data or {}, "_ts": _utcnow()}


def _set_onboard_state(uid: int, state: str, data: dict) -> None:
    """Write onboarding state to memory AND persist to DB so restarts don't lose progress."""
    user_states[uid] = {"state": state, "data": data, "_ts": _utcnow()}
    try:
        save_onboard_state(uid, state, data)
    except Exception as exc:
        log.warning(f"save_onboard_state uid={uid}: {exc}")


def _try_restore_onboard(uid: int) -> None:
    """If the user has no in-memory state, try to restore onboarding progress from DB."""
    if uid in user_states:
        return
    try:
        persisted = load_onboard_state(uid)
    except Exception:
        return
    if persisted and persisted.get("state") in ONBOARD_STATES:
        user_states[uid] = {
            "state": persisted["state"],
            "data": persisted["data"],
            "_ts": _utcnow(),
        }
        log.info(f"Restored onboard state uid={uid} state={persisted['state']}")


def _calc_calorie_goal(gender, age, height_cm, weight_kg, goal_type, activity="moderate"):
    """Mifflin-St Jeor BMR -> TDEE (activity multiplier) -> adjust for goal."""
    PAL = {
        "sedentary": 1.2,    # сидячий образ жизни
        "light":     1.375,  # лёгкие тренировки 1-3 дня/неделю
        "moderate":  1.55,   # умеренные тренировки 3-5 дней/неделю
        "active":    1.725,  # интенсивные тренировки 6-7 дней/неделю
        "very_active": 1.9,  # физическая работа или 2x тренировки
    }
    pal = PAL.get(activity, 1.55)
    if gender == "female":
        bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age - 161
    else:
        bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age + 5
    tdee = round(bmr * pal)
    if goal_type == "lose":
        kcal_goal = max(tdee - 400, 1200)
    elif goal_type == "gain":
        kcal_goal = tdee + 300
    else:
        kcal_goal = tdee
    protein_goal = round(weight_kg * (2.2 if goal_type == "gain" else 2.0))
    return round(kcal_goal), protein_goal


# ── Кнопки меню ──────────────────────────────────────────────────────────────
BTN_PHOTO    = "📸 Анализ фото"
BTN_MANUAL   = "✍️ Вручную"
BTN_PROGRESS = "📊 Мой прогресс"
BTN_SUB      = "⭐ Premium"
BTN_REF      = "🎁 Бонусы"
BTN_PROFILE  = "⚙️ Профиль"
BTN_ADMIN    = "🛠 Админка"
BTN_WATER    = "💧 Вода"
BTN_PLAN     = "🍽 План питания"

MENU_BUTTONS = {
    BTN_PHOTO, BTN_MANUAL, BTN_PROGRESS,
    BTN_SUB, BTN_REF, BTN_PROFILE, BTN_ADMIN,
    BTN_WATER, BTN_PLAN,
}


def main_keyboard(is_admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_PHOTO),   KeyboardButton(text=BTN_MANUAL)],
        [KeyboardButton(text=BTN_PROGRESS), KeyboardButton(text=BTN_PROFILE)],
        [KeyboardButton(text=BTN_WATER),   KeyboardButton(text=BTN_PLAN)],
        [KeyboardButton(text=BTN_SUB),     KeyboardButton(text=BTN_REF)],
    ]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


# ─────────────────── AI анализ ───────────────────────────────────────────────

VISION_PROMPT = """Ты — эксперт по еде. Опиши фото:
1. Блюдо/продукты (точно)
2. Способ приготовления
3. Примерный вес порции (г)
4. Основные ингредиенты и количество

Если это не еда — напиши только: НЕ ЕДА"""

NUTRITION_PROMPT = """Ты — AI-тренер по питанию. Рассчитай КБЖУ.

Блюдо: {desc}

Ответь СТРОГО в этом формате:
🍽 *{{название}}* (~{{вес}} г)

🔥 {{ккал}} ккал
Б {{б}} • Ж {{ж}} • У {{у}} г

💬 {{1-2 коротких строки. Конкретно, без воды. Максимум 15 слов. Один эмодзи в конце.}}

KCAL:{{ккал}}
PROTEIN:{{б}}
FAT:{{ж}}
CARBS:{{у}}
NAME:{{название}}"""

TEXT_NUTRITION_PROMPT = """Ты — AI-тренер по питанию.

Блюдо/продукт: {desc}

Если это не еда — ответь только: НЕ ЕДА

Иначе ответь СТРОГО в этом формате:
🍽 *{{название}}* (~{{вес}} г)

🔥 {{ккал}} ккал
Б {{б}} • Ж {{ж}} • У {{у}} г

💬 {{1-2 коротких строки. Конкретно, без воды. Максимум 15 слов. Один эмодзи в конце.}}

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


def _validate_analysis(display, kcal, protein, fat, carbs) -> tuple[bool, str | None]:
    """Sanity-check AI response before sending to user."""
    if kcal is None:
        return False, "⚠️ Не удалось распознать калории. Опиши блюдо подробнее или введи вручную."
    if not (20 <= kcal <= 6000):
        return False, f"⚠️ Получилось {kcal} ккал — похоже на ошибку. Попробуй ещё раз."
    if protein is not None and not (0 <= protein <= 500):
        return False, "⚠️ Некорректные данные по белку. Попробуй ещё раз."
    if fat is not None and not (0 <= fat <= 500):
        return False, "⚠️ Некорректные данные по жирам. Попробуй ещё раз."
    if carbs is not None and not (0 <= carbs <= 1000):
        return False, "⚠️ Некорректные данные по углеводам. Попробуй ещё раз."
    if not display or len(display.strip()) < 10:
        return False, "⚠️ Пустой ответ от AI. Попробуй ещё раз."
    bad = ["не ед", "error", "sorry", "cannot", "не могу", "не знаю", "unable", "не понимаю"]
    if any(p in display.lower() for p in bad):
        return False, "🙅 Это не похоже на еду. Введи название блюда или продукта."
    return True, None


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
    if score >= 50: return "🌱"
    return "💤"


def format_score(score_100: int) -> float:
    """Convert 0-100 score to 5.0-10.0 display scale (never below 5.0)."""
    return max(round(score_100 / 10, 1), 5.0)


def ai_score_comment(score_100: int, protein: float, carbs: float, kcal: int,
                     goal_kcal: int | None, food_name: str | None) -> str:
    """Return a short friendly AI-coach comment based on the day's nutrition."""
    fn = (food_name or "").lower()
    cheat = any(k in fn for k in ["бургер","пицца","чипсы","фри","kfc","mcdonald","нагетсы"])
    sugar = any(k in fn for k in ["сахар","конфеты","торт","пирожное","кола","газировка"])
    if score_100 >= 85:
        return "Отличный день — так и держи! 💪"
    if cheat:
        return "Чит-мил? Раз в неделю — это нормально 😄 Завтра вернёмся в ритм."
    if sugar:
        return "Многовато сахара, но по калориям всё ок 👌 Запей водой."
    if protein > 0 and protein < 60:
        return "Белка чуть маловато — добавь яйца, творог или курицу 🥩"
    if goal_kcal and kcal > goal_kcal * 1.2:
        return "Немного перебор сегодня — завтра чуть полегче, всё выровняется 👍"
    if goal_kcal and kcal < goal_kcal * 0.7:
        return "Маловато калорий — не голодай, это замедляет прогресс 🙏"
    if score_100 >= 70:
        return "Хороший выбор. Главное — стабильность, а не идеальность 🌱"
    return "Держишь курс — продолжай, всё идёт как надо 🔥"


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
        f"\n🔥 Серия: *{streak} {'день' if streak == 1 else 'дней'}*"
        if streak > 0 else ""
    )

    if not goal:
        return f"\n\n📊 *Сегодня: {total} ккал*{streak_line}"

    remaining = max(goal - total, 0)
    over = total - goal
    status_line = f"⚡ +{over} ккал сверх нормы" if over > 0 else f"Осталось {remaining} ккал"

    return (
        f"\n\n📊 *Сегодня: {total} / {goal} ккал*\n"
        f"{status_line}"
        f"{streak_line}"
    )


# ── Keyboards ─────────────────────────────────────────────────────────────────


def result_keyboard(entry_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✏️ Исправить калории", callback_data=f"correct:{entry_id}")
    ]])


def diary_keyboard(entries: list) -> InlineKeyboardMarkup:
    """One row per entry: [Название — ккал (label)] [✏️] [🗑]."""
    rows = []
    for e in entries:
        kcal = e["calories"] or 0
        name = (e.get("food_name") or "блюдо").strip()
        # Truncate name so total label stays readable; ✏️/🗑 stay narrow on the right
        label = f"{name[:28]} — {kcal} ккал" if len(name) <= 28 else f"{name[:26]}… — {kcal} ккал"
        rows.append([
            InlineKeyboardButton(text=label,  callback_data="noop"),
            InlineKeyboardButton(text="✏️",   callback_data=f"edit_e:{e['id']}"),
            InlineKeyboardButton(text="🗑",   callback_data=f"del_e:{e['id']}"),
        ])
    rows.append([InlineKeyboardButton(text="Сбросить весь день", callback_data="reset_day")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def progress_inline_keyboard() -> InlineKeyboardMarkup:
    """Small inline keyboard shown below the progress message."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📋 Дневник", callback_data="diary"),
    ]])


def new_user_keyboard(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💎 +7 дней",       callback_data=f"give7_{uid}"),
        InlineKeyboardButton(text="🚫 Заблокировать", callback_data=f"block_{uid}"),
    ]])


def profile_keyboard(uid: int, has_goal: bool = False) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Пересчитать норму", callback_data="recalc_norm")],
        [InlineKeyboardButton(text="⚖️ Записать вес", callback_data="weight_opt")],
        [InlineKeyboardButton(text="📈 Неделя", callback_data="profile_week")],
        [InlineKeyboardButton(text="ℹ️ Статус подписки", callback_data="profile_status")],
    ])


def premium_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{SUB_PRICE_STARS} ⭐ — 1 месяц (~7 ₽/день)",
            callback_data="buy_sub:30",
        )],
        [InlineKeyboardButton(
            text=f"{SUB_PRICE_3M} ⭐ — 3 месяца (~6 ₽/день, −20%)",
            callback_data="buy_sub:90",
        )],
        [InlineKeyboardButton(
            text=f"{SUB_PRICE_12M} ⭐ — 12 месяцев (~4 ₽/день, −45%)",
            callback_data="buy_sub:365",
        )],
        [InlineKeyboardButton(text="Получить бесплатно — реферал", callback_data="ref_screen")],
    ])




def admin_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📊 Статистика",    callback_data="adm_stats"),
            InlineKeyboardButton(text="🔄 Обновить",      callback_data="adm_refresh"),
        ],
        [
            InlineKeyboardButton(text="📋 Бета-юзеры",    callback_data="adm_beta"),
            InlineKeyboardButton(text="💎 Платные",       callback_data="adm_paid"),
        ],
        [
            InlineKeyboardButton(text="👥 Все юзеры",     callback_data="adm_users"),
            InlineKeyboardButton(text="📡 Рассылка",      callback_data="adm_broadcast"),
        ],
    ])


def user_action_keyboard(target_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💎 +7 дней",      callback_data=f"give7_{target_id}"),
            InlineKeyboardButton(text="💎 +30 дней",     callback_data=f"give30_{target_id}"),
        ],
        [
            InlineKeyboardButton(text="⚡ +30 дней (старт)", callback_data=f"activate_{target_id}"),
            InlineKeyboardButton(text="🚫 Блокировать",  callback_data=f"block_{target_id}"),
        ],
    ])




# ── Onboarding ────────────────────────────────────────────────────────────────


async def start_onboarding(bot: Bot, uid: int, name: str):
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
    ok, err_msg = _validate_analysis(display, kcal, protein, fat, carbs)
    if not ok:
        try:
            await thinking_msg.delete()
        except Exception:
            pass
        await message.answer(err_msg)
        return
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


    if food_name:
        fun = detect_fun_reaction(food_name.lower(), kcal)
        if fun:
            await message.answer(fun)

    # AI nutrition advice for premium users
    if (not check_subscription_expired(user)) and user.get("status") == "paid":
        try:
            goal = user.get("daily_goal")
            goal_protein = user.get("protein_goal")
            macros_now = get_daily_macros(uid)
            advice_prompt = (
                f"Пользователь только что съел: {food_name or 'блюдо'} ({kcal} ккал, Б{protein}г Ж{fat}г У{carbs}г).\n"
                f"Дневной итог: {macros_now['kcal']} ккал"
                + (f" из {goal}" if goal else "")
                + f", белок {macros_now['protein']}г"
                + (f" из {goal_protein}г" if goal_protein else "") + ".\n"
                "Дай ОДИН конкретный совет (1-2 предложения) что съесть следующим приёмом пищи "
                "для баланса КБЖУ. Без воды, только практика. Один эмодзи."
            )
            advice_resp = await openai_client.chat.completions.create(
                model="meta-llama/llama-4-scout-17b-16e-instruct",
                messages=[
                    {"role": "system", "content": "Краткий нутрициолог. Только конкретные советы."},
                    {"role": "user", "content": advice_prompt},
                ],
                max_tokens=120,
            )
            advice_text = advice_resp.choices[0].message.content or ""
            if advice_text.strip():
                await message.answer(f"💡 _{advice_text.strip()}_", parse_mode="Markdown")
        except Exception as adv_e:
            log.debug(f"ai advice error: {adv_e}")


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

            fs = format_score(score)
            comment = ai_score_comment(score, macros["protein"], macros["carbs"], total, goal, None)
            streak_line = (
                f"\n🔥 Серия: *{streak} {'день' if streak == 1 else 'дней'}* — не останавливайся!"
                if streak > 0 else ""
            )
            protein_line = f"\n🥩 Белок: *{macros['protein']}г*" if macros["protein"] > 0 else ""

            await bot.send_message(
                uid,
                f"🌙 *Итоги дня*\n\n"
                f"*{total}{f' / {goal}' if goal else ''} ккал*\n"
                f"{result_line}{protein_line}"
                f"{streak_line}\n\n"
                f"🍽 Balance Score: *{fs}/10*\n"
                f"💬 _{comment}_",
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

            goal = user.get("daily_goal", 0)
            avg_kcal = stats["avg_kcal"] or 0
            avg_protein = stats["avg_protein"] or 0
            logged = stats["logged_days"]

            forecast_line = ""
            if goal and avg_kcal > 0:
                diff = avg_kcal - goal
                if abs(diff) > 50:
                    kg_week = round(diff * 7 / 7700, 1)
                    sign = "+" if kg_week > 0 else ""
                    forecast_line = f"\n📉 Прогноз: *{sign}{kg_week} кг/нед.*"

            if logged >= 6:
                insight = "🔥 Отличная неделя — так держать!"
            elif logged >= 4:
                insight = "💪 Хорошая неделя — ещё немного стабильности!"
            else:
                insight = "🌱 Попробуй логировать каждый день — разница заметна!"

            await bot.send_message(
                uid,
                f"📊 *Итоги недели*\n\n"
                f"🍽 Среднее: *{avg_kcal} ккал/день*\n"
                f"🥩 Белок: *{avg_protein} г/день*\n"
                f"📅 Дней с записями: *{logged}/7*"
                f"{forecast_line}\n\n"
                f"💡 {insight}",
                parse_mode="Markdown",
            )
        except Exception as e:
            log.debug(f"weekly {uid}: {e}")
        await asyncio.sleep(0.05)  # Telegram rate limit


# ─────────────────── Бот ─────────────────────────────────────────────────────


# ── Subscription expiry reminders ────────────────────────────────────────────

async def send_expiry_reminders(bot: Bot):
    """Notify users 3 days and 1 day before subscription expires."""
    for days_left in (3, 1):
        for user in get_expiring_users(days_left):
            uid = user["telegram_id"]
            name = (user.get("first_name") or "").split()[0] or "Привет"
            exp = user["expires_at"][:10]
            try:
                exp_fmt = datetime.strptime(exp, "%Y-%m-%d").strftime("%d.%m.%Y")
            except Exception:
                exp_fmt = exp
            when = "завтра" if days_left == 1 else "через 3 дня"
            msg = (
                f"*{name}, подписка истекает {when}*\n\n"
                f"Дата окончания: *{exp_fmt}*\n\n"
                f"Продли сейчас — дни добавятся к текущей подписке, стрик и история сохранятся.\n\n"
                f"Тарифы:\n"
                f"• 1 мес — 150 ⭐ (~7 ₽/день)\n"
                f"• 3 мес — 360 ⭐ (~6 ₽/день)\n"
                f"• 12 мес — 990 ⭐ (~4 ₽/день)"
            )
            try:
                await bot.send_message(
                    uid, msg, parse_mode="Markdown",
                    reply_markup=premium_keyboard(),
                )
            except Exception as e:
                log.debug(f"expiry reminder {uid}: {e}")
            await asyncio.sleep(0.05)


async def send_winback_messages(bot: Bot):
    """3 days after expiry — send win-back message with discount offer."""
    for user in get_winback_users():
        uid = user["telegram_id"]
        name = (user.get("first_name") or "").split()[0] or "Привет"
        streak = user.get("streak_days", 0)
        streak_line = (
            f"\n🔥 У тебя был стрик *{streak} дней* — не дай ему пропасть!"
            if streak > 2 else ""
        )
        try:
            await bot.send_message(
                uid,
                f"👋 *{name}, скучаем по тебе!*\n\n"
                f"Прошло 3 дня с окончания подписки."
                f"{streak_line}\n\n"
                f"Возвращайся — продолжи следить за питанием и прогрессом! 💪",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⭐ Возобновить подписку", callback_data="show_premium")],
                ]),
            )
        except Exception as e:
            log.debug(f"winback {uid}: {e}")
        await asyncio.sleep(0.05)


async def send_streak_reminders(bot: Bot):
    """Evening nudge: users with active streaks who haven't logged today."""
    for user in get_streak_users_no_log_today():
        uid = user["telegram_id"]
        streak = user.get("streak_days", 0)
        name = (user.get("first_name") or "").split()[0] or "Привет"
        try:
            await bot.send_message(
                uid,
                f"🔥 *{name}, не прерывай серию!*\n\n"
                f"Ты на *{streak} {'день' if streak == 1 else 'дней'}* подряд — сегодня ещё нет записей.\n\n"
                f"📸 Сфотографируй ужин или введи что ел — займёт 10 секунд!",
                parse_mode="Markdown",
            )
        except Exception as e:
            log.debug(f"streak reminder {uid}: {e}")
        await asyncio.sleep(0.05)



async def main():
    init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()

    scheduler = AsyncIOScheduler(timezone="UTC")
    # Tyumen = UTC+5: 08:00 → 03:00 UTC, 20:00 → 15:00 UTC, Mon 09:00 → Mon 04:00 UTC
    scheduler.add_job(send_morning_checkins, "cron", hour=3,  minute=0, args=[bot])
    scheduler.add_job(send_evening_summaries, "cron", hour=19, minute=0, args=[bot])
    scheduler.add_job(send_weekly_reports, "cron", day_of_week="mon", hour=4, minute=0, args=[bot])
    # UTC+5 (Tyumen): 09:00 → 04:00 UTC, 21:30 → 16:30 UTC
    scheduler.add_job(send_expiry_reminders, "cron", hour=4,  minute=30, args=[bot])
    scheduler.add_job(send_winback_messages, "cron", hour=4,  minute=45, args=[bot])
    scheduler.add_job(send_streak_reminders, "cron", hour=16, minute=30, args=[bot])
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
                if not user["expires_at"] or datetime.fromisoformat(user["expires_at"]) < _utcnow():
                    activate_subscription(uid, 3650)
        elif is_new_user:
            # Auto-approve all new users with 7-day trial — no waiting required
            approve_user(uid, trial_days=7)

        user = get_user(uid)
        if not user or user["status"] == "blocked":
            await message.answer("⛔ Доступ заблокирован.")
            return

        # Notify admin about new users (info only — access already granted)
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

        status = user["status"]
        is_admin = uid == ADMIN_ID

        # Trigger onboarding for new/non-onboarded users
        if is_new_user or not user.get("onboarded"):
            await start_onboarding(bot, uid, name)
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

    # ── /restart_all (admin only) ──────────────────────────────────────────
    @dp.message(Command("restart_all"))
    async def cmd_restart_all(message: Message):
        if message.from_user.id != ADMIN_ID:
            return
        users = get_active_users()
        sent = failed = 0
        for u in users:
            try:
                await bot.send_message(
                    u["telegram_id"],
                    "🔄 *Бот обновлён!*\n\nЕсть новые функции — нажми любую кнопку ниже 👇",
                    parse_mode="Markdown",
                    reply_markup=main_keyboard(u["telegram_id"] == ADMIN_ID),
                )
                sent += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.05)
        await message.answer(f"✅ Разослано: {sent}, не доставлено: {failed}")

    # ── /admin ────────────────────────────────────────────────────────────────
    @dp.message(Command("cleargoals"))
    async def cmd_cleargoals(message: Message):
        if message.from_user.id != ADMIN_ID:
            return
        count = clear_all_goals()
        await message.answer(
            f"✅ Готово. Очищено у *{count}* пользователей:\n"
            f"daily\\_goal, protein\\_goal, goal\\_type, weight\\_kg, height\\_cm, age, gender",
            parse_mode="Markdown",
        )

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
            f"📌 *Команды:*\n"
            f"`/user ID` — карточка пользователя\n"
            f"`/give ID 30` — добавить дни подписки\n"
            f"`/block ID` — заблокировать\n"
            f"`/stats` — статистика  `/users` — список"
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
        approve_user(target_id, trial_days=7)
        await message.answer(f"✅ Пользователь {target_id} одобрен (7-дневный триал).")
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
            await message.answer("Использование: /give ID ДНЕЙ\nПример: /give 123456789 30")
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


    @dp.message(Command("giveall"))
    async def cmd_giveall(message: Message):
        if message.from_user.id != ADMIN_ID:
            return
        parts = message.text.split()
        if len(parts) < 2:
            await message.answer("Использование: /giveall ДНЕЙ\nПример: /giveall 5")
            return
        try:
            days = int(parts[1])
        except ValueError:
            await message.answer("Ошибка: ДНЕЙ должно быть числом.")
            return
        if days < 1 or days > 365:
            await message.answer("Дней должно быть от 1 до 365.")
            return

        # Fetch all non-blocked users
        all_users = get_all_users(None)
        targets = [u for u in all_users if u.get("status") != "blocked"]

        await message.answer(
            f"⏳ Начинаю раздачу +{days} дней для *{len(targets)}* пользователей...",
            parse_mode="Markdown",
        )

        ok = failed = 0
        for u in targets:
            try:
                activate_subscription(u["telegram_id"], days)
                ok += 1
            except Exception as e:
                log.warning(f"giveall error uid={u['telegram_id']}: {e}")
                failed += 1

        await message.answer(
            f"✅ Готово!\n"
            f"💎 +{days} дней выдано: *{ok}* пользователей\n"
            f"❌ Ошибок: {failed}",
            parse_mode="Markdown",
        )

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
        await message.answer(f"🚫 Пользователь {target_id} заблокирован.\n\nДля разблокировки: /give {target_id} 7")

    @dp.message(Command("users"))
    async def cmd_users(message: Message):
        if message.from_user.id != ADMIN_ID:
            return
        parts = message.text.split()
        sf = parts[1] if len(parts) > 1 else None
        users = get_all_users(sf)
        if not users:
            await message.answer("Нет пользователей.\n\nФильтры: /users beta | paid | blocked")
            return
        icons = {"pending": "⏳", "beta": "✅", "paid": "💎", "blocked": "🚫"}
        lines = [f"*{'Все' if not sf else sf.upper()} ({len(users)}):*\n"]
        for u in users[:30]:
            nm = (u["first_name"] or "").replace("_", "\\_").replace("*", "\\*")[:15]
            un = (u["username"] or "").replace("_", "\\_")
            label = f"{nm} (@{un})" if un else f"{nm} (id{u['telegram_id']})"
            streak = u.get("streak_days", 0)
            s_icon = f" 🔥{streak}" if streak > 1 else ""
            lines.append(f"{icons.get(u['status'],'❓')} {label} — `{u['telegram_id']}`{s_icon}")
        try:
            await message.answer("\n".join(lines), parse_mode="Markdown")
        except Exception:
            plain = [lines[0].replace("*", "")] + [
                l.replace("*", "").replace("`", "").replace("\\_", "_") for l in lines[1:]
            ]
            await message.answer("\n".join(plain))

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
                dl = max((dt - _utcnow()).days, 0)
                lines.append(f"🎁 Триал: {dt.strftime('%d.%m.%Y')} ({dl} дн.)")
            except Exception:
                pass
        if u.get("expires_at"):
            try:
                dt = datetime.fromisoformat(u["expires_at"])
                dl = max((dt - _utcnow()).days, 0)
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
    # ── Segmented broadcast ─────────────────────────────────────────────────────
    @dp.callback_query(F.data.startswith("bcast:"))
    async def cb_bcast_segment(callback: CallbackQuery):
        if callback.from_user.id != ADMIN_ID:
            await callback.answer("Нет доступа.", show_alert=True)
            return
        await callback.answer()
        segment = callback.data.split(":")[1]
        segment_labels = {
            "all_active":   "Все активные",
            "trial_active": "Триал-пользователи",
            "paid_active":  "Платные подписки",
            "sub_expired":  "Подписка истекла",
            "no_log_week":  "Не логируют 7+ дней",
        }
        label = segment_labels.get(segment, segment)
        users = get_users_by_segment(segment)
        user_states[callback.from_user.id] = {
            "state": STATES["ADMIN_BROADCAST"],
            "data": {"segment": segment, "segment_label": label, "segment_count": len(users)},
            "_ts": _utcnow(),
        }
        await callback.message.answer(
            f"📡 *Рассылка → {label}*\n"
            f"👥 Получателей: *{len(users)}*\n\n"
            f"Введи текст сообщения для рассылки:",
            parse_mode="Markdown",
        )


    @dp.callback_query(F.data.in_({"adm_stats", "adm_users", "adm_beta",
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
            await callback.message.answer(
                "📡 *Сегментированная рассылка*\n\nВыбери аудиторию:",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="👥 Все активные", callback_data="bcast:all_active")],
                    [InlineKeyboardButton(text="🎁 Триал-пользователи", callback_data="bcast:trial_active")],
                    [InlineKeyboardButton(text="💎 Платные подписки", callback_data="bcast:paid_active")],
                    [InlineKeyboardButton(text="⏰ Подписка истекла", callback_data="bcast:sub_expired")],
                    [InlineKeyboardButton(text="😴 Не логируют 7+ дней", callback_data="bcast:no_log_week")],
                ]),
            )
            return

        status_filter = "beta" if data == "adm_beta" else "paid"
        if data == "adm_users":
            status_filter = None
        users = get_all_users(status_filter)
        icons = {"pending": "⏳", "beta": "✅", "paid": "💎", "blocked": "🚫"}
        if not users:
            await callback.message.answer("Нет пользователей в этой категории.")
            return
        label = {"adm_beta": "БЕТА", "adm_paid": "ПЛАТНЫЕ", "adm_users": "ВСЕ"}.get(data, "ВСЕ")
        lines = [f"*{label} ({min(len(users),25)}):*\n"]
        for u in users[:25]:
            nm = (u["first_name"] or "").replace("_", "\\_").replace("*", "\\*")[:15]
            un = (u["username"] or "").replace("_", "\\_")
            lbl = f"{nm} (@{un})" if un else f"{nm} (id{u['telegram_id']})"
            streak = u.get("streak_days", 0)
            s_icon = f" 🔥{streak}" if streak > 1 else ""
            lines.append(f"{icons.get(u['status'],'❓')} {lbl} — `{u['telegram_id']}`{s_icon}")
        try:
            await callback.message.answer("\n".join(lines), parse_mode="Markdown")
        except Exception:
            plain = [lines[0].replace("*", "")] + [
                l.replace("*", "").replace("`", "").replace("\\_", "_") for l in lines[1:]
            ]
            await callback.message.answer("\n".join(plain))

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

    # ── Profile inline callbacks ───────────────────────────────────────────────
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
            await callback.message.answer("📊 *Ещё нет данных за неделю*\n\nНачни сегодня — и через 7 дней увидишь свой прогресс 💪", parse_mode="Markdown")
            return

        user = get_user(uid)
        goal = user.get("daily_goal") if user else None
        day_names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
        day_lines = []
        for d, data in zip(stats["dates"], stats["daily"]):
            dt = datetime.strptime(d, "%Y-%m-%d")
            dn = day_names[dt.weekday()]
            if data["kcal"] > 0:
                day_lines.append(f"  {dn} — {data['kcal']} ккал")
            else:
                day_lines.append(f"  {dn} — нет записей")
        days_block = "\n".join(day_lines)

        avg_kcal = stats["avg_kcal"] or 0
        avg_protein = stats["avg_protein"] or 0
        logged = stats["logged_days"]

        forecast_line = ""
        if goal and avg_kcal > 0:
            diff = avg_kcal - goal
            if abs(diff) > 50:
                kg_week = round(diff * 7 / 7700, 1)
                sign = "+" if kg_week > 0 else ""
                forecast_line = f"\n📉 Прогноз: *{sign}{kg_week} кг/нед.*"

        if logged >= 5:
            insight = "🔥 Стабильная неделя — отличная работа!"
        elif logged >= 3:
            insight = "💪 Хороший старт — логируй каждый день."
        else:
            insight = "🌱 Ещё немного практики — и привычка закрепится!"

        text_out = (
            f"📊 *Последние 7 дней*\n\n"
            f"{days_block}\n\n"
            f"🍽 Среднее: *{avg_kcal} ккал*\n"
            f"🥩 Белок: *{avg_protein} г/день*"
            f"{forecast_line}\n\n"
            f"💡 {insight}"
        )
        await callback.message.answer(text_out, parse_mode="Markdown")

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
            dl = max((exp_dt - _utcnow()).days, 0)
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
                f"Оформи подписку — ⭐ Подписка",
                parse_mode="Markdown",
            )

    # ── recalc_norm callback ───────────────────────────────────────────────────
    @dp.callback_query(F.data == "recalc_norm")
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
        _set_onboard_state(uid, "ob_goal", {})

    # ── buy_sub callback ───────────────────────────────────────────────────────
    @dp.callback_query(F.data.startswith("buy_sub"))
    async def cb_buy_sub(callback: CallbackQuery):
        uid = callback.from_user.id
        await callback.answer()
        parts = callback.data.split(":")
        plan = parts[1] if len(parts) > 1 else "30"
        plans = {
            "30":  (SUB_PRICE_STARS, SUB_DAYS,    "1 месяц",    "Premium 30 дней"),
            "90":  (SUB_PRICE_3M,   SUB_DAYS_3M,  "3 месяца",   "Premium 90 дней"),
            "365": (SUB_PRICE_12M,  SUB_DAYS_12M, "12 месяцев", "Premium 365 дней"),
        }
        price, days, label, pay_label = plans.get(plan, plans["30"])
        await bot.send_invoice(
            chat_id=uid,
            title=f"NutriAI Premium — {label}",
            description="Безлимит · Трекер калорий · КБЖУ · Стрики · Недельные отчёты · AI-план питания",
            payload=f"sub_{days}d_{uid}",
            currency="XTR",
            prices=[LabeledPrice(label=pay_label, amount=price)],
        )

    @dp.callback_query(F.data == "ref_screen")
    async def cb_ref_screen(callback: CallbackQuery):
        uid = callback.from_user.id
        await callback.answer()
        await _show_referral(callback.message.answer, uid)

    # ── Diary: show today's entries ────────────────────────────────────────────
    async def _show_diary(send_fn, uid: int):
        entries = get_entries_today(uid)
        if not entries:
            await send_fn(
                "*Записей сегодня нет*\n\nОтправь фото или опиши блюдо — добавлю в дневник",
                parse_mode="Markdown",
            )
            return
        total = sum(e["calories"] or 0 for e in entries)
        await send_fn(
            f"*Дневник — {total} ккал*",
            parse_mode="Markdown",
            reply_markup=diary_keyboard(entries),
        )

    @dp.callback_query(F.data == "noop")
    async def cb_noop(callback: CallbackQuery):
        await callback.answer()

    @dp.callback_query(F.data == "history7")
    async def cb_history7(callback: CallbackQuery):
        uid = callback.from_user.id
        await callback.answer()
        user = get_user(uid)
        goal = user.get("daily_goal") if user else None
        stats = get_weekly_stats(uid)
        days = stats["dates"]
        daily = stats["daily"]
        day_names = ["Пн","Вт","Ср","Чт","Пт","Сб","Вс"]
        lines = []
        for i, (d, data) in enumerate(zip(days, daily)):
            from datetime import date as _date
            dt = _date.fromisoformat(d)
            dn = day_names[dt.weekday()]
            kcal = data["kcal"]
            if kcal == 0:
                lines.append(f"{dn} {dt.strftime('%d.%m')}  —")
            elif goal:
                pct = round(kcal / goal * 100)
                bar = "●" * min(pct // 20, 5)
                lines.append(f"{dn} {dt.strftime('%d.%m')}  *{kcal}* / {goal}  {bar}")
            else:
                lines.append(f"{dn} {dt.strftime('%d.%m')}  *{kcal} ккал*")
        avg = stats["avg_kcal"]
        logged = stats["logged_days"]
        avg_line = f"\nСредн: *{avg} ккал/день* · {logged}/7 дней залогировано" if logged else ""
        await callback.message.answer(
            f"📅 *История за 7 дней*\n\n" + "\n".join(lines) + avg_line,
            parse_mode="Markdown",
        )

    @dp.callback_query(F.data == "diary")
    async def cb_diary(callback: CallbackQuery):
        uid = callback.from_user.id
        await callback.answer()
        await _show_diary(callback.message.answer, uid)

    @dp.callback_query(F.data.startswith("edit_e:"))
    async def cb_edit_entry(callback: CallbackQuery):
        uid = callback.from_user.id
        await callback.answer()
        try:
            entry_id = int(callback.data.split(":")[1])
        except (ValueError, IndexError):
            return
        user_states[uid] = {"state": STATES["CORRECT_ENTRY"], "data": {"entry_id": entry_id}}
        await callback.message.answer(
            "✏️ *Введи новое значение калорий:*\n_/cancel — отмена_",
            parse_mode="Markdown",
        )

    @dp.callback_query(F.data.startswith("del_e:"))
    async def cb_del_entry_ask(callback: CallbackQuery):
        uid = callback.from_user.id
        await callback.answer()
        try:
            entry_id = int(callback.data.split(":")[1])
        except (ValueError, IndexError):
            return
        entries = get_entries_today(uid)
        entry = next((e for e in entries if e["id"] == entry_id), None)
        if not entry:
            await callback.message.answer("Запись не найдена.")
            return
        name = (entry["food_name"] or "запись")[:30]
        kcal = entry["calories"] or 0
        await callback.message.answer(
            f"🗑 Удалить *{name} — {kcal} ккал*?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"del_e_ok:{entry_id}"),
                InlineKeyboardButton(text="❌ Нет", callback_data="del_e_cancel"),
            ]]),
        )

    @dp.callback_query(F.data.startswith("del_e_ok:"))
    async def cb_del_entry_ok(callback: CallbackQuery):
        uid = callback.from_user.id
        await callback.answer("Удалено ✅")
        try:
            entry_id = int(callback.data.split(":")[1])
        except (ValueError, IndexError):
            return
        delete_entry(entry_id)
        try:
            await callback.message.delete()
        except Exception:
            pass
        await _show_diary(callback.message.answer, uid)

    @dp.callback_query(F.data == "del_e_cancel")
    async def cb_del_entry_cancel(callback: CallbackQuery):
        await callback.answer("Отменено")
        try:
            await callback.message.delete()
        except Exception:
            pass

    @dp.callback_query(F.data == "reset_day")
    async def cb_reset_day_ask(callback: CallbackQuery):
        uid = callback.from_user.id
        await callback.answer()
        macros = get_daily_macros(uid)
        total = macros["kcal"]
        await callback.message.answer(
            f"🗑 *Сбросить весь день?*\n\nБудут удалены все записи за сегодня ({total} ккал).\nОтменить это действие нельзя.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Да, сбросить", callback_data="reset_day_ok"),
                InlineKeyboardButton(text="❌ Нет", callback_data="del_e_cancel"),
            ]]),
        )

    @dp.callback_query(F.data == "reset_day_ok")
    async def cb_reset_day_ok(callback: CallbackQuery):
        uid = callback.from_user.id
        await callback.answer("День сброшен ✅")
        reset_today_entries(uid)
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.answer(
            "✅ *День сброшен*\n\nВсе записи за сегодня удалены. Начинай заново 💪",
            parse_mode="Markdown",
            reply_markup=main_keyboard(uid == ADMIN_ID),
        )

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
    # ── Onboarding callbacks ─────────────────────────────────────────────────────
    @dp.callback_query(F.data.startswith("ob_goal:"))
    async def cb_ob_goal(callback: CallbackQuery):
        uid = callback.from_user.id
        goal_type = callback.data.split(":")[1]
        await callback.answer()
        current = _get_state(uid)
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

    @dp.callback_query(F.data.startswith("ob_gender:"))
    async def cb_ob_gender(callback: CallbackQuery):
        uid = callback.from_user.id
        gender = callback.data.split(":")[1]
        await callback.answer()
        current = _get_state(uid)
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



    @dp.callback_query(F.data.startswith("ob_activity:"))
    async def cb_ob_activity(callback: CallbackQuery):
        uid = callback.from_user.id
        activity = callback.data.split(":")[1]
        await callback.answer()
        activity_labels = {
            "sedentary":  "🛋 Сидячий",
            "light":      "🚶 Лёгкая",
            "moderate":   "🏃 Средняя",
            "active":     "💪 Высокая",
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
        # Calculate weight forecast for onboarding completion message
        goal_type_local = ob_data.get("goal_type", "maintain")
        forecast_line = ""
        if goal_type_local == "lose":
            kg_month = round((goal_kcal * 30 - (goal_kcal + 400) * 30) / 7700 * (-1), 1)
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
    # ── show_premium callback ───────────────────────────────────────────────────
    @dp.callback_query(F.data == "show_premium")
    async def cb_show_premium(callback: CallbackQuery):
        uid = callback.from_user.id
        await callback.answer()
        user = get_user(uid)
        await _show_premium_screen(callback.message.answer, uid, user)

    # ── water tracker callbacks ─────────────────────────────────────────────────
    @dp.callback_query(F.data == "water_add")
    async def cb_water_add(callback: CallbackQuery):
        uid = callback.from_user.id
        await callback.answer()
        glasses = add_water_log(uid)
        filled = min(glasses, WATER_GOAL)
        bar = "💧" * filled + "⬜" * max(0, WATER_GOAL - filled)
        pct = round(glasses / WATER_GOAL * 100)
        status = "✅ Норма выполнена!" if glasses >= WATER_GOAL else f"Осталось: {WATER_GOAL - glasses} ст."
        try:
            await callback.message.edit_text(
                f"💧 *Вода сегодня*\n\n"
                f"{bar}\n"
                f"*{glasses} / {WATER_GOAL} стаканов* — {pct}%\n"
                f"{status}",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="+ Стакан воды", callback_data="water_add")],
                    [InlineKeyboardButton(text="🔄 Сбросить", callback_data="water_reset")],
                ]),
            )
        except Exception:
            pass

    @dp.callback_query(F.data == "water_reset")
    async def cb_water_reset(callback: CallbackQuery):
        uid = callback.from_user.id
        await callback.answer("Сброшено")
        reset_water_today(uid)
        try:
            await callback.message.edit_text(
                f"💧 *Вода сегодня*\n\n{'⬜' * WATER_GOAL}\n*0 / {WATER_GOAL} стаканов*",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="+ Стакан воды", callback_data="water_add")],
                ]),
            )
        except Exception:
            pass


    @dp.pre_checkout_query()
    async def pre_checkout(query: PreCheckoutQuery):
        await query.answer(ok=True)

    @dp.message(F.successful_payment)
    async def payment_done(message: Message):
        uid = message.from_user.id
        payload = message.successful_payment.invoice_payload
        # payload format: sub_Xd_UID
        try:
            days = int(payload.split("_")[1].rstrip("d"))
        except Exception:
            days = SUB_DAYS
        activate_subscription(uid, days)
        exp = (_utcnow() + timedelta(days=days)).strftime("%d.%m.%Y")
        plan_label = {30: "1 месяц", 90: "3 месяца", 365: "12 месяцев"}.get(days, f"{days} дней")
        await message.answer(
            f"🎉 *Оплата прошла! Добро пожаловать в Premium!*\n\n"
            f"📅 Подписка *{plan_label}* — до *{exp}*\n"
            f"📸 Безлимитные анализы разблокированы\n"
            f"🍽 AI-план питания доступен\n"
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
        await notify_admin(bot, f"💰 Оплата: {user_label(user)} · {plan_label} → до {exp}")

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
                "✍️ *Что добавить в дневник?*\n\n"
                "📝 Опиши блюдо: «борщ 300г», «яблоко», «куриная грудка 150г»\n"
                "🔢 Или просто введи калории: «450»\n\n"
                "_/cancel — отмена_",
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
            score = calc_daily_score(total, macros["protein"], macros["fat"],
                                     macros["carbs"], goal, goal_protein, meals)
            fs = format_score(score)

            streak_line = (
                f"\n🔥 Серия: *{streak} {'день' if streak == 1 else 'дней'}*"
                if streak > 0 else ""
            )
            macros_line = (
                f"\n🥩 Б: *{macros['protein']}г*  Ж: *{macros['fat']}г*  У: *{macros['carbs']}г*"
                if macros["protein"] > 0 else ""
            )
            comment = ai_score_comment(score, macros["protein"], macros["carbs"],
                                       total, goal, None) if total > 0 else ""
            score_line = f"\n🍽 Balance Score: *{fs}/10*" if total > 0 else ""
            comment_line = f"\n💬 _{comment}_" if comment else ""

            if goal:
                remaining = max(goal - total, 0)
                over = total - goal
                if over > 0:
                    status = f"⚡ +{over} ккал сверх нормы"
                else:
                    pct = round(total / goal * 100)
                    status = f"Выполнено {pct}% — ещё {remaining} ккал"
                text_out = (
                    f"📊 *Мой прогресс*\n\n"
                    f"*{total} / {goal} ккал*\n"
                    f"{status}\n"
                    f"Приёмов: {meals}{macros_line}"
                    f"{streak_line}"
                    f"{score_line}{comment_line}"
                )
            else:
                text_out = (
                    f"📊 *Мой прогресс*\n\n"
                    f"*{total} ккал* сегодня\n"
                    f"Приёмов: {meals}{macros_line}"
                    f"{streak_line}"
                    f"{score_line}{comment_line}"
                    f"\n\n_💡 Установи цель в ⚙️ Профиль_"
                )
            await message.answer(text_out, parse_mode="Markdown",
                                  reply_markup=progress_inline_keyboard())
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

        if text == BTN_WATER:
            glasses = get_water_today(uid)
            filled = min(glasses, WATER_GOAL)
            bar = "💧" * filled + "⬜" * max(0, WATER_GOAL - filled)
            pct = round(glasses / WATER_GOAL * 100) if WATER_GOAL else 0
            status = "✅ Норма выполнена!" if glasses >= WATER_GOAL else f"Осталось: {WATER_GOAL - glasses} ст."
            await message.answer(
                f"💧 *Вода сегодня*\n\n"
                f"{bar}\n"
                f"*{glasses} / {WATER_GOAL} стаканов* — {pct}%\n"
                f"{status}",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="+ Стакан воды", callback_data="water_add")],
                    [InlineKeyboardButton(text="🔄 Сбросить", callback_data="water_reset")],
                ]),
            )
            return

        if text == BTN_PLAN:
            ok, reason = access_check(user)
            if not ok:
                await deny(message, reason)
                return
            if user.get("status") != "paid" or check_subscription_expired(user):
                await message.answer(
                    "🍽 *AI-план питания* — Premium функция\n\nПолучи персональный план питания на день на основе твоих целей и нормы КБЖУ 🎯",


                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="⭐ Открыть Premium", callback_data="show_premium")],
                    ]),
                )
                return
            if not user.get("daily_goal"):
                await message.answer(
                    "⚙️ Сначала настрой профиль — нажми *Профиль* → *Пересчитать норму*.",
                    parse_mode="Markdown",
                )
                return
            thinking_msg = await message.answer("🤔 *Составляю план питания...*", parse_mode="Markdown")
            try:
                goal_kcal = user.get("daily_goal", 2000)
                protein_g = user.get("protein_goal", 150)
                goal_type = user.get("goal_type", "maintain")
                gender = user.get("gender", "male")
                goal_labels = {"lose": "похудение", "maintain": "поддержание", "gain": "набор массы"}
                plan_prompt = (
                    f"Составь персональный план питания на один день.\n"
                    f"Параметры: цель={goal_labels.get(goal_type,'поддержание')}, "
                    f"норма={goal_kcal} ккал, белок={protein_g}г, пол={'мужской' if gender=='male' else 'женский'}.\n"
                    f"Формат: 4 приёма пищи (завтрак, обед, перекус, ужин).\n"
                    f"Для каждого: название + калории + КБЖУ (Б/Ж/У в граммах). "
                    f"В конце итого. Кратко, конкретно. Только реальные блюда, без экзотики."
                )
                resp = await openai_client.chat.completions.create(
                    model="meta-llama/llama-4-scout-17b-16e-instruct",
                    messages=[
                        {"role": "system", "content": "Профессиональный нутрициолог. Составляй практичные планы питания."},
                        {"role": "user", "content": plan_prompt},
                    ],
                    max_tokens=700,
                )
                plan_text = resp.choices[0].message.content or ""
                try:
                    await thinking_msg.delete()
                except Exception:
                    pass
                await message.answer(
                    f"🍽 *Твой план питания на сегодня*\n\n{plan_text}",
                    parse_mode="Markdown",
                )
            except Exception as plan_e:
                log.error(f"meal plan error: {plan_e}")
                try:
                    await thinking_msg.delete()
                except Exception:
                    pass
                await message.answer("⚠️ Не удалось составить план. Попробуй чуть позже.")
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
            dl = max((exp_dt - _utcnow()).days, 0)
            await send_fn(
                f"💎 *Подписка активна*\n\n"
                f"Действует до *{exp}* — осталось *{dl} дн.*\n\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"*Продлить заранее?*\n"
                f"Дни добавятся к текущей подписке:\n"
                f"• 1 мес — 150 ⭐ (~7 ₽/день)\n"
                f"• 3 мес — 360 ⭐ (~6 ₽/день)\n"
                f"• 12 мес — 990 ⭐ (~4 ₽/день)",
                parse_mode="Markdown",
                reply_markup=premium_keyboard(),
            )
            return

        await send_fn(
            "*Premium подписка*\n\n"
            "━━━━━━━━━━━━━━━━\n"
            "*Бесплатно*\n"
            "• 5 анализов в день\n"
            "• Базовое КБЖУ\n"
            "• Дневной трекер\n\n"
            "*Premium — 150 ⭐ / месяц*\n"
            "• Безлимитные анализы\n"
            "• AI-комментарий тренера\n"
            "• Утро/вечер итоги дня\n"
            "• Недельные отчёты\n"
            "• Трекер веса\n"
            "• Стрики и ачивки\n"
            "• Balance Score дня\n"
            "• AI-план питания на день\n"
            "• Трекер воды\n"
            "━━━━━━━━━━━━━━━━\n\n"
            "Тарифы:\n"
            "• 1 мес — 150 ⭐ (~7 ₽/день)\n"
            "• 3 мес — 360 ⭐ (~6 ₽/день)\n"
            "• 12 мес — 990 ⭐ (~4 ₽/день)",
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

        # If the user is still in onboarding and the bot was restarted (state lost),
        # try to restore their progress from the persistent DB store
        if not user.get("onboarded"):
            _try_restore_onboard(uid)

        state_data = user_states.get(uid, {})
        state = state_data.get("state")

      # ── Onboarding: age ────────────────────────────────────────────────────────
        if state == "ob_age":
            try:
                age = int(text.strip())
                if not (10 <= age <= 100):
                    raise ValueError
            except ValueError:
                await message.answer(
                    "⚠️ Введи возраст числом от 10 до 100 или /cancel:"
                )
                return
            ob_data = user_states.get(uid, {}).get("data", {})
            ob_data["age"] = age
            await message.answer(
                "📏 *Какой у тебя рост?*\n\n_Введи в сантиметрах (например: 175)_",
                parse_mode="Markdown",
            )
            _set_onboard_state(uid, "ob_height", ob_data)
            return

        # ── Onboarding: height ─────────────────────────────────────────────────────
        if state == "ob_height":
            try:
                height = float(text.replace(",", "."))
                if not (100 <= height <= 250):
                    raise ValueError
            except ValueError:
                await message.answer(
                    "⚠️ Введи рост в сантиметрах от 100 до 250 или /cancel:"
                )
                return
            ob_data = user_states.get(uid, {}).get("data", {})
            ob_data["height"] = height
            await message.answer(
                "⚖️ *Какой у тебя вес?*\n\n_Введи в кг (например: 70 или 70.5)_",
                parse_mode="Markdown",
            )
            _set_onboard_state(uid, "ob_weight", ob_data)
            return

        # ── Onboarding: weight -> calculate and complete ───────────────────────────
        if state == "ob_weight":
            try:
                weight = float(text.replace(",", "."))
                if not (30 <= weight <= 300):
                    raise ValueError
            except ValueError:
                await message.answer(
                    "⚠️ Введи вес в кг от 30 до 300 или /cancel:"
                )
                return
            ob_data = user_states.get(uid, {}).get("data", {})
            ob_data["weight"] = weight
            _set_onboard_state(uid, "ob_activity", ob_data)
            activity_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🛋 Сидячий (офис, мало движения)",    callback_data="ob_activity:sedentary")],
                [InlineKeyboardButton(text="🚶 Лёгкая (прогулки, 1-3 трен/нед)", callback_data="ob_activity:light")],
                [InlineKeyboardButton(text="🏃 Средняя (3-5 трен/нед)",           callback_data="ob_activity:moderate")],
                [InlineKeyboardButton(text="💪 Высокая (6-7 трен/нед)",           callback_data="ob_activity:active")],
                [InlineKeyboardButton(text="🔥 Очень высокая (физ. труд + трен)", callback_data="ob_activity:very_active")],
            ])
            await message.answer(
                "⚡️ *Какой у тебя уровень активности?*",
                parse_mode="Markdown",
                reply_markup=activity_kb,
            )
            return
            ob_data = user_states.get(uid, {}).get("data", {})
            goal_kcal, protein_goal = _calc_calorie_goal(
                ob_data.get("gender", "male"),
                ob_data.get("age", 25),
                ob_data.get("height", 170),
                weight,
                ob_data.get("goal_type", "maintain"),
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
            )
            mark_onboarded(uid)
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
            await message.answer(
                f"🎉 *Профиль настроен!*\n\n"
                f"🎯 Цель: *{goal_label}*\n"
                f"🔥 Норма калорий: *{goal_kcal} ккал/день*\n"
                f"🥩 Норма белка: *{protein_goal} г/день*\n\n"
                f"Отправляй фото еды или описывай что съел — "
                f"буду следить за прогрессом! 📸",
                parse_mode="Markdown",
                reply_markup=main_keyboard(uid == ADMIN_ID),
            )
            return

          # ── Admin broadcast state ──────────────────────────────────────────────
        if state == STATES["ADMIN_BROADCAST"] and uid == ADMIN_ID:
              s_data = _get_state(uid).get("data", {})
              segment = s_data.get("segment", "all_active")
              segment_label = s_data.get("segment_label", "Все активные")
              users = get_users_by_segment(segment)
              sent = failed = 0
              for u in users:
                  try:
                      await bot.send_message(u["telegram_id"], text)
                      sent += 1
                  except Exception:
                      failed += 1
                  await asyncio.sleep(0.05)
              user_states.pop(uid, None)
              await message.answer(
                  f"📡 *Рассылка завершена*\n"
                  f"👥 Аудитория: {segment_label}\n"
                  f"✅ Отправлено: {sent}  ❌ Ошибок: {failed}",
                  parse_mode="Markdown",
              )
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

            # ── Direct calorie input: just a number ─────────────────────────
            try:
                kcal_direct = int(text.strip())
                if 50 <= kcal_direct <= 9999:
                    entry_id = record_usage(uid, kcal_direct, None, None, None,
                                            f"запись {kcal_direct} ккал")
                    streak, milestone = update_streak(uid, user=user)
                    macros  = get_daily_macros(uid)
                    fu      = get_user(uid)
                    progress = daily_progress_text(uid, user=fu, macros=macros)
                    hint = "\n\n_Установи норму в ⚙️ Профиль_" if not user.get("daily_goal") else ""
                    user_states.pop(uid, None)
                    await message.answer(
                        f"✅ *{kcal_direct} ккал* записано{progress}{hint}",
                        parse_mode="Markdown",
                        reply_markup=main_keyboard(uid == ADMIN_ID),
                    )

                    if milestone and streak in STREAK_MILESTONES:
                        await message.answer(
                            f"🎉 *Ачивка разблокирована!*\n"
                            f"_{STREAK_MILESTONES[streak]}_\n\nПродолжай! 💪",
                            parse_mode="Markdown",
                        )
                    return
            except ValueError:
                pass  # not a plain number → fall through to AI analysis

            # ── AI food analysis ────────────────────────────────────────────
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
