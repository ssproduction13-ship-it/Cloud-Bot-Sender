import asyncio
import logging
import httpx
from datetime import datetime, timezone

from aiogram import Router, F, Bot
from aiogram.types import (
    Message, PhotoSize, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

from config import (
    ADMIN_ID, BETA_DAILY_LIMIT, STATES, STREAK_MILESTONES,
    BOT_TOKEN,
)
from db import (
    upsert_user, get_user, record_usage, get_daily_usage, get_daily_macros,
    update_streak, get_weekly_stats, check_subscription_expired, track_event,
    get_weight_history, add_weight_log,
)
from keyboards import (
    main_keyboard, premium_keyboard, profile_keyboard, progress_inline_keyboard,
    BTN_PHOTO, BTN_MANUAL, BTN_PROGRESS, BTN_SUB, BTN_REF, BTN_PROFILE, BTN_ADMIN,
)
from services.state import user_states, _get_state, _set_state, _try_restore_onboard, _last_scan, SCAN_COOLDOWN_SEC
from services.ai_service import (
    analyze_food_photo, analyze_food_text, _validate_analysis, openai_client,
)
from utils.formatting import detect_fun_reaction

log = logging.getLogger(__name__)
router = Router()

_utcnow = lambda: datetime.now(timezone.utc).replace(tzinfo=None)


def access_check(user_row) -> tuple[bool, str]:
    if user_row is None:
        return False, "not_registered"
    s = user_row["status"]
    if s == "blocked":
        return False, "blocked"
    if s == "pending":
        return False, "pending"
    if s == "paid":
        if check_subscription_expired(user_row):
            return False, "sub_expired"
        return True, "paid"
    if s == "beta":
        from db import is_trial_expired
        if is_trial_expired(user_row):
            return False, "trial_expired"
        return True, "beta"
    return False, "unknown"


async def deny(message: Message, reason: str):
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⭐ Оформить подписку", callback_data="buy_sub:30")
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


def daily_progress_text(uid: int, user: dict | None = None, macros: dict | None = None) -> str:
    if macros is None:
        macros = get_daily_macros(uid)
    if user is None:
        user = get_user(uid)
    total        = macros["kcal"]
    goal         = user["daily_goal"] if user else None
    streak       = user.get("streak_days", 0) if user else 0
    streak_line  = (
        f"\n🔥 Серия: *{streak} {'день' if streak == 1 else 'дней'}*"
        if streak > 0 else ""
    )
    if not goal:
        return f"\n\n📊 *Сегодня: {total} ккал*{streak_line}"
    remaining   = max(goal - total, 0)
    over        = total - goal
    status_line = f"⚡ +{over} ккал сверх нормы" if over > 0 else f"Осталось {remaining} ккал"
    return (
        f"\n\n📊 *Сегодня: {total} / {goal} ккал*\n"
        f"{status_line}"
        f"{streak_line}"
    )


async def _deliver_analysis(
    message: Message,
    uid: int,
    user: dict,
    display: str,
    kcal, protein, fat, carbs,
    food_name: str | None,
    thinking_msg,
):
    ok, err_msg = _validate_analysis(display, kcal, protein, fat, carbs)
    if not ok:
        try:
            await thinking_msg.delete()
        except Exception:
            pass
        await message.answer(err_msg)
        return

    # P6: capture previous protein best before recording
    from db import get_user_best_daily_protein_excl_today, get_user_scan_count
    prev_protein_best = get_user_best_daily_protein_excl_today(uid)

    entry_id = record_usage(uid, kcal, protein, fat, carbs, food_name)
    daily_count = get_daily_usage(uid)
    if daily_count == 1:
        track_event(uid, "first_food_scan")
    elif daily_count == 2:
        track_event(uid, "second_food_scan")

    # P7: daily active user event
    track_event(uid, "daily_active_user")

    streak, milestone = update_streak(uid, user=user) if kcal else (0, False)
    if streak > 0:
        track_event(uid, "streak_updated", {"streak": streak, "milestone": milestone})

    macros     = get_daily_macros(uid)
    fresh_user = get_user(uid)
    progress   = daily_progress_text(uid, user=fresh_user, macros=macros)
    hint       = "\n\n_Установи норму в ⚙️ Профиль_" if not user.get("daily_goal") else ""

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
            goal         = user.get("daily_goal")
            goal_protein = user.get("protein_goal")
            macros_now   = get_daily_macros(uid)
            weekly       = get_weekly_stats(uid)
            patterns     = []
            logged       = weekly.get("logged_days", 0)
            if logged >= 3:
                avg_p = weekly.get("avg_protein", 0)
                avg_k = weekly.get("avg_kcal", 0)
                if goal_protein and avg_p >= goal_protein * 0.85:
                    patterns.append(f"уже {logged} дня подряд держит белок в норме")
                elif goal_protein and avg_p < goal_protein * 0.55:
                    patterns.append(f"регулярно не добирает белок (avg {avg_p}г)")
                if goal and avg_k > goal * 1.15:
                    patterns.append(f"среднее за неделю выше нормы ({avg_k} ккал/д)")
            context_line = "Паттерн: " + "; ".join(patterns) + ".\n" if patterns else ""
            advice_prompt = (
                f"{context_line}"
                f"Только что съел: {food_name or 'блюдо'} ({kcal} ккал, Б{protein}г Ж{fat}г У{carbs}г).\n"
                f"Итог дня: {macros_now['kcal']} ккал"
                + (f" из {goal}" if goal else "")
                + f", белок {macros_now['protein']}г"
                + (f" из {goal_protein}г" if goal_protein else "") + ".\n"
                "Дай ОДИН короткий совет что съесть следующим приёмом для баланса. "
                "Говори как живой тренер: коротко, конкретно, по-человечески. 1-2 предложения, один эмодзи."
            )
            advice_resp = await openai_client.chat.completions.create(
                model="meta-llama/llama-4-scout-17b-16e-instruct",
                messages=[
                    {"role": "system", "content": "Ты — дружелюбный AI-компаньон по питанию. Короткие живые советы. По-русски."},
                    {"role": "user",   "content": advice_prompt},
                ],
                max_tokens=120,
            )
            advice_text = advice_resp.choices[0].message.content or ""
            if advice_text.strip():
                await message.answer(f"💡 _{advice_text.strip()}_", parse_mode="Markdown")
        except Exception as adv_e:
            log.debug(f"ai advice error: {adv_e}")

    # P6: Streak milestone message
    if milestone and streak in STREAK_MILESTONES:
        await message.answer(
            f"🎉 *{STREAK_MILESTONES[streak]}*\n\nСерия {streak} дней — это уже характер.",
            parse_mode="Markdown",
        )

    # P6: Scan count milestones
    total_scans = get_user_scan_count(uid)
    if total_scans in (10, 25, 50, 100):
        await message.answer(
            f"📸 *Уже {total_scans} анализов еды!*\n\nОтличная привычка — продолжай! 🔥",
            parse_mode="Markdown",
        )

    # P6: New daily protein record
    if protein and protein > 0 and macros["protein"] > prev_protein_best and macros["protein"] >= 80:
        await message.answer(
            f"🥩 *Рекорд по белку за день: {round(macros['protein'])}г!*\n\n"
            f"Лучший результат — так держать! 💪",
            parse_mode="Markdown",
        )

    # Share button
    if kcal and food_name:
        import urllib.parse as _urlp
        from config import BOT_USERNAME
        prot_str = f" · Б{round(protein)}г" if protein else ""
        fat_str  = f" · Ж{round(fat)}г"    if fat    else ""
        carb_str = f" · У{round(carbs)}г"  if carbs  else ""
        share_text = (
            f"🥗 Засканировал {food_name} в NutriAI\n"
            f"{kcal} ккал{prot_str}{fat_str}{carb_str}\n\n"
            f"Трекай питание с AI 👉 @{BOT_USERNAME or 'NutriAI'}"
        )
        bot_link = f"https://t.me/{BOT_USERNAME}" if BOT_USERNAME else "https://t.me/share"
        share_url = f"https://t.me/share/url?url={_urlp.quote(bot_link, safe='')}&text={_urlp.quote(share_text, safe='')}"
        await message.answer(
            "📤 _Поделись с друзьями — возможно, им тоже понравится_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="📤 Поделиться", url=share_url),
            ]]),
        )


