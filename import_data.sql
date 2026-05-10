-- ============================================
-- Запустите этот SQL в Railway: 
-- PostgreSQL console вашего проекта
-- ============================================

CREATE TABLE IF NOT EXISTS users (
    telegram_id      BIGINT PRIMARY KEY,
    username         TEXT,
    first_name       TEXT,
    status           TEXT DEFAULT 'pending',
    subscribed_at    TEXT,
    expires_at       TEXT,
    created_at       TEXT,
    referred_by      BIGINT,
    daily_goal       INTEGER,
    trial_expires_at TEXT
);

CREATE TABLE IF NOT EXISTS usage (
    id          SERIAL PRIMARY KEY,
    telegram_id BIGINT,
    used_at     TEXT,
    date        TEXT,
    calories    INTEGER
);

CREATE TABLE IF NOT EXISTS referrals (
    id          SERIAL PRIMARY KEY,
    referrer_id BIGINT,
    referred_id BIGINT UNIQUE,
    paid        INTEGER DEFAULT 0,
    bonus_given INTEGER DEFAULT 0,
    created_at  TEXT
);

-- Пользователи (7):
INSERT INTO users (telegram_id,username,first_name,status,subscribed_at,expires_at,created_at,referred_by,daily_goal,trial_expires_at) VALUES (444063837,'voots','V','beta',NULL,NULL,'2026-05-07 08:04:11',NULL,'2415','2026-05-10T08:04:27.923671') ON CONFLICT DO NOTHING;
INSERT INTO users (telegram_id,username,first_name,status,subscribed_at,expires_at,created_at,referred_by,daily_goal,trial_expires_at) VALUES (499403104,'Alexandr_Ardashev','Александр','beta',NULL,NULL,'2026-05-07 05:51:33',NULL,'2344','2026-05-07 05:51:33') ON CONFLICT DO NOTHING;
INSERT INTO users (telegram_id,username,first_name,status,subscribed_at,expires_at,created_at,referred_by,daily_goal,trial_expires_at) VALUES (758226511,'kamenskayad','Дарья','beta',NULL,NULL,'2026-05-07 07:54:20',NULL,'1878','2026-05-10T07:54:29.862741') ON CONFLICT DO NOTHING;
INSERT INTO users (telegram_id,username,first_name,status,subscribed_at,expires_at,created_at,referred_by,daily_goal,trial_expires_at) VALUES (788898834,'vanyaresh23','Иван','beta',NULL,NULL,'2026-05-07 08:03:37',NULL,NULL,'2026-05-10T08:03:41.826246') ON CONFLICT DO NOTHING;
INSERT INTO users (telegram_id,username,first_name,status,subscribed_at,expires_at,created_at,referred_by,daily_goal,trial_expires_at) VALUES (1257060825,'Dar_ptv','Darya','beta',NULL,NULL,'2026-05-07 07:12:12',NULL,'1584','2026-05-07 07:12:12') ON CONFLICT DO NOTHING;
INSERT INTO users (telegram_id,username,first_name,status,subscribed_at,expires_at,created_at,referred_by,daily_goal,trial_expires_at) VALUES (1308010331,'PloxoiKloyn','Плохойклоун','beta',NULL,NULL,'2026-05-07 08:28:06',NULL,'2600','2026-05-10T08:28:13.827056') ON CONFLICT DO NOTHING;
INSERT INTO users (telegram_id,username,first_name,status,subscribed_at,expires_at,created_at,referred_by,daily_goal,trial_expires_at) VALUES (5145986404,'dashkenchik_1399','Darina <З','beta',NULL,NULL,'2026-05-07 11:36:02','1308010331','2151','2026-05-10T11:36:12.971941') ON CONFLICT DO NOTHING;

-- История использования (10):
INSERT INTO usage (telegram_id,used_at,date,calories) VALUES (499403104,'2026-05-07 06:05:40','2026-05-07',NULL);
INSERT INTO usage (telegram_id,used_at,date,calories) VALUES (1257060825,'2026-05-07 07:21:21','2026-05-07',NULL);
INSERT INTO usage (telegram_id,used_at,date,calories) VALUES (1257060825,'2026-05-07 07:22:06','2026-05-07','390');
INSERT INTO usage (telegram_id,used_at,date,calories) VALUES (499403104,'2026-05-07 07:27:26','2026-05-07','103');
INSERT INTO usage (telegram_id,used_at,date,calories) VALUES (788898834,'2026-05-07 08:04:08','2026-05-07',NULL);
INSERT INTO usage (telegram_id,used_at,date,calories) VALUES (444063837,'2026-05-07 10:02:36','2026-05-07','760');
INSERT INTO usage (telegram_id,used_at,date,calories) VALUES (1308010331,'2026-05-07 10:46:13','2026-05-07','58');
INSERT INTO usage (telegram_id,used_at,date,calories) VALUES (1308010331,'2026-05-07 10:46:37','2026-05-07','53');
INSERT INTO usage (telegram_id,used_at,date,calories) VALUES (1308010331,'2026-05-07 10:47:05','2026-05-07','190');
INSERT INTO usage (telegram_id,used_at,date,calories) VALUES (1308010331,'2026-05-07 10:47:30','2026-05-07','55');

-- Рефералы (1):
INSERT INTO referrals (referrer_id,referred_id,paid,bonus_given) VALUES (1308010331,5145986404,0,0) ON CONFLICT DO NOTHING;