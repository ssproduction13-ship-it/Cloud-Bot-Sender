import os
import json
import psycopg2
import psycopg2.extras
from contextlib import contextmanager
from psycopg2.pool import ThreadedConnectionPool
from datetime import datetime, timedelta, date

DATABASE_URL = os.environ["DATABASE_URL"]

_pool: ThreadedConnectionPool | None = None

def _init_pool():
    global _pool
    if _pool is None:
        _pool = ThreadedConnectionPool(1, 10, DATABASE_URL)

@contextmanager
def get_conn():
    _init_pool()
    conn = _pool.getconn()
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)

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
                    trial_expires_at TEXT,
                    streak_days      INTEGER DEFAULT 0,
                    best_streak      INTEGER DEFAULT 0,
                    last_active_date TEXT,
                    protein_goal     INTEGER,
                    weight_kg        REAL,
                    height_cm        REAL,
                    age              INTEGER,
                    goal_type        TEXT DEFAULT 'track',
                    gender           TEXT,
                    onboarded        INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS usage (
                    id          SERIAL PRIMARY KEY,
                    telegram_id BIGINT,
                    used_at     TEXT DEFAULT (NOW()::TEXT),
                    date        TEXT DEFAULT (CURRENT_DATE::TEXT),
                    calories    INTEGER,
                    protein_g   REAL,
                    fat_g       REAL,
                    carbs_g     REAL,
                    food_name   TEXT
                );

                CREATE TABLE IF NOT EXISTS referrals (
                    id          SERIAL PRIMARY KEY,
                    referrer_id BIGINT,
                    referred_id BIGINT UNIQUE,
                    paid        INTEGER DEFAULT 0,
                    bonus_given INTEGER DEFAULT 0,
                    created_at  TEXT DEFAULT (NOW()::TEXT)
                );

                CREATE TABLE IF NOT EXISTS weight_logs (
                    id          SERIAL PRIMARY KEY,
                    telegram_id BIGINT,
                    weight_kg   REAL,
                    logged_at   TEXT DEFAULT (NOW()::TEXT),
                    date        TEXT DEFAULT (CURRENT_DATE::TEXT)
                );

                CREATE TABLE IF NOT EXISTS onboard_state (
                    telegram_id BIGINT PRIMARY KEY,
                    state       TEXT NOT NULL,
                    data_json   TEXT DEFAULT '{}',
                    updated_at  TEXT DEFAULT (NOW()::TEXT)
                );
            """)

        # Commit table creations BEFORE the ALTER TABLE migration loop.
        # Each ALTER TABLE failure calls conn.rollback(); without this commit,
        # that rollback would also undo the CREATE TABLE statements above
        # (including onboard_state), leaving the table permanently missing.
        conn.commit()

        with conn.cursor() as cur:
            new_user_cols = [
                ("streak_days",      "INTEGER DEFAULT 0"),
                ("best_streak",      "INTEGER DEFAULT 0"),
                ("last_active_date", "TEXT"),
                ("protein_goal",     "INTEGER"),
                ("weight_kg",        "REAL"),
                ("height_cm",        "REAL"),
                ("age",              "INTEGER"),
                ("goal_type",        "TEXT DEFAULT 'track'"),
                ("activity",        "TEXT DEFAULT 'moderate'"),
                ("gender",           "TEXT"),
                ("onboarded",        "INTEGER DEFAULT 0"),
            ]
            for col, definition in new_user_cols:
                cur.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col} {definition}")

            new_usage_cols = [
                ("protein_g", "REAL"),
                ("fat_g",     "REAL"),
                ("carbs_g",   "REAL"),
                ("food_name", "TEXT"),
            ]
            for col, definition in new_usage_cols:
                cur.execute(f"ALTER TABLE usage ADD COLUMN IF NOT EXISTS {col} {definition}")

        conn.commit()

# ── Users ──────────────────────────────────────────────────────────────────

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

def is_trial_expired(user_or_id) -> bool:
    """Accept either a user dict (no extra DB query) or a telegram_id."""
    user = user_or_id if isinstance(user_or_id, dict) else get_user(user_or_id)
    if not user or not user.get("trial_expires_at"):
        return True
    return datetime.utcnow() > datetime.fromisoformat(user["trial_expires_at"])

def set_daily_goal(telegram_id, goal, protein_goal=None, goal_type=None,
                   weight_kg=None, height_cm=None, age=None, gender=None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            fields = ["daily_goal=%s"]
            vals = [goal]
            if protein_goal is not None:
                fields.append("protein_goal=%s"); vals.append(protein_goal)
            if goal_type is not None:
                fields.append("goal_type=%s"); vals.append(goal_type)
            if weight_kg is not None:
                fields.append("weight_kg=%s"); vals.append(weight_kg)
            if height_cm is not None:
                fields.append("height_cm=%s"); vals.append(height_cm)
            if age is not None:
                fields.append("age=%s"); vals.append(age)
            if gender is not None:
                fields.append("gender=%s"); vals.append(gender)
            vals.append(telegram_id)
            cur.execute(
                f"UPDATE users SET {', '.join(fields)} WHERE telegram_id=%s", vals
            )
        conn.commit()

def clear_all_goals():
    """Set daily_goal, protein_goal, goal_type, weight_kg, height_cm, age, gender to NULL for every user."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE users
                SET daily_goal=NULL, protein_goal=NULL, goal_type=NULL,
                    weight_kg=NULL, height_cm=NULL, age=NULL, gender=NULL
            """)
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM users")
            return cur.fetchone()[0]

def mark_onboarded(telegram_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET onboarded=1 WHERE telegram_id=%s", (telegram_id,))
        conn.commit()

def set_user_goals(telegram_id, *, daily_goal, protein_goal, weight_kg,
                   height_cm, age, gender, goal_type, activity="moderate"):
    """Save calculated onboarding goals to the users table."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE users
                   SET daily_goal=%s, protein_goal=%s, weight_kg=%s,
                       height_cm=%s, age=%s, gender=%s, goal_type=%s, activity=%s
                   WHERE telegram_id=%s""",
                (daily_goal, protein_goal, weight_kg,
                 height_cm, age, gender, goal_type, activity, telegram_id),
            )
        conn.commit()

# ── Onboarding persistent state ─────────────────────────────────────────────

def save_onboard_state(telegram_id: int, state: str, data: dict) -> None:
    """Persist onboarding FSM state so it survives bot restarts."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO onboard_state (telegram_id, state, data_json, updated_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (telegram_id) DO UPDATE
                    SET state      = EXCLUDED.state,
                        data_json  = EXCLUDED.data_json,
                        updated_at = EXCLUDED.updated_at
                """,
                (telegram_id, state, json.dumps(data, default=str),
                 datetime.utcnow().isoformat()),
            )
        conn.commit()

def load_onboard_state(telegram_id: int) -> dict | None:
    """Return persisted onboarding state or None if absent / expired (>24 h)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT state, data_json, updated_at FROM onboard_state WHERE telegram_id=%s",
                (telegram_id,),
            )
            row = cur.fetchone()
    if not row:
        return None
    state, data_json, updated_at = row
    try:
        ts = datetime.fromisoformat(updated_at)
        if (datetime.utcnow() - ts).total_seconds() > 86400:
            return None
    except Exception:
        pass
    try:
        data = json.loads(data_json) if data_json else {}
    except Exception:
        data = {}
    return {"state": state, "data": data}

