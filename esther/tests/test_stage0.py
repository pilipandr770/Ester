import json
from datetime import datetime, timedelta, timezone

import pytest

import billing
import config
import evolution
import pipeline
from models import db, User, Chat, Message, Event
from tests.helpers import post, csrf_token  # noqa: F401

UTC = timezone.utc


# ---------- конфигурация ----------

def test_validate_config_fails_in_production(monkeypatch):
    monkeypatch.setattr(config.Config, "APP_ENV", "production")
    monkeypatch.setattr(config.Config, "SECRET_KEY", "dev")
    monkeypatch.setattr(config.Config, "COMPANY_NAME", "")  # не зависеть от локального .env
    with pytest.raises(RuntimeError) as e:
        config.validate_config()
    assert "SECRET_KEY" in str(e.value) and "COMPANY_NAME" in str(e.value)


def test_validate_config_ok_in_production(monkeypatch):
    for k, v in dict(APP_ENV="production", SECRET_KEY="x" * 40, EVOLUTION_API_KEY="k" * 20,
                     COMPANY_NAME="Esther Ltd", COMPANY_ID="515", COMPANY_ADDRESS="Tel Aviv",
                     CONTACT_EMAIL="help@esther.app").items():
        monkeypatch.setattr(config.Config, k, v)
    config.validate_config()


# ---------- CSRF, лимиты, здоровье ----------

def test_post_without_csrf_is_rejected(client, make_user):
    u = make_user()
    r = client.post("/login", data={"email": u.email, "password": "password123"})
    assert r.status_code == 302 and r.headers["Location"] == "/"
    assert client.get("/dashboard").status_code == 302  # не залогинен


def test_webhooks_are_csrf_exempt(client):
    assert client.post("/webhook/stripe", data=b"{}").status_code == 400  # подпись, а не CSRF
    assert client.post("/webhook/evolution/nope", json={}).status_code == 404


def test_login_rate_limit(client, make_user):
    make_user()
    codes = [post(client, "/login", {"email": "a@example.org", "password": "bad"}).status_code
             for _ in range(12)]
    assert codes[:10] == [200] * 10 and codes[10] == 429


def test_health(client):
    assert client.get("/health").get_json() == {"status": "ok"}


def test_cookie_flags(client):
    r = client.get("/login")
    assert "HttpOnly" in r.headers["Set-Cookie"] and "SameSite=Lax" in r.headers["Set-Cookie"]


# ---------- регистрация и сброс пароля ----------

def test_register_validates_password_length(client):
    r = post(client, "/register", {"email": "n@example.org", "password": "x" * 200, "agree": "1"},
             form_page="/register")
    assert r.status_code == 200 and User.query.count() == 0


def test_password_reset_flow(client, make_user, monkeypatch):
    import app as app_module
    sent = []
    monkeypatch.setattr(app_module.mailer, "send_mail", lambda to, s, b: sent.append((to, b)) or True)
    u = make_user()
    r = post(client, "/forgot", {"email": u.email}, form_page="/forgot")
    assert r.status_code == 302 and len(sent) == 1
    post(client, "/forgot", {"email": "ghost@example.org"}, form_page="/forgot")
    assert len(sent) == 1  # неизвестному адресу письмо не уходит, ответ тот же

    token = sent[0][1].split("/reset/")[1].split()[0]
    r = post(client, f"/reset/{token}", {"password": "newpassword1"}, form_page=f"/reset/{token}")
    assert r.status_code == 302
    assert db.session.get(User, u.id).check_password("newpassword1")
    # ссылка одноразовая: после смены пароля она недействительна
    assert client.get(f"/reset/{token}").status_code == 302
    assert client.get(f"/reset/{token}").headers["Location"].endswith("/forgot")


def test_reset_rejects_garbage_and_expired_token(client, make_user, monkeypatch):
    import app as app_module
    assert client.get("/reset/garbage").headers["Location"].endswith("/forgot")
    u = make_user()
    token = app_module.make_reset_token(u)
    monkeypatch.setattr(app_module, "RESET_MAX_AGE", -1)
    assert app_module.user_from_reset_token(token) is None


# ---------- настройки и ввод ----------

