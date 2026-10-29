"""Обработка входящего вебхука и вечернего дайджеста."""
import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError

import ai
import evolution
from config import Config
from models import db, User, Chat, Message, Event, Digest, WebhookJob

log = logging.getLogger("esther")

VALID_KINDS = {"event", "payment", "bring", "dress", "deadline"}


SUPPORTED_MESSAGES = ("conversation", "extendedTextMessage", "imageMessage", "audioMessage")
JOB_STALE = timedelta(minutes=10)  # задача в обработке дольше этого считается потерянной


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def handle_webhook(user, payload):
    """Быстрая часть вебхука (без ИИ): принимаем ТОЛЬКО сообщения из групп, выбранных
    пользователем, и ставим их в очередь. Всё остальное отбрасывается сразу и нигде
    не сохраняется. Тяжёлую обработку делает воркер (process_message)."""
    event = (payload.get("event") or "").lower().replace("_", ".")
    data = payload.get("data") or {}

    if event == "connection.update":
        was_connected = user.wa_connected
        state = data.get("state")
        # «connecting» (переподключение) не считаем отключением, поэтому статус не трогаем
        if state == "open":
            user.wa_connected = True
            user.wa_alert_pending = False
        elif state == "close":
            user.wa_connected = False
            if was_connected:
                user.wa_alert_pending = True  # письмо отправит воркер
        db.session.commit()
        return

    if event != "messages.upsert":
        return

    key = data.get("key") or {}
    jid = key.get("remoteJid", "")
    if not jid.endswith("@g.us"):
        return  # личные переписки не трогаем никогда
    chat = Chat.query.filter_by(user_id=user.id, jid=jid, enabled=True).first()
    if not chat:
        return
    wa_id = key.get("id")
    if not wa_id or not any(k in (data.get("message") or {}) for k in SUPPORTED_MESSAGES):
        return  # стикеры, реакции и прочее не нужны
    if (Message.query.filter_by(wa_id=wa_id).first()
            or WebhookJob.query.filter_by(user_id=user.id, wa_id=wa_id).first()):
        return

    db.session.add(WebhookJob(user_id=user.id, chat_id=chat.id, wa_id=wa_id,
                              payload=json.dumps(data, ensure_ascii=False)))
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()  # параллельная повторная доставка


def _usage_today(user):
    """(сообщений, из них медиа) за сегодня: защита от неконтролируемых расходов на ИИ."""
    start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    q = (db.session.query(Message).join(Chat, Message.chat_id == Chat.id)
         .filter(Chat.user_id == user.id, Message.ts >= start))
    return q.count(), q.filter(Message.kind.in_(("image", "audio"))).count()


def process_message(user, chat, data, final_attempt=False):
    """Тяжёлая часть: OCR / расшифровка / перевод и сохранение сообщения.
    Исключение означает «повторить позже». На последней попытке сообщение
    сохраняется и без перевода."""
    key = data.get("key") or {}
    wa_id = key.get("id")
    if Message.query.filter_by(wa_id=wa_id).first():
        return
    msg = data.get("message") or {}
    sender = data.get("pushName") or (key.get("participant") or "").split("@")[0]
    ts = datetime.utcfromtimestamp(_int(data.get("messageTimestamp")) or int(datetime.utcnow().timestamp()))
    n_msgs, n_media = _usage_today(user)
    over_cap = n_msgs >= Config.MAX_MESSAGES_PER_DAY

    kind, original = "text", None
    if "conversation" in msg:
        original = msg["conversation"]
    elif "extendedTextMessage" in msg:
        original = msg["extendedTextMessage"].get("text")
    elif "imageMessage" in msg or "audioMessage" in msg:
        kind = "image" if "imageMessage" in msg else "audio"
        part = msg["imageMessage"] if kind == "image" else msg["audioMessage"]
        if over_cap or n_media >= Config.MAX_MEDIA_PER_DAY:
            log.warning("media limit reached for user %s, skipped", user.id)
            return
        if (_int(part.get("fileLength")) > Config.MAX_MEDIA_MB * 1024 * 1024
                or (kind == "audio" and _int(part.get("seconds")) > Config.MAX_AUDIO_SECONDS)):
            return  # слишком большой файл: не тратим на него ИИ
        b64, mime = evolution.get_media_base64(user.instance_name, data)
        if not b64 or len(b64) * 3 // 4 > Config.MAX_MEDIA_MB * 1024 * 1024:
            return
        if kind == "image":
            ocr = ai.ocr_image(b64, mime or "image/jpeg")
            original = ((part.get("caption") or "") + "\n" + ocr).strip()
        else:
            original = ai.transcribe(b64, mime or "audio/ogg")
    else:
        return

    if not original or not original.strip():
        return

    lang, ru = "und", ""
    if over_cap:
        log.warning("message limit reached for user %s, saved without translation", user.id)
    else:
        try:
            lang, ru = ai.translate(original)
        except Exception:
            if not final_attempt:
                raise
            log.exception("translate failed, saving untranslated")

    db.session.add(Message(chat_id=chat.id, wa_id=wa_id, sender=sender, ts=ts, kind=kind,
                           original=original, lang=lang, translation=ru))
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()  # то же сообщение уже сохранено


