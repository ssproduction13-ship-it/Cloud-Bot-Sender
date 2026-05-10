# ArdashevFood Bot

Telegram бот для подсчёта калорий по фото еды.

## Переменные окружения (Railway)

| Переменная | Описание |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Токен бота от @BotFather |
| `TELEGRAM_CHAT_ID` | Telegram ID администратора |
| `BOT_USERNAME` | Username бота (без @) |
| `AI_INTEGRATIONS_OPENAI_API_KEY` | Ключ OpenAI API |
| `AI_INTEGRATIONS_OPENAI_BASE_URL` | Base URL для OpenAI |
| `DATABASE_URL` | PostgreSQL connection string (Railway даёт автоматически) |

## Деплой на Railway

1. Создать проект на [railway.app](https://railway.app)
2. Добавить PostgreSQL плагин
3. Подключить этот репозиторий
4. Добавить все переменные окружения
5. Запустить `python migrate_to_pg.py` один раз для переноса данных
