from datetime import datetime

from aiogram import Router, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

from config import ADMIN_ID
from db import (
    get_user, get_weekly_stats, get_entries_today, get_daily_macros,
    update_entry_calories, delete_entry, reset_today_entries,
)
from keyboards import main_keyboard, diary_keyboard
from services.state import user_states, _set_state
from config import STATES

router = Router()


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


@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data == "history7")
async def cb_history7(callback: CallbackQuery):
    uid = callback.from_user.id
    await callback.answer()
    user  = get_user(uid)
    goal  = user.get("daily_goal") if user else None
    stats = get_weekly_stats(uid)
    days  = stats["dates"]
    daily = stats["daily"]
    day_names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    lines = []
    for d, data in zip(days, daily):
        from datetime import date as _date
        dt   = _date.fromisoformat(d)
        dn   = day_names[dt.weekday()]
        kcal = data["kcal"]
        if kcal == 0:
            lines.append(f"{dn} {dt.strftime('%d.%m')}  —")
        elif goal:
            pct = round(kcal / goal * 100)
            bar = "●" * min(pct // 20, 5)
            lines.append(f"{dn} {dt.strftime('%d.%m')}  *{kcal}* / {goal}  {bar}")
        else:
            lines.append(f"{dn} {dt.strftime('%d.%m')}  *{kcal} ккал*")
    avg    = stats["avg_kcal"]
    logged = stats["logged_days"]
    avg_line = f"\nСредн: *{avg} ккал/день* · {logged}/7 дней залогировано" if logged else ""
    await callback.message.answer(
        "📅 *История за 7 дней*\n\n" + "\n".join(lines) + avg_line,
        parse_mode="Markdown",
    )


@router.callback_query(F.data == "diary")
async def cb_diary(callback: CallbackQuery):
    uid = callback.from_user.id
    await callback.answer()
    await _show_diary(callback.message.answer, uid)


@router.callback_query(F.data.startswith("edit_e:"))
async def cb_edit_entry(callback: CallbackQuery):
    uid = callback.from_user.id
    await callback.answer()
    try:
        entry_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        return
    _set_state(uid, STATES["CORRECT_ENTRY"], {"entry_id": entry_id})
    await callback.message.answer(
        "✏️ *Введи новое значение калорий:*\n_/cancel — отмена_",
        parse_mode="Markdown",
    )


@router.callback_query(F.data.startswith("del_e:"))
async def cb_del_entry_ask(callback: CallbackQuery):
    uid = callback.from_user.id
    await callback.answer()
    try:
        entry_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        return
    entries = get_entries_today(uid)
    entry   = next((e for e in entries if e["id"] == entry_id), None)
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
            InlineKeyboardButton(text="❌ Нет",         callback_data="del_e_cancel"),
        ]]),
    )


@router.callback_query(F.data.startswith("del_e_ok:"))
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


@router.callback_query(F.data == "del_e_cancel")
async def cb_del_entry_cancel(callback: CallbackQuery):
    await callback.answer("Отменено")
    try:
        await callback.message.delete()
    except Exception:
        pass


@router.callback_query(F.data == "reset_day")
async def cb_reset_day_ask(callback: CallbackQuery):
    uid    = callback.from_user.id
    await callback.answer()
    macros = get_daily_macros(uid)
    total  = macros["kcal"]
    await callback.message.answer(
        f"🗑 *Сбросить весь день?*\n\nБудут удалены все записи за сегодня ({total} ккал).\nОтменить нельзя.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Да, сбросить", callback_data="reset_day_ok"),
            InlineKeyboardButton(text="❌ Нет",           callback_data="del_e_cancel"),
        ]]),
    )


@router.callback_query(F.data == "reset_day_ok")
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


@router.callback_query(F.data.startswith("correct:"))
async def cb_correct_entry(callback: CallbackQuery):
    uid      = callback.from_user.id
    await callback.answer()
    entry_id = int(callback.data.split(":")[1])
    _set_state(uid, STATES["CORRECT_ENTRY"], {"entry_id": entry_id})
    await callback.message.answer(
        "✏️ Введи правильное количество калорий (целое число):"
    )