def test_settings_valid_and_invalid(logged_in):
    client, u = logged_in
    ok = {"timezone": "Europe/Berlin", "digest_time": "21:30", "remind_hours_before": "12",
          "phone": "+972 50-123-4567"}
    post(client, "/settings", ok, form_page="/settings")
    u = db.session.get(User, u.id)
    assert (u.timezone, u.digest_time, u.remind_hours_before, u.phone) == (
        "Europe/Berlin", "21:30", 12, "972501234567")

    for bad in ({"digest_time": "25:99"}, {"remind_hours_before": "abc"},
                {"timezone": "Mars/Base"}, {"phone": "123"}):
        r = post(client, "/settings", {**ok, **{"timezone": "Asia/Jerusalem"}, **bad}, form_page="/settings")
        assert r.status_code == 200  # без 500, форма показана заново
        assert db.session.get(User, u.id).timezone == "Europe/Berlin"  # ничего не сохранено


def test_event_add_validation_and_actions(logged_in):
    client, u = logged_in
    post(client, "/events/add", {"title": "  ", "date": "2026-05-01"}, form_page="/settings")
    post(client, "/events/add", {"title": "x", "date": "2026-13-01"}, form_page="/settings")
    assert Event.query.count() == 0
    post(client, "/events/add", {"title": "Оплата", "date": "2026-05-01", "kind": "bogus"}, form_page="/settings")
    e = Event.query.one()
    assert e.kind == "event" and e.all_day
    assert post(client, f"/events/{e.id}/explode", form_page="/settings").status_code == 404
    post(client, f"/events/{e.id}/done", form_page="/settings")
    assert db.session.get(Event, e.id).done


# ---------- доступ ----------

def test_has_access_rules():
    now = datetime.utcnow()
    future, past = now + timedelta(days=1), now - timedelta(days=1)
    assert User(sub_status="none", trial_ends_at=future).has_access
    assert not User(sub_status="none", trial_ends_at=past).has_access
    assert User(sub_status="active", trial_ends_at=past).has_access
    assert User(sub_status="past_due", trial_ends_at=past).has_access
    # отменённая подписка не продлевается остатком пробного периода
    assert not User(sub_status="canceled", trial_ends_at=future).has_access
    assert not User(sub_status="unpaid", trial_ends_at=future).has_access


# ---------- вечерний разбор ----------

def _user_with_message(make_user, **kw):
    u = make_user(timezone="Asia/Jerusalem", digest_time="20:00", wa_connected=True, **kw)
    c = Chat(user_id=u.id, jid="1@g.us", name="Сад")
    db.session.add(c)
    db.session.commit()
    db.session.add(Message(chat_id=c.id, wa_id="m1", sender="A", original="שלום", translation="привет",
                           ts=datetime(2026, 1, 5, 10, 0)))
    db.session.commit()
    return u


def _fake_digest(calls):
    def fake(chat_name, instruction, rows, today):
        calls.append(today)
        return {"summary": "ок", "important": [], "terms": [], "events": []}
    return fake


@pytest.fixture(autouse=True)
def _reset_attempts():
    pipeline._digest_attempts.clear()


def test_digest_not_due_before_time_and_runs_once(make_user, monkeypatch):
    calls = []
    monkeypatch.setattr(pipeline.ai, "digest", _fake_digest(calls))
    u = _user_with_message(make_user)

    assert pipeline.maybe_run_digest(u, datetime(2026, 1, 5, 17, 30, tzinfo=UTC)) == []  # 19:30 локально
    assert calls == []

    # воркер «проспал» ровно 20:00: разбор всё равно должен произойти позже
    assert pipeline.maybe_run_digest(u, datetime(2026, 1, 5, 18, 40, tzinfo=UTC)) == ["Сад"]
    assert pipeline.maybe_run_digest(u, datetime(2026, 1, 5, 19, 0, tzinfo=UTC)) == []
    assert len(calls) == 1 and str(u.last_digest_day) == "2026-01-05"

    # на следующий день разбор снова возможен
    db.session.add(Message(chat_id=u.chats[0].id, wa_id="m2", original="x", translation="y",
                           ts=datetime(2026, 1, 6, 9, 0)))
    db.session.commit()
    assert pipeline.maybe_run_digest(u, datetime(2026, 1, 6, 18, 5, tzinfo=UTC)) == ["Сад"]


