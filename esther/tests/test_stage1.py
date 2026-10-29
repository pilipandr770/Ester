import json
from datetime import datetime, timedelta

import pytest

import ai
import evolution
import pipeline
import worker
from config import Config
from models import db, User, Chat, Message, WebhookJob
from tests.helpers import post


def _setup(make_user, **kw):
    u = make_user(**kw)
    c = Chat(user_id=u.id, jid="1@g.us", name="Сад")
    db.session.add(c)
    db.session.commit()
    return u, c


def _payload(wa_id="w1", jid="1@g.us", message=None, event="messages.upsert", ts=1767600000):
    return {"event": event, "data": {
        "key": {"remoteJid": jid, "id": wa_id, "participant": "972501@s.whatsapp.net"},
        "pushName": "Dana", "messageTimestamp": ts,
        "message": message if message is not None else {"conversation": "שלום"}}}


@pytest.fixture(autouse=True)
def _no_ai(monkeypatch):
    monkeypatch.setattr(ai, "translate", lambda t: ("he", "привет"))


# ---------- очередь ----------

def test_webhook_enqueues_only_selected_group_text(make_user):
    u, c = _setup(make_user)
    pipeline.handle_webhook(u, _payload(jid="2@s.whatsapp.net"))          # личный чат
    pipeline.handle_webhook(u, _payload(jid="9@g.us", wa_id="w2"))        # группа не выбрана
    pipeline.handle_webhook(u, _payload(wa_id="w3", message={"stickerMessage": {}}))
    assert WebhookJob.query.count() == 0
    pipeline.handle_webhook(u, _payload(wa_id="w4"))
    pipeline.handle_webhook(u, _payload(wa_id="w4"))  # повторная доставка
    assert WebhookJob.query.count() == 1 and Message.query.count() == 0  # ИИ в вебхуке не вызывался

    c.enabled = False
    db.session.commit()
    pipeline.handle_webhook(u, _payload(wa_id="w5"))
    assert WebhookJob.query.count() == 1


def test_process_jobs_saves_message_and_clears_job(make_user):
    u, c = _setup(make_user)
    pipeline.handle_webhook(u, _payload())
    assert pipeline.process_jobs() == 1
    m = Message.query.one()
    assert (m.original, m.translation, m.lang, m.sender) == ("שלום", "привет", "he", "Dana")
    assert WebhookJob.query.count() == 0  # текст сообщения в очереди не остаётся


def test_failed_job_is_retried_with_backoff_then_saved_untranslated(make_user, monkeypatch):
    def boom(t):
        raise RuntimeError("api down")
    monkeypatch.setattr(ai, "translate", boom)
    u, c = _setup(make_user)
    pipeline.handle_webhook(u, _payload())
    now = datetime.utcnow()

    job = pipeline.claim_job(now)
    pipeline.run_job(job, now)
    job = WebhookJob.query.one()
    assert job.status == "pending" and job.attempts == 1 and job.run_after > now
    assert pipeline.claim_job(now) is None  # рано

    for i in range(2, Config.JOB_MAX_ATTEMPTS + 1):
        now += timedelta(hours=1)
        job = pipeline.claim_job(now)
        assert job is not None
        pipeline.run_job(job, now)
    assert WebhookJob.query.count() == 0
    m = Message.query.one()
    assert m.original == "שלום" and m.translation == ""  # сообщение не потеряно


def test_stale_processing_job_is_requeued(make_user):
    u, c = _setup(make_user)
    pipeline.handle_webhook(u, _payload())
    now = datetime.utcnow()
    assert pipeline.claim_job(now) is not None
    pipeline.requeue_stale_jobs(now + timedelta(minutes=5))
    assert WebhookJob.query.one().status == "processing"
    pipeline.requeue_stale_jobs(now + timedelta(minutes=11))
    assert WebhookJob.query.one().status == "pending"


