import secrets
from datetime import datetime
from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


def _token():
    return secrets.token_urlsafe(24)


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    timezone = db.Column(db.String(64), default="Asia/Jerusalem")
    digest_time = db.Column(db.String(5), default="20:00")  # локальное время HH:MM
    remind_hours_before = db.Column(db.Integer, default=18)  # за сколько часов напомнить
    phone = db.Column(db.String(32))  # свой номер для напоминаний, только цифры

    # Evolution
    instance_name = db.Column(db.String(64), unique=True)
    webhook_token = db.Column(db.String(64), default=_token, unique=True)
    wa_connected = db.Column(db.Boolean, default=False)
    ics_token = db.Column(db.String(64), default=_token, unique=True)

    # Stripe
    stripe_customer_id = db.Column(db.String(64))
    stripe_subscription_id = db.Column(db.String(64))
    sub_status = db.Column(db.String(32), default="none")  # none/trialing/active/past_due/canceled/unpaid/...
    trial_ends_at = db.Column(db.DateTime)

    last_digest_day = db.Column(db.Date)  # локальная дата последнего завершённого вечернего разбора
    wa_alert_pending = db.Column(db.Boolean, default=False)  # WhatsApp отключился: нужно письмо

    chats = db.relationship("Chat", backref="user", cascade="all, delete-orphan")
    events = db.relationship("Event", backref="user", cascade="all, delete-orphan")
    jobs = db.relationship("WebhookJob", backref="user", cascade="all, delete-orphan")

    def set_password(self, p):
        self.password_hash = generate_password_hash(p)

    def check_password(self, p):
        return check_password_hash(self.password_hash, p)

    @property
    def has_access(self):
        # past_due: Stripe ещё повторяет списание, доступ сохраняем (льготный период)
        if self.sub_status in ("active", "trialing", "past_due"):
            return True
        # пробный период только у тех, кто ещё не оформлял подписку
        if self.sub_status in (None, "", "none"):
            return bool(self.trial_ends_at and self.trial_ends_at > datetime.utcnow())
        return False


class Chat(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    jid = db.Column(db.String(128), nullable=False)  # xxx@g.us
    name = db.Column(db.String(255))
    instruction = db.Column(db.Text, default="")
    enabled = db.Column(db.Boolean, default=True)
    messages = db.relationship("Message", backref="chat", cascade="all, delete-orphan")
    jobs = db.relationship("WebhookJob", backref="chat", cascade="all, delete-orphan")
    __table_args__ = (db.UniqueConstraint("user_id", "jid"),)


class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    chat_id = db.Column(db.Integer, db.ForeignKey("chat.id"), nullable=False, index=True)
    wa_id = db.Column(db.String(128), unique=True)
    sender = db.Column(db.String(255))
    ts = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    kind = db.Column(db.String(16), default="text")  # text/image/audio
    original = db.Column(db.Text)
    lang = db.Column(db.String(16))
    translation = db.Column(db.Text)  # на русском
    digested = db.Column(db.Boolean, default=False, index=True)


class Event(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    chat_id = db.Column(db.Integer, db.ForeignKey("chat.id"))
    title = db.Column(db.String(255), nullable=False)
    kind = db.Column(db.String(16), default="event")  # event/payment/bring/dress/deadline
    due = db.Column(db.DateTime, nullable=False, index=True)  # локальное «наивное» время пользователя
    all_day = db.Column(db.Boolean, default=True)
    details = db.Column(db.Text)
    amount = db.Column(db.String(64))
    reminded = db.Column(db.Boolean, default=False)
    done = db.Column(db.Boolean, default=False)


class WebhookJob(db.Model):
    """Очередь входящих сообщений: вебхук только записывает, воркер обрабатывает ИИ."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    chat_id = db.Column(db.Integer, db.ForeignKey("chat.id"), nullable=False)
    wa_id = db.Column(db.String(128), index=True)
    payload = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(16), default="pending", index=True)  # pending/processing
    attempts = db.Column(db.Integer, default=0)
    run_after = db.Column(db.DateTime, default=datetime.utcnow)
    started_at = db.Column(db.DateTime)
    last_error = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class StripeEvent(db.Model):
    """Уже обработанные события Stripe (идемпотентность вебхука)."""
    id = db.Column(db.String(64), primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Digest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    chat_id = db.Column(db.Integer, db.ForeignKey("chat.id"))
    day = db.Column(db.Date, index=True)
    summary = db.Column(db.Text)
    important = db.Column(db.Text)  # JSON-список
    terms = db.Column(db.Text)  # JSON-список {term, meaning}
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
