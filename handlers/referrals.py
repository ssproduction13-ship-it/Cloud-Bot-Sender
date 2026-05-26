from aiogram import Router, F
from aiogram.types import CallbackQuery

from config import BOT_USERNAME, REFERRAL_JOIN_BONUS_DAYS, REFERRAL_BONUS_DAYS
from db import get_referral_stats, track_event
from utils.helpers import ref_link

router = Router()


async def _show_referral(send_fn, uid: int):
    stats = get_referral_stats(uid)
    link  = ref_link(uid)
    await send_fn(
        f"🎁 *Реферальная программа*\n\n"
        f"Приглашай друзей — получай бонусы:\n"
        f"• *+{REFERRAL_JOIN_BONUS_DAYS} дня* когда друг зарегистрируется\n"
        f"• *+{REFERRAL_BONUS_DAYS} дней* когда оформит подписку\n\n"
        f"Твоя ссылка:\n`{link}`\n\n"
        f"👥 Приглашено: *{stats['total']}*  |  Оплатили: *{stats['paid']}*",
        parse_mode="Markdown",
    )


@router.callback_query(F.data == "ref_screen")
async def cb_ref_screen(callback: CallbackQuery):
    uid = callback.from_user.id
    await callback.answer()
    track_event(uid, "referral_opened")
    await _show_referral(callback.message.answer, uid)
