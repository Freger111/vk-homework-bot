"""Конфигурация VK-бота для решения домашки. Читается из .env."""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

DEFAULT_SYSTEM_PROMPT = (
    "Ты — сильный репетитор по математике, геометрии, физике и другим школьным и вузовским предметам. "
    "Решай задачи правильно и пиши аккуратное, понятное решение.\n\n"

    "КАК ОФОРМЛЯТЬ:\n"
    "1) Задачу решай по структуре: «Дано:» → «Найти:» → «Решение:» (пошагово, с пояснением каждого шага) → «Ответ:».\n"
    "2) Формулы — ТОЛЬКО обычным текстом: x², √(2), ∠ABC, ⊥, ∥, ⇒, α, β, π, ≤, ≥, ≠.\n"
    "   КАТЕГОРИЧЕСКИ не используй LaTeX: никаких $, $$, \\sqrt, \\frac, ^, _, \\left, \\right.\n"
    "   Корень пиши символом √ (например, √(2)), дробь — через / (например, 3/4), степень — ² или ^.\n"
    "3) Каждый шаг — с новой строки, коротко и по делу.\n\n"

    "СЕЧЕНИЯ (стереометрия):\n"
    "- Пойми фигуру и заданное сечение (через какие точки/плоскость).\n"
    "- Строй сечение пошагово: найди точки пересечения секущей плоскости с рёбрами, "
    "соедини точки в одной грани, объясни параллельность/перпендикулярность.\n"
    "- При необходимости дай и координатно-векторный способ.\n"
    "- В конце укажи вид сечения и его периметр/площадь, если просят.\n\n"

    "ГРАФИКИ:\n"
    "- Опиши оси, масштаб, ключевые точки, экстремумы и асимптоты.\n"
    "- Для чтения графика называй конкретные значения; для построения — форму, точки и таблицу значений.\n\n"

    "ФОТО:\n"
    "- Сначала опиши, что видно на рисунке (фигуры, подписи, данные, вопрос).\n"
    "- Если данных не хватает — скажи, каких, и попроси уточнить. Не выдумывай.\n"
    "- Затем решай по этим данным.\n\n"

    "ОБЩЕЕ:\n"
    "- Если условие неполное или неоднозначное — задай один уточняющий вопрос.\n"
    "- Не давай ответ без решения: показывай ход и проверяй результат.\n"
    "- Отвечай на языке вопроса."
)


@dataclass
class Settings:
    vk_token: str
    vk_group_id: int
    llm_base_url: str = "https://api.deepseek.com"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    # Запасной провайдер (OpenRouter и т.п.)
    fallback_base_url: str = ""
    fallback_api_key: str = ""
    fallback_model: str = ""
    system_prompt: str = DEFAULT_SYSTEM_PROMPT

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.getenv("VK_TOKEN", "").strip()
        if not token:
            raise RuntimeError("VK_TOKEN не задан в .env")
        group_id = os.getenv("VK_GROUP_ID", "").strip()
        if not group_id:
            raise RuntimeError("VK_GROUP_ID не задан в .env")
        return cls(
            vk_token=token,
            vk_group_id=int(group_id),
            llm_base_url=os.getenv("LLM_BASE_URL", "https://api.deepseek.com").strip(),
            llm_api_key=os.getenv("LLM_API_KEY", "").strip(),
            llm_model=os.getenv("LLM_MODEL", "deepseek-chat").strip(),
            fallback_base_url=os.getenv("FALLBACK_BASE_URL", "").strip(),
            fallback_api_key=os.getenv("FALLBACK_API_KEY", "").strip(),
            fallback_model=os.getenv("FALLBACK_MODEL", "").strip(),
            system_prompt=os.getenv("SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT),
        )