def clear_onboard_state(telegram_id: int) -> None:
    """Remove persisted onboarding state after completion or cancellation."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM onboard_state WHERE telegram_id=%s", (telegram_id,))
        conn.commit()

def activate_subscription(telegram_id, days):
    user = get_user(telegram_id)
    now = datetime.utcnow()
    if user and user.get("expires_at"):
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

def check_subscription_expired(user_or_id) -> bool:
    """Accept either a user dict (no extra DB query) or a telegram_id."""
    user = user_or_id if isinstance(user_or_id, dict) else get_user(user_or_id)
    if not user or not user.get("expires_at"):
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

def get_active_users():
    week_ago = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT DISTINCT u.* FROM users u
                JOIN usage g ON g.telegram_id = u.telegram_id
                WHERE u.status IN ('beta', 'paid')
                  AND g.date >= %s
                """,
                (week_ago,),
            )
            return [dict(r) for r in cur.fetchall()]

# ── Streak ─────────────────────────────────────────────────────────────────

def update_streak(telegram_id, user=None) -> tuple[int, bool]:
    """Pass pre-fetched user dict to avoid an extra DB round-trip."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    yesterday = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")

    if user is None:
        user = get_user(telegram_id)
    if not user:
        return 0, False

    last = user.get("last_active_date")
    streak = user.get("streak_days") or 0
    best = user.get("best_streak") or 0

    if last == today:
        return streak, False

    if last == yesterday:
        streak += 1
    else:
        streak = 1

    new_best = max(best, streak)
    milestone = streak in (3, 7, 14, 30, 60, 100)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE users
                   SET streak_days=%s, best_streak=%s, last_active_date=%s
                   WHERE telegram_id=%s""",
                (streak, new_best, today, telegram_id),
            )
        conn.commit()

    return streak, milestone

