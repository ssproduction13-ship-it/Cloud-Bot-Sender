import logging
from datetime import datetime, timezone

from aiogram import Router, Bot, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

from config import ADMIN_ID, SUB_DAYS
from db import (
    get_user, set_status, approve_user, activate_subscription,
    get_all_users, get_active_users, get_total_stats, get_referral_stats,
    get_users_by_segment, track_event,
    fix_all_streaks,
)
from keyboards import main_keyboard, admin_panel_keyboard, user_action_keyboard
from services.state import user_states, _set_state
from config import STATES

log = logging.getLogger(__name__)
router = Router()

_utcnow = lambda: datetime.now(timezone.utc).replace(tzinfo=None)


def _fmt_user_card(u: dict) -> str:
    icons     = {"pending": "⏳", "beta": "✅", "paid": "💎", "blocked": "🚫"}
    safe_name = (u.get("first_name") or "").replace("_", "\\_").replace("*", "\\*")
    un        = (u.get("username") or "").replace("_", "\\_")
    un_str    = f"@{un}" if un else f"id{u['telegram_id']}"
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


def _build_admin_stats_text() -> str:
    s = get_total_stats()
    return (
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


async def show_admin_panel(send_fn):
    try:
        text = _build_admin_stats_text()
    except Exception as e:
        log.error(f"admin panel stats error: {e}")
        text = "🛡 *Admin Panel*\n\n⚠️ Ошибка загрузки статистики.\n\nПопробуй /stats"
    await send_fn(text, parse_mode="Markdown", reply_markup=admin_panel_keyboard())


@router.message(Command("restart_all"))
async def cmd_restart_all(message: Message, bot: Bot):
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
    await message.answer(f"✅ Разослано: {sent}, не доставлено: {failed}")


@router.message(Command("cleargoals"))
async def cmd_cleargoals(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    from db import clear_all_goals
    count = clear_all_goals()
    await message.answer(
        f"✅ Готово. Очищено у *{count}* пользователей:\n"
        f"daily\\_goal, protein\\_goal, goal\\_type, weight\\_kg, height\\_cm, age, gender",
        parse_mode="Markdown",
    )


@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    await show_admin_panel(message.answer)


@router.message(Command("user"))
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


@router.message(Command("approve"))
async def cmd_approve(message: Message, bot: Bot):
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
            from handlers.onboarding import start_onboarding
            await start_onboarding(bot, target_id, u.get("first_name") or "друг")
        else:
            await bot.send_message(target_id,
                "✅ *Доступ открыт!* Отправляй фото еды — считаю калории 📸",
                parse_mode="Markdown", reply_markup=main_keyboard(False))
    except Exception:
        pass


@router.message(Command("activate"))
async def cmd_activate(message: Message, bot: Bot):
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
    u   = get_user(target_id)
    exp = datetime.fromisoformat(u["expires_at"]).strftime("%d.%m.%Y") if u and u.get("expires_at") else "?"
    await message.answer(f"💎 Подписка активирована. {target_id} → до {exp}")
    try:
        await bot.send_message(target_id,
            f"🎉 *Подписка активирована* до *{exp}*!\n\nОтправляй фото без ограничений 🚀",
            parse_mode="Markdown")
    except Exception:
        pass


@router.message(Command("give"))
async def cmd_give(message: Message, bot: Bot):
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Использование: /give ID ДНЕЙ\nПример: /give 123456789 30")
        return
    try:
        target_id = int(parts[1])
        days      = int(parts[2]) if len(parts) > 2 else 7
    except ValueError:
        await message.answer("Ошибка парсинга.")
        return
    activate_subscription(target_id, days)
    u   = get_user(target_id)
    exp = datetime.fromisoformat(u["expires_at"]).strftime("%d.%m.%Y") if u and u.get("expires_at") else "?"
    await message.answer(f"✅ +{days} дней → {target_id} до {exp}")
    try:
        await bot.send_message(target_id,
            f"🎁 *+{days} дней* добавлено к подписке → до *{exp}*!",
            parse_mode="Markdown")
    except Exception:
        pass


@router.message(Command("giveall"))
async def cmd_giveall(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Использование: /giveall ДНЕЙ")
        return
    try:
        days = int(parts[1])
    except ValueError:
        await message.answer("Ошибка: ДНЕЙ должно быть числом.")
        return
    if days < 1 or days > 365:
        await message.answer("Дней должно быть от 1 до 365.")
        return
    targets = [u for u in get_all_users(None) if u.get("status") != "blocked"]
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
        f"✅ Готово!\n💎 +{days} дней выдано: *{ok}* пользователей\n❌ Ошибок: {failed}",
        parse_mode="Markdown",
    )


@router.message(Command("block"))
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
    await message.answer(
        f"🚫 Пользователь {target_id} заблокирован.\n\nДля разблокировки: /give {target_id} 7"
    )


@router.message(Command("users"))
async def cmd_users(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split()
    sf    = parts[1] if len(parts) > 1 else None
    users = get_all_users(sf)
    if not users:
        await message.answer("Нет пользователей.\n\nФильтры: /users beta | paid | blocked")
        return
    icons = {"pending": "⏳", "beta": "✅", "paid": "💎", "blocked": "🚫"}
    lines = [f"*{'Все' if not sf else sf.upper()} ({len(users)}):*\n"]
    for u in users[:30]:
        nm     = (u["first_name"] or "").replace("_", "\\_").replace("*", "\\*")[:15]
        un     = (u["username"] or "").replace("_", "\\_")
        label  = f"{nm} (@{un})" if un else f"{nm} (id{u['telegram_id']})"
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


@router.message(Command("stats"))
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
        f"📈 D1: {s['d1_retention']}%  |  D7: {s['d7_retention']}%\n"
        f"🔥 Средний стрик: {s['avg_streak']} дн.\n"
        f"🔗 Реф. оплат: {s['referrals_paid']}",
        parse_mode="Markdown",
    )


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, bot: Bot):
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /broadcast ТЕКСТ")
        return
    text  = parts[1]
    users = get_active_users()
    sent = failed = 0
    for u in users:
        try:
            await bot.send_message(u["telegram_id"], text)
            sent += 1
        except Exception:
            failed += 1
    await message.answer(f"📡 Рассылка завершена.\n✅ Отправлено: {sent}\n❌ Ошибок: {failed}")


@router.callback_query(F.data.startswith("bcast:"))
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
    _set_state(callback.from_user.id, STATES["ADMIN_BROADCAST"],
               {"segment": segment, "segment_label": label, "segment_count": len(users)})
    await callback.message.answer(
        f"📡 *Рассылка → {label}*\n"
        f"👥 Получателей: *{len(users)}*\n\n"
        f"Введи текст сообщения для рассылки:",
        parse_mode="Markdown",
    )


@router.callback_query(F.data == "adm_funnel")
async def cb_admin_funnel(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.answer()
    from db import get_events_summary, get_total_stats
    s = get_total_stats()
    total       = s["total_users"]
    ob_done     = get_events_summary("onboarding_completed", days=30)
    first_scan  = get_events_summary("first_food_scan", days=30)
    second_scan = get_events_summary("second_food_scan", days=30)
    d1          = s["d1_retention"]
    d7          = s["d7_retention"]
    prem_click  = get_events_summary("premium_clicked", days=30)
    prem_buy    = get_events_summary("premium_purchased", days=30)

    def pct(num, base):
        return f"{round(num * 100 / base)}%" if base else "—"

    text = (
        "📈 *Воронка (30 дней)*\n\n"
        f"👥 Всего юзеров:        *{total}*\n"
        f"✅ Онбординг пройден:   *{ob_done}* ({pct(ob_done, total)})\n"
        f"📸 Первый скан:         *{first_scan}* ({pct(first_scan, ob_done)})\n"
        f"🔄 Второй скан:         *{second_scan}* ({pct(second_scan, first_scan)})\n"
        f"📅 D1 retention:        *{d1}%*\n"
        f"📅 D7 retention:        *{d7}%*\n"
        f"⭐ Нажали Premium:      *{prem_click}* ({pct(prem_click, first_scan)})\n"
        f"💎 Оформили Premium:    *{prem_buy}* ({pct(prem_buy, prem_click)})\n"
    )
    await callback.message.answer(text, parse_mode="Markdown",
                                   reply_markup=admin_panel_keyboard())


@router.callback_query(F.data.in_({"adm_stats", "adm_users", "adm_beta",
                                    "adm_paid", "adm_refresh", "adm_broadcast"}))
async def cb_admin_panel(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.answer()
    data = callback.data

    if data in ("adm_stats", "adm_refresh"):
        try:
            text = _build_admin_stats_text()
        except Exception as e:
            log.error(f"admin refresh error: {e}")
            text = "🛡 *Admin Panel*\n\n⚠️ Ошибка загрузки статистики."
        try:
            await callback.message.edit_text(
                text, parse_mode="Markdown", reply_markup=admin_panel_keyboard()
            )
        except Exception:
            await callback.message.answer(
                text, parse_mode="Markdown", reply_markup=admin_panel_keyboard()
            )
        return

    if data == "adm_broadcast":
        await callback.message.answer(
            "📡 *Сегментированная рассылка*\n\nВыбери аудиторию:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="👥 Все активные",        callback_data="bcast:all_active")],
                [InlineKeyboardButton(text="🎁 Триал-пользователи",  callback_data="bcast:trial_active")],
                [InlineKeyboardButton(text="💎 Платные подписки",    callback_data="bcast:paid_active")],
                [InlineKeyboardButton(text="⏰ Подписка истекла",    callback_data="bcast:sub_expired")],
                [InlineKeyboardButton(text="😴 Не логируют 7+ дней", callback_data="bcast:no_log_week")],
            ]),
        )
        return

    status_filter = "beta" if data == "adm_beta" else "paid" if data == "adm_paid" else None
    users  = get_all_users(status_filter)
    icons  = {"pending": "⏳", "beta": "✅", "paid": "💎", "blocked": "🚫"}
    if not users:
        await callback.message.answer("Нет пользователей в этой категории.")
        return
    label  = {"adm_beta": "БЕТА", "adm_paid": "ПЛАТНЫЕ", "adm_users": "ВСЕ"}.get(data, "ВСЕ")
    lines  = [f"*{label} ({min(len(users),25)}):*\n"]
    for u in users[:25]:
        nm     = (u["first_name"] or "").replace("_", "\\_").replace("*", "\\*")[:15]
        un     = (u["username"] or "").replace("_", "\\_")
        lbl    = f"{nm} (@{un})" if un else f"{nm} (id{u['telegram_id']})"
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


@router.callback_query(F.data.startswith("approve_"))
async def cb_approve(callback: CallbackQuery, bot: Bot):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    target_id = int(callback.data.split("_")[1])
    approve_user(target_id, trial_days=3)
    await callback.answer("✅ Одобрено")
    try:
        u = get_user(target_id)
        if u and not u.get("onboarded"):
            from handlers.onboarding import start_onboarding
            await start_onboarding(bot, target_id, u.get("first_name") or "друг")
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


@router.callback_query(F.data.startswith("block_"))
async def cb_block(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    target_id = int(callback.data.split("_")[1])
    set_status(target_id, "blocked")
    await callback.answer("🚫 Заблокировано")
    try:
        await callback.message.edit_text(f"🚫 Заблокирован: `{target_id}`", parse_mode="Markdown")
    except Exception:
        pass


@router.callback_query(F.data.startswith("give7_"))
async def cb_give7(callback: CallbackQuery, bot: Bot):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    target_id = int(callback.data.split("_")[1])
    activate_subscription(target_id, 7)
    u   = get_user(target_id)
    exp = datetime.fromisoformat(u["expires_at"]).strftime("%d.%m.%Y") if u and u.get("expires_at") else "?"
    await callback.answer(f"✅ +7 дней → до {exp}")
    try:
        await bot.send_message(target_id, f"🎁 *+7 дней* к подписке → до *{exp}*!",
                               parse_mode="Markdown")
    except Exception:
        pass


@router.callback_query(F.data.startswith("give30_"))
async def cb_give30(callback: CallbackQuery, bot: Bot):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    target_id = int(callback.data.split("_")[1])
    activate_subscription(target_id, 30)
    u   = get_user(target_id)
    exp = datetime.fromisoformat(u["expires_at"]).strftime("%d.%m.%Y") if u and u.get("expires_at") else "?"
    await callback.answer(f"✅ +30 дней → до {exp}")
    try:
        await bot.send_message(target_id, f"🎁 *+30 дней* к подписке → до *{exp}*!",
                               parse_mode="Markdown")
    except Exception:
        pass


@router.callback_query(F.data.startswith("activate_"))
async def cb_activate(callback: CallbackQuery, bot: Bot):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    target_id = int(callback.data.split("_")[1])
    activate_subscription(target_id, SUB_DAYS)
    u   = get_user(target_id)
    exp = datetime.fromisoformat(u["expires_at"]).strftime("%d.%m.%Y") if u and u.get("expires_at") else "?"
    await callback.answer(f"⚡ Активировано до {exp}")
    try:
        await bot.send_message(target_id,
            f"⚡ *Подписка активирована* до *{exp}*!",
            parse_mode="Markdown", reply_markup=main_keyboard(False))
    except Exception:
        pass


@router.message(Command("fixstreaks"))
async def cmd_fix_streaks(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("🔧 Пересчитываю стрики по истории записей...")
    try:
        changes = fix_all_streaks()
        if not changes:
            await message.answer("✅ Все стрики уже верны, изменений нет.")
            return
        lines_out = [f"uid {c['telegram_id']}: {c['old']} → {c['new']}" for c in changes[:30]]
        total = len(changes)
        tail  = f"\n...и ещё {total - 30}" if total > 30 else ""
        await message.answer(
            f"✅ Пересчитано: {total} пользователей\n\n" + "\n".join(lines_out) + tail
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


@router.message(Command("checkstreaks"))
async def cmd_check_streaks(message: Message):
    """Dry-run: show what fix_all_streaks WOULD do, without changing anything."""
    if message.from_user.id != ADMIN_ID:
        return
    from datetime import date as _date, timedelta as _td
    from db import get_conn
    import psycopg2.extras
    from db import _utcnow
    await message.answer("🔍 Проверяю стрики (без изменений)...")
    try:
        rows_out = []
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT DISTINCT u.telegram_id, u.streak_days, u.first_name "
                    "FROM users u "
                    "JOIN usage g ON g.telegram_id = u.telegram_id "
                    "WHERE (g.deleted IS NULL OR g.deleted = FALSE) "
                    "ORDER BY u.streak_days DESC"
                )
                users = list(cur.fetchall())
            for user in users:
                uid = user["telegram_id"]
                cur_streak = user["streak_days"] or 0
                name = (user.get("first_name") or str(uid))[:12]
                with conn.cursor() as cur2:
                    cur2.execute(
                        "SELECT DISTINCT date FROM usage "
                        "WHERE telegram_id=%s AND (deleted IS NULL OR deleted=FALSE) "
                        "ORDER BY date DESC LIMIT 30",
                        (uid,),
                    )
                    date_rows = cur2.fetchall()
                logged = set()
                for dr in date_rows:
                    v = dr[0]
                    if isinstance(v, str):
                        try: v = _date.fromisoformat(v[:10])
                        except: continue
                    logged.add(v)
                if not logged:
                    continue
                anchor = max(logged)
                real_streak = 0
                check = anchor
                while check in logged:
                    real_streak += 1
                    check -= _td(days=1)
                recent = sorted(logged, reverse=True)[:5]
                recent_str = ", ".join(str(x) for x in recent)
                rows_out.append(f"{name} (uid {uid}): DB={cur_streak} → real={real_streak} | last 5: {recent_str}")
        if not rows_out:
            await message.answer("Нет пользователей с записями.")
            return
        text = "📊 Стрики (расчёт без изменений):

" + "
".join(rows_out[:20])
        text += "

Когда всё верно — запусти /fixstreaks"
        await message.answer(text)
    except Exception as e:
        await message.answer(f"❌ {e}")
