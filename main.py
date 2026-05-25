import asyncio
import os
import re
import sys
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
    activate_subscription,
    check_subscription_expired,
    get_all_users,
    get_active_users,
    record_usage,
    get_daily_usage,
    get_daily_calories,
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

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_ID = int(os.environ["TELEGRAM_CHAT_ID"])
BOT_USERNAME = os.environ.get("BOT_USERNAME", "")

BETA_DAILY_LIMIT = 5
SUB_PRICE_STARS = 150
SUB_DAYS = 30
REFERRAL_BONUS_DAYS = 7
REFERRAL_JOIN_BONUS_DAYS = 3

STREAK_MILESTONES = {3: "🥉 3 дня", 7: "🥈 7 дней", 14: "🥇 14 дней", 30: "🏆 30 дней!", 60: "👑 60 дней!", 100: "🌟 100 дней!!"}

openai_client = AsyncOpenAI(
    api_key=os.environ["GROQ_API_KEY"],
    base_url="https://api.groq.com/openai/v1",
    max_retries=3,
    timeout=60,
)

# ── Conversation state ──────────────────────────────────────────────────────
user_states: dict[int, dict] = {}

STATES = {
    "GOAL_ASK":      "goal_ask",
    "GOAL_ENTER":    "goal_enter",
    "CALC_AGE":      "calc_age",
    "CALC_WEIGHT":   "calc_weight",
    "CALC_HEIGHT":   "calc_height",
    "MANUAL_ENTRY":  "manual_entry",
    "CORRECT_ENTRY": "correct_entry",
    "WEIGHT_LOG":    "weight_log",
}

# ── Кнопки меню ─────────────────────────────────────────────────────────────
BTN_TODAY   = "📊 Сегодня"
BTN_GOAL    = "🎯 Норма"
BTN_BUY     = "💳 Подписка"
BTN_REF     = "👥 Пригласить"
BTN_STATUS  = "ℹ️ Статус"
BTN_USERS   = "👤 Пользователи"
BTN_STATS   = "📈 Статистика"
BTN_ADD     = "✏️ Добавить вручную"
BTN_WEIGHT  = "⚖️ Мой вес"

MENU_BUTTONS = {BTN_TODAY, BTN_GOAL, BTN_BUY, BTN_REF, BTN_STATUS,
                BTN_USERS, BTN_STATS, BTN_ADD, BTN_WEIGHT}


def main_keyboard(is_admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_TODAY),  KeyboardButton(text=BTN_GOAL)],
        [KeyboardButton(text=BTN_ADD),    KeyboardButton(text=BTN_WEIGHT)],
        [KeyboardButton(text=BTN_BUY),    KeyboardButton(text=BTN_REF)],
        [KeyboardButton(text=BTN_STATUS)],
    ]
    if is_admin:
        rows.append([KeyboardButton(text=BTN_USERS), KeyboardButton(text=BTN_STATS)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


# ─────────────────── AI анализ ──────────────────────────────────────────────

VISION_PROMPT = """Ты — эксперт по еде. Опиши фото:
1. Блюдо/продукты (точно)
2. Способ приготовления
3. Примерный вес порции (г)
4. Основные ингредиенты и количество

Если это не еда — напиши только: НЕ ЕДА"""

NUTRITION_PROMPT = """Ты — дружелюбный AI-тренер по питанию с характером. Рассчитай КБЖУ для блюда.

Блюдо: {desc}

Ответь СТРОГО в этом формате (без отклонений):
🍽 *{{название}}* (~{{вес}} г)

🔥 {{ккал}} ккал  |  Б {{б}}г  Ж {{ж}}г  У {{у}}г

💬 {{живой короткий комментарий от тренера: оцени блюдо, дай совет или мотивацию. 1-2 предложения с эмодзи. Будь как друг, не как справочник.}}

KCAL:{{ккал}}
PROTEIN:{{б}}
FAT:{{ж}}
CARBS:{{у}}"""

TEXT_NUTRITION_PROMPT = """Ты — дружелюбный AI-тренер по питанию с характером.

Блюдо/продукт: {desc}

Если это не еда — ответь только: НЕ ЕДА

Иначе ответь СТРОГО в этом формате:
🍽 *{{название}}* (~{{вес}} г)

🔥 {{ккал}} ккал  |  Б {{б}}г  Ж {{ж}}г  У {{у}}г

💬 {{живой короткий комментарий: оцени блюдо, дай совет. 1-2 предложения с эмодзи.}}

KCAL:{{ккал}}
PROTEIN:{{б}}
FAT:{{ж}}
CARBS:{{у}}"""


def _parse_macros(raw: str) -> tuple[str, int | None, float | None, float | None, float | None]:
    """Extract kcal/protein/fat/carbs from AI response and clean display text."""
    kcal = protein = fat = carbs = None
    m = re.search(r"KCAL:(\d+)", raw)
    if m:
        kcal = int(m.group(1))
    m = re.search(r"PROTEIN:([\d.]+)", raw)
    if m:
        protein = float(m.group(1))
    m = re.search(r"FAT:([\d.]+)", raw)
    if m:
        fat = float(m.group(1))
    m = re.search(r"CARBS:([\d.]+)", raw)
    if m:
        carbs = float(m.group(1))
    display = re.sub(r"\s*(KCAL|PROTEIN|FAT|CARBS):\S+", "", raw).strip()
    return display, kcal, protein, fat, carbs


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
        return "🙅 На фото не еда. Пришли фото блюда — посчитаю калории!", None, None, None, None

    nutrition = await openai_client.chat.completions.create(
        model="meta-llama/llama-4-scout-17b-16e-instruct",
        messages=[
            {"role": "system", "content": "Точный нутрициолог и дружелюбный тренер. Строго по шаблону."},
            {"role": "user", "content": NUTRITION_PROMPT.format(desc=desc)},
        ],
        max_tokens=450,
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
        max_tokens=450,
    )
    raw = response.choices[0].message.content or ""
    if "НЕ ЕДА" in raw.upper():
        return "🙅 Это не похоже на еду. Введи название блюда или продукта.", None, None, None, None
    return _parse_macros(raw)


# ─────────────────── Хелперы ────────────────────────────────────────────────


def user_label(row) -> str:
    name = row["first_name"] or ""
    un = f"@{row['username']}" if row["username"] else f"id{row['telegram_id']}"
    return f"{name} ({un})"


def ref_link(uid: int) -> str:
    bot_un = BOT_USERNAME.lstrip("@") or "YOUR_BOT"
    return f"https://t.me/{bot_un}?start=ref_{uid}"


def result_keyboard(entry_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✏️ Скорректировать калории", callback_data=f"correct:{entry_id}")
    ]])