def claim_job(now=None):
    """Атомарно берёт самую старую готовую задачу (безопасно при нескольких воркерах)."""
    now = now or datetime.utcnow()
    ids = [i for (i,) in db.session.query(WebhookJob.id)
           .filter(WebhookJob.status == "pending", WebhookJob.run_after <= now)
           .order_by(WebhookJob.id).limit(5)]
    for jid in ids:
        taken = (WebhookJob.query.filter_by(id=jid, status="pending")
                 .update({"status": "processing", "started_at": now}))
        db.session.commit()
        if taken:
            return db.session.get(WebhookJob, jid)
    return None


def run_job(job, now=None):
    now = now or datetime.utcnow()
    user, chat = db.session.get(User, job.user_id), db.session.get(Chat, job.chat_id)
    if not user or not chat or not chat.enabled or not user.has_access:
        db.session.delete(job)
        db.session.commit()
        return
    attempt = job.attempts + 1
    try:
        process_message(user, chat, json.loads(job.payload), final_attempt=attempt >= Config.JOB_MAX_ATTEMPTS)
        db.session.delete(job)  # содержимое сообщения в очереди не храним
    except Exception as e:
        db.session.rollback()
        job = db.session.get(WebhookJob, job.id)
        if attempt >= Config.JOB_MAX_ATTEMPTS:
            log.exception("job %s dropped after %s attempts", job.id, attempt)
            db.session.delete(job)
        else:
            log.warning("job %s failed (attempt %s): %s", job.id, attempt, e)
            job.attempts, job.status = attempt, "pending"
            job.run_after = now + timedelta(seconds=30 * 2 ** attempt)
            job.last_error = str(e)[:500]
    db.session.commit()


def requeue_stale_jobs(now=None):
    now = now or datetime.utcnow()
    (WebhookJob.query.filter(WebhookJob.status == "processing",
                             WebhookJob.started_at < now - JOB_STALE)
     .update({"status": "pending", "run_after": now}))
    db.session.commit()


def process_jobs(limit=20):
    requeue_stale_jobs()
    done = 0
    while done < limit:
        job = claim_job()
        if not job:
            break
        run_job(job)
        done += 1
    return done


def _parse_due(date_s, time_s):
    d = datetime.strptime(date_s, "%Y-%m-%d")
    if time_s and time_s != "null":
        try:
            t = datetime.strptime(time_s, "%H:%M")
            return d.replace(hour=t.hour, minute=t.minute), False
        except ValueError:
            pass
    return d.replace(hour=9, minute=0), True


def run_digest_for_user(user):
    """Разбор всех ещё не разобранных сообщений по каждому включённому чату.
    Возвращает (названия разобранных чатов, названия чатов, где разбор не удался)."""
    tz = ZoneInfo(user.timezone or "Asia/Jerusalem")
    today = datetime.now(tz).date()
    made, failed = [], []
    for chat in Chat.query.filter_by(user_id=user.id, enabled=True).all():
        msgs = (Message.query.filter_by(chat_id=chat.id, digested=False)
                .order_by(Message.ts).all())
        if not msgs:
            continue
        rows = [(m.ts.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz), m.sender, m.original, m.translation)
                for m in msgs]
        try:
            res = ai.digest(chat.name, chat.instruction, rows, today)
        except Exception:
            log.exception("digest failed for chat %s", chat.id)
            failed.append(chat.name)
            continue  # сообщения остаются не разобранными, попробуем позже

        db.session.add(Digest(
            user_id=user.id, chat_id=chat.id, day=today, summary=res["summary"],
            important=json.dumps(res["important"], ensure_ascii=False),
            terms=json.dumps(res["terms"], ensure_ascii=False)))

        for e in res["events"]:
            try:
                due, all_day = _parse_due(e["date"], e.get("time"))
            except Exception:
                continue
            title = (e.get("title") or "").strip()[:250]
            if not title:
                continue
            dup = Event.query.filter_by(user_id=user.id, due=due, title=title).first()
            if dup:
                continue
            kind = e.get("kind") if e.get("kind") in VALID_KINDS else "event"
            amt = e.get("amount")
            db.session.add(Event(
                user_id=user.id, chat_id=chat.id, title=title, kind=kind, due=due,
                all_day=all_day, details=e.get("details"),
                amount=None if amt in (None, "null") else str(amt)))
        for m in msgs:
            m.digested = True
        db.session.commit()
        made.append(chat.name)
    return made, failed