@router.callback_query(F.data == "food_photo_mode")
async def cb_food_photo_mode(callback: CallbackQuery):
    uid  = callback.from_user.id
    user = get_user(uid)
    ok, reason = access_check(user)
    await callback.answer()
    if not ok:
        await deny(callback.message, reason)
        return
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(
        "📸 *Отправь фото блюда* — посчитаю КБЖУ.\n\n"
        "_Держи телефон в 10–15 см от еды для лучшего результата_",
        parse_mode="Markdown",
    )


@router.callback_query(F.data == "food_text_mode")
async def cb_food_text_mode(callback: CallbackQuery):
    uid  = callback.from_user.id
    user = get_user(uid)
    ok, reason = access_check(user)
    await callback.answer()
    if not ok:
        await deny(callback.message, reason)
        return
    _set_state(uid, STATES["MANUAL_ENTRY"])
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(
        "✍️ *Что съел?*\n\n"
        "_Напиши название блюда (например: «гречка 200г с курицей»)_\n\n"
        "/cancel — отмена",
        parse_mode="Markdown",
    )


@router.callback_query(F.data == "food_kcal_mode")
async def cb_food_kcal_mode(callback: CallbackQuery):
    uid  = callback.from_user.id
    user = get_user(uid)
    ok, reason = access_check(user)
    await callback.answer()
    if not ok:
        await deny(callback.message, reason)
        return
    _set_state(uid, STATES["MANUAL_ENTRY"])
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(
        "🔢 *Введи количество калорий:*\n\n"
        "_Например: 450_\n\n"
        "/cancel — отмена",
        parse_mode="Markdown",
    )