def ref_keyboard(uid: int) -> InlineKeyboardMarkup:
    link = ref_link(uid)
    share_text = "Считаю калории по фото еды 🍽 Попробуй бесплатно:"
    share_url = f"https://t.me/share/url?url={urllib.parse.quote(link)}&text={urllib.parse.quote(share_text)}"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📤 Поделиться с другом", url=share_url)
    ]])


def new_user_keyboard(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Одобрить",       callback_data=f"approve_{uid}"),
        InlineKeyboardButton(text="🚫 Заблокировать",  callback_data=f"block_{uid}"),
    ]])


async def notify_admin(bot: Bot, text: str):
    try:
        await bot.send_message(ADMIN_ID, text, parse_mode="Markdown")
    except Exception as e:
        log.warning(f"notify_admin: {e}")


def access_check(user_row) -> tuple[bool, str]:
    if user_row is None:
        return False, "not_registered"
    s = user_row["status"]
    if s == "blocked":
        return False, "blocked"
    if s == "pending":
        return False, "pending"
    if s == "paid":
        return True, "paid"
    if s == "beta":
        if is_trial_expired(user_row["telegram_id"]):
            return False, "trial_expired"
        return True, "beta"
    return False, "unknown"


async def deny(message: Message, reason: str):
    if reason == "pending":
        await message.answer("⏳ Твоя заявка рассматривается. Ожидай одобрения.")
    elif reason == "blocked":
        await message.answer("⛔ Доступ заблокирован.")
    elif reason == "trial_expired":
        await message.answer(
            "⏰ *Бесплатный период закончился*\n\nОформи подписку, чтобы продолжить.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="💳 Купить подписку (150 ⭐)", callback_data="buy_sub")
            ]]),
        )
    else:
        await message.answer("Напиши /start для регистрации.")


def progress_bar(current: int, goal: int, width: int = 10) -> str:
    filled = min(int(width * current / goal), width) if goal else 0
    pct = min(int(100 * current / goal), 100) if goal else 0
    return f"{'█' * filled}{'░' * (width - filled)} {pct}%"


def streak_emoji(streak: int) -> str:
    if streak >= 30:
        return "🏆"
    if streak >= 14:
        return "🥇"
    if streak >= 7:
        return "🥈"
    if streak >= 3:
        return "🥉"
    return "🔥"


def daily_progress_text(uid: int) -> str:
    macros = get_daily_macros(uid)
    total = macros["kcal"]
    user = get_user(uid)
    goal = user["daily_goal"] if user else None
    streak = user.get("streak_days", 0) if user else 0

    streak_line = f"\n{streak_emoji(streak)} Серия: *{streak} {'день' if streak == 1 else 'дней'}*" if streak > 0 else ""

    if not goal:
        return f"\n\n📊 *Сегодня:* {total} ккал{streak_line}"

    remaining = max(goal - total, 0)
    bar = progress_bar(total, goal)
    over = total - goal
    extra = f"⚠️ Превышение на {over} ккал" if over > 0 else f"Осталось: {remaining} ккал"

    protein_line = ""
    if macros["protein"] > 0:
        protein_line = f"\nБ {macros['protein']}г  Ж {macros['fat']}г  У {macros['carbs']}г"

    return f"\n\n📊 *Сегодня:* {total} / {goal} ккал\n{bar}\n{extra}{protein_line}{streak_line}"


# ── Setup flow ────────────────────────────────────────────────────────────────


def goal_ask_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Знаю норму",    callback_data="goal_know"),
        InlineKeyboardButton(text="Рассчитай мне", callback_data="goal_calc"),
        InlineKeyboardButton(text="Пропустить",    callback_data="goal_skip"),
    ]])


def gender_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Мужчина", callback_data="gender_m"),
        InlineKeyboardButton(text="Женщина", callback_data="gender_f"),
    ]])


def activity_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛋 Сидячий образ жизни",         callback_data="act_1.2")],
        [InlineKeyboardButton(text="🚶 Лёгкая активность (1-3 дня)", callback_data="act_1.375")],
        [InlineKeyboardButton(text="🏃 Средняя активность (3-5 дней)", callback_data="act_1.55")],
        [InlineKeyboardButton(text="🏋 Высокая активность (6-7 дней)", callback_data="act_1.725")],
        [InlineKeyboardButton(text="⚡ Очень высокая / спортсмен",   callback_data="act_1.9")],
    ])


async def ask_daily_goal(bot: Bot, uid: int):
    user_states[uid] = {"state": STATES["GOAL_ASK"], "data": {}}
    await bot.send_message(
        uid,
        "🎯 *Установи дневную норму калорий*\n\nЭто поможет отслеживать прогресс после каждого приёма пищи.",
        parse_mode="Markdown",
        reply_markup=goal_ask_keyboard(),
    )


def calc_tdee(gender: str, age: int, weight: float, height: float, activity: float) -> tuple[int, int]:
    bmr = 10 * weight + 6.25 * height - 5 * age + (5 if gender == "m" else -161)
    tdee = round(bmr * activity)
    protein = round(weight * 1.6)
    return tdee, protein


# ─────────────────── Планировщик (APScheduler) ──────────────────────────────


async def send_morning_checkins(bot: Bot):
    """Утренние напоминания всем активным пользователям (8:00 МСК)."""
    users = get_active_users()
    for user in users:
        uid = user["telegram_id"]
        goal = user.get("daily_goal")
        streak = user.get("streak_days", 0)
        name = user.get("first_name") or "Привет"
        try:
            goal_line = f"🎯 Цель: *{goal} ккал*" if goal else "🎯 Норма не задана — установи в меню"
            streak_line = f"\n{streak_emoji(streak)} Серия: *{streak} {'день' if streak == 1 else 'дней'}* — не прерывай!" if streak > 1 else ""
            await bot.send_message(
                uid,
                f"☀️ *Доброе утро, {name}!*\n\n{goal_line}{streak_line}\n\n📸 Начни день — сфотографируй завтрак!",
                parse_mode="Markdown",
            )
        except Exception as e:
            log.debug(f"morning checkin {uid}: {e}")


