import re
import base64
import os

from openai import AsyncOpenAI

openai_client = AsyncOpenAI(
    api_key=os.environ["GROQ_API_KEY"],
    base_url="https://api.groq.com/openai/v1",
    max_retries=3,
    timeout=60,
)

VISION_PROMPT = """Ты — эксперт по еде. Опиши фото:
1. Блюдо/продукты (точно)
2. Способ приготовления
3. Примерный вес порции (г)
4. Основные ингредиенты и количество

Если это не еда — напиши только: НЕ ЕДА"""

NUTRITION_PROMPT = """Ты — AI-тренер по питанию. Рассчитай КБЖУ.

Блюдо: {desc}

Ответь СТРОГО в этом формате:
🍽 *{{название}}* (~{{вес}} г)

🔥 {{ккал}} ккал
Б {{б}} • Ж {{ж}} • У {{у}} г

💬 {{1-2 коротких строки. Конкретно, без воды. Максимум 15 слов. Один эмодзи в конце.}}

KCAL:{{ккал}}
PROTEIN:{{б}}
FAT:{{ж}}
CARBS:{{у}}
NAME:{{название}}"""

TEXT_NUTRITION_PROMPT = """Ты — AI-тренер по питанию.

Блюдо/продукт: {desc}

Если это не еда — ответь только: НЕ ЕДА

Иначе ответь СТРОГО в этом формате:
🍽 *{{название}}* (~{{вес}} г)

🔥 {{ккал}} ккал
Б {{б}} • Ж {{ж}} • У {{у}} г

💬 {{1-2 коротких строки. Конкретно, без воды. Максимум 15 слов. Один эмодзи в конце.}}

KCAL:{{ккал}}
PROTEIN:{{б}}
FAT:{{ж}}
CARBS:{{у}}
NAME:{{название}}"""


def _parse_macros(raw: str):
    kcal = protein = fat = carbs = food_name = None
    m = re.search(r"KCAL:(\d+)", raw);       kcal      = int(m.group(1))   if m else None
    m = re.search(r"PROTEIN:([\d.]+)", raw); protein   = float(m.group(1)) if m else None
    m = re.search(r"FAT:([\d.]+)", raw);     fat       = float(m.group(1)) if m else None
    m = re.search(r"CARBS:([\d.]+)", raw);   carbs     = float(m.group(1)) if m else None
    m = re.search(r"NAME:(.+)", raw);        food_name = m.group(1).strip() if m else None
    display = re.sub(r"\s*(KCAL|PROTEIN|FAT|CARBS|NAME):[^\n]+", "", raw).strip()
    # Sync displayed numbers with parsed (stored) values to prevent mismatch
    if kcal is not None:
        display = re.sub(r"\d+(?=\s*ккал)", str(kcal), display)
    if protein is not None and fat is not None and carbs is not None:
        display = re.sub(r"Б\s*[\d.]+", f"Б {round(protein)}", display)
        display = re.sub(r"Ж\s*[\d.]+", f"Ж {round(fat)}", display)
        display = re.sub(r"У\s*[\d.]+\s*г", f"У {round(carbs)} г", display)
    return display, kcal, protein, fat, carbs, food_name

def _validate_analysis(display, kcal, protein, fat, carbs) -> tuple[bool, str | None]:
    if kcal is None:
        return False, "⚠️ Не удалось распознать калории. Опиши блюдо подробнее или введи вручную."
    if not (20 <= kcal <= 6000):
        return False, f"⚠️ Получилось {kcal} ккал — похоже на ошибку. Попробуй ещё раз."
    if protein is not None and not (0 <= protein <= 500):
        return False, "⚠️ Некорректные данные по белку. Попробуй ещё раз."
    if fat is not None and not (0 <= fat <= 500):
        return False, "⚠️ Некорректные данные по жирам. Попробуй ещё раз."
    if carbs is not None and not (0 <= carbs <= 1000):
        return False, "⚠️ Некорректные данные по углеводам. Попробуй ещё раз."
    if not display or len(display.strip()) < 10:
        return False, "⚠️ Пустой ответ от AI. Попробуй ещё раз."
    bad = ["не ед", "error", "sorry", "cannot", "не могу", "не знаю", "unable", "не понимаю"]
    if any(p in display.lower() for p in bad):
        return False, "🙅 Это не похоже на еду. Введи название блюда или продукта."
    return True, None


async def analyze_food_photo(photo_bytes: bytes):
    b64 = base64.b64encode(photo_bytes).decode()
    vision = await openai_client.chat.completions.create(
        model="meta-llama/llama-4-scout-17b-16e-instruct",
        messages=[{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                {"type": "text", "text": VISION_PROMPT},
            ],
        }],
        max_tokens=400,
    )
    desc = vision.choices[0].message.content or ""
    if "НЕ ЕДА" in desc.upper():
        return "🙅 На фото не еда. Пришли фото блюда — посчитаю калории!", None, None, None, None, None

    nutrition = await openai_client.chat.completions.create(
        model="meta-llama/llama-4-scout-17b-16e-instruct",
        messages=[
            {"role": "system", "content": "Точный нутрициолог и дружелюбный тренер. Строго по шаблону."},
            {"role": "user",   "content": NUTRITION_PROMPT.format(desc=desc)},
        ],
        max_tokens=500,
    )
    raw = nutrition.choices[0].message.content or ""
    return _parse_macros(raw)


async def analyze_food_text(description: str):
    response = await openai_client.chat.completions.create(
        model="meta-llama/llama-4-scout-17b-16e-instruct",
        messages=[
            {"role": "system", "content": "Точный нутрициолог и дружелюбный тренер. Строго по шаблону."},
            {"role": "user",   "content": TEXT_NUTRITION_PROMPT.format(desc=description)},
        ],
        max_tokens=500,
    )
    raw = response.choices[0].message.content or ""
    if "НЕ ЕДА" in raw.upper():
        return "🙅 Это не похоже на еду. Введи название блюда или продукта.", None, None, None, None, None
    return _parse_macros(raw)
