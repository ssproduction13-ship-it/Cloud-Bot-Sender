import os
import json
import psycopg2
import psycopg2.extras
from contextlib import contextmanager
from psycopg2.pool import ThreadedConnectionPool
from datetime import datetime, timedelta, date, timezone

DATABASE_URL = os.environ["DATABASE_URL"]

_utcnow = lambda: datetime.now(timezone.utc).replace(tzinfo=None)

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

                CREATE TABLE IF NOT EXISTS water_logs (
                    id          SERIAL PRIMARY KEY,
                    telegram_id BIGINT,
                    glasses     INTEGER DEFAULT 0,
                    date        TEXT DEFAULT (CURRENT_DATE::TEXT),
                    updated_at  TEXT DEFAULT (NOW()::TEXT),
                    UNIQUE(telegram_id, date)
                );

                CREATE TABLE IF NOT EXISTS onboard_state (
                    telegram_id BIGINT PRIMARY KEY,
                    state       TEXT NOT NULL,
                    data_json   TEXT DEFAULT '{}',
                    updated_at  TEXT DEFAULT (NOW()::TEXT)
                );

                CREATE TABLE IF NOT EXISTS events (
                    id          SERIAL PRIMARY KEY,
                    telegram_id BIGINT,
                    event_name  TEXT NOT NULL,
                    payload     TEXT DEFAULT '{}',
                    created_at  TEXT DEFAULT (NOW()::TEXT)
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
                ("deleted",   "BOOLEAN DEFAULT FALSE"),
                ("meal_type", "TEXT DEFAULT 'other'"),
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
    expires = (_utcnow() + timedelta(days=trial_days)).isoformat()
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
    return _utcnow() > datetime.fromisoformat(user["trial_expires_at"])

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
                 _utcnow().isoformat()),
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
        if (_utcnow() - ts).total_seconds() > 86400:
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
    now = _utcnow()
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
    return _utcnow() > datetime.fromisoformat(user["expires_at"])

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
    """Return users active in the last 7 days with a valid subscription.

    Paid users with an expired subscription are excluded — they have no bot
    access and should not receive 'send your meal photo' notifications.
    Beta (trial) users are included while their trial is active.
    """
    week_ago = (_utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
    now_iso  = _utcnow().isoformat()
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT DISTINCT u.* FROM users u
                JOIN usage g ON g.telegram_id = u.telegram_id
                WHERE g.date >= %s
                  AND (
                      (u.status = 'beta'
                       AND (u.trial_expires_at IS NULL OR u.trial_expires_at > %s))
                      OR
                      (u.status = 'paid'
                       AND u.expires_at IS NOT NULL
                       AND u.expires_at > %s)
                  )
                """,
                (week_ago, now_iso, now_iso),
            )
            return [dict(r) for r in cur.fetchall()]

def get_users_for_notifications():
    """All users with an active trial or paid subscription.
    Unlike get_active_users(), does NOT require recent food logs.
    Used for morning/evening push notifications.
    """
    now_iso = _utcnow().isoformat()
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT * FROM users
                WHERE
                    (status = 'beta'
                     AND (trial_expires_at IS NULL OR trial_expires_at > %s))
                    OR
                    (status = 'paid'
                     AND expires_at IS NOT NULL
                     AND expires_at > %s)
                ORDER BY telegram_id
                """,
                (now_iso, now_iso),
            )
            return [dict(r) for r in cur.fetchall()]

# ── Streak ─────────────────────────────────────────────────────────────────

