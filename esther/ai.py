"""ИИ-конвейер: перевод + определение языка (Claude), OCR картинок (Claude vision),
расшифровка голосовых (Whisper), вечерний разбор чата (Claude)."""
import base64
import io
import json
import re
from datetime import date

from anthropic import Anthropic
from openai import OpenAI
from config import Config

_claude = None
_openai = None


def claude():
    global _claude
    if _claude is None:
        _claude = Anthropic(api_key=Config.ANTHROPIC_API_KEY, max_retries=3, timeout=90)
    return _claude


def oai():
    global _openai
    if _openai is None:
        _openai = OpenAI(api_key=Config.OPENAI_API_KEY, max_retries=3, timeout=90)
    return _openai


def extract_json(text):
    """Достаёт JSON из ответа модели (на случай обрамления ```)."""
    text = text.strip()
    m = re.search(r"\{.*\}|\[.*\]", text, re.S)
    if not m:
        raise ValueError("no json in model output")
    return json.loads(m.group(0))


def _ask(model, system, content, max_tokens=2000):
    r = claude().messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": content}],
    )
    return "".join(b.text for b in r.content if b.type == "text")


TRANSLATE_SYS = (
    "Ты переводчик для родителей-эмигрантов. Определи язык исходного текста сам "
    "(иврит, немецкий, английский, арабский и т.д.) и переведи на русский естественно и точно. "
    "Сохрани числа, даты, суммы, имена, названия мест. Не добавляй ничего от себя. "
    'Ответ строго JSON: {"lang": "код ISO 639-1", "ru": "перевод"}. '
    "Если текст уже на русском, lang=ru и ru равен исходному тексту."
)


def translate(text):
    if not text or not text.strip():
        return "und", ""
    out = _ask(Config.MODEL_FAST, TRANSLATE_SYS, text[:6000], 1500)
    try:
        j = extract_json(out)
        return j.get("lang", "und"), j.get("ru", "")
    except Exception:
        return "und", out.strip()


def ocr_image(b64, mimetype="image/jpeg"):
    """Вытащить текст с картинки (скриншот объявления, расписания и т.п.)."""
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": mimetype, "data": b64}},
        {
            "type": "text",
            "text": "Выпиши весь текст с изображения дословно, на исходном языке. "
            "Если текста нет, коротко опиши, что на фото, в скобках. Только результат.",
        },
    ]
    return _ask(Config.MODEL_FAST, "Ты OCR-система.", content, 1500).strip()


def transcribe(b64, mimetype="audio/ogg"):
    """Whisper сам определяет язык."""
    ext = "ogg" if "ogg" in (mimetype or "") else "mp3" if "mpeg" in (mimetype or "") else "m4a"
    buf = io.BytesIO(base64.b64decode(b64))
    buf.name = f"voice.{ext}"
    r = oai().audio.transcriptions.create(model="whisper-1", file=buf)
    return r.text


DIGEST_SYS = """Ты помощник родителя-эмигранта, который не знает язык чата. Тебе дают сообщения
из одного школьного/садиковского WhatsApp-чата за день (с переводом на русский) и инструкцию
родителя, что именно делать с этим чатом. Сегодня {today}.

Верни строго JSON:
{{
 "summary": "краткое саммари дня на русском, 2-6 предложений, без воды",
 "important": ["то, что нельзя пропустить: каждое пунктом, коротко и конкретно"],
 "terms": [{{"term": "слово/выражение на языке чата", "meaning": "что значит и что от родителя ожидается"}}],
 "events": [{{
    "title": "что нужно сделать/принести/во что одеть ребёнка",
    "kind": "event|payment|bring|dress|deadline",
    "date": "YYYY-MM-DD",
    "time": "HH:MM или null",
    "amount": "сумма с валютой или null",
    "details": "детали на русском"
 }}]
}}
Правила:
- В events включай ТОЛЬКО то, что требует действия родителя и имеет конкретную дату (прямую или
  вычисляемую от сегодняшней, например «в четверг», «завтра»). Если даты нет, не придумывай:
  положи это в important.
- Оплаты, дресс-код («все в белом»), «принести», экскурсии, собрания, дедлайны: обязательно.
- Не выдумывай. Если сообщения пустая болтовня, summary так и скажи, important и events пустые.
- Всё на русском, кроме поля term.
"""


DIGEST_CHUNK_CHARS = 30000  # примерно 10 тыс. токенов на запрос
LINE_MAX_CHARS = 4000


def _format_line(ts, sender, orig, tr):
    line = f"[{ts:%d.%m %H:%M}] {sender or '?'}: {(orig or '')[:LINE_MAX_CHARS]}"
    if tr and tr != orig:
        line += f"\n   → {tr[:LINE_MAX_CHARS]}"
    return line


def _chunk_lines(lines, budget=None):
    """Режет сообщения на куски по размеру, не разрывая сообщение."""
    budget = budget or DIGEST_CHUNK_CHARS
    chunks, cur, size = [], [], 0
    for line in lines:
        if cur and size + len(line) > budget:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(line)
        size += len(line) + 1
    if cur:
        chunks.append(cur)
    return chunks


def _digest_lines(chat_name, instruction, lines, today):
    """Один запрос к модели. Если ответ не разобрался как JSON (обычно обрезан), делим пополам."""
    user = (
        f"Чат: {chat_name}\n"
        f"Инструкция родителя: {instruction or 'нет, сделай стандартный разбор'}\n\n"
        "Сообщения:\n" + "\n".join(lines)
    )
    try:
        out = _ask(Config.MODEL_SMART, DIGEST_SYS.format(today=today.isoformat()), user, 4000)
        j = extract_json(out)
    except ValueError:  # включает json.JSONDecodeError
        if len(lines) < 2:
            raise
        mid = len(lines) // 2
        return _merge([_digest_lines(chat_name, instruction, lines[:mid], today),
                       _digest_lines(chat_name, instruction, lines[mid:], today)])
    for k, default in (("summary", ""), ("important", []), ("terms", []), ("events", [])):
        j.setdefault(k, default)
    return j


def _merge(parts):
    if len(parts) == 1:
        return parts[0]
    seen_terms, terms, important = set(), [], []
    for p in parts:
        for t in p["terms"]:
            key = (t.get("term") or "").strip().lower() if isinstance(t, dict) else ""
            if key and key not in seen_terms:
                seen_terms.add(key)
                terms.append(t)
        for i in p["important"]:
            if i not in important:
                important.append(i)
    return {
        "summary": "\n\n".join(p["summary"] for p in parts if p["summary"]),
        "important": important,
        "terms": terms,
        "events": [e for p in parts for e in p["events"]],
    }


def digest(chat_name, instruction, messages, today=None):
    """messages: список (время, отправитель, оригинал, перевод). Большие чаты режутся на куски."""
    today = today or date.today()
    lines = [_format_line(*m) for m in messages]
    return _merge([_digest_lines(chat_name, instruction, c, today) for c in _chunk_lines(lines)])