def test_job_dropped_when_chat_disabled_or_no_access(make_user):
    u, c = _setup(make_user)
    pipeline.handle_webhook(u, _payload())
    c.enabled = False
    db.session.commit()
    pipeline.process_jobs()
    assert WebhookJob.query.count() == 0 and Message.query.count() == 0


def test_duplicate_wa_id_does_not_crash(make_user):
    u, c = _setup(make_user)
    pipeline.handle_webhook(u, _payload())
    db.session.add(Message(chat_id=c.id, wa_id="w1", original="x"))
    db.session.commit()
    pipeline.process_jobs()
    assert Message.query.count() == 1


# ---------- лимиты ----------

def test_media_limits(make_user, monkeypatch):
    calls = []
    monkeypatch.setattr(evolution, "get_media_base64", lambda n, d: calls.append(1) or ("aGk=", "audio/ogg"))
    monkeypatch.setattr(ai, "transcribe", lambda b, m: "голос")
    u, c = _setup(make_user)

    big = {"audioMessage": {"seconds": Config.MAX_AUDIO_SECONDS + 1}}
    huge = {"imageMessage": {"fileLength": Config.MAX_MEDIA_MB * 1024 * 1024 + 1}}
    for i, m in enumerate((big, huge)):
        pipeline.handle_webhook(u, _payload(wa_id=f"b{i}", message=m))
    pipeline.process_jobs()
    assert calls == [] and Message.query.count() == 0  # файл даже не скачивали

    pipeline.handle_webhook(u, _payload(wa_id="ok", message={"audioMessage": {"seconds": 5}}))
    pipeline.process_jobs()
    assert Message.query.one().kind == "audio" and len(calls) == 1

    monkeypatch.setattr(Config, "MAX_MEDIA_PER_DAY", 1)
    db.session.query(Message).update({"ts": datetime.utcnow()})
    db.session.commit()
    pipeline.handle_webhook(u, _payload(wa_id="over", message={"audioMessage": {"seconds": 5}}))
    pipeline.process_jobs()
    assert Message.query.count() == 1 and len(calls) == 1  # дневной лимит медиа


def test_daily_message_cap_saves_without_translation(make_user, monkeypatch):
    monkeypatch.setattr(Config, "MAX_MESSAGES_PER_DAY", 1)
    monkeypatch.setattr(ai, "translate", lambda t: pytest.fail("перевод сверх лимита"))
    u, c = _setup(make_user)
    db.session.add(Message(chat_id=c.id, wa_id="old", original="x", ts=datetime.utcnow()))
    db.session.commit()
    pipeline.handle_webhook(u, _payload(wa_id="new"))
    pipeline.process_jobs()
    m = Message.query.filter_by(wa_id="new").one()
    assert m.original == "שלום" and m.translation == ""


def test_chat_limit(logged_in, monkeypatch):
    client, u = logged_in
    monkeypatch.setattr(Config, "MAX_CHATS", 2)
    for i in range(3):
        post(client, "/chats/add", {"jid": f"{i}@g.us", "name": f"g{i}"}, form_page="/settings")
    assert Chat.query.filter_by(user_id=u.id).count() == 2


# ---------- отключение WhatsApp ----------

def test_disconnect_triggers_single_email(make_user, monkeypatch):
    sent = []
    monkeypatch.setattr(worker.mailer, "send_mail", lambda to, s, b: sent.append(to) or True)
    u = make_user(wa_connected=True)
    ev = {"event": "connection.update", "data": {"state": "close"}}

    pipeline.handle_webhook(u, {"event": "connection.update", "data": {"state": "connecting"}})
    assert not u.wa_alert_pending
    pipeline.handle_webhook(u, ev)
    assert u.wa_alert_pending and not u.wa_connected
    pipeline.handle_webhook(u, ev)  # повторное «close» не создаёт новое письмо

    worker.send_wa_alerts()
    worker.send_wa_alerts()
    assert sent == [u.email]

    pipeline.handle_webhook(u, {"event": "connection.update", "data": {"state": "open"}})
    assert u.wa_connected and not u.wa_alert_pending