def update_streak(telegram_id, user=None) -> tuple[int, bool]:
    """
    Update streak when a user logs food today.

    Truth source priority:
    1. last_active_date == today  → already counted, return as-is.
    2. last_active_date == yesterday → normal increment.
    3. last_active_date is stale (e.g. after a migration/fix) → fall back to
       checking the usage table directly: if there is an entry dated yesterday,
       the user WAS active yesterday and the streak should continue.
    4. Otherwise → reset to 1.

    This makes the streak resilient to last_active_date being set to a
    historical date by fix_all_streaks or any other admin operation.
    """
    today     = _utcnow().strftime("%Y-%m-%d")
    yesterday = (_utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")

    if user is None:
        user = get_user(telegram_id)
    if not user:
        return 0, False

    last   = user.get("last_active_date")
    streak = user.get("streak_days") or 0
    best   = user.get("best_streak") or 0

    # Already counted today — nothing to do
    if last == today:
        return streak, False

    if last == yesterday:
        streak += 1
    else:
        # last_active_date is stale (e.g. set by fix_all_streaks to a historical
        # date). Check the usage table for yesterday specifically.
        # NOTE: we must check ONLY yesterday, not today — record_usage has
        # already inserted today's entry before this function runs, so a
        # "date >= two_days_ago" query would find today's row and falsely
        # preserve the streak even after a multi-day gap.
        had_yesterday = False
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT 1 FROM usage "
                        "WHERE telegram_id=%s AND date=%s "
                        "  AND (deleted IS NULL OR deleted=FALSE) "
                        "LIMIT 1",
                        (telegram_id, yesterday),
                    )
                    had_yesterday = cur.fetchone() is not None
        except Exception as _e:
            import logging as _log
            _log.getLogger(__name__).warning(
                "update_streak fallback query failed uid=%s: %s", telegram_id, _e
            )
            had_yesterday = streak > 1  # fail-safe: preserve streak on DB error

        if had_yesterday:
            streak += 1
        else:
            streak = 1    # genuine gap — reset

    new_best  = max(best, streak)
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


def reset_stale_streaks() -> int:
    """Reset streak_days to 0 for users who missed yesterday.

    Called nightly so profiles always show the correct (broken) streak value
    without waiting for the user to log something.
    Returns the number of users whose streak was reset.
    """
    yesterday = (_utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE users
                   SET streak_days = 0
                   WHERE streak_days > 0
                     AND (last_active_date IS NULL OR last_active_date < %s)""",
                (yesterday,)
            )
            count = cur.rowcount
        conn.commit()
    return count


# ── Usage & Macros ─────────────────────────────────────────────────────────

def record_usage(telegram_id, kcal=None, protein=None, fat=None, carbs=None,
                 food_name=None, meal_type="other") -> int:
    now = _utcnow()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO usage
                   (telegram_id, used_at, date, calories, protein_g, fat_g, carbs_g, food_name, meal_type)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (telegram_id, now.isoformat(), now.strftime("%Y-%m-%d"),
                 kcal, protein, fat, carbs, food_name, meal_type),
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
    today = _utcnow().strftime("%Y-%m-%d")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM usage WHERE telegram_id=%s AND date=%s",
                (telegram_id, today),
            )
            return cur.fetchone()[0]

def get_daily_macros(telegram_id):
    today = _utcnow().strftime("%Y-%m-%d")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT COALESCE(SUM(calories),0),
                          COALESCE(SUM(protein_g),0),
                          COALESCE(SUM(fat_g),0),
                          COALESCE(SUM(carbs_g),0)
                   FROM usage WHERE telegram_id=%s AND date=%s AND (deleted IS NULL OR deleted=FALSE)""",
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
    today = _utcnow().strftime("%Y-%m-%d")
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """SELECT id, food_name, calories, protein_g, fat_g, carbs_g, used_at
                   FROM usage WHERE telegram_id=%s AND date=%s AND (deleted IS NULL OR deleted=FALSE)
                   ORDER BY used_at ASC""",
                (telegram_id, today),
            )
            return [dict(r) for r in cur.fetchall()]

