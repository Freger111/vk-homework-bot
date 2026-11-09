"""VK-бот для решения домашнего задания по тексту или фото.

Работает через Long Poll API ВКонтакте + любой OpenAI-совместимый LLM.
Без тяжёлых библиотек — только httpx.

Запуск из папки vk_bot:
    python bot.py
"""
import asyncio
import base64
import io
import json
import logging
import random
import re
import time
from pathlib import Path

import httpx

from config import Settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("vk_bot")

settings = Settings.from_env()

VK_API = "https://api.vk.com/method"
API_VERSION = "5.131"

history: dict[int, list[dict]] = {}
MAX_HISTORY = 6
levels: dict[int, str] = {}
awaiting_level: set[int] = set()
awaiting_graph: set[int] = set()
awaiting_section: set[int] = set()
busy: set[int] = set()
subject: dict[int, str] = {}
mode: dict[int, str] = {}
quiz: dict[int, dict] = {}
socratic: set[int] = set()
persona: dict[int, str] = {}
roast: set[int] = set()
memes: set[int] = set()

# Ссылка на чат с администратором (для кнопки «Помощь»)
ADMIN_VK = settings.admin_vk.strip()


def admin_chat_url() -> str:
    if ADMIN_VK.isdigit():
        return f"https://vk.com/im?sel={ADMIN_VK}"
    return f"https://vk.me/{ADMIN_VK.lstrip('@')}"


ADMIN_ID = 0
try:
    ADMIN_ID = int(
        settings.admin_id or (settings.admin_vk if settings.admin_vk.isdigit() else "0")
    )
except ValueError:
    ADMIN_ID = 0


def is_admin(peer_id: int) -> bool:
    return bool(ADMIN_ID) and int(peer_id) == ADMIN_ID


# --- Хранение данных (история, лимиты, баны) ---
DATA_FILE = Path(__file__).resolve().parent / "data.json"
FREE_LIMIT = settings.free_limit
_data: dict = {}


def load_data() -> None:
    global _data
    try:
        if DATA_FILE.exists():
            _data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("data.json не прочитан: %s", e)
        _data = {}


