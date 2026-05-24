import os
import psycopg2
import psycopg2.extras
from datetime import datetime, timedelta

DATABASE_URL = os.environ["DATABASE_URL"]


def get_conn():
    conn = psycopg2.connect(DATABASE_URL)
    return conn


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id      BIGINT PRIMARY KEY,
                    username         TEXT,
                    first_name       TEXT,
                    status           TEXT DEFAULT 'pending',
                    subscribed_at    TEXT,
                    expires_at       TEXT,
                    created_at       TEXT DEFAULT (NOW()::TEXT),
                    referred_by      BIGINT,
                    daily_goal       INTEGER,
                    trial_expires_at TEXT
                );

                CREATE TABLE IF NOT EXISTS usage (
                    id          SERIAL PRIMARY KEY,
                    telegram_id BIGINT,
                    used_at     TEXT DEFAULT (NOW()::TEXT),
                    date        TEXT DEFAULT (CURRENT_DATE::TEXT),
                    calories    INTEGER
                );

                CREATE TABLE IF NOT EXISTS referrals (
                    id          SERIAL PRIMARY KEY,
                    referrer_id BIGINT,
                    referred_id BIGINT UNIQUE,
                    paid        INTEGER DEFAULT 0,
                    bonus_given INTEGER DEFAULT 0,
                    created_at  TEXT DEFAULT (NOW()::TEXT)
                );
            """)
        conn.commit()


def upsert_user(telegram_id, username, first_name, referred_by=None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (telegram_id, username, first_name, referred_by)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (telegram_id) DO UPDATE
                    SET username = EXCLUDED.username,
                        first_name = EXCLUDED.first_name
                """,
                (telegram_id, username, first_name, referred_by),
            )
        conn.commit()


def get_user(telegram_id):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE telegram_id=%s", (telegram_id,))
            row = cur.fetchone()
            return dict(row) if row else None


def set_status(telegram_id, status):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET status=%s WHERE telegram_id=%s",
                (status, telegram_id),
            )
        conn.commit()


def approve_user(telegram_id, trial_days=3):
    expires = (datetime.utcnow() + timedelta(days=trial_days)).isoformat()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET status='beta', trial_expires_at=%s WHERE telegram_id=%s",
                (expires, telegram_id),
            )
        conn.commit()


def is_trial_expired(telegram_id):
    user = get_user(telegram_id)
    if not user or not user["trial_expires_at"]:
        return True
    return datetime.utcnow() > datetime.fromisoformat(user["trial_expires_at"])


def set_daily_goal(telegram_id, goal):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET daily_goal=%s WHERE telegram_id=%s",
                (goal, telegram_id),
            )
        conn.commit()


def activate_subscription(telegram_id, days):
    user = get_user(telegram_id)
    now = datetime.utcnow()
    if user and user["expires_at"]:
        try:
            current_exp = datetime.fromisoformat(user["expires_at"])
            base = max(current_exp, now)
        except Exception:
            base = now
    else:
        base = now
    new_exp = (base + timedelta(days=days)).isoformat()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET status='paid', expires_at=%s, subscribed_at=%s WHERE telegram_id=%s",
                (new_exp, now.isoformat(), telegram_id),
            )
        conn.commit()


def check_subscription_expired(telegram_id):
    user = get_user(telegram_id)
    if not user or not user["expires_at"]:
        return True
    return datetime.utcnow() > datetime.fromisoformat(user["expires_at"])


def get_all_users(status_filter=None):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if status_filter:
                cur.execute(
                    "SELECT * FROM users WHERE status=%s ORDER BY created_at DESC",
                    (status_filter,),
                )
            else:
                cur.execute("SELECT * FROM users ORDER BY created_at DESC")
            return [dict(r) for r in cur.fetchall()]


def record_usage(telegram_id, kcal=None) -> int:
    now = datetime.utcnow()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO usage (telegram_id, used_at, date, calories) VALUES (%s, %s, %s, %s) RETURNING id",
                (telegram_id, now.isoformat(), now.strftime("%Y-%m-%d"), kcal),
            )
            entry_id = cur.fetchone()[0]
        conn.commit()
    return entry_id


def update_entry_calories(entry_id: int, new_kcal: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE usage SET calories=%s WHERE id=%s",
                (new_kcal, entry_id),
            )
        conn.commit()


def get_daily_usage(telegram_id):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM usage WHERE telegram_id=%s AND date=%s",
                (telegram_id, today),
            )
            return cur.fetchone()[0]


def get_daily_calories(telegram_id):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(calories),0) FROM usage WHERE telegram_id=%s AND date=%s",
                (telegram_id, today),
            )
            return int(cur.fetchone()[0])


def get_total_stats():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM users")
            total = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM users WHERE status='pending'")
            pending = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM users WHERE status='beta'")
            beta = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM users WHERE status='paid'")
            paid = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM users WHERE status='blocked'")
            blocked = cur.fetchone()[0]
            today_str = datetime.utcnow().strftime("%Y-%m-%d")
            cur.execute("SELECT COUNT(*) FROM usage WHERE date=%s", (today_str,))
            today_a = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM usage")
            total_a = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM referrals WHERE paid=1")
            ref_paid = cur.fetchone()[0]
            return {
                "total_users": total,
                "pending": pending,
                "beta": beta,
                "paid": paid,
                "blocked": blocked,
                "analyses_today": today_a,
                "analyses_total": total_a,
                "referrals_paid": ref_paid,
            }


def register_referral(referrer_id, referee_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO referrals (referrer_id, referred_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (referrer_id, referee_id),
            )
        conn.commit()


def mark_referral_paid(referee_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT referrer_id FROM referrals WHERE referred_id=%s AND paid=0",
                (referee_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            referrer_id = row[0]
            cur.execute(
                "UPDATE referrals SET paid=1, bonus_given=1 WHERE referred_id=%s",
                (referee_id,),
            )
        conn.commit()
        return referrer_id


def get_referral_stats(telegram_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM referrals WHERE referrer_id=%s", (telegram_id,)
            )
            total = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM referrals WHERE referrer_id=%s AND paid=1",
                (telegram_id,),
            )
            paid = cur.fetchone()[0]
            return {"total": total, "paid": paid}
