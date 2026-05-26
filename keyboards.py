from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from config import (
    ADMIN_ID, SUB_PRICE_STARS, SUB_PRICE_3M, SUB_PRICE_12M,
    SUB_DAYS, SUB_DAYS_3M, SUB_DAYS_12M,
)
from db import get_entries_today

BTN_PHOTO    = "📸 Анализ еды"
BTN_MANUAL   = "✍️ Вручную"
BTN_PROGRESS = "📋 Сегодня"
BTN_SUB      = "⭐ Premium"
BTN_REF      = "👥 Рефералы"
BTN_PROFILE  = "👤 Профиль"
BTN_ADMIN    = "🛠 Админка"

MENU_BUTTONS = {
    BTN_PHOTO, BTN_MANUAL, BTN_PROGRESS,
    BTN_SUB, BTN_REF, BTN_PROFILE, BTN_ADMIN,
}


def main_keyboard(is_admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_PHOTO)],
        [KeyboardButton(text=BTN_PROGRESS), KeyboardButton(text=BTN_PROFILE)],
        [KeyboardButton(text=BTN_REF),      KeyboardButton(text=BTN_SUB)],
    ]
    if is_admin:
        rows.append([KeyboardButton(text=BTN_ADMIN)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def result_keyboard(entry_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✏️ Исправить калории", callback_data=f"correct:{entry_id}")
    ]])


def diary_keyboard(entries: list) -> InlineKeyboardMarkup:
    rows = []
    for e in entries:
        kcal  = e["calories"] or 0
        name  = (e.get("food_name") or "блюдо").strip()
        label = f"{name[:28]} — {kcal} ккал" if len(name) <= 28 else f"{name[:26]}… — {kcal} ккал"
        rows.append([
            InlineKeyboardButton(text=label, callback_data="noop"),
            InlineKeyboardButton(text="✏️",  callback_data=f"edit_e:{e['id']}"),
            InlineKeyboardButton(text="🗑",  callback_data=f"del_e:{e['id']}"),
        ])
    rows.append([InlineKeyboardButton(text="Сбросить весь день", callback_data="reset_day")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def progress_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Дневник", callback_data="diary")],
    ])


def new_user_keyboard(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💎 +7 дней",       callback_data=f"give7_{uid}"),
        InlineKeyboardButton(text="🚫 Заблокировать", callback_data=f"block_{uid}"),
    ]])


def profile_keyboard(uid: int, has_goal: bool = False) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📈 Неделя",            callback_data="profile_week")],
        [InlineKeyboardButton(text="🍽 План питания",      callback_data="show_meal_plan")],
        [InlineKeyboardButton(text="⚖️ Записать вес",      callback_data="profile_weight")],
        [InlineKeyboardButton(text="🔄 Пересчитать норму", callback_data="recalc_norm")],
        [InlineKeyboardButton(text="ℹ️ Статус подписки",  callback_data="profile_status")],
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
            InlineKeyboardButton(text="📊 Статистика", callback_data="adm_stats"),
            InlineKeyboardButton(text="🔄 Обновить",   callback_data="adm_refresh"),
        ],
        [
            InlineKeyboardButton(text="📋 Бета-юзеры", callback_data="adm_beta"),
            InlineKeyboardButton(text="💎 Платные",    callback_data="adm_paid"),
        ],
        [
            InlineKeyboardButton(text="👥 Все юзеры",  callback_data="adm_users"),
            InlineKeyboardButton(text="📡 Рассылка",   callback_data="adm_broadcast"),
        ],
        [
            InlineKeyboardButton(text="📈 Воронка",    callback_data="adm_funnel"),
        ],
    ])


def user_action_keyboard(target_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💎 +7 дней",         callback_data=f"give7_{target_id}"),
            InlineKeyboardButton(text="💎 +30 дней",        callback_data=f"give30_{target_id}"),
        ],
        [
            InlineKeyboardButton(text="⚡ +30 дней (старт)", callback_data=f"activate_{target_id}"),
            InlineKeyboardButton(text="🚫 Блокировать",     callback_data=f"block_{target_id}"),
        ],
    ])
