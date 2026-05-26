"""
utils/formatting.py — Score calculation, AI comments, food keyword lists.
Extracted from main.py (P1 module refactoring).
"""
import random as _rnd

CHEAT_KEYWORDS = [
    "бургер", "гамбургер", "чизбургер", "kfc", "mcdonald", "макдональдс",
    "пицца", "чипсы", "картофель фри", "фри", "шоколад", "конфеты", "торт",
    "пончик", "donut", "мороженое", "fast food", "фастфуд", "нагетсы",
    "хот-дог", "hotdog", "картошка фри", "сникерс", "kit kat",
]
SUGAR_KEYWORDS = ["сахар", "конфеты", "сладкое", "торт", "пирожное", "газировка", "кола"]


def calc_daily_score(kcal: int, protein: float, fat: float, carbs: float,
                     goal_kcal: int | None, goal_protein: int | None, meals: int) -> int:
    """Calculate nutrition score 0-100."""
    score = 0
    if goal_kcal and goal_kcal > 0:
        ratio = kcal / goal_kcal
        if 0.85 <= ratio <= 1.10:
            score += 35
        elif 0.70 <= ratio < 0.85 or 1.10 < ratio <= 1.20:
            score += 20
        elif ratio <= 1.30:
            score += 10
    else:
        score += 20

    if goal_protein and goal_protein > 0 and protein > 0:
        p_ratio = protein / goal_protein
        if p_ratio >= 0.85:
            score += 30
        elif p_ratio >= 0.65:
            score += 20
        elif p_ratio >= 0.45:
            score += 10
    elif protein > 0:
        score += 15

    if meals >= 3:
        score += 20
    elif meals == 2:
        score += 12
    elif meals == 1:
        score += 5

    total_macros = protein + fat + carbs
    if total_macros > 0 and protein / total_macros >= 0.20:
        score += 15

    return min(score, 100)


def format_score(score_100: int) -> float:
    """Convert 0-100 score to 5.0-10.0 display scale. Minimum 5.0."""
    return max(round(score_100 / 10, 1), 5.0)


def ai_score_comment(score_100: int, protein: float, carbs: float, kcal: int,
                     goal_kcal: int | None, food_name: str | None) -> str:
    """Return a short companion-style comment based on the day's nutrition."""
    fn = (food_name or "").lower()
    cheat = any(k in fn for k in ["бургер","пицца","чипсы","фри","kfc","mcdonald","нагетсы"])
    sugar = any(k in fn for k in ["сахар","конфеты","торт","пирожное","кола","газировка"])
    if score_100 >= 85:
        return _rnd.choice([
            "Идеальный день 🔥 Так и держи.",
            "Отличный результат 💪 Всё в норме.",
            "Вот это баланс 👌 Продолжай.",
        ])
    if cheat:
        return _rnd.choice([
            "Чит-мил — это нормально 😄 Завтра возвращаемся в ритм.",
            "Раз в неделю — не страшно 👊 Главное не делать это системой.",
        ])
    if sugar:
        return "Многовато сахара, но по калориям норм 👌"
    if protein > 0 and protein < 60:
        return _rnd.choice([
            "Белка маловато — добавь яйца, творог или курицу 🥩",
            "Подтяни белок — он держит мышцы и сытость 💪",
        ])
    if goal_kcal and kcal > goal_kcal * 1.2:
        return _rnd.choice([
            "Чуть перебор — завтра полегче, всё выровняется 👍",
            "Один день не ломает прогресс. Завтра вернёмся в норму 🙏",
        ])
    if goal_kcal and kcal < goal_kcal * 0.7:
        return _rnd.choice([
            "Маловато калорий — не голодай, это замедляет прогресс 🌱",
            "Дефицит должен быть мягким — не пропускай приёмы пищи 👌",
        ])
    if score_100 >= 70:
        return _rnd.choice([
            "Хороший день. Стабильность — это и есть прогресс 🌱",
            "Всё на месте. Продолжай в том же духе 👌",
        ])
    return _rnd.choice([
        "Держишь курс — всё идёт как надо 🔥",
        "Небольшие улучшения каждый день — вот что работает 💪",
    ])


def detect_fun_reaction(food_name_lower: str, kcal: int | None) -> str | None:
    """Return a fun reaction for specific foods."""
    for kw in CHEAT_KEYWORDS:
        if kw in food_name_lower:
            return _rnd.choice([
                "🍗 Чит-мил детектирован! Раз в неделю можно — наслаждайся без вины 😄",
                "🍔 Зафиксировано. Завтра компенсируем лёгким ужином 💪",
                "🍕 Калорийная бомба принята. Главное — не делать из этого систему 😅",
            ])
    for kw in SUGAR_KEYWORDS:
        if kw in food_name_lower:
            return "🍬 Сахарная атака! Сладкое хорошо в меру — запей водой и всё будет ок 😊"
    if kcal and kcal > 900:
        return "😅 Вот это порция! Мощно. Остаток дня — полегче 💪"
    return None
