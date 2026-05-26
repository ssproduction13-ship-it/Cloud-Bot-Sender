"""
utils/helpers.py — Small pure helper functions shared across modules.
Extracted from main.py (P1 module refactoring).
"""
import os


def user_label(row: dict) -> str:
    name = row.get("first_name") or ""
    un = f"@{row['username']}" if row.get("username") else f"id{row['telegram_id']}"
    return f"{name} ({un})"


def ref_link(uid: int) -> str:
    bot_un = (os.getenv("BOT_USERNAME") or "").lstrip("@") or "YOUR_BOT"
    return f"https://t.me/{bot_un}?start=ref_{uid}"


def streak_emoji(streak: int) -> str:
    if streak >= 30: return "🏆"
    if streak >= 14: return "🥇"
    if streak >= 7:  return "🥈"
    if streak >= 3:  return "🥉"
    return "🔥"


def progress_bar(current: int, goal: int, length: int = 10) -> str:
    """Return a text progress bar, e.g. '████░░░░░░ 60%'."""
    if not goal or goal <= 0:
        return ""
    ratio = min(current / goal, 1.0)
    filled = round(ratio * length)
    bar = "█" * filled + "░" * (length - filled)
    pct = round(ratio * 100)
    return f"{bar} {pct}%"