async def send_evening_summaries(bot: Bot):
    """Вечерние итоги дня (21:00 МСК)."""
    users = get_active_users()
    for user in users:
        uid = user["telegram_id"]
        goal = user.get("daily_goal")
        streak = user.get("streak_days", 0)
        try:
            macros = get_daily_macros(uid)
            total = macros["kcal"]
            if total == 0:
                continue

            if goal:
                pct = round(total / goal * 100)
                result_line = (
                    f"✅ Отличный день — {pct}% нормы" if 85 <= pct <= 115
                    else f"⚠️ Перебор на {total - goal} ккал" if pct > 115
                    else f"📉 Недобор — {goal - total} ккал осталось"
                )
            else:
                result_line = f"📊 Итого за день: {total} ккал"

            protein_line = f"💪 Белок: {macros['protein']}г" if macros["protein"] > 0 else ""
            streak_line = f"\n🔥 Серия: *{streak} {'день' if streak == 1 else 'дней'}* подряд!" if streak > 0 else ""

            await bot.send_message(
                uid,
                f"🌙 *Итоги дня*\n\n"
                f"🔥 Калории: *{total}* {f'/ {goal}' if goal else ''} ккал\n"
                f"{protein_line}\n"
                f"{result_line}{streak_line}",
                parse_mode="Markdown",
            )
        except Exception as e:
            log.debug(f"evening summary {uid}: {e}")


async def send_weekly_reports(bot: Bot):
    """Еженедельные отчёты (понедельник 9:00 МСК)."""
    users = get_active_users()
    for user in users:
        uid = user["telegram_id"]
        try:
            stats = get_weekly_stats(uid)
            if stats["logged_days"] < 2:
                continue

            consistency_icon = "🔥" if stats["consistency"] >= 80 else "📊" if stats["consistency"] >= 50 else "💤"
            goal = user.get("daily_goal", 0)
            avg_vs_goal = ""
            if goal and stats["avg_kcal"]:
                diff = stats["avg_kcal"] - goal
                avg_vs_goal = f" ({'➕' if diff > 0 else '➖'}{abs(diff)} от нормы)"

            await bot.send_message(
                uid,
                f"📊 *Итоги недели*\n\n"
                f"🍽 Дней с записями: *{stats['logged_days']}/7*\n"
                f"🔥 Среднее за день: *{stats['avg_kcal']} ккал*{avg_vs_goal}\n"
                f"💪 Средний белок: *{stats['avg_protein']}г*\n"
                f"{consistency_icon} Постоянство: *{stats['consistency']}%*\n\n"
                f"{'🔥 Отличная неделя — так держать!' if stats['consistency'] >= 80 else '💪 Старайся логировать каждый день — это ключ к результату!'}",
                parse_mode="Markdown",
            )
        except Exception as e:
            log.debug(f"weekly report {uid}: {e}")


# ─────────────────── Бот ────────────────────────────────────────────────────


