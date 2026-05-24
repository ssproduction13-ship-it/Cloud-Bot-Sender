# ArdashevFood Bot

Telegram бот для подсчёта калорий по фото еды.

## Переменные окружения (Railway)

| Переменная | Описание |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Токен бота от @BotFather |
| `TELEGRAM_CHAT_ID` | Telegram ID администратора |
| `BOT_USERNAME` | Username бота (без @) |
| `OPENAI_API_KEY` | Ключ OpenAI API (`sk-...`) |
| `OPENAI_BASE_URL` | Опционально. По умолчанию `https://api.openai.com/v1` |
| `DATABASE_URL` | PostgreSQL connection string (Railway даёт автоматически) |

## Деплой на Railway

1. Создать проект на [railway.app](https://railway.app)
2. Добавить PostgreSQL плагин (Database → Add PostgreSQL)
3. Подключить этот репозиторий
4. Добавить переменные окружения (Settings → Variables)
5. Railway автоматически запустит `python main.py`