@router.message(F.photo)
async def handle_photo(message: Message, bot: Bot):
    uid  = message.from_user.id
    upsert_user(uid, message.from_user.username or "", message.from_user.first_name or "")
    user = get_user(uid)

    ok, reason = access_check(user)
    if not ok:
        await deny(message, reason)
        return

    now  = _utcnow()
    last = _last_scan.get(uid)
    if last and (now - last).total_seconds() < SCAN_COOLDOWN_SEC:
        await message.answer("Подожди пару секунд перед следующим сканированием.")
        return
    _last_scan[uid] = now

    if user["status"] == "beta":
        used = get_daily_usage(uid)
        if used >= BETA_DAILY_LIMIT:
            await message.answer(
                f"📊 *Лимит {BETA_DAILY_LIMIT} анализов в день*\n\nОформи подписку — безлимит 🚀",
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
        file  = await bot.get_file(photo.file_id)
        url   = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file.file_path}"
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


@router.message(F.text)
async def handle_text(message: Message, bot: Bot):
    uid  = message.from_user.id
    upsert_user(uid, message.from_user.username or "", message.from_user.first_name or "")
    user = get_user(uid)
    text = (message.text or "").strip()

    _try_restore_onboard(uid)
    state_data = _get_state(uid)
    state      = state_data.get("state")

    # ── Onboarding text inputs ───────────────────────────────────────────
    if state == "ob_age":
        try:
            age = int(text)
            if not (10 <= age <= 100):
                raise ValueError
        except ValueError:
            await message.answer("⚠️ Введи возраст числом (например: 25):")
            return
        ob_data = state_data.get("data", {})
        ob_data["age"] = age
        await message.answer("📏 *Твой рост в сантиметрах?*\n\n_Например: 175_",
                             parse_mode="Markdown")
        from services.state import _set_onboard_state
        _set_onboard_state(uid, "ob_height", ob_data)
        return

    if state == "ob_height":
        try:
            height = float(text.replace(",", "."))
            if not (100 <= height <= 250):
                raise ValueError
        except ValueError:
            await message.answer("⚠️ Введи рост числом в сантиметрах (например: 175):")
            return
        ob_data = state_data.get("data", {})
        ob_data["height"] = height
        await message.answer("⚖️ *Твой вес в килограммах?*\n\n_Например: 70 или 70.5_",
                             parse_mode="Markdown")
        from services.state import _set_onboard_state
        _set_onboard_state(uid, "ob_weight", ob_data)
        return

    if state == "ob_weight":
        try:
            weight = float(text.replace(",", "."))
            if not (20 <= weight <= 400):
                raise ValueError
        except ValueError:
            await message.answer("⚠️ Введи вес числом в кг (например: 70):")
            return
        ob_data = state_data.get("data", {})
        ob_data["weight"] = weight
        await message.answer(
            "⚡️ *Уровень физической активности?*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🛋 Сидячий (офис, мало движения)",   callback_data="ob_activity:sedentary")],
                [InlineKeyboardButton(text="🚶 Лёгкая (1-2 тренировки/нед)",     callback_data="ob_activity:light")],
                [InlineKeyboardButton(text="🏃 Средняя (3-5 тренировок/нед)",    callback_data="ob_activity:moderate")],
                [InlineKeyboardButton(text="💪 Высокая (6-7 тренировок/нед)",    callback_data="ob_activity:active")],
                [InlineKeyboardButton(text="🔥 Очень высокая (физ. работа / 2x)", callback_data="ob_activity:very_active")],
            ]),
        )
        from services.state import _set_onboard_state
        _set_onboard_state(uid, "ob_activity", ob_data)
        return

    # ── Admin broadcast ──────────────────────────────────────────────────
    if state == STATES["ADMIN_BROADCAST"]:
        seg_data       = state_data.get("data", {})
        segment        = seg_data.get("segment", "all_active")
        segment_label  = seg_data.get("segment_label", "Все активные")
        from db import get_users_by_segment
        users_list     = get_users_by_segment(segment)
        sent = failed  = 0
        for u in users_list:
            try:
                await bot.send_message(u["telegram_id"], text, parse_mode="Markdown")
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

    # ── Admin give days ──────────────────────────────────────────────────
    if state == STATES["ADMIN_GIVE_DAYS"]:
        try:
            days = int(text)
        except ValueError:
            await message.answer("⚠️ Введи число дней (например: 30):")
            return
        target_id = state_data["data"].get("target_id")
        if target_id:
            from db import activate_subscription
            activate_subscription(target_id, days)
        user_states.pop(uid, None)
        await message.answer(
            f"✅ *+{days} дней* выдано пользователю `{target_id}`.",
            parse_mode="Markdown",
            reply_markup=main_keyboard(uid == ADMIN_ID),
        )
        return

    # ── Correct entry ────────────────────────────────────────────────────
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
            from db import update_entry_calories
            update_entry_calories(entry_id, new_kcal)
        user_states.pop(uid, None)
        progress = daily_progress_text(uid)
        await message.answer(
            f"✅ *Исправлено: {new_kcal} ккал*{progress}",
            parse_mode="Markdown",
            reply_markup=main_keyboard(uid == ADMIN_ID),
        )
        return

    # ── Weight log ───────────────────────────────────────────────────────
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
        history  = get_weight_history(uid, days=30)
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

    # ── Manual food entry ────────────────────────────────────────────────
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

        # Direct calorie input: just a number
        try:
            kcal_direct = int(text.strip())
            if 50 <= kcal_direct <= 9999:
                record_usage(uid, kcal_direct, None, None, None, f"запись {kcal_direct} ккал")
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
            pass

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

    # ── Menu buttons ─────────────────────────────────────────────────────
    if text == BTN_PHOTO:
        ok, reason = access_check(user)
        if not ok:
            await deny(message, reason)
            return
        await message.answer(
            "Как хочешь добавить?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📸 Фото блюда",        callback_data="food_photo_mode")],
                [InlineKeyboardButton(text="✍️ Описать словами",   callback_data="food_text_mode")],
                [InlineKeyboardButton(text="🔢 Только калории",    callback_data="food_kcal_mode")],
            ]),
        )
        return

    if text == BTN_MANUAL:
        ok, reason = access_check(user)
        if not ok:
            await deny(message, reason)
            return
        _set_state(uid, STATES["MANUAL_ENTRY"])
        await message.answer(
            "✍️ *Что съел?*\n\n"
            "_Напиши название блюда (например: «гречка 200г с курицей»)_\n\n"
            "/cancel — отмена",
            parse_mode="Markdown",
        )
        return

    if text == BTN_PROGRESS:
        ok, reason = access_check(user)
        if not ok:
            await deny(message, reason)
            return
        macros   = get_daily_macros(uid)
        progress = daily_progress_text(uid, user=user, macros=macros)
        await message.answer(
            progress.strip(),
            parse_mode="Markdown",
            reply_markup=progress_inline_keyboard(),
        )
        return

    if text == BTN_SUB:
        from handlers.premium import _show_premium_screen
        track_event(uid, "premium_clicked")
        await _show_premium_screen(message.answer, uid, user)
        return

    if text == BTN_REF:
        from handlers.referrals import _show_referral
        track_event(uid, "referral_opened")
        await _show_referral(message.answer, uid)
        return

    if text == BTN_PROFILE:
        if not user:
            await message.answer("Напиши /start для регистрации.")
            return
        from handlers.profile import _send_status
        await _send_status(
            message.answer, uid, user,
            reply_markup=profile_keyboard(uid, has_goal=bool(user.get("daily_goal"))),
        )
        return

    if text == BTN_ADMIN and uid == ADMIN_ID:
        from handlers.admin import show_admin_panel
        await show_admin_panel(message.answer)
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
            "• нажать *📸 Анализ еды* и выбрать способ\n"
            "• или написать что съел (например: «гречка 200г»)\n"
            "• или ввести только число калорий (например: 450)",
            parse_mode="Markdown",
            reply_markup=main_keyboard(uid == ADMIN_ID),
        )
