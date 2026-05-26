"""utils package — shared formatting, scoring, and helper utilities.
Extracted from main.py as part of P1 module refactoring.
"""
from utils.formatting import (
    calc_daily_score,
    format_score,
    score_emoji,
    ai_score_comment,
    detect_fun_reaction,
    CHEAT_KEYWORDS,
    SUGAR_KEYWORDS,
)
from utils.helpers import (
    user_label,
    ref_link,
    streak_emoji,
    progress_bar,
)

__all__ = [
    "calc_daily_score", "format_score", "score_emoji",
    "ai_score_comment", "detect_fun_reaction",
    "CHEAT_KEYWORDS", "SUGAR_KEYWORDS",
    "user_label", "ref_link", "streak_emoji", "progress_bar",
]
