import os

BOT_TOKEN    = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_ID     = int(os.environ["TELEGRAM_CHAT_ID"])
BOT_USERNAME = os.environ.get("BOT_USERNAME", "")

BETA_DAILY_LIMIT       = 5
SUB_PRICE_STARS        = 150
SUB_DAYS               = 30
REFERRAL_BONUS_DAYS    = 7
REFERRAL_JOIN_BONUS_DAYS = 3
SUB_PRICE_3M           = 360
SUB_PRICE_12M          = 990
SUB_DAYS_3M            = 90
SUB_DAYS_12M           = 365

STREAK_MILESTONES = {
    3:   "🥉 3 дня подряд",
    7:   "🥈 7 дней подряд",
    14:  "🥇 14 дней подряд",
    30:  "🏆 30 дней подряд!",
    60:  "👑 60 дней подряд!",
    100: "🌟 100 дней!!",
}

STATES = {
    "MANUAL_ENTRY":    "manual_entry",
    "CORRECT_ENTRY":   "correct_entry",
    "WEIGHT_LOG":      "weight_log",
    "ADMIN_GIVE_DAYS": "admin_give_days",
    "ADMIN_BROADCAST": "admin_broadcast",
}

ONBOARD_STATES: set = {
    "ob_goal", "ob_gender", "ob_age", "ob_height", "ob_weight", "ob_activity"
}

STATE_TTL_SECONDS = 3600