# ── Usage & Macros ─────────────────────────────────────────────────────────

def record_usage(telegram_id, kcal=None, protein=None, fat=None, carbs=None,
                 food_name=None) -> int:
    now = datetime.utcnow()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO usage
                   (telegram_id, used_at, date, calories, protein_g, fat_g, carbs_g, food_name)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (telegram_id, now.isoformat(), now.strftime("%Y-%m-%d"),
                 kcal, protein, fat, carbs, food_name),
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

def get_daily_macros(telegram_id):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT COALESCE(SUM(calories),0),
                          COALESCE(SUM(protein_g),0),
                          COALESCE(SUM(fat_g),0),
                          COALESCE(SUM(carbs_g),0)
                   FROM usage WHERE telegram_id=%s AND date=%s""",
                (telegram_id, today),
            )
            row = cur.fetchone()
            return {
                "kcal": int(row[0]),
                "protein": round(row[1]),
                "fat": round(row[2]),
                "carbs": round(row[3]),
            }

def get_entries_today(telegram_id):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """SELECT id, food_name, calories, protein_g, fat_g, carbs_g, used_at
                   FROM usage WHERE telegram_id=%s AND date=%s
                   ORDER BY used_at ASC""",
                (telegram_id, today),
            )
            return [dict(r) for r in cur.fetchall()]

def delete_entry(entry_id: int):
    """Delete a single usage entry by id."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM usage WHERE id=%s", (entry_id,))
        conn.commit()

def reset_today_entries(telegram_id: int):
    """Delete all usage entries for today for a given user."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM usage WHERE telegram_id=%s AND date=%s",
                (telegram_id, today),
            )
        conn.commit()

def get_calories_for_date(telegram_id, date_str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(calories),0) FROM usage WHERE telegram_id=%s AND date=%s",
                (telegram_id, date_str),
            )
            return int(cur.fetchone()[0])

# ── Weekly stats ───────────────────────────────────────────────────────────

def get_weekly_stats(telegram_id):
    today = datetime.utcnow().date()
    days = [(today - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT date,
                          COALESCE(SUM(calories),0) AS kcal,
                          COALESCE(SUM(protein_g),0) AS protein
                   FROM usage
                   WHERE telegram_id=%s AND date >= %s
                   GROUP BY date""",
                (telegram_id, days[0]),
            )
            rows = {r[0]: {"kcal": int(r[1]), "protein": round(r[2])} for r in cur.fetchall()}

    daily = [rows.get(d, {"kcal": 0, "protein": 0}) for d in days]
    days_with_data = [d for d in daily if d["kcal"] > 0]
    logged_days = len(days_with_data)

    avg_kcal = round(sum(d["kcal"] for d in days_with_data) / logged_days) if logged_days else 0
    avg_protein = round(sum(d["protein"] for d in days_with_data) / logged_days) if logged_days else 0
    best_day_kcal = max((d["kcal"] for d in daily), default=0)
    consistency = round(logged_days / 7 * 100)

    return {
        "logged_days": logged_days,
        "avg_kcal": avg_kcal,
        "avg_protein": avg_protein,
        "best_day_kcal": best_day_kcal,
        "consistency": consistency,
        "daily": daily,
        "dates": days,
    }

# ── Weight logs ────────────────────────────────────────────────────────────