def delete_entry(entry_id: int):
    """Delete a single usage entry by id."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE usage SET deleted=TRUE WHERE id=%s", (entry_id,))
        conn.commit()

def reset_today_entries(telegram_id: int):
    """Delete all usage entries for today for a given user."""
    today = _utcnow().strftime("%Y-%m-%d")
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
                "SELECT COALESCE(SUM(calories),0) FROM usage WHERE telegram_id=%s AND date=%s AND (deleted IS NULL OR deleted=FALSE)",
                (telegram_id, date_str),
            )
            return int(cur.fetchone()[0])

# ── Weekly stats ───────────────────────────────────────────────────────────

def get_weekly_stats(telegram_id):
    today = _utcnow().date()
    days = [(today - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT date,
                          COALESCE(SUM(calories),0) AS kcal,
                          COALESCE(SUM(protein_g),0) AS protein
                   FROM usage
                   WHERE telegram_id=%s AND date >= %s AND (deleted IS NULL OR deleted=FALSE)
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
    now = _utcnow()
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
    cutoff = (_utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
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
    today_str = _utcnow().strftime("%Y-%m-%d")
    yesterday_str = (_utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
    week_ago = (_utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
    day7_str = (_utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")

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
                    COUNT(*) FILTER (WHERE LEFT(created_at, 10) = %s) AS new_today
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

            # D1 retention — use LEFT(created_at,10) instead of DATE() to avoid TEXT tz-suffix cast issues
            cur.execute(
                """SELECT COUNT(*) FROM users u
                   WHERE LEFT(u.created_at, 10) = %s
                   AND EXISTS (
                       SELECT 1 FROM usage g WHERE g.telegram_id=u.telegram_id AND g.date=%s
                   )""",
                (yesterday_str, today_str),
            )
            d1_num = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM users WHERE LEFT(created_at, 10) = %s", (yesterday_str,)
            )
            d1_den = cur.fetchone()[0]
            d1_ret = round(d1_num / d1_den * 100) if d1_den else 0

            # D7 retention — users registered 7 days ago who used it today (their day 7)
            cur.execute(
                """SELECT COUNT(*) FROM users u
                   WHERE LEFT(u.created_at, 10) = %s
                   AND EXISTS (
                       SELECT 1 FROM usage g WHERE g.telegram_id=u.telegram_id AND g.date=%s
                   )""",
                (day7_str, today_str),
            )
            d7_num = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM users WHERE LEFT(created_at, 10) = %s", (day7_str,)
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


# ── Subscription expiry helpers ─────────────────────────────────────────────

def get_expiring_users(days_ahead: int) -> list:
    """Return paid users whose subscription expires in exactly days_ahead days."""
    target_date = (_utcnow() + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """SELECT * FROM users
                   WHERE status = 'paid'
                     AND expires_at IS NOT NULL
                     AND LEFT(expires_at, 10) = %s""",
                (target_date,),
            )
            return [dict(r) for r in cur.fetchall()]


def get_winback_users() -> list:
    """Return paid users whose subscription expired exactly 3 days ago."""
    target_date = (_utcnow() - timedelta(days=3)).strftime("%Y-%m-%d")
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """SELECT * FROM users
                   WHERE status = 'paid'
                     AND expires_at IS NOT NULL
                     AND LEFT(expires_at, 10) = %s""",
                (target_date,),
            )
            return [dict(r) for r in cur.fetchall()]


def get_streak_users_no_log_today() -> list:
    """Return active users with a still-alive streak who have no entries today.

    Only includes users where last_active_date >= yesterday — i.e. the streak
    is genuinely alive. Users who already missed a day (streak is effectively
    broken) are excluded so we never send 'You have a N-day streak!' to someone
    whose streak has already reset.
    """
    today     = _utcnow().strftime("%Y-%m-%d")
    yesterday = (_utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """SELECT u.* FROM users u
                   WHERE u.streak_days > 0
                     AND u.status IN ('beta', 'paid')
                     AND u.last_active_date >= %s
                     AND NOT EXISTS (
                         SELECT 1 FROM usage g
                         WHERE g.telegram_id = u.telegram_id
                           AND g.date = %s
                           AND (g.deleted IS NULL OR g.deleted = FALSE)
                     )""",
                (yesterday, today),
            )
            return [dict(r) for r in cur.fetchall()]


# ── Water tracker ─────────────────────────────────────────────────────────────

def add_water_log(telegram_id: int) -> int:
    """Add one glass of water for today; return total glasses today."""
    today = _utcnow().strftime("%Y-%m-%d")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO water_logs (telegram_id, glasses, date, updated_at)
                   VALUES (%s, 1, %s, %s)
                   ON CONFLICT (telegram_id, date)
                   DO UPDATE SET glasses    = water_logs.glasses + 1,
                                 updated_at = EXCLUDED.updated_at
                   RETURNING glasses""",
                (telegram_id, today, _utcnow().isoformat()),
            )
            result = cur.fetchone()
        conn.commit()
    return result[0] if result else 1


