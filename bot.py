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
busy: set[int] = set()

# Ссылка на чат с администратором (для кнопки «Помощь»)
ADMIN_VK = settings.admin_vk.strip()


def admin_chat_url() -> str:
    if ADMIN_VK.isdigit():
        return f"https://vk.com/im?sel={ADMIN_VK}"
    return f"https://vk.me/{ADMIN_VK.lstrip('@')}"


# Запас под лимит VK в 4096 символов на одно сообщение
MAX_VK_LEN = 4000

HELP_TEXT = (
    "Привет! Я решаю домашнее задание 📚\n\n"
    "• Напиши задачу текстом — решу пошагово с объяснением.\n"
    "• Пришли фото задачи — разберу.\n\n"
    "Кнопки внизу: фото, сечение, график, уровень, помощь, сброс."
)

BTN_PHOTO = "📷 Прислать фото"
BTN_SECTION = "📐 Сечение"
BTN_GRAPH = "📊 График"
BTN_LEVEL = "⭐ Уровень"
BTN_HELP = "❓ Помощь"
BTN_RESET = "🧹 Сбросить"

HELP_BUTTON = (
    {
        "action": {"type": "link", "label": BTN_HELP, "link": admin_chat_url()},
        "color": "secondary",
    }
    if ADMIN_VK
    else {"action": {"type": "text", "label": BTN_HELP}, "color": "secondary"}
)

MAIN_KEYBOARD = {
    "one_time": False,
    "buttons": [
        [{"action": {"type": "text", "label": BTN_PHOTO}, "color": "primary"}],
        [
            {"action": {"type": "text", "label": BTN_SECTION}, "color": "secondary"},
            {"action": {"type": "text", "label": BTN_GRAPH}, "color": "secondary"},
        ],
        [
            {"action": {"type": "text", "label": BTN_LEVEL}, "color": "secondary"},
            HELP_BUTTON,
        ],
        [{"action": {"type": "text", "label": BTN_RESET}, "color": "secondary"}],
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
    base = settings.system_prompt
    lvl = levels.get(peer_id)
    if lvl:
        return base + f"\n\nУровень ученика: {lvl}. Подстраивай объяснение под этот уровень."
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
    if post:
        r = await client.post(url, data=params)
    else:
        r = await client.get(url, params=params)
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
    """Оставляет картинку только в последнем сообщении — чтобы не раздувать запрос."""
    out = []
    for i, m in enumerate(messages):
        content = m.get("content")
        if isinstance(content, list) and i != len(messages) - 1:
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


def photo_url(attachments: list) -> str | None:
    """Возвращает URL самого большого фото из вложений."""
    for a in attachments or []:
        if a.get("type") == "photo":
            sizes = a["photo"].get("sizes", [])
            if sizes:
                best = max(sizes, key=lambda s: s.get("width", 0) * s.get("height", 0))
                return best.get("url")
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

    try:
        up = await vk_call(client, "photos.getMessagesUploadServer", peer_id=peer_id)
        r = await client.post(
            up["upload_url"], files={"photo": ("graph.png", png, "image/png")}
        )
        data = r.json()
        saved = await vk_call(
            client,
            "photos.saveMessagesPhoto",
            photo=data["photo"],
            server=data["server"],
            hash=data["hash"],
        )
        p = saved[0]
        att = f"photo{p['owner_id']}_{p['id']}"
        await send_message(
            client,
            peer_id,
            "📊 Вот график:",
            attachment=att,
            keyboard=FOLLOWUP_KEYBOARD,
        )
        return True
    except Exception as e:
        log.warning("Загрузка графика не удалась: %s", e)
        return False


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


async def run_llm_task(
    client: httpx.AsyncClient, peer_id: int, prompt: str, keyboard: dict | None
) -> None:
    """Решает задачу с анти-спамом: один запрос за раз."""
    if peer_id in busy:
        await send_message(client, peer_id, "⏳ Секунду, я ещё решаю предыдущую задачу…")
        return
    busy.add(peer_id)
    try:
        await send_message(client, peer_id, "⏳ Решаю задачу…")
        push(peer_id, "user", prompt)
        reply = await ask_llm(client, peer_id)
        push(peer_id, "assistant", reply)
        await send_long(client, peer_id, reply, keyboard=keyboard)
    finally:
        busy.discard(peer_id)


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

    low = text.lower()
    if low in ("начать", "start", "помощь", "help", "привет", "/start", BTN_HELP.lower()):
        await send_message(client, peer_id, HELP_TEXT, keyboard=MAIN_KEYBOARD)
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
    if low in ("фото", BTN_PHOTO.lower()):
        await send_message(
            client,
            peer_id,
            "Пришлите фото задачи — разберу и решу.",
            keyboard=MAIN_KEYBOARD,
        )
        return
    if low in (BTN_SECTION.lower(), "сечение"):
        await send_message(
            client,
            peer_id,
            "Пришлите фото или текст задачи на сечение — разберу построение по шагам.",
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
    if peer_id in awaiting_graph and text:
        awaiting_graph.discard(peer_id)
        await plot_task(client, peer_id, text)
        return

    photo = photo_url(attachments)
    if photo:
        if peer_id in busy:
            await send_message(client, peer_id, "⏳ Секунду, я ещё решаю предыдущую задачу…")
            return
        busy.add(peer_id)
        try:
            await send_message(client, peer_id, "⏳ Решаю задачу с фото…")
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
            content = [
                {
                    "type": "text",
                    "text": (
                        "Реши задачу с этого фото. Сначала опиши, что видно на рисунке "
                        "(фигура, подписи, данные, вопрос), затем покажи полное пошаговое решение."
                    ),
                },
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                },
            ]
            push(peer_id, "user", content)
            reply = await ask_llm(client, peer_id)
            push(peer_id, "assistant", reply)
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
        await run_llm_task(client, peer_id, text, FOLLOWUP_KEYBOARD)


async def init_longpoll(client: httpx.AsyncClient) -> tuple[str, str, str]:
    resp = await vk_call(client, "groups.getLongPollServer", group_id=settings.vk_group_id)
    return resp["key"], resp["server"], resp["ts"]


async def main() -> None:
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
            except Exception as e:
                log.exception("Ошибка long poll: %s", e)
                await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(main())