def test_digest_failure_retries_then_gives_up(make_user, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("api down")
    monkeypatch.setattr(pipeline.ai, "digest", boom)
    u = _user_with_message(make_user)
    t0 = datetime(2026, 1, 5, 18, 5, tzinfo=UTC)  # 20:05 локально

    assert pipeline.maybe_run_digest(u, t0) == []
    assert u.last_digest_day is None  # день не закрыт, будет повтор
    n = []
    monkeypatch.setattr(pipeline.ai, "digest", _fake_digest(n))
    assert pipeline.maybe_run_digest(u, t0 + timedelta(minutes=2)) == []  # рано для повтора
    assert n == []
    assert pipeline.maybe_run_digest(u, t0 + timedelta(minutes=11)) == ["Сад"]
    assert u.last_digest_day is not None


def test_digest_gives_up_after_window(make_user, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("api down")
    monkeypatch.setattr(pipeline.ai, "digest", boom)
    u = _user_with_message(make_user)
    pipeline.maybe_run_digest(u, datetime(2026, 1, 5, 18, 5, tzinfo=UTC))
    assert u.last_digest_day is None
    pipeline.maybe_run_digest(u, datetime(2026, 1, 5, 21, 30, tzinfo=UTC))  # 23:30 локально, >3 ч после 20:00
    assert str(u.last_digest_day) == "2026-01-05"


# ---------- напоминания ----------

def _event(u, due):
    e = Event(user_id=u.id, title="Оплата", due=due, all_day=False)
    db.session.add(e)
    db.session.commit()
    return e


def test_reminders_without_phone_are_kept(make_user, monkeypatch):
    sent = []
    monkeypatch.setattr(evolution, "send_text", lambda *a: sent.append(a))
    u = make_user(timezone="UTC", remind_hours_before=18)
    e = _event(u, datetime(2026, 1, 5, 12, 0))
    now = datetime(2026, 1, 5, 6, 0, tzinfo=UTC)
    assert pipeline.send_due_reminders(u, now) == 0
    assert not db.session.get(Event, e.id).reminded and sent == []

    u.phone = "972501234567"
    db.session.commit()
    assert pipeline.send_due_reminders(u, now) == 1  # номер появился: напоминание уходит
    assert db.session.get(Event, e.id).reminded


def test_reminder_send_failure_is_retried(make_user, monkeypatch):
    def fail(*a):
        raise RuntimeError("evolution down")
    monkeypatch.setattr(evolution, "send_text", fail)
    u = make_user(timezone="UTC", remind_hours_before=18, phone="972501234567")
    e1 = _event(u, datetime(2026, 1, 5, 12, 0))
    now = datetime(2026, 1, 5, 6, 0, tzinfo=UTC)
    assert pipeline.send_due_reminders(u, now) == 0
    assert not db.session.get(Event, e1.id).reminded

    sent = []
    monkeypatch.setattr(evolution, "send_text", lambda *a: sent.append(a))
    assert pipeline.send_due_reminders(u, now) == 1 and len(sent) == 1


# ---------- Stripe ----------

def _stripe_event(monkeypatch, ev_id, etype, obj, sub_status=None):
    monkeypatch.setattr(billing.stripe.Webhook, "construct_event", lambda *a, **k: None)
    if sub_status:
        monkeypatch.setattr(billing.stripe.Subscription, "retrieve", lambda sid: {"status": sub_status})
    payload = json.dumps({"id": ev_id, "type": etype, "data": {"object": obj}}).encode()
    return billing.handle_event(payload, "sig")


def test_checkout_completed_uses_real_subscription_status(make_user, monkeypatch):
    u = make_user()
    _stripe_event(monkeypatch, "evt_1", "checkout.session.completed",
                  {"client_reference_id": str(u.id), "customer": "cus_1", "subscription": "sub_1"}, "trialing")
    u = db.session.get(User, u.id)
    assert (u.stripe_customer_id, u.stripe_subscription_id, u.sub_status) == ("cus_1", "sub_1", "trialing")


def test_stripe_events_are_idempotent(make_user, monkeypatch):
    u = make_user(stripe_customer_id="cus_1", stripe_subscription_id="sub_1", sub_status="active")
    obj = {"id": "sub_1", "customer": "cus_1"}
    _stripe_event(monkeypatch, "evt_2", "customer.subscription.updated", obj, "past_due")
    assert db.session.get(User, u.id).sub_status == "past_due"
    u.sub_status = "active"
    db.session.commit()
    _stripe_event(monkeypatch, "evt_2", "customer.subscription.updated", obj, "past_due")  # повтор
    assert db.session.get(User, u.id).sub_status == "active"


def test_payment_failed_and_deleted(make_user, monkeypatch):
    u = make_user(stripe_customer_id="cus_1", stripe_subscription_id="sub_1", sub_status="active")
    _stripe_event(monkeypatch, "evt_3", "invoice.payment_failed", {"customer": "cus_1"})
    assert db.session.get(User, u.id).sub_status == "past_due"
    _stripe_event(monkeypatch, "evt_4", "customer.subscription.deleted",
                  {"id": "sub_1", "customer": "cus_1"}, "canceled")
    u = db.session.get(User, u.id)
    assert u.sub_status == "canceled" and not u.has_access


def test_old_subscription_event_does_not_override_new(make_user, monkeypatch):
    u = make_user(stripe_customer_id="cus_1", stripe_subscription_id="sub_new", sub_status="active")
    _stripe_event(monkeypatch, "evt_5", "customer.subscription.deleted",
                  {"id": "sub_old", "customer": "cus_1"}, "canceled")
    u = db.session.get(User, u.id)
    assert u.sub_status == "active" and u.stripe_subscription_id == "sub_new"


def test_stripe_webhook_valid_signature_is_accepted(make_user):
    import hashlib
    import hmac
    import time
    u = make_user(stripe_customer_id="cus_9", stripe_subscription_id="sub_9", sub_status="active")
    payload = json.dumps({"id": "evt_sig", "object": "event", "type": "invoice.payment_failed",
                          "data": {"object": {"object": "invoice", "customer": "cus_9"}}}).encode()
    ts = str(int(time.time()))
    sig = hmac.new(b"whsec_test", ts.encode() + b"." + payload, hashlib.sha256).hexdigest()
    billing.handle_event(payload, f"t={ts},v1={sig}")
    assert db.session.get(User, u.id).sub_status == "past_due"


def test_stripe_webhook_bad_signature_is_400(client):
    r = client.post("/webhook/stripe", data=b"{}", headers={"Stripe-Signature": "t=1,v1=bad"})
    assert r.status_code == 400


def test_checkout_redirects_active_user_to_portal(logged_in, monkeypatch):
    client, u = logged_in
    u.stripe_customer_id, u.sub_status = "cus_1", "active"
    db.session.commit()
    monkeypatch.setattr(billing, "portal_url", lambda user: "https://portal.test/")
    monkeypatch.setattr(billing, "checkout_url", lambda user: pytest.fail("второй checkout"))
    r = post(client, "/billing/checkout", form_page="/billing")
    assert r.headers["Location"] == "https://portal.test/"


# ---------- удаление аккаунта ----------

def test_account_delete_aborts_if_stripe_cancel_fails(logged_in, monkeypatch):
    client, u = logged_in
    monkeypatch.setattr(billing, "cancel_subscriptions", lambda user: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(evolution, "delete_instance", lambda n: pytest.fail("инстанс не должен удаляться"))
    post(client, "/account/delete", form_page="/settings")
    assert db.session.get(User, u.id) is not None


def test_account_delete_success(logged_in, monkeypatch):
    client, u = logged_in
    called = []
    monkeypatch.setattr(billing, "cancel_subscriptions", lambda user: called.append("stripe"))
    monkeypatch.setattr(evolution, "delete_instance", lambda n: called.append("evo"))
    post(client, "/account/delete", form_page="/settings")
    assert called == ["stripe", "evo"] and db.session.get(User, u.id) is None