async def main():
    init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()

    # ── Scheduler setup ──
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(send_morning_checkins, "cron", hour=8,  minute=0, args=[bot])
    scheduler.add_job(send_evening_summaries, "cron", hour=21, minute=0, args=[bot])
    scheduler.add_job(send_weekly_reports,    "cron", day_of_week="mon", hour=9, minute=0, args=[bot])
    scheduler.start()

    # ── /start ──────────────────────────────────────────────────────────────
    @dp.message(Command("start"))
    async def cmd_start(message: Message):
        uid = message.from_user.id
        name = message.from_user.first_name or ""
        un = message.from_user.username or ""
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
            if user and user["status"] in ("pending", "beta", "blocked"):
                approve_user(uid, trial_days=3650)
            elif user and user["status"] == "paid":
                if not user["expires_at"] or datetime.fromisoformat(user["expires_at"]) < datetime.utcnow():
                    activate_subscription(uid, 3650)

        user = get_user(uid)
        status = user["status"] if user else "pending"

        if status == "blocked":
            await message.answer("⛔ Доступ заблокирован.")
            return

        if status == "pending":
            ref_info = f"\n🔗 Пригласил: id{referrer_id}" if referrer_id else ""
            await message.answer(
                f"👋 Привет, {name}!\n\n"
                "Я считаю калории по фото еды 📸\n\n"
                "⏳ Заявка отправлена — жди одобрения."
            )
            try:
                safe_name = (name or "").replace("_", "\\_")
                safe_un = (un or "").replace("_", "\\_")
                un_str = f"@{safe_un}" if safe_un else f"id{uid}"
                await bot.send_message(
                    ADMIN_ID,
                    f"🆕 Новый пользователь:\n"
                    f"👤 {safe_name} ({un_str})\n"
                    f"🆔 `{uid}`{ref_info}",
                    parse_mode="Markdown",
                    reply_markup=new_user_keyboard(uid),
                )
            except Exception as e:
                log.warning(f"notify_admin new user: {e}")
            return

        used = get_daily_usage(uid)
        limit = BETA_DAILY_LIMIT if status == "beta" else "∞"
        sub_info = ""
        streak = user.get("streak_days", 0)

        if status == "paid" and user["expires_at"]:
            exp = datetime.fromisoformat(user["expires_at"]).strftime("%d.%m.%Y")
            sub_info = f"\n💎 Подписка до {exp}"
        elif status == "beta" and user["trial_expires_at"]:
            trial_exp = datetime.fromisoformat(user["trial_expires_at"]).strftime("%d.%m.%Y")
            sub_info = f"\n🎁 Бесплатный период до {trial_exp}"

        streak_info = f"\n{streak_emoji(streak)} Серия: {streak} дней" if streak > 0 else ""

        await message.answer(
            f"👋 Привет, {name}!\n\n"
            f"📸 Пришли фото еды — посчитаю калории и БЖУ.\n"
            f"📊 Сегодня: {used}/{limit}{sub_info}{streak_info}",
            reply_markup=main_keyboard(uid == ADMIN_ID),
        )

        if user["daily_goal"] is None and uid not in user_states:
            await ask_daily_goal(bot, uid)

    # ── Callbacks: покупка ───────────────────────────────────────────────────
    @dp.callback_query(F.data == "buy_sub")
    async def cb_buy_sub(callback: CallbackQuery):
        uid = callback.from_user.id
        await callback.answer()
        await bot.send_invoice(
            chat_id=uid,
            title="CalorieBot — 30 дней",
            description="✅ Безлимит  ✅ Трекер калорий  ✅ Точный расчёт КБЖУ  🔥 Стрики",
            payload=f"sub_30d_{uid}",
            currency="XTR",
            prices=[LabeledPrice(label="Подписка 30 дней", amount=SUB_PRICE_STARS)],
        )

    # ── Callbacks: одобрение/блок ─────────────────────────────────────────────
    def safe_md(text: str) -> str:
        return (text or "").replace("_", "\\_").replace("*", "\\*")

    def user_card_md(user) -> str:
        name = safe_md(user["first_name"] or "")
        un = safe_md(user["username"] or "")
        un_str = f"@{un}" if un else f"id{user['telegram_id']}"
        return f"👤 {name} ({un_str})\n🆔 `{user['telegram_id']}`"

    @dp.callback_query(F.data.startswith("approve_"))
    async def cb_approve(callback: CallbackQuery):
        if callback.from_user.id != ADMIN_ID:
            await callback.answer("Нет доступа.", show_alert=True)
            return
        target_id = int(callback.data.split("_")[1])
        user = get_user(target_id)
        if not user:
            await callback.answer("Пользователь не найден.", show_alert=True)
            return
        approve_user(target_id, trial_days=3)
        approved = get_user(target_id)
        trial_exp = (
            datetime.fromisoformat(approved["trial_expires_at"]).strftime("%d.%m.%Y")
            if approved["trial_expires_at"] else "?"
        )
        await callback.answer("✅ Одобрено!")
        await callback.message.edit_text(
            f"🆕 Новый пользователь:\n{user_card_md(user)}\n\n✅ *Одобрён* (триал до {trial_exp})",
            parse_mode="Markdown",
            reply_markup=None,
        )
        try:
            await bot.send_message(
                target_id,
                f"✅ Доступ одобрен!\n\n"
                f"🎁 *Бесплатный период:* 3 дня, до {trial_exp}\n"
                f"📸 До {BETA_DAILY_LIMIT} анализов в день\n\n"
                f"Отправь фото еды — посчитаю калории!\n\n"
                f"📏 *Совет:* фотографируй еду с расстояния *10–15 см* — так точнее.",
                parse_mode="Markdown",
                reply_markup=main_keyboard(target_id == ADMIN_ID),
            )
            approved2 = get_user(target_id)
            if approved2 and approved2["daily_goal"] is None:
                await ask_daily_goal(bot, target_id)
        except Exception as e:
            log.warning(f"approve notify user: {e}")

    @dp.callback_query(F.data.startswith("block_"))
    async def cb_block(callback: CallbackQuery):
        if callback.from_user.id != ADMIN_ID:
            await callback.answer("Нет доступа.", show_alert=True)
            return
        target_id = int(callback.data.split("_")[1])
        user = get_user(target_id)
        if not user:
            await callback.answer("Пользователь не найден.", show_alert=True)
            return
        set_status(target_id, "blocked")
        await callback.answer("🚫 Заблокирован.")
        await callback.message.edit_text(
            f"🆕 Новый пользователь:\n{user_card_md(user)}\n\n🚫 *Заблокирован*",
            parse_mode="Markdown",
            reply_markup=None,
        )

    # ── Callbacks: setup flow ────────────────────────────────────────────────
    @dp.callback_query(F.data.in_({"goal_know", "goal_calc", "goal_skip"}))
    async def cb_goal_choice(callback: CallbackQuery):
        uid = callback.from_user.id
        await callback.answer()
        if callback.data == "goal_skip":
            user_states.pop(uid, None)
            await callback.message.edit_text("Норма не задана. Изменить через кнопку 🎯 Норма")
            return
        if callback.data == "goal_know":
            user_states[uid] = {"state": STATES["GOAL_ENTER"], "data": {}}
            await callback.message.edit_text("Введи свою дневную норму в ккал (например: 2000):")
            return
        user_states[uid] = {"state": STATES["CALC_AGE"], "data": {}}
        await callback.message.edit_text(
            "Рассчитаем норму по формуле Миффлина. Выбери пол:",
            reply_markup=gender_keyboard(),
        )

    @dp.callback_query(F.data.in_({"gender_m", "gender_f"}))
    async def cb_gender(callback: CallbackQuery):
        uid = callback.from_user.id
        await callback.answer()
        if user_states.get(uid, {}).get("state") != STATES["CALC_AGE"]:
            return
        user_states[uid]["data"]["gender"] = "m" if callback.data == "gender_m" else "f"
        await callback.message.edit_text("Сколько тебе лет? (введи число)")

    @dp.callback_query(F.data.startswith("act_"))
    async def cb_activity(callback: CallbackQuery):
        uid = callback.from_user.id
        await callback.answer()
        if user_states.get(uid, {}).get("state") != STATES["CALC_HEIGHT"]:
            return
        factor = float(callback.data[4:])
        data = user_states[uid]["data"]
        tdee, protein = calc_tdee(data["gender"], data["age"], data["weight"], data["height"], factor)
        set_daily_goal(uid, tdee, protein_goal=protein)
        user_states.pop(uid, None)
        await callback.message.edit_text(
            f"✅ *Твоя дневная норма: {tdee} ккал*\n"
            f"💪 Цель по белку: *{protein}г*\n\n"
            f"Рассчитано по формуле TDEE. Изменить → 🎯 Норма",
            parse_mode="Markdown",
        )

    # ── Callback: коррекция результата ──────────────────────────────────────
    @dp.callback_query(F.data.startswith("correct:"))
    async def cb_correct(callback: CallbackQuery):
        uid = callback.from_user.id
        await callback.answer()
        try:
            entry_id = int(callback.data.split(":")[1])
        except (ValueError, IndexError):
            return
        user_states[uid] = {"state": STATES["CORRECT_ENTRY"], "data": {"entry_id": entry_id}}
        await callback.message.edit_text(
            "✏️ Введи правильное количество калорий (только число, например: 450):\n\nИли /cancel для отмены."
        )

    # ── Текстовые сообщения ──────────────────────────────────────────────────
    @dp.message(F.text)
    async def handle_text(message: Message):
        uid = message.from_user.id
        text = message.text.strip() if message.text else ""
        state = user_states.get(uid)

        # ── Кнопки меню ──
        if text in MENU_BUTTONS:
            try:
                await message.delete()
            except Exception:
                pass

        if text == BTN_TODAY:
            await show_today(message)
            return
        if text == BTN_GOAL:
            await show_goal_setup(message, bot)
            return
        if text == BTN_STATUS:
            await show_status(message)
            return
        if text == BTN_REF:
            await show_ref(message)
            return
        if text == BTN_BUY:
            await do_buy(message, bot)
            return
        if text == BTN_WEIGHT:
            await show_weight(message)
            return
        if text == BTN_USERS and uid == ADMIN_ID:
            await show_users(message)
            return
        if text == BTN_STATS and uid == ADMIN_ID:
            await show_stats(message)
            return
        if text == BTN_ADD:
            user = get_user(uid)
            ok, reason = access_check(user)
            if not ok:
                await deny(message, reason)
                return
            user_states[uid] = {"state": STATES["MANUAL_ENTRY"], "data": {}}
            await message.answer(
                "✏️ *Ручной ввод*\n\n"
                "• *Число* → запишет калории напрямую (например: `350`)\n"
                "• *Название блюда* → рассчитаю КБЖУ (например: `гречка с курицей 200г`)\n\n"
                "Или /cancel для отмены.",
                parse_mode="Markdown",
            )
            return

        # ── State machine ──
        if not state:
            return

        s = state["state"]
        data = state["data"]

        if s == STATES["GOAL_ENTER"]:
            try:
                goal = int(re.sub(r"\D", "", text))
                if goal < 500 or goal > 10000:
                    raise ValueError
            except (ValueError, TypeError):
                await message.answer("Введи число от 500 до 10000 ккал:")
                return
            set_daily_goal(uid, goal)
            user_states.pop(uid, None)
            await message.answer(
                f"✅ Норма установлена: *{goal} ккал/день*",
                parse_mode="Markdown",
                reply_markup=main_keyboard(uid == ADMIN_ID),
            )
            return

        if s == STATES["CALC_AGE"]:
            try:
                age = int(re.sub(r"\D", "", text))
                if age < 10 or age > 100:
                    raise ValueError
            except (ValueError, TypeError):
                await message.answer("Введи возраст (10–100):")
                return
            data["age"] = age
            user_states[uid]["state"] = STATES["CALC_WEIGHT"]
            await message.answer("Сколько весишь? (кг, например: 70)")
            return

        if s == STATES["CALC_WEIGHT"]:
            try:
                weight = float(re.sub(r"[^\d.]", "", text))
                if weight < 30 or weight > 300:
                    raise ValueError
            except (ValueError, TypeError):
                await message.answer("Введи вес в кг (30–300):")
                return
            data["weight"] = weight
            user_states[uid]["state"] = STATES["CALC_HEIGHT"]
            await message.answer("Твой рост? (см, например: 175)")
            return

        if s == STATES["CALC_HEIGHT"]:
            try:
                height = float(re.sub(r"[^\d.]", "", text))
                if height < 100 or height > 250:
                    raise ValueError
            except (ValueError, TypeError):
                await message.answer("Введи рост в см (100–250):")
                return
            data["height"] = height
            await message.answer("Выбери уровень активности:", reply_markup=activity_keyboard())
            return

        if s == STATES["WEIGHT_LOG"]:
            user_states.pop(uid, None)
            try:
                w = float(re.sub(r"[^\d.]", "", text))
                if w < 20 or w > 300:
                    raise ValueError
            except (ValueError, TypeError):
                await message.answer("Введи вес в кг (например: 75.5):")
                user_states[uid] = {"state": STATES["WEIGHT_LOG"], "data": {}}
                return
            add_weight_log(uid, w)
            history = get_weight_history(uid, days=30)
            diff_line = ""
            if len(history) >= 2:
                first_w = history[0][1]
                diff = round(w - first_w, 1)
                sign = "+" if diff > 0 else ""
                diff_line = f"\n📉 За {len(history)} записей: *{sign}{diff} кг*"
            await message.answer(
                f"⚖️ Записано: *{w} кг*{diff_line}",
                parse_mode="Markdown",
                reply_markup=main_keyboard(uid == ADMIN_ID),
            )
            return

        if s == STATES["MANUAL_ENTRY"]:
            user_states.pop(uid, None)
            digits_only = re.sub(r"\D", "", text)
            if digits_only and len(text.strip()) <= 6:
                kcal = int(digits_only)
                if kcal < 1 or kcal > 9999:
                    await message.answer("Введи число от 1 до 9999 ккал:")
                    user_states[uid] = {"state": STATES["MANUAL_ENTRY"], "data": {}}
                    return
                record_usage(uid, kcal)
                progress = daily_progress_text(uid)
                await message.answer(
                    f"✅ Записано *{kcal} ккал*{progress}",
                    parse_mode="Markdown",
                    reply_markup=main_keyboard(uid == ADMIN_ID),
                )
            else:
                user = get_user(uid)
                if user["status"] == "beta" and get_daily_usage(uid) >= BETA_DAILY_LIMIT:
                    await message.answer(
                        f"⚠️ Лимит {BETA_DAILY_LIMIT} анализов в день исчерпан.",
                        reply_markup=main_keyboard(uid == ADMIN_ID),
                    )
                    return
                thinking_msg = await message.answer("🔍 Считаю калории...")
                try:
                    display, kcal, protein, fat, carbs = await analyze_food_text(text)
                    entry_id = None
                    if kcal:
                        entry_id = record_usage(uid, kcal, protein, fat, carbs)
                        streak, milestone = update_streak(uid)
                    progress = daily_progress_text(uid)
                    try:
                        await thinking_msg.delete()
                    except Exception:
                        pass
                    await message.answer(
                        display + progress,
                        parse_mode="Markdown",
                        reply_markup=main_keyboard(uid == ADMIN_ID),
                    )
                    if kcal and entry_id:
                        await message.answer(
                            "Результат неточный? Можно исправить:",
                            reply_markup=result_keyboard(entry_id),
                        )
                    if kcal and milestone and streak in STREAK_MILESTONES:
                        await message.answer(
                            f"🎉 Ачивка разблокирована!\n{STREAK_MILESTONES[streak]} подряд!\n\nТак держать! 💪",
                        )
                except Exception as e:
                    log.error(f"analyze_food_text error: {e}")
                    try:
                        await thinking_msg.delete()
                    except Exception:
                        pass
                    await message.answer(
                        "⚠️ Не удалось рассчитать. Попробуй снова.",
                        reply_markup=main_keyboard(uid == ADMIN_ID),
                    )
            return

        if s == STATES["CORRECT_ENTRY"]:
            entry_id = data.get("entry_id")
            try:
                new_kcal = int(re.sub(r"\D", "", text))
                if new_kcal < 1 or new_kcal > 9999:
                    raise ValueError
            except (ValueError, TypeError):
                await message.answer("Введи число от 1 до 9999 ккал:")
                return
            user_states.pop(uid, None)
            if entry_id:
                update_entry_calories(entry_id, new_kcal)
            progress = daily_progress_text(uid)
            await message.answer(
                f"✅ Скорректировано: *{new_kcal} ккал*{progress}",
                parse_mode="Markdown",
                reply_markup=main_keyboard(uid == ADMIN_ID),
            )
            return

    # ── Фото ────────────────────────────────────────────────────────────────
    @dp.message(F.photo)
    async def handle_photo(message: Message):
        uid = message.from_user.id
        upsert_user(uid, message.from_user.username or "", message.from_user.first_name or "")
        user = get_user(uid)
        ok, reason = access_check(user)
        if not ok:
            await deny(message, reason)
            return

        if user["status"] == "beta" and get_daily_usage(uid) >= BETA_DAILY_LIMIT:
            await message.answer(
                f"⚠️ Лимит {BETA_DAILY_LIMIT} анализов в день исчерпан.",
                reply_markup=main_keyboard(uid == ADMIN_ID),
            )
            return

        thinking_msg = await message.answer(
            "🔍 Анализирую...\n\n📏 _Держи телефон в 10–15 см от еды для лучшего результата_",
            parse_mode="Markdown",
        )
        try:
            photo: PhotoSize = message.photo[-1]
            file = await bot.get_file(photo.file_id)
            url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file.file_path}"
            async with httpx.AsyncClient() as client:
                photo_bytes = (await client.get(url, timeout=15)).content

            display, kcal, protein, fat, carbs = await analyze_food_photo(photo_bytes)
            entry_id = record_usage(uid, kcal, protein, fat, carbs)
            streak, milestone = update_streak(uid) if kcal else (0, False)

            progress = daily_progress_text(uid)
            hint = "\n\n_Установи норму — кнопка 🎯 Норма_" if user["daily_goal"] is None else ""

            try:
                await thinking_msg.delete()
            except Exception:
                pass

            await message.answer(
                display + progress + hint,
                parse_mode="Markdown",
                reply_markup=main_keyboard(uid == ADMIN_ID),
            )
            if kcal:
                await message.answer(
                    "Результат неточный? Можно исправить:",
                    reply_markup=result_keyboard(entry_id),
                )
            if milestone and streak in STREAK_MILESTONES:
                await message.answer(
                    f"🎉 Ачивка разблокирована!\n{STREAK_MILESTONES[streak]} подряд!\n\nПродолжай в том же духе! 💪",
                )

        except Exception as e:
            log.error(f"Ошибка анализа: {e}")
            try:
                await thinking_msg.delete()
            except Exception:
                pass
            await message.answer("⚠️ Не удалось проанализировать. Попробуй ещё раз.")

    # ── Платёж ──────────────────────────────────────────────────────────────
    @dp.pre_checkout_query()
    async def pre_checkout(query: PreCheckoutQuery):
        await query.answer(ok=True)

    @dp.message(F.successful_payment)
    async def payment_done(message: Message):
        uid = message.from_user.id
        activate_subscription(uid, SUB_DAYS)
        exp = (datetime.utcnow() + timedelta(days=SUB_DAYS)).strftime("%d.%m.%Y")
        await message.answer(
            f"🎉 Оплата прошла! Подписка до *{exp}*.\nОтправляй фото без ограничений 📸",
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
                    if ref_user["expires_at"] else "—"
                )
                await bot.send_message(
                    referrer_id,
                    f"🎁 Твой реферал оплатил! *+{REFERRAL_BONUS_DAYS} дней* → до *{new_exp}*",
                    parse_mode="Markdown",
                )
            except Exception as e:
                log.warning(f"Referral notify: {e}")
        user = get_user(uid)
        await notify_admin(bot, f"💰 Оплата: {user_label(user)} → до {exp}")

    # ─── Кнопочные функции ───────────────────────────────────────────────────

    async def show_today(message: Message):
        uid = message.from_user.id
        upsert_user(uid, message.from_user.username or "", message.from_user.first_name or "")
        user = get_user(uid)
        ok, reason = access_check(user)
        if not ok:
            await deny(message, reason)
            return

        macros = get_daily_macros(uid)
        total = macros["kcal"]
        meals = get_daily_usage(uid)
        goal = user["daily_goal"]
        streak = user.get("streak_days", 0)
        streak_line = f"\n{streak_emoji(streak)} Серия: *{streak} {'день' if streak == 1 else 'дней'}*" if streak > 0 else ""

        macros_line = ""
        if macros["protein"] > 0:
            macros_line = f"\n💪 Б: {macros['protein']}г  Ж: {macros['fat']}г  У: {macros['carbs']}г"

        if goal:
            remaining = max(goal - total, 0)
            bar = progress_bar(total, goal)
            over = total - goal
            extra = f"⚠️ Превышение на {over} ккал" if over > 0 else f"Осталось: {remaining} ккал"
            text = (
                f"📊 *Сегодня*\n\n"
                f"🔥 {total} / {goal} ккал\n"
                f"{bar}\n"
                f"{extra}{macros_line}\n"
                f"🍽 Приёмов пищи: {meals}{streak_line}"
            )
        else:
            text = (
                f"📊 *Сегодня*\n\n"
                f"🔥 Съедено: {total} ккал{macros_line}\n"
                f"🍽 Приёмов пищи: {meals}{streak_line}\n\n"
                f"Нажми 🎯 Норма — установить дневную цель"
            )
        await message.answer(text, parse_mode="Markdown")

    async def show_goal_setup(message: Message, bot: Bot):
        uid = message.from_user.id
        upsert_user(uid, message.from_user.username or "", message.from_user.first_name or "")
        user = get_user(uid)
        ok, reason = access_check(user)
        if not ok:
            await deny(message, reason)
            return
        current = user["daily_goal"]
        prefix = f"Текущая норма: *{current} ккал*\n\n" if current else ""
        user_states[uid] = {"state": STATES["GOAL_ASK"], "data": {}}
        await message.answer(
            f"{prefix}🎯 *Дневная норма калорий*\n\nВыбери способ:",
            parse_mode="Markdown",
            reply_markup=goal_ask_keyboard(),
        )

    async def show_status(message: Message):
        uid = message.from_user.id
        upsert_user(uid, message.from_user.username or "", message.from_user.first_name or "")
        user = get_user(uid)
        if not user or user["status"] == "pending":
            await message.answer("⏳ Заявка на рассмотрении.")
            return

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
            f"\n{streak_emoji(streak)} Серия: *{streak}* дней  |  Рекорд: *{best_streak}*"
            if streak > 0 or best_streak > 0 else ""
        )

        if status == "paid" and not check_subscription_expired(uid):
            exp_dt = datetime.fromisoformat(user["expires_at"])
            exp = exp_dt.strftime("%d.%m.%Y")
            days_left = max((exp_dt - datetime.utcnow()).days, 0)
            await message.answer(
                f"💎 *Подписка активна*\n"
                f"📅 До {exp} — осталось *{days_left} дн.*\n"
                f"📸 Анализов сегодня: {used}\n"
                f"🔥 Ккал: {kcal_str}\n"
                f"👥 Рефералов: {ref_s['total']} (оплатили: {ref_s['paid']})"
                f"{streak_block}",
                parse_mode="Markdown",
            )
        elif status == "beta" and user.get("trial_expires_at"):
            trial_dt = datetime.fromisoformat(user["trial_expires_at"])
            trial_exp = trial_dt.strftime("%d.%m.%Y")
            days_left = max((trial_dt - datetime.utcnow()).days, 0)
            await message.answer(
                f"🎁 *Бесплатный период*\n"
                f"📅 До {trial_exp} — осталось *{days_left} дн.*\n"
                f"📊 Анализов: {used}/{BETA_DAILY_LIMIT}\n"
                f"🔥 Ккал: {kcal_str}\n"
                f"👥 Рефералов: {ref_s['total']} (оплатили: {ref_s['paid']})"
                f"{streak_block}",
                parse_mode="Markdown",
            )
        else:
            await message.answer(
                f"⏰ *Подписка истекла*\n"
                f"📊 Анализов сегодня: {used}\n"
                f"🔥 Ккал: {kcal_str}\n"
                f"👥 Рефералов: {ref_s['total']} (оплатили: {ref_s['paid']})"
                f"{streak_block}\n\n"
                f"Оформи подписку — кнопка 💳 Подписка",
                parse_mode="Markdown",
            )

    async def show_ref(message: Message):
        uid = message.from_user.id
        upsert_user(uid, message.from_user.username or "", message.from_user.first_name or "")
        user = get_user(uid)
        ok, reason = access_check(user)
        if not ok:
            await deny(message, reason)
            return
        stats = get_referral_stats(uid)
        link = ref_link(uid)
        await message.answer(
            f"👥 *Пригласи друга — получи дни бесплатно!*\n\n"
            f"🔗 Твоя ссылка:\n{link}\n\n"
            f"🎁 За регистрацию друга — *+{REFERRAL_JOIN_BONUS_DAYS} дня*\n"
            f"💰 За его оплату подписки — ещё *+{REFERRAL_BONUS_DAYS} дней*\n\n"
            f"📊 Приглашено: {stats['total']}  |  Оплатили: {stats['paid']}",
            parse_mode="Markdown",
            reply_markup=ref_keyboard(uid),
        )

    async def show_weight(message: Message):
        uid = message.from_user.id
        upsert_user(uid, message.from_user.username or "", message.from_user.first_name or "")
        user = get_user(uid)
        ok, reason = access_check(user)
        if not ok:
            await deny(message, reason)
            return

        history = get_weight_history(uid, days=30)
        if history:
            current_w = history[-1][1]
            lines = [f"⚖️ *История веса (30 дней)*\n\nТекущий: *{current_w} кг*\n"]
            for d, w in history[-7:]:
                lines.append(f"  {d}: {w} кг")
            if len(history) >= 2:
                diff = round(history[-1][1] - history[0][1], 1)
                sign = "+" if diff > 0 else ""
                lines.append(f"\n📉 Изменение за период: *{sign}{diff} кг*")
            lines.append("\nВведи новый вес (кг), или /cancel для отмены:")
            await message.answer("\n".join(lines), parse_mode="Markdown")
        else:
            await message.answer(
                "⚖️ *Трекер веса*\n\nЗаписей пока нет.\n\nВведи свой текущий вес в кг (например: 75.5):",
                parse_mode="Markdown",
            )
        user_states[uid] = {"state": STATES["WEIGHT_LOG"], "data": {}}

    async def do_buy(message: Message, bot: Bot):
        uid = message.from_user.id
        upsert_user(uid, message.from_user.username or "", message.from_user.first_name or "")
        user = get_user(uid)
        ok, reason = access_check(user)
        if not ok:
            await deny(message, reason)
            return
        await bot.send_invoice(
            chat_id=uid,
            title="CalorieBot — 30 дней",
            description="✅ Безлимит  ✅ Трекер калорий  ✅ Точный расчёт КБЖУ  🔥 Стрики",
            payload=f"sub_30d_{uid}",
            currency="XTR",
            prices=[LabeledPrice(label="Подписка 30 дней", amount=SUB_PRICE_STARS)],
        )

    async def show_users(message: Message):
        users = get_all_users()
        if not users:
            await message.answer("Нет пользователей.")
            return
        icons = {"pending": "⏳", "beta": "✅", "paid": "💎", "blocked": "🚫"}
        lines = ["👥 Пользователи:\n"]
        for u in users[:30]:
            name = (u["first_name"] or "").replace("_", " ").replace("*", "").replace("`", "")
            un = u["username"] or ""
            label = f"{name} (@{un})" if un else f"{name} (id{u['telegram_id']})"
            streak = u.get("streak_days", 0)
            streak_icon = f" 🔥{streak}" if streak > 1 else ""
            lines.append(f"{icons.get(u['status'], '❓')} {label} — {u['telegram_id']}{streak_icon}")
        await message.answer("\n".join(lines))

    async def show_stats(message: Message):
        s = get_total_stats()
        await message.answer(
            f"📈 *Статистика*\n\n"
            f"👥 Всего: {s['total_users']}  (⏳{s['pending']} ✅{s['beta']} 💎{s['paid']} 🚫{s['blocked']})\n"
            f"📸 Сегодня: {s['analyses_today']}  |  Всего: {s['analyses_total']}\n"
            f"📅 Активных за 7 дней: {s['wau']}\n"
            f"🔗 Реф. оплат: {s['referrals_paid']}",
            parse_mode="Markdown",
        )

    # ── Admin команды ────────────────────────────────────────────────────────
    def is_admin(m: Message) -> bool:
        return m.from_user.id == ADMIN_ID

    @dp.message(Command("approve"))
    async def cmd_approve(message: Message):
        if not is_admin(message):
            return
        parts = (message.text or "").split()
        if len(parts) < 2:
            await message.answer("Использование: /approve USER_ID")
            return
        try:
            target_id = int(parts[1])
        except ValueError:
            await message.answer("Неверный ID")
            return
        user = get_user(target_id)
        if not user:
            await message.answer("Пользователь не найден.")
            return
        approve_user(target_id, trial_days=3)
        approved = get_user(target_id)
        trial_exp = (
            datetime.fromisoformat(approved["trial_expires_at"]).strftime("%d.%m.%Y")
            if approved["trial_expires_at"] else "?"
        )
        await message.answer(f"✅ {user_label(user)} одобрен (триал до {trial_exp}).")
        try:
            await bot.send_message(
                target_id,
                f"✅ Доступ одобрен!\n\n"
                f"🎁 *Бесплатный период:* 3 дня, до {trial_exp}\n"
                f"📸 До {BETA_DAILY_LIMIT} анализов в день\n\n"
                f"Отправь фото еды — посчитаю калории!\n\n"
                f"📏 *Совет:* фотографируй еду с расстояния *10–15 см* — так точнее.",
                parse_mode="Markdown",
                reply_markup=main_keyboard(target_id == ADMIN_ID),
            )
            if approved and approved["daily_goal"] is None:
                await ask_daily_goal(bot, target_id)
        except Exception:
            pass

    @dp.message(Command("block"))
    async def cmd_block(message: Message):
        if not is_admin(message):
            return
        parts = (message.text or "").split()
        if len(parts) < 2:
            await message.answer("Использование: /block USER_ID")
            return
        try:
            target_id = int(parts[1])
        except ValueError:
            await message.answer("Неверный ID")
            return
        user = get_user(target_id)
        if not user:
            await message.answer("Пользователь не найден.")
            return
        set_status(target_id, "blocked")
        await message.answer(f"🚫 {user_label(user)} заблокирован.")

    @dp.message(Command("give"))
    async def cmd_give(message: Message):
        if not is_admin(message):
            return
        parts = (message.text or "").split()
        if len(parts) < 2:
            await message.answer("Использование: /give USER_ID [дней]")
            return
        try:
            target_id = int(parts[1])
            days = int(parts[2]) if len(parts) > 2 else 30
        except ValueError:
            await message.answer("Неверные параметры.")
            return
        user = get_user(target_id)
        if not user:
            await message.answer("Пользователь не найден.")
            return
        activate_subscription(target_id, days)
        exp = (datetime.utcnow() + timedelta(days=days)).strftime("%d.%m.%Y")
        await message.answer(f"💎 {user_label(user)} — подписка до {exp}.")
        try:
            await bot.send_message(
                target_id,
                f"🎁 Тебе выдан доступ до *{exp}*! 📸",
                parse_mode="Markdown",
                reply_markup=main_keyboard(target_id == ADMIN_ID),
            )
        except Exception:
            pass

    @dp.message(Command("stats"))
    async def cmd_stats(message: Message):
        if not is_admin(message):
            return
        await show_stats(message)

    @dp.message(Command("users"))
    async def cmd_users(message: Message):
        if not is_admin(message):
            return
        parts = (message.text or "").split()
        status_filter = parts[1] if len(parts) > 1 else None
        users = get_all_users(status_filter)
        if not users:
            await message.answer("Нет пользователей.")
            return
        icons = {"pending": "⏳", "beta": "✅", "paid": "💎", "blocked": "🚫"}
        lines = [f"👥 Пользователи{' (' + status_filter + ')' if status_filter else ''}:\n"]
        for u in users[:30]:
            lines.append(f"{icons.get(u['status'], '❓')} {user_label(u)} — `{u['telegram_id']}`")
        await message.answer("\n".join(lines), parse_mode="Markdown")

    @dp.message(Command("myid"))
    async def cmd_myid(message: Message):
        uid = message.from_user.id
        is_adm = uid == ADMIN_ID
        await message.answer(
            f"🆔 Твой Telegram ID: `{uid}`\n"
            f"👑 Статус админа: {'✅ Да' if is_adm else '❌ Нет'}\n\n"
            f"{'Всё верно — ты администратор.' if is_adm else f'Если ты администратор, установи TELEGRAM_CHAT_ID={uid} в переменных Railway.'}",
            parse_mode="Markdown",
        )

    @dp.message(Command("cancel"))
    async def cmd_cancel(message: Message):
        uid = message.from_user.id
        if uid in user_states:
            user_states.pop(uid)
            await message.answer(
                "❌ Отменено.",
                reply_markup=main_keyboard(uid == ADMIN_ID),
            )
        else:
            await message.answer("Нечего отменять.", reply_markup=main_keyboard(uid == ADMIN_ID))

    log.info("CalorieBot запущен.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