DIGEST_RETRY = timedelta(minutes=10)  # пауза между повторами при сбое ИИ
DIGEST_GIVE_UP = timedelta(hours=3)   # после этого срока за сегодня не повторяем
_digest_attempts = {}  # user.id -> время последней неудачной попытки (UTC)


def _parse_hhmm(s):
    try:
        t = datetime.strptime(s or "20:00", "%H:%M")
    except ValueError:
        t = datetime.strptime("20:00", "%H:%M")
    return t.hour, t.minute


def digest_is_due(user, now_utc):
    """Пора ли делать вечерний разбор: время наступило и за сегодня он ещё не завершён.
    Сравнение «не раньше», поэтому пропущенная минута или простой воркера не теряют разбор."""
    local = now_utc.astimezone(ZoneInfo(user.timezone or "Asia/Jerusalem"))
    if user.last_digest_day == local.date():
        return False
    return (local.hour, local.minute) >= _parse_hhmm(user.digest_time)


def maybe_run_digest(user, now_utc=None):
    """Запускает разбор, если пора. Возвращает список разобранных чатов (или [])."""
    now_utc = now_utc or datetime.now(ZoneInfo("UTC"))
    if not digest_is_due(user, now_utc):
        return []
    last = _digest_attempts.get(user.id)
    if last and now_utc - last < DIGEST_RETRY:
        return []
    local = now_utc.astimezone(ZoneInfo(user.timezone or "Asia/Jerusalem"))
    h, m = _parse_hhmm(user.digest_time)
    overdue = now_utc - local.replace(hour=h, minute=m, second=0, microsecond=0)

    made, failed = run_digest_for_user(user)
    if failed and overdue < DIGEST_GIVE_UP:
        _digest_attempts[user.id] = now_utc
    else:
        if failed:
            log.error("digest gave up for user %s: %s", user.id, failed)
        _digest_attempts.pop(user.id, None)
        user.last_digest_day = local.date()
        db.session.commit()
    return made


KIND_ICON = {"payment": "💶", "bring": "🎒", "dress": "👕", "deadline": "⏰", "event": "📅"}


def due_reminders(user, now_utc=None):
    """События, о которых пора напомнить (локальное время пользователя)."""
    tz = ZoneInfo(user.timezone or "Asia/Jerusalem")
    now_local = (now_utc or datetime.now(ZoneInfo("UTC"))).astimezone(tz).replace(tzinfo=None)
    horizon = now_local + timedelta(hours=user.remind_hours_before or 18)
    return (Event.query.filter_by(user_id=user.id, reminded=False, done=False)
            .filter(Event.due <= horizon, Event.due >= now_local - timedelta(hours=6)).all())


def send_due_reminders(user, now_utc=None):
    """Отправляет напоминания. Без номера ничего не делает и событие не помечает,
    при сбое отправки событие остаётся и уйдёт на следующем тике."""
    if not user.phone:
        return 0
    sent = 0
    for e in due_reminders(user, now_utc):
        try:
            evolution.send_text(user.instance_name, user.phone, format_reminder(e))
            e.reminded = True
            db.session.commit()
            sent += 1
        except Exception:
            db.session.rollback()
            log.exception("reminder %s for user %s failed", e.id, user.id)
    return sent


def format_reminder(e):
    icon = KIND_ICON.get(e.kind, "📅")
    when = e.due.strftime("%d.%m") + ("" if e.all_day else e.due.strftime(" в %H:%M"))
    s = f"{icon} Эстер напоминает: {e.title}\nКогда: {when}"
    if e.amount:
        s += f"\nСумма: {e.amount}"
    if e.details:
        s += f"\n{e.details}"
    return s