def save_data() -> None:
    try:
        DATA_FILE.write_text(
            json.dumps(_data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        log.warning("data.json не сохранён: %s", e)


def user(peer_id: int) -> dict:
    u = _data.setdefault(str(peer_id), {})
    u.setdefault("tasks", [])
    u.setdefault("usage", {"date": "", "count": 0})
    u.setdefault("unlimited", False)
    u.setdefault("banned", False)
    return u


def add_task(peer_id: int, task: str, answer: str) -> None:
    u = user(peer_id)
    u["tasks"].append(
        {
            "task": (task or "")[:300],
            "answer": (answer or "")[:500],
            "ts": int(time.time()),
        }
    )
    if len(u["tasks"]) > 30:
        u["tasks"] = u["tasks"][-30:]
    save_data()


def check_limit(peer_id: int) -> bool:
    if is_admin(peer_id):
        return True
    u = user(peer_id)
    if u.get("unlimited"):
        return True
    today = time.strftime("%Y-%m-%d")
    if u["usage"].get("date") != today:
        u["usage"] = {"date": today, "count": 0}
    limit = int(u.get("limit_override", FREE_LIMIT))
    return u["usage"].get("count", 0) < limit


def bump_usage(peer_id: int) -> None:
    u = user(peer_id)
    today = time.strftime("%Y-%m-%d")
    if u["usage"].get("date") != today:
        u["usage"] = {"date": today, "count": 0}
    u["usage"]["count"] = u["usage"].get("count", 0) + 1
    save_data()


# Запас под лимит VK в 4096 символов на одно сообщение
MAX_VK_LEN = 4000

HELP_TEXT = (
    "Привет! Я решаю домашнее задание 📚\n\n"
    "• Выбери предмет: математика, русский, физика, химия, биология, география.\n"
    "• «🧠 Решить задачу» — пришли задачу текстом или фото, решу пошагово.\n"
    "• «📝 Тест» — мини-тест с мгновенной проверкой.\n"
    "• «📚 Объясни тему» — разбор темы с примерами.\n"
    "• «🎲 Генератор заданий» — тренировочная задача.\n"
    "• «🧩 Помощь с проектом», «📄 Примеры», «✍️ Орфография», «💡 Советы», «⏰ Напоминания».\n\n"
    f"Бесплатно: {FREE_LIMIT} задач в день.\n"
    "«⚙️ Ещё» — история, уровень, помощь, сброс."
)

BTN_PHOTO = "📷 Прислать фото"
BTN_SECTION = "📐 Сечение"
BTN_GRAPH = "📊 График"
BTN_LEVEL = "⭐ Уровень"
BTN_HELP = "❓ Помощь"
BTN_RESET = "🧹 Сбросить"
BTN_HISTORY = "📚 Мои задачи"

HELP_BUTTON = {"action": {"type": "text", "label": BTN_HELP}, "color": "secondary"}

BTN_MORE = "⚙️ Ещё"
BTN_BACK = "⬅️ Меню"

SUBJECTS = [
    ("📐 Математика", "математика"),
    ("📖 Русский язык", "русский язык"),
    ("⚛️ Физика", "физика"),
    ("🧪 Химия", "химия"),
    ("🧬 Биология", "биология"),
    ("🌍 География", "география"),
]

BTN_SOLVE = "🧠 Решить задачу"
BTN_TEST = "📝 Тест"
BTN_EXPLAIN = "📚 Объясни тему"
BTN_GENERATE = "🎲 Генератор заданий"
BTN_PROJECT = "🧩 Помощь с проектом"
BTN_EXAMPLES = "📄 Показать примеры"
BTN_SPELL = "✍️ Проверка орфографии"
BTN_TIPS = "💡 Советы по учёбе"
BTN_REMIND = "⏰ Напоминания"
BTN_CHECK = "🔍 Проверить решение"
BTN_MISTAKES = "📝 Работа над ошибками"
BTN_REF = "👥 Пригласить друга"
BTN_SOCRATIC = "🧑‍🏫 Наставник"

BTN_PERSONA = "🎭 Личность"
BTN_ROAST = "🔥 Роаст"
BTN_MEMES = "😄 Мемы"

PERSONAS = {
    "normal": "",
    "teacher": "Стиль ответа: строгая, но справедливая училка. Требовательно, с нравоучениями.",
    "cat": "Стиль ответа: добрый ленивый кот. Мурлычь, вставляй «мяу», будь милым.",
    "gopnik": "Стиль ответа: гопник-учитель. Сленг, понты, «братан», «чё», «ну ты понял». Можешь материться.",
    "anime": "Стиль ответа: аниме-тян. Эмоционально, «ня», «кавай», восклицания.",
    "robot": "Стиль ответа: робот-терминатор. Коротко, технично, «ТЕРМИНАЦИЯ ЗАДАЧИ», без эмоций.",
}

ROAST_STYLE = (
    "Если ученик ошибся или ответил глупо — жёстко подколи его, можно с матом "
    "(но без оскорблений личности и травли). Пример тона: «серьёзно? это же изи, ты чё, спишь?»."
)

MEME_PHRASES = [
    "я не тупой, я креативный",
    "математика — это когда всё сходится, но не у тебя",
    "домашка сделана на 5. из 100",
    "я не списывал, я консультировался",
    "мозг: включён. руки: не в курсе",
    "учил, но забыл. как всегда",
    "2+2=4, но это не точно",
    "главное — уверенность, а не правильный ответ",
]

SOCRATIC_PROMPT = (
    "Ты — терпеливый наставник. НЕ давай готовое решение. "
    "Веди ученика к ответу наводящими вопросами и маленькими подсказками.\n"
    "Правила:\n"
    "- За один ответ задавай ОДИН вопрос или давай ОДНУ подсказку.\n"
    "- Не пиши готовый ответ, пока ученик сам не придёт к нему.\n"
    "- Если ученик ошибается — мягко укажи на противоречие и задай наводящий вопрос.\n"
    "- Хвали за верные шаги.\n"
    "- Полный ответ показывай только если ученик прямо попросит «покажи ответ».\n"
)


def _btn(label: str, color: str = "secondary") -> dict:
    return {"action": {"type": "text", "label": label}, "color": color}


MAIN_KEYBOARD = {
    "one_time": False,
    "buttons": [
        [_btn(SUBJECTS[0][0]), _btn(SUBJECTS[1][0]), _btn(SUBJECTS[2][0])],
        [_btn(SUBJECTS[3][0]), _btn(SUBJECTS[4][0]), _btn(BTN_MORE)],
        [
            _btn(BTN_SOLVE, "positive"),
            _btn(BTN_SOCRATIC, "positive"),
            _btn(BTN_TEST, "primary"),
        ],
        [_btn(BTN_EXPLAIN), _btn(BTN_GENERATE), _btn(BTN_PROJECT)],
        [_btn(BTN_EXAMPLES), _btn(BTN_SPELL), _btn(BTN_TIPS)],
        [_btn(BTN_REMIND)],
    ],
}

MORE_KEYBOARD = {
    "one_time": False,
    "buttons": [
        [_btn(BTN_PERSONA, "primary"), _btn(BTN_ROAST, "negative"), _btn(BTN_MEMES)],
        [_btn(BTN_CHECK, "positive"), _btn(BTN_MISTAKES, "primary")],
        [_btn(BTN_SECTION), _btn(BTN_GRAPH)],
        [_btn(BTN_HISTORY), _btn(BTN_LEVEL)],
        [_btn(BTN_REF), _btn(BTN_HELP)],
        [_btn(BTN_RESET), _btn(BTN_BACK)],
    ],
}

PERSONA_KEYBOARD = {
    "one_time": True,
    "buttons": [
        [_btn("🙂 Обычный", "secondary"), _btn("👩‍🏫 Училка", "primary")],
        [_btn("🐱 Кот", "secondary"), _btn("🧢 Гопник", "negative")],
        [_btn("🌸 Аниме", "primary"), _btn("🤖 Робот", "secondary")],
        [_btn(BTN_BACK)],
    ],
}

QUIZ_KEYBOARD = {
    "one_time": False,
    "buttons": [
        [
            _btn("1", "primary"),
            _btn("2", "primary"),
            _btn("3", "primary"),
            _btn("4", "primary"),
        ],
    ],
}

BTN_OK = "✅ Понятно"
BTN_SIMPLER = "🤔 Объясни проще"
BTN_SIMILAR = "🔁 Похожая задача"

FOLLOWUP_KEYBOARD = {
    "one_time": False,
    "buttons": [
        [
            {"action": {"type": "text", "label": BTN_OK}, "color": "positive"},
            {"action": {"type": "text", "label": BTN_SIMPLER}, "color": "secondary"},
        ],
        [{"action": {"type": "text", "label": BTN_SIMILAR}, "color": "primary"}],
    ],
}


def system_prompt_for(peer_id: int) -> str:
    base = SOCRATIC_PROMPT if peer_id in socratic else settings.system_prompt
    p = PERSONAS.get(persona.get(peer_id, "normal"), "")
    if p:
        base += "\n\n" + p
    if peer_id in roast:
        base += "\n\n" + ROAST_STYLE
    parts = []
    s = subject.get(peer_id)
    if s:
        parts.append(f"Предмет: {s}.")
    lvl = levels.get(peer_id)
    if lvl:
        parts.append(f"Уровень ученика: {lvl}. Подстраивай объяснение под этот уровень.")
    if parts:
        return base + "\n\n" + "\n".join(parts)
    return base


def get_history(peer_id: int) -> list[dict]:
    if peer_id not in history:
        history[peer_id] = [{"role": "system", "content": system_prompt_for(peer_id)}]
    return history[peer_id]


def set_level(peer_id: int, level: str) -> None:
    levels[peer_id] = level
    if peer_id in history:
        history[peer_id][0]["content"] = system_prompt_for(peer_id)


def push(peer_id: int, role: str, content) -> None:
    h = get_history(peer_id)
    h.append({"role": role, "content": content})
    while len(h) > MAX_HISTORY + 1:
        h.pop(1)


async def vk_call(
    client: httpx.AsyncClient, method: str, post: bool = False, **params
) -> dict:
    params.setdefault("access_token", settings.vk_token)
    params.setdefault("v", API_VERSION)
    url = f"{VK_API}/{method}"
    r = None
    for attempt in range(3):
        try:
            if post:
                r = await client.post(url, data=params)
            else:
                r = await client.get(url, params=params)
            break
        except (
            httpx.ConnectTimeout,
            httpx.ConnectError,
            httpx.ReadTimeout,
            httpx.ReadError,
            httpx.RemoteProtocolError,
        ):
            if attempt == 2:
                raise
            await asyncio.sleep(1 + attempt)
    try:
        data = r.json()
    except Exception:
        raise RuntimeError(
            f"VK {method}: HTTP {r.status_code}, ответ не JSON: {r.text[:200]!r}"
        )
    if "error" in data:
        raise RuntimeError(f"VK error {data['error']['error_code']}: {data['error']['error_msg']}")
    return data.get("response", data)


async def send_message(
    client: httpx.AsyncClient,
    peer_id: int,
    text: str,
    keyboard: dict | None = None,
    attachment: str | None = None,
) -> None:
    params = {
        "peer_id": peer_id,
        "message": text,
        "random_id": random.randint(0, 2**31 - 1),
    }
    if keyboard is not None:
        params["keyboard"] = json.dumps(keyboard, ensure_ascii=False)
    if attachment:
        params["attachment"] = attachment
    await vk_call(client, "messages.send", post=True, **params)


def split_text(text: str, limit: int = MAX_VK_LEN) -> list[str]:
    """Режет длинный текст на части под лимит VK, по возможности по строкам."""
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n")
    if text:
        parts.append(text)
    return parts or [""]


async def send_long(
    client: httpx.AsyncClient, peer_id: int, text: str, keyboard: dict | None = None
) -> None:
    """Отправляет длинное сообщение несколькими частями, чтобы не обрезалось."""
    parts = [p for p in split_text(text) if p.strip()]
    if not parts:
        parts = [""]
    for i, part in enumerate(parts):
        kb = keyboard if i == len(parts) - 1 else None
        await send_message(client, peer_id, part, keyboard=kb)


def prepare_image(data: bytes) -> str:
    """Уменьшает фото и возвращает base64 JPEG. Без Pillow — отправляет как есть."""
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(data))
        img = img.convert("RGB")
        img.thumbnail((1280, 1280))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        log.warning("Уменьшение фото не удалось (%s), отправляю оригинал", e)
        return base64.b64encode(data).decode()


_SUP = {
    "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴",
    "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹", "-": "⁻",
}


def latex_to_text(text: str) -> str:
    """Переводит частую LaTeX-запись в обычный читаемый текст для ВК."""
    text = re.sub(r"\\sqrt\[([^\]]*)\]\{([^{}]*)\}", r"\1√(\2)", text)
    text = re.sub(r"\\sqrt\{([^{}]*)\}", r"√(\1)", text)
    text = re.sub(r"\bsqrt\(([^()]*)\)", r"√(\1)", text)
    text = re.sub(r"\bsqrt\s*([A-Za-z0-9])", r"√\1", text)
    text = re.sub(r"\\frac\{([^{}]*)\}\{([^{}]*)\}", r"(\1)/(\2)", text)
    text = re.sub(r"\^\s*\\circ", "°", text)
    text = re.sub(r"\\vec\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"\\overline\{([^{}]*)\}", r"\1", text)

    def _sup(m):
        s = m.group(1)
        if s and all(ch in _SUP for ch in s):
            return "".join(_SUP[ch] for ch in s)
        return "^" + s

    text = re.sub(r"\^\{([^{}]*)\}", _sup, text)
    text = re.sub(r"_\{([^{}]*)\}", r"_\1", text)

    for cmd, repl in [
        ("\\cdot", "·"),
        ("\\times", "×"),
        ("\\div", "÷"),
        ("\\pm", "±"),
        ("\\circ", "°"),
        ("\\degree", "°"),
        ("\\angle", "∠"),
        ("\\perp", "⊥"),
        ("\\parallel", "∥"),
        ("\\triangle", "△"),
        ("\\alpha", "α"),
        ("\\beta", "β"),
        ("\\gamma", "γ"),
        ("\\delta", "δ"),
        ("\\theta", "θ"),
        ("\\lambda", "λ"),
        ("\\mu", "μ"),
        ("\\pi", "π"),
        ("\\phi", "φ"),
        ("\\omega", "ω"),
        ("\\sigma", "σ"),
        ("\\epsilon", "ε"),
        ("\\leq", "≤"),
        ("\\le", "≤"),
        ("\\geq", "≥"),
        ("\\ge", "≥"),
        ("\\neq", "≠"),
        ("\\ne", "≠"),
        ("\\approx", "≈"),
        ("\\infty", "∞"),
        ("\\in", "∈"),
        ("\\subset", "⊂"),
        ("\\cup", "∪"),
        ("\\cap", "∩"),
        ("\\sum", "Σ"),
        ("\\int", "∫"),
        ("\\rightarrow", "→"),
        ("\\to", "→"),
        ("\\Rightarrow", "⇒"),
        ("\\leftarrow", "←"),
        ("\\leftrightarrow", "↔"),
        ("\\left", ""),
        ("\\right", ""),
        ("\\displaystyle", ""),
        ("\\quad", "  "),
        ("\\qquad", "    "),
        ("\\,", " "),
        ("\\;", " "),
        ("\\!", ""),
    ]:
        text = text.replace(cmd, repl)

    text = text.replace("$$", "").replace("$", "")
    text = re.sub(r"\\[A-Za-z]+", "", text)
    return text


LLM_FALLBACK_MODELS = ["gemini-3.6-flash", "gemini-3.7-flash"]


def strip_old_images(messages: list[dict]) -> list[dict]:
    """Оставляет только самую свежую картинку — чтобы не раздувать запрос."""
    last_img = -1
    for i, m in enumerate(messages):
        if isinstance(m.get("content"), list):
            last_img = i
    out = []
    for i, m in enumerate(messages):
        content = m.get("content")
        if isinstance(content, list) and i != last_img:
            texts = [p.get("text", "") for p in content if p.get("type") == "text"]
            out.append(
                {"role": m["role"], "content": " ".join(t for t in texts if t) or "[фото]"}
            )
        else:
            out.append(m)
    return out


async def ask_llm(client: httpx.AsyncClient, peer_id: int) -> str:
    messages = strip_old_images(get_history(peer_id))

    # Сначала OpenRouter (доступнее из РФ), затем Gemini.
    providers = []
    if settings.fallback_base_url and settings.fallback_model:
        providers.append(
            (
                settings.fallback_base_url,
                settings.fallback_api_key,
                settings.fallback_model,
            )
        )
    for m in [settings.llm_model, *LLM_FALLBACK_MODELS]:
        providers.append((settings.llm_base_url, settings.llm_api_key, m))

    errors: list[str] = []
    for base_url, api_key, model in providers:
        url = base_url.rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        body = {"model": model, "messages": messages, "temperature": 0.5}
        try:
            r = await client.post(
                url,
                headers=headers,
                json=body,
                timeout=httpx.Timeout(90.0, connect=8.0),
            )
            if r.status_code == 200:
                data = r.json()
                message = (data.get("choices") or [{}])[0].get("message") or {}
                content = message.get("content") or ""
                if content.strip():
                    log.info("LLM %s ответил (%d симв.)", model, len(content))
                    return latex_to_text(content)
                errors.append(f"{model}: пустой ответ")
                continue
            errors.append(f"{model}: HTTP {r.status_code} {r.text[:120]}")
            log.warning("LLM %s -> %s", model, r.status_code)
        except Exception as e:
            errors.append(f"{model}: {type(e).__name__}")
            log.warning("LLM %s -> %s", model, type(e).__name__)
    detail = " | ".join(errors[-6:])
    log.error("Все модели LLM недоступны: %s", detail)
    return (
        "⚠️ Не удалось получить ответ модели. Попробуйте ещё раз.\n"
        f"(подробно: {detail})"
    )


async def stt_gemini(client: httpx.AsyncClient, audio_bytes: bytes) -> str | None:
    """Распознаёт речь через Gemini (native API)."""
    audio_b64 = base64.b64encode(audio_bytes).decode()
    candidates = [
        (settings.llm_base_url, settings.llm_api_key, settings.llm_model),
        (settings.fallback_base_url, settings.fallback_api_key, settings.fallback_model),
    ]
    for raw_base, key, model in candidates:
        if not key or "googleapis" not in (raw_base or ""):
            continue
        base = raw_base.rstrip("/")
        if base.endswith("/openai"):
            base = base[: -len("/openai")]
        url = f"{base}/models/{model}:generateContent?key={key}"
        payload = {
            "contents": [
                {
                    "parts": [
                        {
                            "text": "Расшифруй это голосовое сообщение в текст. "
                            "Верни только распознанный текст, без пояснений."
                        },
                        {"inline_data": {"mime_type": "audio/ogg", "data": audio_b64}},
                    ]
                }
            ]
        }
        try:
            r = await client.post(
                url, json=payload, timeout=httpx.Timeout(90.0, connect=8.0)
            )
            if r.status_code != 200:
                log.warning("STT: HTTP %s %s", r.status_code, r.text[:200])
                continue
            data = r.json()
            cands = data.get("candidates") or []
            if not cands:
                continue
            parts = cands[0].get("content", {}).get("parts") or []
            text = " ".join(p.get("text", "") for p in parts).strip()
            if text:
                return text
        except Exception as e:
            log.warning("STT: %s", e)
    return None


def photo_url(attachments: list) -> str | None:
    """Возвращает URL самого большого фото из вложений."""
    for a in attachments or []:
        if a.get("type") == "photo":
            sizes = a["photo"].get("sizes", [])
            if sizes:
                best = max(sizes, key=lambda s: s.get("width", 0) * s.get("height", 0))
                return best.get("url")
    return None


def audio_url(attachments: list) -> str | None:
    """Возвращает URL голосового сообщения."""
    for a in attachments or []:
        if a.get("type") == "audio_message":
            am = a.get("audio_message") or {}
            return am.get("link_mp3") or am.get("link_ogg")
    return None


async def upload_graph(
    client: httpx.AsyncClient, peer_id: int, xs: list, ys: list, title: str
) -> bool:
    """Строит график matplotlib и отправляет его в чат."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(6, 4), dpi=110)
        ax.plot(xs, ys, color="#2b6cb0", linewidth=2)
        ax.axhline(0, color="#888", linewidth=0.8)
        ax.axvline(0, color="#888", linewidth=0.8)
        ax.grid(True, alpha=0.3)
        ax.set_title(title[:80])
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight")
        plt.close(fig)
        png = buf.getvalue()
    except Exception as e:
        log.warning("График не построен: %s", e)
        return False

    return await _upload_and_send(client, peer_id, png, "📊 Вот график:")


async def plot_task(client: httpx.AsyncClient, peer_id: int, expr: str) -> None:
    """Просит LLM таблицу точек и строит по ней график."""
    await send_message(client, peer_id, "⏳ Строю график…")
    prompt = (
        f"Функция/задача: {expr}\n\n"
        "Верни ТОЛЬКО таблицу точек для графика: 40 строк вида 'x,y' "
        "(x от -10 до 10), без текста и пояснений."
    )
    push(peer_id, "user", prompt)
    raw = await ask_llm(client, peer_id)
    push(peer_id, "assistant", raw)
    pts = []
    for line in raw.splitlines():
        line = line.replace(";", ",").strip()
        if "," in line:
            a, b = line.split(",", 1)
            try:
                pts.append((float(a.strip()), float(b.strip())))
            except ValueError:
                continue
    if len(pts) < 3:
        await send_message(
            client,
            peer_id,
            "Не понял функцию. Напишите точнее, например: y = x^2 - 4x + 3",
            keyboard=MAIN_KEYBOARD,
        )
        return
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    if not await upload_graph(client, peer_id, xs, ys, expr):
        await send_message(
            client, peer_id, "Не удалось отправить картинку графика.", keyboard=MAIN_KEYBOARD
        )


async def send_meme(client: httpx.AsyncClient, peer_id: int) -> None:
    try:
        from PIL import Image, ImageDraw, ImageFont

        text = random.choice(MEME_PHRASES)
        img = Image.new("RGB", (600, 320), (25, 25, 35))
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.load_default(size=28)
        except Exception:
            font = ImageFont.load_default()
        draw.multiline_text((30, 110), text, fill=(255, 255, 255), font=font, spacing=10)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        await _upload_and_send(
            client, peer_id, buf.getvalue(), "😄", filename="meme.png", mime="image/png"
        )
    except Exception as e:
        log.warning("Мем: %s", e)


async def run_llm_task(
    client: httpx.AsyncClient,
    peer_id: int,
    prompt: str,
    keyboard: dict | None,
    ack: str = "⏳ Решаю задачу…",
) -> None:
    """Решает задачу с анти-спамом: один запрос за раз."""
    if peer_id in busy:
        await send_message(client, peer_id, "⏳ Секунду, я ещё решаю предыдущую задачу…")
        return
    busy.add(peer_id)
    try:
        await send_message(client, peer_id, ack)
        push(peer_id, "user", prompt)
        reply = await ask_llm(client, peer_id)
        push(peer_id, "assistant", reply)
        bump_usage(peer_id)
        add_task(peer_id, prompt, reply)
        await send_long(client, peer_id, reply, keyboard=keyboard)
        if peer_id in memes:
            await send_meme(client, peer_id)
    finally:
        busy.discard(peer_id)


async def _upload_and_send(
    client: httpx.AsyncClient,
    peer_id: int,
    data: bytes,
    text: str,
    filename: str = "img.png",
    mime: str = "image/png",
) -> bool:
    try:
        up = await vk_call(client, "photos.getMessagesUploadServer", peer_id=peer_id)
        r = await client.post(
            up["upload_url"], files={"photo": (filename, data, mime)}
        )
        data = r.json()
        if not data.get("photo"):
            log.warning("Загрузка фото вернула без photo: %s", str(data)[:200])
            return False
        saved = await vk_call(
            client,
            "photos.saveMessagesPhoto",
            post=True,
            photo=data["photo"],
            server=data["server"],
            hash=data["hash"],
        )
        p = saved[0]
        att = f"photo{p['owner_id']}_{p['id']}"
        await send_message(
            client, peer_id, text, attachment=att, keyboard=FOLLOWUP_KEYBOARD
        )
        return True
    except Exception as e:
        log.warning("Загрузка картинки не удалась: %s", e)
        return False


async def draw_section(
    client: httpx.AsyncClient, peer_id: int, pts: list, title: str = "Сечение"
) -> bool:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        xs = [p[0] for p in pts] + [pts[0][0]]
        ys = [p[1] for p in pts] + [pts[0][1]]
        fig, ax = plt.subplots(figsize=(5, 5), dpi=110)
        ax.plot(xs, ys, color="#2b6cb0", linewidth=2)
        ax.fill(xs, ys, color="#2b6cb0", alpha=0.15)
        ax.set_aspect("equal", adjustable="datalim")
        ax.grid(True, alpha=0.3)
        ax.set_title(title[:60])
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight")
        plt.close(fig)
        png = buf.getvalue()
    except Exception as e:
        log.warning("Сечение не нарисовано: %s", e)
        return False
    return await _upload_and_send(client, peer_id, png, "📐 Форма сечения:")


def _parse_point3(line: str):
    parts = line.replace(";", ",").replace(" ", ",").split(",")
    nums = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        try:
            nums.append(float(p))
        except ValueError:
            return None
    if len(nums) >= 3:
        return (nums[0], nums[1], nums[2])
    return None


def _parse_section_data(raw: str):
    vertices, edges, section = [], [], []
    solution = raw
    marker = "ВЕРШИНЫ:"
    if marker in raw:
        solution = raw.split(marker, 1)[0]
        tail = raw.split(marker, 1)[1]
        groups = {"vertices": [], "edges": [], "section": []}
        cur = "vertices"
        for line in tail.splitlines():
            s = line.strip()
            if s.startswith("РЁБРА:"):
                cur = "edges"
                continue
            if s.startswith("СЕЧЕНИЕ:"):
                cur = "section"
                continue
            if not s:
                continue
            groups[cur].append(s)
        for line in groups["vertices"]:
            p = _parse_point3(line)
            if p:
                vertices.append(p)
        for line in groups["edges"]:
            parts = line.replace(";", ",").split(",")
            if len(parts) == 2:
                try:
                    edges.append((int(float(parts[0])), int(float(parts[1]))))
                except ValueError:
                    pass
        for line in groups["section"]:
            p = _parse_point3(line)
            if p:
                section.append(p)
    return vertices, edges, section, solution


async def draw_section_3d(
    client: httpx.AsyncClient,
    peer_id: int,
    vertices: list,
    edges: list,
    section: list,
) -> bool:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from mpl_toolkits.mplot3d.art3d import Poly3DCollection

        fig = plt.figure(figsize=(6, 6), dpi=110)
        ax = fig.add_subplot(111, projection="3d")

        for a, b in edges:
            if 0 <= a < len(vertices) and 0 <= b < len(vertices):
                pa, pb = vertices[a], vertices[b]
                ax.plot(
                    [pa[0], pb[0]],
                    [pa[1], pb[1]],
                    [pa[2], pb[2]],
                    color="#333",
                    linewidth=1,
                )
        for v in vertices:
            ax.scatter([v[0]], [v[1]], [v[2]], color="#333", s=12)
        if len(section) >= 3:
            poly = Poly3DCollection(
                [section], alpha=0.45, facecolor="#2b6cb0", edgecolor="#1a4f8a"
            )
            ax.add_collection3d(poly)

        ax.set_box_aspect((1, 1, 1))
        ax.set_title("Сечение (3D)")
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight")
        plt.close(fig)
        png = buf.getvalue()
    except Exception as e:
        log.warning("3D-сечение не нарисовано: %s", e)
        return False
    return await _upload_and_send(client, peer_id, png, "📐 Сечение (3D):")


def draw_on_image(
    data: bytes, section: list, points: list, helpers: list
) -> bytes | None:
    """Рисует на фото: вспомогательные линии (синие), сечение (красное), точки с подписями."""
    try:
        from PIL import Image, ImageDraw

        img = Image.open(io.BytesIO(data)).convert("RGB")
        img.thumbnail((1600, 1600))
        w, h = img.size
        draw = ImageDraw.Draw(img)
        lw = max(3, w // 250)
        for x1, y1, x2, y2 in helpers:
            draw.line(
                [(x1 * w, y1 * h), (x2 * w, y2 * h)],
                fill=(30, 120, 255),
                width=max(2, lw - 1),
            )
        if len(section) >= 2:
            coords = [(x * w, y * h) for x, y in section]
            draw.line(coords + [coords[0]], fill=(255, 0, 0), width=lw)
        r = max(4, w // 150)
        for name, x, y in points:
            px, py = x * w, y * h
            draw.ellipse([px - r, py - r, px + r, py + r], fill=(0, 160, 0))
            if name:
                draw.text((px + r + 2, py - r), name, fill=(0, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=88)
        return buf.getvalue()
    except Exception as e:
        log.warning("Не удалось дорисовать на фото: %s", e)
        return None


def _parse_image_lines(raw: str):
    solution = raw
    section, points, helpers = [], [], []
    marker = "ЛИНИИ СЕЧЕНИЯ"
    if marker in raw:
        solution, tail = raw.split(marker, 1)
        groups = {"section": [], "points": [], "helpers": []}
        cur = "section"
        for line in tail.splitlines():
            s = line.strip()
            if s.startswith("ТОЧКИ"):
                cur = "points"
                continue
            if s.startswith("ВСПОМОГАТЕЛЬНЫЕ"):
                cur = "helpers"
                continue
            if not s:
                continue
            groups[cur].append(s)

        def parts_float(line):
            out = []
            for p in line.replace(";", ",").split(","):
                p = p.strip()
                if not p:
                    continue
                try:
                    out.append(float(p))
                except ValueError:
                    out.append(None)
            return out

        for line in groups["section"]:
            v = parts_float(line)
            if len(v) >= 2 and v[0] is not None and v[1] is not None:
                section.append((v[0], v[1]))
        for line in groups["points"]:
            parts = [p.strip() for p in line.replace(";", ",").split(",") if p.strip()]
            name = ""
            if len(parts) >= 3:
                try:
                    float(parts[0])
                except ValueError:
                    name = parts[0]
                    parts = parts[1:]
            if len(parts) >= 2:
                try:
                    points.append((name, float(parts[0]), float(parts[1])))
                except ValueError:
                    pass
        for line in groups["helpers"]:
            v = [x for x in parts_float(line) if x is not None]
            if len(v) >= 4:
                helpers.append((v[0], v[1], v[2], v[3]))
    return solution, section, points, helpers


def _parse_helpers(raw: str):
    out = []
    for line in raw.splitlines():
        parts = [p.strip() for p in line.replace(";", ",").split(",") if p.strip()]
        try:
            nums = [float(p) for p in parts]
        except ValueError:
            continue
        if len(nums) >= 4:
            out.append((nums[0], nums[1], nums[2], nums[3]))
    return out


async def section_task(
    client: httpx.AsyncClient, peer_id: int, text: str, attachments: list
) -> None:
    if peer_id in busy:
        await send_message(client, peer_id, "⏳ Секунду, я ещё решаю предыдущую задачу…")
        return
    busy.add(peer_id)
    try:
        photo = photo_url(attachments)
        if photo:
            await send_message(client, peer_id, "⏳ Решаю и дорисовываю сечение…")
            r = await client.get(
                photo, headers={"User-Agent": "Mozilla/5.0"}, timeout=60
            )
            if r.status_code != 200 or not r.content:
                raise RuntimeError(f"фото не скачалось (HTTP {r.status_code})")
            photo_bytes = r.content
            b64 = prepare_image(photo_bytes)
            instruction = (
                "Реши задачу на сечение по этому фото. ОБЯЗАТЕЛЬНО в конце добавь "
                "все три блока (даже если пусто — пиши заголовок):\n"
                "ЛИНИИ СЕЧЕНИЯ:\n"
                "x,y\n"
                "x,y\n"
                "ТОЧКИ:\n"
                "Имя,x,y\n"
                "Имя,x,y\n"
                "ВСПОМОГАТЕЛЬНЫЕ:\n"
                "x1,y1,x2,y2\n"
                "x1,y1,x2,y2\n"
                "Координаты — в долях от ширины/высоты фото (0..1, 0,0 — левый верхний угол). "
                "ВСПОМОГАТЕЛЬНЫЕ — это линии, которыми ты находил неизвестные точки "
                "(соединял известные точки, продлевал рёбра и т.п.), 2–6 штук. "
                "Только числа и подписи через запятую, без текста внутри блоков."
            )
            content = [
                {"type": "text", "text": instruction + "\n\n" + (text or "")},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                },
            ]
            push(peer_id, "user", content)
            raw = await ask_llm(client, peer_id)
            push(peer_id, "assistant", raw)
            bump_usage(peer_id)
            add_task(peer_id, text or "фото сечения", raw)
            solution, section, points, helpers = _parse_image_lines(raw)
            log.info(
                "Сечение: section=%d points=%d helpers=%d",
                len(section),
                len(points),
                len(helpers),
            )
            await send_long(client, peer_id, solution.strip() or raw)
            if not helpers:
                push(
                    peer_id,
                    "user",
                    "Теперь укажи ТОЛЬКО вспомогательные линии построения сечения "
                    "(как находил неизвестные точки): строки 'x1,y1,x2,y2' в долях 0..1 "
                    "от ширины/высоты фото, 2–6 линий, без текста.",
                )
                raw2 = await ask_llm(client, peer_id)
                helpers = _parse_helpers(raw2)
                log.info("Сечение: fallback helpers=%d", len(helpers))
                if history.get(peer_id):
                    history[peer_id].pop()
            if len(section) >= 2 or points:
                annotated = draw_on_image(photo_bytes, section, points, helpers)
                if annotated:
                    await _upload_and_send(
                        client,
                        peer_id,
                        annotated,
                        "📐 Сечение на фото:",
                        filename="photo.jpg",
                        mime="image/jpeg",
                    )
            return

        instruction = (
            "Реши задачу на сечение пошагово. В конце добавь блок данных для 3D-рисунка "
            "строго в таком формате:\n"
            "ВЕРШИНЫ:\n"
            "x,y,z\n"
            "x,y,z\n"
            "РЁБРА:\n"
            "0,1\n"
            "1,2\n"
            "СЕЧЕНИЕ:\n"
            "x,y,z\n"
            "x,y,z\n"
            "Требования к блоку:\n"
            "- ВЕРШИНЫ — вершины фигуры по порядку, только числа через запятую (без индексов).\n"
            "- Используй стандартные координаты (куб от -1 до 1 или от 0 до 1).\n"
            "- Каждая точка СЕЧЕНИЯ ОБЯЗАНА лежать на ребре фигуры — проверь по координатам.\n"
            "- РЁБРА — пары индексов вершин.\n"
            "- Реши задачу, вычисли точки, затем запиши блок. Без текста внутри блока."
        )
        await send_message(client, peer_id, "⏳ Решаю и рисую сечение…")
        push(peer_id, "user", f"{instruction}\n\nУсловие: {text}")
        raw = await ask_llm(client, peer_id)
        push(peer_id, "assistant", raw)
        bump_usage(peer_id)
        add_task(peer_id, text, raw)
        vertices, edges, section, solution = _parse_section_data(raw)
        await send_long(client, peer_id, solution.strip() or raw)
        if len(vertices) >= 3 and len(edges) >= 3 and len(section) >= 3:
            await draw_section_3d(client, peer_id, vertices, edges, section)
    except Exception as e:
        log.exception("Ошибка сечения")
        await send_message(
            client,
            peer_id,
            f"Не удалось разобрать сечение ({type(e).__name__}): {e}",
            keyboard=MAIN_KEYBOARD,
        )
    finally:
        busy.discard(peer_id)


async def admin_command(client: httpx.AsyncClient, peer_id: int, text: str) -> bool:
    low = text.lower()
    parts = text.split()
    if low.startswith("/stats"):
        today = time.strftime("%Y-%m-%d")
        users = len(_data)
        total = sum(len(v.get("tasks", [])) for v in _data.values())
        today_tasks = sum(
            v.get("usage", {}).get("count", 0)
            for v in _data.values()
            if v.get("usage", {}).get("date") == today
        )
        banned = sum(1 for v in _data.values() if v.get("banned"))
        await send_message(
            client,
            peer_id,
            f"👥 Пользователей: {users}\n"
            f"📝 Задач всего: {total}\n"
            f"📅 Задач сегодня: {today_tasks}\n"
            f"🚫 Забанено: {banned}\n"
            f"👑 Твой ID: {ADMIN_ID}",
            keyboard=MAIN_KEYBOARD,
        )
        return True
    if low.startswith("/broadcast"):
        msg = text[len("/broadcast"):].strip()
        if not msg:
            await send_message(client, peer_id, "Использование: /broadcast текст")
            return True
        sent = 0
        for uid in list(_data.keys()):
            try:
                await send_message(client, int(uid), "📢 " + msg)
                sent += 1
            except Exception:
                pass
        await send_message(client, peer_id, f"Разослано: {sent}")
        return True
    if low.startswith("/users"):
        lines = []
        for uid, v in list(_data.items())[:30]:
            mark = " 🚫" if v.get("banned") else ""
            inf = " ∞" if v.get("unlimited") else ""
            lines.append(f"{uid}: задач {len(v.get('tasks', []))}{mark}{inf}")
        await send_message(
            client, peer_id, "Пользователи:\n" + ("\n".join(lines) or "пусто")
        )
        return True
    if len(parts) >= 2 and parts[0].lower() in ("/ban", "/unban", "/grant", "/revoke"):
        try:
            uid = int(parts[1])
        except ValueError:
            await send_message(client, peer_id, "Нужен числовой ID.")
            return True
        u = user(uid)
        cmd = parts[0].lower()
        if cmd == "/ban":
            u["banned"] = True
        elif cmd == "/unban":
            u["banned"] = False
        elif cmd == "/grant":
            u["unlimited"] = True
        elif cmd == "/revoke":
            u["unlimited"] = False
        save_data()
        await send_message(client, peer_id, f"Готово: {cmd} {uid}", keyboard=MAIN_KEYBOARD)
        return True
    if len(parts) >= 3 and parts[0].lower() == "/limit":
        try:
            uid, n = int(parts[1]), int(parts[2])
        except ValueError:
            await send_message(client, peer_id, "Использование: /limit <id> <n>")
            return True
        user(uid)["limit_override"] = n
        save_data()
        await send_message(client, peer_id, f"Лимит {uid}: {n}", keyboard=MAIN_KEYBOARD)
        return True
    return False


async def start_quiz(client: httpx.AsyncClient, peer_id: int) -> None:
    subj = subject.get(peer_id, "общие знания")
    await send_message(client, peer_id, "⏳ Готовлю тест…")
    push(
        peer_id,
        "user",
        f"Составь 5 тестовых вопросов по предмету «{subj}» с 4 вариантами ответа "
        "и укажи правильный. Формат строго: ВОПРОС|A|B|C|D|N, где N — номер "
        "правильного ответа (1-4). По одной строке на вопрос, без текста вокруг.",
    )
    raw = await ask_llm(client, peer_id)
    questions = []
    for line in raw.splitlines():
        s = line.strip()
        if s.count("|") >= 5:
            parts = [p.strip() for p in s.split("|")]
            if len(parts) >= 6:
                questions.append({"q": parts[0], "opts": parts[1:5], "ans": parts[5]})
    if not questions:
        await send_message(
            client,
            peer_id,
            "Не удалось составить тест. Попробуйте ещё раз.",
            keyboard=MAIN_KEYBOARD,
        )
        return
    quiz[peer_id] = {"questions": questions[:5], "idx": 0, "score": 0}
    await send_quiz_question(client, peer_id)


async def send_quiz_question(client: httpx.AsyncClient, peer_id: int) -> None:
    q = quiz.get(peer_id)
    if not q:
        return
    if q["idx"] >= len(q["questions"]):
        score, total = q["score"], len(q["questions"])
        quiz.pop(peer_id, None)
        await send_message(
            client,
            peer_id,
            f"🏁 Тест завершён! Правильно: {score} из {total}.",
            keyboard=MAIN_KEYBOARD,
        )
        return
    item = q["questions"][q["idx"]]
    body = f"Вопрос {q['idx'] + 1}/{len(q['questions'])}\n{item['q']}\n\n"
    body += "\n".join(f"{i + 1}. {o}" for i, o in enumerate(item["opts"]))
    body += "\n\nОтветьте номером (1-4)."
    await send_message(client, peer_id, body, keyboard=QUIZ_KEYBOARD)


async def quiz_answer(client: httpx.AsyncClient, peer_id: int, answer: str) -> None:
    q = quiz.get(peer_id)
    if not q:
        return
    item = q["questions"][q["idx"]]
    correct = str(item["ans"]).strip() == answer
    if correct:
        q["score"] += 1
    q["idx"] += 1
    if correct:
        await send_message(client, peer_id, "✅ Верно!")
    else:
        try:
            right = item["opts"][int(item["ans"]) - 1]
        except Exception:
            right = item["ans"]
        u = user(peer_id)
        u.setdefault("mistakes", []).append(
            {"q": item["q"], "opts": item["opts"], "ans": item["ans"]}
        )
        u["mistakes"] = u["mistakes"][-10:]
        save_data()
        await send_message(
            client, peer_id, f"❌ Неверно. Правильный ответ: {item['ans']}) {right}"
        )
    await send_quiz_question(client, peer_id)


def add_reminder(peer_id: int, text: str) -> bool:
    m = re.search(r"(\d{1,2})\.(\d{1,2})\s+(\d{1,2}):(\d{2})\s+(.+)", text)
    if not m:
        return False
    day, month, hour, minute, body = m.groups()
    year = time.localtime().tm_year
    try:
        ts = time.mktime(
            (year, int(month), int(day), int(hour), int(minute), 0, 0, 0, -1)
        )
    except Exception:
        return False
    if ts < time.time():
        ts = time.mktime(
            (year + 1, int(month), int(day), int(hour), int(minute), 0, 0, 0, -1)
        )
    u = user(peer_id)
    u.setdefault("reminders", []).append(
        {"ts": int(ts), "text": body.strip(), "sent": False}
    )
    save_data()
    return True


async def check_reminders(client: httpx.AsyncClient) -> None:
    now = time.time()
    changed = False
    for uid, u in list(_data.items()):
        for r in u.get("reminders", []):
            if not r.get("sent") and r.get("ts", 0) <= now:
                try:
                    await send_message(client, int(uid), f"⏰ Напоминание: {r['text']}")
                    r["sent"] = True
                    changed = True
                except Exception:
                    pass
    if changed:
        save_data()


async def handle_update(client: httpx.AsyncClient, update: dict) -> None:
    if update.get("type") != "message_new":
        return
    msg = update.get("object", {}).get("message", {})
    if msg.get("out"):
        return

    peer_id = msg.get("peer_id")
    text = (msg.get("text") or "").strip()
    attachments = msg.get("attachments") or []
    log.info(
        "message_new: peer=%s text=%r attachments=%s",
        peer_id,
        text[:100],
        [a.get("type") for a in attachments],
    )

    if is_admin(peer_id) and text.startswith("/"):
        if await admin_command(client, peer_id, text):
            return
    if user(peer_id).get("banned") and not is_admin(peer_id):
        return

    m_ref = re.match(r"/ref\s+(\d+)", text, re.I)
    if m_ref:
        ref_id = int(m_ref.group(1))
        u = user(peer_id)
        if ref_id == peer_id or str(ref_id) not in _data:
            await send_message(
                client,
                peer_id,
                "Код не найден. Проверь ID друга.",
                keyboard=MAIN_KEYBOARD,
            )
            return
        if u.get("ref_by"):
            await send_message(
                client,
                peer_id,
                "Ты уже использовал реферальный код.",
                keyboard=MAIN_KEYBOARD,
            )
            return
        u["ref_by"] = ref_id
        u["limit_override"] = int(u.get("limit_override", FREE_LIMIT)) + 3
        ru = user(ref_id)
        ru["limit_override"] = int(ru.get("limit_override", FREE_LIMIT)) + 5
        save_data()
        await send_message(
            client,
            peer_id,
            "🎁 Бонус за приглашение: +3 задачи! Другу — +5.",
            keyboard=MAIN_KEYBOARD,
        )
        try:
            await send_message(
                client, ref_id, "🎉 По твоей ссылке пришёл друг! +5 задач к лимиту."
            )
        except Exception:
            pass
        return

    low = text.lower()

    # Тест: ответы 1-4
    if peer_id in quiz and text in ("1", "2", "3", "4"):
        await quiz_answer(client, peer_id, text)
        return

    # Выбор предмета
    for label, subj in SUBJECTS:
        if low == label.lower():
            subject[peer_id] = subj
            mode.pop(peer_id, None)
            history.pop(peer_id, None)
            await send_message(
                client,
                peer_id,
                f"Предмет: {subj}. Пришлите задачу или выберите действие.",
                keyboard=MAIN_KEYBOARD,
            )
            return

    if low == BTN_MORE.lower():
        await send_message(client, peer_id, "Дополнительно:", keyboard=MORE_KEYBOARD)
        return
    if low == BTN_BACK.lower():
        mode.pop(peer_id, None)
        await send_message(client, peer_id, "Главное меню.", keyboard=MAIN_KEYBOARD)
        return
    if low == BTN_SOLVE.lower():
        mode[peer_id] = "solve"
        await send_message(
            client,
            peer_id,
            f"Пришлите задачу по предмету «{subject.get(peer_id, 'любой')}» текстом или фото.",
            keyboard=MAIN_KEYBOARD,
        )
        return
    if low == BTN_TEST.lower():
        await start_quiz(client, peer_id)
        return
    if low == BTN_EXPLAIN.lower():
        mode[peer_id] = "explain"
        await send_message(
            client,
            peer_id,
            "Какую тему объяснить? Напишите название.",
            keyboard=MAIN_KEYBOARD,
        )
        return
    if low == BTN_GENERATE.lower():
        mode[peer_id] = "generate"
        await send_message(
            client,
            peer_id,
            "По какой теме сгенерировать задание?",
            keyboard=MAIN_KEYBOARD,
        )
        return
    if low == BTN_PROJECT.lower():
        mode[peer_id] = "project"
        await send_message(
            client,
            peer_id,
            "Опишите проект: тема, что уже сделано, что нужно. Предложу структуру и идеи.",
            keyboard=MAIN_KEYBOARD,
        )
        return
    if low == BTN_EXAMPLES.lower():
        mode[peer_id] = "examples"
        await send_message(
            client,
            peer_id,
            "По какой теме показать примеры решения?",
            keyboard=MAIN_KEYBOARD,
        )
        return
    if low == BTN_SPELL.lower():
        mode[peer_id] = "spell"
        await send_message(
            client,
            peer_id,
            "Пришлите текст — проверю орфографию и пунктуацию.",
            keyboard=MAIN_KEYBOARD,
        )
        return
    if low == BTN_TIPS.lower():
        await run_llm_task(
            client,
            peer_id,
            "Дай 7 практичных советов по учёбе и выполнению домашних заданий. Кратко, по делу.",
            MAIN_KEYBOARD,
            ack="⏳ Секунду, готовлю советы…",
        )
        return
    if low == BTN_REMIND.lower():
        mode[peer_id] = "remind"
        await send_message(
            client,
            peer_id,
            "Напишите напоминание в формате: ДД.ММ ЧЧ:ММ текст.\n"
            "Например: 15.11 18:00 контрольная по физике",
            keyboard=MAIN_KEYBOARD,
        )
        return
    if low == BTN_CHECK.lower():
        mode[peer_id] = "check"
        await send_message(
            client,
            peer_id,
            "Пришлите своё решение (текстом или фото) — проверю и укажу ошибки.",
            keyboard=MAIN_KEYBOARD,
        )
        return
    if low == BTN_MISTAKES.lower():
        mistakes = user(peer_id).get("mistakes", [])
        if not mistakes:
            await send_message(
                client,
                peer_id,
                "Пока нет ошибок из тестов — всё отлично!",
                keyboard=MAIN_KEYBOARD,
            )
            return
        lines = "\n".join(f"- {m['q']}" for m in mistakes[-5:])
        await run_llm_task(
            client,
            peer_id,
            "Ученик ошибался в этих вопросах:\n"
            + lines
            + "\n\nОбъясни правильные решения и дай 3 похожих задания на отработку.",
            MAIN_KEYBOARD,
            ack="⏳ Готовлю работу над ошибками…",
        )
        return
    if low == BTN_REF.lower():
        link = f"https://vk.me/club{settings.vk_group_id}?ref={peer_id}"
        await send_message(
            client,
            peer_id,
            "👥 Пригласи друга и получи бонусные задачи!\n\n"
            f"Твой код: {peer_id}\n"
            f"Ссылка: {link}\n\n"
            f"Пусть друг напишет боту: /ref {peer_id}\n"
            "Тебе +5 задач, другу +3.",
            keyboard=MAIN_KEYBOARD,
        )
        return
    if low == BTN_SOCRATIC.lower():
        socratic.add(peer_id)
        history.pop(peer_id, None)
        await send_message(
            client,
            peer_id,
            "🧑‍🏫 Режим наставника включён: я поведу тебя вопросами, а не дам готовый ответ.\n"
            "Пришли задачу. Напиши «стоп», чтобы выйти.",
            keyboard=MAIN_KEYBOARD,
        )
        return
    if low in ("стоп", "stop", "выйти"):
        socratic.discard(peer_id)
        history.pop(peer_id, None)
        await send_message(
            client, peer_id, "Режим наставника выключен.", keyboard=MAIN_KEYBOARD
        )
        return
    if low == BTN_PERSONA.lower():
        await send_message(
            client, peer_id, "🎭 Выбери личность бота:", keyboard=PERSONA_KEYBOARD
        )
        return
    persona_map = {
        "🙂 обычный": "normal",
        "👩‍🏫 училка": "teacher",
        "🐱 кот": "cat",
        "🧢 гопник": "gopnik",
        "🌸 аниме": "anime",
        "🤖 робот": "robot",
    }
    if low in persona_map:
        persona[peer_id] = persona_map[low]
        history.pop(peer_id, None)
        await send_message(client, peer_id, f"Личность: {text}.", keyboard=MAIN_KEYBOARD)
        return
    if low == BTN_ROAST.lower():
        if peer_id in roast:
            roast.discard(peer_id)
            history.pop(peer_id, None)
            await send_message(
                client, peer_id, "🔥 Роаст выключен.", keyboard=MAIN_KEYBOARD
            )
        else:
            roast.add(peer_id)
            history.pop(peer_id, None)
            await send_message(
                client,
                peer_id,
                "🔥 Роаст включён: буду подкалывать за ошибки.",
                keyboard=MAIN_KEYBOARD,
            )
        return
    if low == BTN_MEMES.lower():
        if peer_id in memes:
            memes.discard(peer_id)
            await send_message(
                client, peer_id, "😄 Мемы выключены.", keyboard=MAIN_KEYBOARD
            )
        else:
            memes.add(peer_id)
            await send_message(
                client,
                peer_id,
                "😄 Мемы включены: после ответа буду кидать мем.",
                keyboard=MAIN_KEYBOARD,
            )
        return

    # Ожидание ввода для инструментов
    if mode.get(peer_id) in ("explain", "generate", "project", "examples", "spell") and text:
        m = mode.pop(peer_id)
        subj = subject.get(peer_id, "")
        prefix = f"Предмет: {subj}. " if subj else ""
        prompts = {
            "explain": f"Объясни тему «{text}»: суть, ключевые правила/формулы, 1–2 примера.",
            "generate": f"Придумай одно задание по теме «{text}» и реши его пошагово.",
            "project": f"Помоги структурировать проект. Описание: {text}. Дай план и идеи.",
            "examples": f"Покажи 2 примера решённых задач по теме «{text}» пошагово.",
            "spell": f"Проверь орфографию и пунктуацию, перечисли ошибки и дай исправленный текст:\n\n{text}",
        }
        await run_llm_task(
            client, peer_id, prefix + prompts[m], MAIN_KEYBOARD, ack="⏳ Секунду…"
        )
        return
    if mode.get(peer_id) == "remind" and text:
        mode.pop(peer_id, None)
        if add_reminder(peer_id, text):
            await send_message(
                client, peer_id, "⏰ Напоминание сохранено.", keyboard=MAIN_KEYBOARD
            )
        else:
            await send_message(
                client,
                peer_id,
                "Не понял дату. Формат: ДД.ММ ЧЧ:ММ текст (например: 15.11 18:00 контрольная).",
                keyboard=MAIN_KEYBOARD,
            )
        return
    if mode.get(peer_id) == "check" and text:
        mode.pop(peer_id, None)
        await run_llm_task(
            client,
            peer_id,
            "Проверь решение ученика: найди ошибки, объясни, где неверно, и покажи "
            "правильный ход. Если всё верно — подтверди.\n\nРешение ученика:\n" + text,
            FOLLOWUP_KEYBOARD,
            ack="⏳ Проверяю решение…",
        )
        return

    if low in ("начать", "start", "помощь", "help", "привет", "/start", BTN_HELP.lower()):
        text_out = HELP_TEXT
        if ADMIN_VK:
            text_out += f"\n\nВопросы администратору: {admin_chat_url()}"
        await send_message(client, peer_id, text_out, keyboard=MAIN_KEYBOARD)
        return
    if low in ("сброс", "/reset", BTN_RESET.lower()):
        history.pop(peer_id, None)
        levels.pop(peer_id, None)
        awaiting_level.discard(peer_id)
        await send_message(
            client,
            peer_id,
            "История и уровень очищены. Пришлите новую задачу.",
            keyboard=MAIN_KEYBOARD,
        )
        return
    if low in (BTN_HISTORY.lower(), "мои задачи", "история"):
        tasks = user(peer_id).get("tasks", [])[-5:]
        if not tasks:
            await send_message(
                client, peer_id, "Пока нет решённых задач.", keyboard=MAIN_KEYBOARD
            )
            return
        lines = [f"{i}. {t['task'][:80]}" for i, t in enumerate(reversed(tasks), 1)]
        await send_message(
            client,
            peer_id,
            "📚 Последние задачи:\n" + "\n".join(lines),
            keyboard=MAIN_KEYBOARD,
        )
        return
    if low in ("фото", BTN_PHOTO.lower()):
        await send_message(
            client,
            peer_id,
            "Пришлите фото задачи — разберу и решу.",
            keyboard=MAIN_KEYBOARD,
        )
        return
    if low in (BTN_SECTION.lower(), "сечение"):
        awaiting_section.add(peer_id)
        await send_message(
            client,
            peer_id,
            "Пришлите фото или текст задачи на сечение — решу и нарисую форму сечения.",
            keyboard=MAIN_KEYBOARD,
        )
        return
    if low in (BTN_GRAPH.lower(), "график"):
        awaiting_graph.add(peer_id)
        await send_message(
            client,
            peer_id,
            "Пришлите функцию, например «y = x^2 - 4x + 3», — построю график картинкой.",
            keyboard=MAIN_KEYBOARD,
        )
        return
    if low in (BTN_LEVEL.lower(), "уровень"):
        awaiting_level.add(peer_id)
        await send_message(
            client,
            peer_id,
            "Напишите ваш класс или курс (например, «8 класс», «1 курс») — подстрою объяснение.",
            keyboard=MAIN_KEYBOARD,
        )
        return
    if peer_id in awaiting_level and text:
        awaiting_level.discard(peer_id)
        set_level(peer_id, text)
        await send_message(
            client,
            peer_id,
            f"Учту уровень: {text}. Теперь пришлите задачу.",
            keyboard=MAIN_KEYBOARD,
        )
        return
    if low in (BTN_OK.lower(), "понятно", "спасибо"):
        await send_message(
            client,
            peer_id,
            "Рад, что помогло! Присылайте следующую задачу.",
            keyboard=MAIN_KEYBOARD,
        )
        return
    if low in (BTN_SIMPLER.lower(), "проще", "объясни проще"):
        await run_llm_task(
            client,
            peer_id,
            "Объясни это решение проще, как для новичка, но с теми же выкладками.",
            FOLLOWUP_KEYBOARD,
        )
        return
    if low in (BTN_SIMILAR.lower(), "похожая", "ещё"):
        await run_llm_task(
            client,
            peer_id,
            "Придумай и реши похожую задачу для тренировки (с другим условием).",
            FOLLOWUP_KEYBOARD,
        )
        return
    if not text:
        audio = audio_url(attachments)
        if audio:
            await send_message(client, peer_id, "⏳ Распознаю голосовое…")
            try:
                r = await client.get(audio, timeout=60)
                text = (await stt_gemini(client, r.content) or "").strip()
            except Exception as e:
                log.warning("Голосовое: %s", e)
            if not text:
                await send_message(
                    client,
                    peer_id,
                    "Не удалось распознать голосовое. Напишите задачу текстом.",
                    keyboard=MAIN_KEYBOARD,
                )
                return
            await send_message(client, peer_id, f"🎤 «{text[:300]}»")

    if (text or photo_url(attachments)) and not check_limit(peer_id):
        await send_message(
            client,
            peer_id,
            f"Лимит бесплатных задач на сегодня исчерпан ({FREE_LIMIT}).\n"
            "Напишите администратору, чтобы увеличить лимит или оформить подписку.",
            keyboard=MAIN_KEYBOARD,
        )
        return

    if peer_id in awaiting_graph and text:
        awaiting_graph.discard(peer_id)
        await plot_task(client, peer_id, text)
        return
    if peer_id in awaiting_section and (text or attachments):
        awaiting_section.discard(peer_id)
        await section_task(client, peer_id, text, attachments)
        return

    photo = photo_url(attachments)
    if photo:
        is_check = mode.pop(peer_id, None) == "check"
        if peer_id in busy:
            await send_message(client, peer_id, "⏳ Секунду, я ещё решаю предыдущую задачу…")
            return
        busy.add(peer_id)
        try:
            await send_message(
                client,
                peer_id,
                "⏳ Проверяю решение на фото…" if is_check else "⏳ Решаю задачу с фото…",
            )
            r = await client.get(
                photo,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=60,
            )
            if r.status_code != 200 or not r.content:
                raise RuntimeError(
                    f"фото не скачалось (HTTP {r.status_code}, {len(r.content)} байт)"
                )
            b64 = prepare_image(r.content)
            photo_text = (
                "Проверь решение на этом фото: найди ошибки, объясни, где неверно, "
                "и покажи правильный ход. Если всё верно — подтверди."
                if is_check
                else "Реши задачу с этого фото. Сначала опиши, что видно на рисунке "
                "(фигура, подписи, данные, вопрос), затем покажи полное пошаговое решение."
            )
            content = [
                {"type": "text", "text": photo_text},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                },
            ]
            push(peer_id, "user", content)
            reply = await ask_llm(client, peer_id)
            push(peer_id, "assistant", reply)
            bump_usage(peer_id)
            add_task(peer_id, "фото задачи", reply)
            await send_long(client, peer_id, reply, keyboard=FOLLOWUP_KEYBOARD)
        except Exception as e:
            log.exception("Ошибка обработки фото")
            await send_message(
                client,
                peer_id,
                f"Не удалось разобрать фото ({type(e).__name__}): {e}\n"
                "Попробуйте прислать фото ещё раз или напишите задачу текстом.",
            )
        finally:
            busy.discard(peer_id)
        return

    if any(a.get("type") == "photo" for a in attachments):
        await send_message(
            client,
            peer_id,
            "Пришло фото, но я не смог его получить. Пришлите ещё раз или опишите задачу текстом.",
        )
        return

    if text:
        await run_llm_task(
            client,
            peer_id,
            text,
            FOLLOWUP_KEYBOARD,
            ack="⏳ Думаю, как подсказать…" if peer_id in socratic else "⏳ Решаю задачу…",
        )


async def init_longpoll(client: httpx.AsyncClient) -> tuple[str, str, str]:
    resp = await vk_call(client, "groups.getLongPollServer", group_id=settings.vk_group_id)
    return resp["key"], resp["server"], resp["ts"]


async def main() -> None:
    load_data()
    log.info("VK-бот запущен (модель: %s)", settings.llm_model)
    async with httpx.AsyncClient(timeout=120) as client:
        key, server, ts = await init_longpoll(client)
        while True:
            try:
                r = await client.get(
                    server, params={"act": "a_check", "key": key, "ts": ts, "wait": 25}
                )
                data = r.json()
                if "failed" in data:
                    log.warning("Long Poll сброшен (failed=%s), переинициализация", data["failed"])
                    key, server, ts = await init_longpoll(client)
                    continue
                ts = data.get("ts", ts)
                for u in data.get("updates", []):
                    await handle_update(client, u)
                await check_reminders(client)
            except Exception as e:
                log.exception("Ошибка long poll: %s", e)
                await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(main())