def reset_water_today(telegram_id: int) -> None:
    today = _utcnow().strftime("%Y-%m-%d")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE water_logs SET glasses=0 WHERE telegram_id=%s AND date=%s",
                (telegram_id, today),
            )
        conn.commit()


def get_water_today(telegram_id: int) -> int:
    today = _utcnow().strftime("%Y-%m-%d")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT glasses FROM water_logs WHERE telegram_id=%s AND date=%s",
                (telegram_id, today),
            )
            row = cur.fetchone()
    return row[0] if row else 0


# ── Segmented broadcast helpers ───────────────────────────────────────────────

def track_event(telegram_id: int, event_name: str, payload: dict | None = None) -> None:
    """Append a product analytics event. Fire-and-forget — never crashes the bot."""
    import json as _json
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO events (telegram_id, event_name, payload) VALUES (%s, %s, %s)",
                    (telegram_id, event_name, _json.dumps(payload or {})),
                )
            conn.commit()
    except Exception:
        pass


def get_events_summary(event_name: str, days: int = 7) -> int:
    """Count unique users for an event in the last N days."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT COUNT(DISTINCT telegram_id) FROM events
                       WHERE event_name=%s
                         AND created_at::TIMESTAMP >= NOW() - (%s * INTERVAL '1 day')""",
                    (event_name, days),
                )
                row = cur.fetchone()
                return int(row[0]) if row else 0
    except Exception as e:
        import logging; logging.getLogger(__name__).warning("get_events_summary error: %s", e)
        return 0


def get_users_by_segment(segment: str) -> list:
    """
    segment values:
      'all_active'   — status in beta/paid
      'trial_active' — status=beta, trial not expired
      'sub_expired'  — status=paid, subscription expired
      'no_log_week'  — no usage records in last 7 days
      'paid_active'  — status=paid, subscription active
    """
    now = _utcnow()
    today = now.strftime("%Y-%m-%d")
    week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if segment == "all_active":
                cur.execute("SELECT * FROM users WHERE status IN ('beta','paid')")
            elif segment == "trial_active":
                cur.execute(
                    "SELECT * FROM users WHERE status='beta' AND trial_expires_at > %s",
                    (today,),
                )
            elif segment == "sub_expired":
                cur.execute(
                    "SELECT * FROM users WHERE status='paid' AND LEFT(expires_at,10) < %s",
                    (today,),
                )
            elif segment == "no_log_week":
                cur.execute(
                    """SELECT u.* FROM users u
                       WHERE u.status IN ('beta','paid')
                         AND NOT EXISTS (
                             SELECT 1 FROM usage g
                             WHERE g.telegram_id = u.telegram_id
                               AND g.date >= %s
                         )""",
                    (week_ago,),
                )
            elif segment == "paid_active":
                cur.execute(
                    "SELECT * FROM users WHERE status='paid' AND LEFT(expires_at,10) >= %s",
                    (today,),
                )
            else:
                cur.execute("SELECT * FROM users WHERE status IN ('beta','paid')")
            return [dict(r) for r in cur.fetchall()]


# ── P6: Scan count & protein record helpers ────────────────────────────────────