def test_manual_disconnect_does_not_alert(make_user):
    u = make_user(wa_connected=False)
    pipeline.handle_webhook(u, {"event": "connection.update", "data": {"state": "close"}})
    assert not u.wa_alert_pending


# ---------- разбор больших чатов ----------

def test_chunking_and_merge(monkeypatch):
    lines = ["x" * 100] * 10
    chunks = ai._chunk_lines(lines, budget=350)
    assert [len(c) for c in chunks] == [3, 3, 3, 1] and sum(chunks, []) == lines

    def fake_ask(model, system, content, max_tokens=2000):
        n = content.count("msg")
        return json.dumps({"summary": f"s{n}", "important": ["a"], "terms": [{"term": "T", "meaning": "m"}],
                           "events": [{"title": "e", "date": "2026-01-05"}]})
    monkeypatch.setattr(ai, "_ask", fake_ask)
    monkeypatch.setattr(ai, "DIGEST_CHUNK_CHARS", 60)
    ts = datetime(2026, 1, 5, 10, 0)
    rows = [(ts, "A", f"msg{i}" + "y" * 30, "") for i in range(4)]
    res = ai.digest("Сад", "", rows)
    assert len(res["events"]) >= 2 and res["important"] == ["a"] and len(res["terms"]) == 1


def test_truncated_json_is_split_and_retried(monkeypatch):
    calls = []

    def fake_ask(model, system, content, max_tokens=2000):
        calls.append(content.count("msg"))
        if content.count("msg") > 1:
            return '{"summary": "обрезано'  # битый JSON
        return json.dumps({"summary": "ok", "important": [], "terms": [], "events": []})
    monkeypatch.setattr(ai, "_ask", fake_ask)
    ts = datetime(2026, 1, 5, 10, 0)
    res = ai.digest("Сад", "", [(ts, "A", f"msg{i}", "") for i in range(4)])
    assert calls == [4, 2, 1, 1, 2, 1, 1] and res["summary"].count("ok") == 4


# ---------- календарь и экспорт ----------

def test_ics_rotation_invalidates_old_link(logged_in):
    client, u = logged_in
    old = u.ics_token
    assert client.get(f"/ics/{old}.ics").status_code == 200
    post(client, "/calendar/rotate-ics", form_page="/calendar")
    assert client.get(f"/ics/{old}.ics").status_code == 404
    assert client.get(f"/ics/{db.session.get(User, u.id).ics_token}.ics").status_code == 200


def test_export_contains_only_own_data(logged_in, make_user):
    client, u = logged_in
    other, oc = _setup(make_user, email="other@example.org")
    db.session.add(Message(chat_id=oc.id, wa_id="secret", original="чужое"))
    mine = Chat(user_id=u.id, jid="5@g.us", name="Моя")
    db.session.add(mine)
    db.session.commit()
    db.session.add(Message(chat_id=mine.id, wa_id="mine", original="моё", translation="mine"))
    db.session.commit()
    r = client.get("/account/export")
    assert r.status_code == 200 and "attachment" in r.headers["Content-Disposition"]
    data = r.get_json()
    assert data["account"]["email"] == u.email
    assert [m["original"] for c in data["chats"] for m in c["messages"]] == ["моё"]
    assert "чужое" not in r.get_data(as_text=True)


def test_export_requires_login(client):
    assert client.get("/account/export").status_code == 302


def test_account_delete_removes_jobs(logged_in, monkeypatch):
    import billing
    client, u = logged_in
    c = Chat(user_id=u.id, jid="1@g.us", name="Сад")
    db.session.add(c)
    db.session.commit()
    pipeline.handle_webhook(u, _payload())
    monkeypatch.setattr(billing, "cancel_subscriptions", lambda user: None)
    monkeypatch.setattr(evolution, "delete_instance", lambda n: None)
    post(client, "/account/delete", form_page="/settings")
    assert db.session.get(User, u.id) is None and WebhookJob.query.count() == 0
