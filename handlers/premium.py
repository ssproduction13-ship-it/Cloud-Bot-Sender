import logging
from datetime import datetime

from aiogram import Router, F, Bot
from aiogram.types import (
    Message, CallbackQuery, PreCheckoutQuery,
    LabeledPrice,
)

from config import (
    ADMIN_ID, SUB_PRICE_STARS, SUB_DAYS, SUB_DAYS_3M, SUB_DAYS_12M,
    SUB_PRICE_3M, SUB_PRICE_12M,
)
from db import get_user, activate_subscription, track_event, mark_referral_paid
from keyboards import main_keyboard, premium_keyboard

log = logging.getLogger(__name__)
router = Router()


async def _show_premium_screen(send_fn, uid: int, user: dict | None):
    await send_fn(
        "⭐ *NutriAI Premium*\n\n"
        "Всё что нужно для реального результата:\n\n"
        "📸 Безлимит AI-сканирований\n"
        "💡 Персональный совет после каждого приёма пищи\n"
        "📊 Недельные AI-отчёты о питании\n"
        "🥩 Детальная аналитика КБЖУ\n"
        "🔥 Расширенная статистика стриков\n"
        "⚡ Приоритетная обработка запросов\n\n"
        "_Бесплатно: 5 сканирований в день, базовый трекинг_\n\n"
        "Выбери тариф 👇",
        parse_mode="Markdown",
        reply_markup=premium_keyboard(),
    )


@router.callback_query(F.data == "show_premium")
async def cb_show_premium(callback: CallbackQuery):
    uid = callback.from_user.id
    await callback.answer()
    track_event(uid, "premium_clicked")
    user = get_user(uid)
    await _show_premium_screen(callback.message.answer, uid, user)


@router.callback_query(F.data.startswith("buy_sub"))
async def cb_buy_sub(callback: CallbackQuery, bot: Bot):
    uid = callback.from_user.id
    await callback.answer()
    track_event(uid, "premium_initiated", {"plan": callback.data})
    parts = callback.data.split(":")
    plan  = parts[1] if len(parts) > 1 else "30"
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


@router.pre_checkout_query()
async def handle_pre_checkout(pre_checkout: PreCheckoutQuery, bot: Bot):
    await bot.answer_pre_checkout_query(pre_checkout.id, ok=True)


@router.message(F.successful_payment)
async def handle_successful_payment(message: Message):
    uid     = message.from_user.id
    payload = message.successful_payment.invoice_payload
    try:
        parts = payload.split("_")
        days  = int(parts[1].rstrip("d"))
    except Exception:
        days = SUB_DAYS

    activate_subscription(uid, days)
    track_event(uid, "premium_purchased", {"days": days, "payload": payload})

    # Give referral bonus to referrer if applicable
    user = get_user(uid)
    if user and user.get("referred_by"):
        try:
            from config import REFERRAL_BONUS_DAYS
            referrer_id = user["referred_by"]
            activate_subscription(referrer_id, REFERRAL_BONUS_DAYS)
            mark_referral_paid(referrer_id, uid)
            ref_user = get_user(referrer_id)
            new_exp  = (
                datetime.fromisoformat(ref_user["expires_at"]).strftime("%d.%m.%Y")
                if ref_user and ref_user.get("expires_at") else "—"
            )
            await bot.send_message(
                referrer_id,
                f"🎉 Твой реферал оформил подписку!\n"
                f"*+{REFERRAL_BONUS_DAYS} дней* начислено → до *{new_exp}*",
                parse_mode="Markdown",
            )
        except Exception as e:
            log.warning(f"referral bonus: {e}")

    await message.answer(
        f"🎉 *Premium активирован на {days} дней!*\n\n"
        f"Теперь — безлимит анализов, AI-советы и недельные отчёты 🚀",
        parse_mode="Markdown",
        reply_markup=main_keyboard(uid == ADMIN_ID),
    )