def add_weight_log(telegram_id, weight_kg):
    now = datetime.utcnow()
    today = now.strftime("%Y-%m-%d")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO weight_logs (telegram_id, weight_kg, logged_at, date)"
                " VALUES (%s, %s, %s, %s)",
                (telegram_id, weight_kg, now.isoformat(), today),
            )
            cur.execute(
                "UPDATE users SET weight_kg=%s WHERE telegram_id=%s",
                (weight_kg, telegram_id),
            )
        conn.commit()

def get_weight_history(telegram_id, days=14):
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT date, weight_kg FROM weight_logs
                   WHERE telegram_id=%s AND date >= %s
                   ORDER BY date ASC""",
                (telegram_id, cutoff),
            )
            return cur.fetchall()

# ── Global stats ────────────────────────────────────────────────────────────

def get_total_stats():
    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    yesterday_str = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
    week_ago = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
    day7_str = yesterday_str  # D7: registered 7 days ago
    day7_str = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")

    with get_conn() as conn:
        with conn.cursor() as cur:
            # All user status counts in a single query
            cur.execute("""
                SELECT
                    COUNT(*)                                          AS total,
                    COUNT(*) FILTER (WHERE status='pending')         AS pending,
                    COUNT(*) FILTER (WHERE status='beta')            AS beta,
                    COUNT(*) FILTER (WHERE status='paid')            AS paid,
                    COUNT(*) FILTER (WHERE status='blocked')         AS blocked,
                    COUNT(*) FILTER (WHERE DATE(created_at) = %s)   AS new_today
                FROM users
            """, (today_str,))
            row = cur.fetchone()
            total, pending, beta, paid, blocked, new_today = row

            cur.execute("SELECT COUNT(*) FROM usage WHERE date=%s", (today_str,))
            today_a = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM usage")
            total_a = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM referrals WHERE paid=1")
            ref_paid = cur.fetchone()[0]

            cur.execute(
                "SELECT COUNT(DISTINCT telegram_id) FROM usage WHERE date=%s", (today_str,)
            )
            dau = cur.fetchone()[0]

            cur.execute(
                "SELECT COUNT(DISTINCT telegram_id) FROM usage WHERE date >= %s", (week_ago,)
            )
            wau = cur.fetchone()[0]

            # D1 retention
            cur.execute(
                """SELECT COUNT(*) FROM users u
                   WHERE DATE(u.created_at) = %s
                   AND EXISTS (
                       SELECT 1 FROM usage g WHERE g.telegram_id=u.telegram_id AND g.date=%s
                   )""",
                (yesterday_str, today_str),
            )
            d1_num = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM users WHERE DATE(created_at) = %s", (yesterday_str,)
            )
            d1_den = cur.fetchone()[0]
            d1_ret = round(d1_num / d1_den * 100) if d1_den else 0

            # D7 retention
            cur.execute(
                """SELECT COUNT(*) FROM users u
                   WHERE DATE(u.created_at) = %s
                   AND EXISTS (
                       SELECT 1 FROM usage g WHERE g.telegram_id=u.telegram_id AND g.date=%s
                   )""",
                (day7_str, today_str),
            )
            d7_num = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM users WHERE DATE(created_at) = %s", (day7_str,)
            )
            d7_den = cur.fetchone()[0]
            d7_ret = round(d7_num / d7_den * 100) if d7_den else 0

            cur.execute(
                "SELECT COALESCE(AVG(streak_days),0) FROM users WHERE status IN ('beta','paid')"
            )
            avg_streak = round(cur.fetchone()[0], 1)

            return {
                "total_users": total,
                "pending": pending,
                "beta": beta,
                "paid": paid,
                "blocked": blocked,
                "analyses_today": today_a,
                "analyses_total": total_a,
                "referrals_paid": ref_paid,
                "dau": dau,
                "wau": wau,
                "d1_retention": d1_ret,
                "d7_retention": d7_ret,
                "avg_streak": avg_streak,
                "new_today": new_today,
            }

# ── Referrals ──────────────────────────────────────────────────────────────

def register_referral(referrer_id, referee_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO referrals (referrer_id, referred_id)"
                " VALUES (%s, %s) ON CONFLICT DO NOTHING",
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
                """SELECT
                       COUNT(*)                           AS total,
                       COUNT(*) FILTER (WHERE paid = 1)  AS paid
                   FROM referrals WHERE referrer_id=%s""",
                (telegram_id,),
            )
            row = cur.fetchone()
            return {"total": row[0], "paid": row[1]}