def get_user_scan_count(telegram_id: int) -> int:
    """Total number of food analyses ever logged for a user."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM usage WHERE telegram_id=%s", (telegram_id,))
                row = cur.fetchone()
                return int(row[0]) if row else 0
    except Exception:
        return 0


def get_user_best_daily_protein_excl_today(telegram_id: int) -> float:
    """Best single-day protein total *before* today (for new-record detection)."""
    today = _utcnow().strftime("%Y-%m-%d")
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COALESCE(MAX(daily_p), 0) FROM (
                        SELECT SUM(protein_g) AS daily_p
                        FROM usage
                        WHERE telegram_id=%s
                          AND protein_g IS NOT NULL
                          AND date < %s
                        GROUP BY date
                    ) t
                    """,
                    (telegram_id, today),
                )
                row = cur.fetchone()
                return float(row[0]) if row else 0.0
    except Exception:
        return 0.0

def fix_all_streaks() -> list[dict]:
    """
    Recalculate streak_days from actual usage history.

    Algorithm: scan the last 90 days and find the LONGEST consecutive chain.
    This correctly restores streaks broken by migrations/outages — e.g. a
    15-day chain that ended before a 2-day gap is restored as 15, not 0.

    best_streak is set to max(existing_best, longest_chain).
    last_active_date is set to the END date of the longest chain.
    """
    from datetime import date as _date, timedelta as _td

    changes = []
    today   = _date.today()
    cutoff  = (today - _td(days=90)).isoformat()

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT DISTINCT u.telegram_id, u.streak_days, u.best_streak "
                "FROM users u "
                "JOIN usage g ON g.telegram_id = u.telegram_id "
                "WHERE (g.deleted IS NULL OR g.deleted = FALSE)"
            )
            users = list(cur.fetchall())

        for user in users:
            uid        = user["telegram_id"]
            old_streak = user["streak_days"] or 0
            old_best   = user["best_streak"] or 0

            # All distinct logged dates within the last 90 days
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT DISTINCT date FROM usage "
                    "WHERE telegram_id=%s "
                    "  AND (deleted IS NULL OR deleted=FALSE) "
                    "  AND date >= %s "
                    "ORDER BY date ASC",
                    (uid, cutoff),
                )
                rows = cur.fetchall()

            if not rows:
                continue

            # Parse dates
            logged = []
            for r in rows:
                v = r[0]
                if isinstance(v, str):
                    try:
                        v = _date.fromisoformat(v[:10])
                    except ValueError:
                        continue
                if isinstance(v, _date):
                    logged.append(v)

            if not logged:
                continue

            logged.sort()

            # Find the longest consecutive chain (scan once O(n))
            best_len  = 1
            best_end  = logged[0]
            cur_len   = 1
            cur_end   = logged[0]

            for i in range(1, len(logged)):
                if logged[i] - logged[i - 1] == _td(days=1):
                    cur_len += 1
                    cur_end  = logged[i]
                else:
                    # chain broken — reset
                    cur_len = 1
                    cur_end = logged[i]
                if cur_len > best_len:
                    best_len = cur_len
                    best_end = cur_end

            streak          = best_len
            new_best        = max(old_best, streak)
            most_recent_log = logged[-1]   # last date user actually logged

            # Bridge migration gaps: if the most recent log is older than
            # yesterday, use yesterday as last_active_date so the NEXT log
            # continues the streak via the normal yesterday-check path
            # instead of hitting the fallback and resetting to 1.
            yesterday_d = today - _td(days=1)
            bridge_date = most_recent_log if most_recent_log >= yesterday_d else yesterday_d

            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE users SET streak_days=%s, last_active_date=%s, best_streak=%s "
                    "WHERE telegram_id=%s",
                    (streak, bridge_date.isoformat(), new_best, uid),
                )
            conn.commit()

            if streak != old_streak:
                changes.append({"telegram_id": uid, "old": old_streak, "new": streak})

    return changes
