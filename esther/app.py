import hashlib
import json
import logging
import re
import secrets
from datetime import datetime, timedelta, date
from functools import wraps
from zoneinfo import available_timezones

from flask import (Flask, render_template, request, redirect, url_for, flash,
                   jsonify, abort, Response)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import (LoginManager, login_user, logout_user, login_required,
                         current_user)
from flask_migrate import Migrate
from flask_wtf.csrf import CSRFProtect, CSRFError, generate_csrf
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from markupsafe import Markup
from sqlalchemy import text
from werkzeug.middleware.proxy_fix import ProxyFix

import billing
import evolution
import mailer
import pipeline
from config import Config, validate_config
from models import db, User, Chat, Message, Event, Digest

logging.basicConfig(level=logging.INFO)
validate_config()
if Config.SENTRY_DSN:
    import sentry_sdk
    # сообщения чатов содержат данные третьих лиц: тела запросов и локальные переменные не отправляем
    sentry_sdk.init(dsn=Config.SENTRY_DSN, send_default_pii=False, include_local_variables=False,
                    max_request_body_size="never", traces_sample_rate=0)
app = Flask(__name__)
app.config.from_object(Config)
if Config.PROXY_HOPS:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=Config.PROXY_HOPS, x_proto=Config.PROXY_HOPS,
                            x_host=Config.PROXY_HOPS)
db.init_app(app)
migrate = Migrate(app, db)  # схема БД: flask db upgrade
csrf = CSRFProtect(app)
limiter = Limiter(get_remote_address, app=app, default_limits=[])

lm = LoginManager(app)
lm.login_view = "login"
lm.login_message = "Войдите, чтобы продолжить."

PASSWORD_MIN, PASSWORD_MAX = 8, 128
RESET_MAX_AGE = 3600
INSTRUCTION_MAX = 2000


@lm.user_loader
def load_user(uid):
    return db.session.get(User, int(uid))


@app.template_global()
def csrf_input():
    return Markup(f'<input type="hidden" name="csrf_token" value="{generate_csrf()}">')


@app.errorhandler(CSRFError)
def csrf_failed(e):
    flash("Сессия устарела, повторите действие.", "err")
    return redirect(url_for("index"))


@app.errorhandler(429)
def too_many(e):
    return render_template("message.html", title="Слишком много попыток",
                           text="Подождите немного и попробуйте снова."), 429


@app.get("/health")
def health():
    try:
        db.session.execute(text("SELECT 1"))
    except Exception:
        return jsonify(status="db_error"), 503
    return jsonify(status="ok")


def subscribed(f):
    @wraps(f)
    @login_required
    def w(*a, **kw):
        if not current_user.has_access:
            flash("Пробный период закончился. Оформите подписку.", "warn")
            return redirect(url_for("billing_page"))
        return f(*a, **kw)
    return w


@app.template_filter("fromjson")
def fromjson(s):
    try:
        return json.loads(s or "[]")
    except Exception:
        return []


# ---------- юридические страницы ----------

@app.route("/legal/<doc>/<lang>")
def legal(doc, lang):
    from legal_texts import LEGAL
    if doc not in LEGAL or lang not in ("ru", "he"):
        abort(404)
    title, sections = LEGAL[doc][lang]
    vals = dict(company=Config.COMPANY_NAME, company_id=Config.COMPANY_ID,
                address=Config.COMPANY_ADDRESS, email=Config.CONTACT_EMAIL,
                retention_days=Config.RETENTION_DAYS, trial_days=Config.TRIAL_DAYS)
    vals = {k: (v or "[НЕ ЗАПОЛНЕНО]") for k, v in vals.items()}
    sections = [(h, t.format(**vals)) for h, t in sections]
    return render_template("legal.html", doc=doc, lang=lang, title=title, sections=sections)


# ---------- публичные ----------

@app.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return render_template("landing.html")


@app.route("/register", methods=["GET", "POST"])
@limiter.limit("10 per hour", methods=["POST"])
def register():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        pw = request.form.get("password", "")
        if not request.form.get("agree"):
            flash("Нужно принять условия и политику конфиденциальности.", "err")
        elif (len(email) > 255 or not re.match(r"[^@\s]+@[^@\s]+\.[^@\s]+$", email)
              or not PASSWORD_MIN <= len(pw) <= PASSWORD_MAX):
            flash(f"Нужен корректный email и пароль от {PASSWORD_MIN} до {PASSWORD_MAX} символов.", "err")
        elif User.query.filter_by(email=email).first():
            flash("Этот email уже зарегистрирован.", "err")
        else:
            u = User(email=email, trial_ends_at=datetime.utcnow() + timedelta(days=Config.TRIAL_DAYS))
            u.set_password(pw)
            db.session.add(u)
            db.session.commit()
            u.instance_name = f"esther_{u.id}_{secrets.token_hex(3)}"
            db.session.commit()
            login_user(u)
            return redirect(url_for("connect"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute; 50 per hour", methods=["POST"])
def login():
    if request.method == "POST":
        u = User.query.filter_by(email=request.form.get("email", "").strip().lower()).first()
        if u and u.check_password(request.form.get("password", "")):
            login_user(u, remember=True)
            return redirect(url_for("dashboard"))
        flash("Неверный email или пароль.", "err")
    return render_template("login.html")


@app.route("/logout")
def logout():
    logout_user()
    return redirect(url_for("index"))


# ---------- сброс пароля ----------

def _reset_serializer():
    return URLSafeTimedSerializer(app.config["SECRET_KEY"], salt="password-reset")


def _pw_fingerprint(u):
    # после смены пароля старые ссылки перестают работать
    return hashlib.sha256(u.password_hash.encode()).hexdigest()[:16]


def make_reset_token(u):
    return _reset_serializer().dumps({"id": u.id, "h": _pw_fingerprint(u)})


def user_from_reset_token(token):
    try:
        data = _reset_serializer().loads(token, max_age=RESET_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    u = db.session.get(User, data.get("id"))
    return u if u and data.get("h") == _pw_fingerprint(u) else None


@app.route("/forgot", methods=["GET", "POST"])
@limiter.limit("5 per hour", methods=["POST"])
def forgot():
    if request.method == "POST":
        u = User.query.filter_by(email=request.form.get("email", "").strip().lower()).first()
        if u:
            link = f"{Config.BASE_URL}{url_for('reset', token=make_reset_token(u))}"
            mailer.send_mail(
                u.email, "Эстер: сброс пароля",
                f"Чтобы задать новый пароль, откройте ссылку (действует 1 час):\n{link}\n\n"
                "Если вы не запрашивали сброс, просто проигнорируйте письмо.")
        # одинаковый ответ, чтобы не раскрывать, какие email зарегистрированы
        flash("Если такой email зарегистрирован, мы отправили на него ссылку для сброса пароля.", "ok")
        return redirect(url_for("login"))
    return render_template("forgot.html")


@app.route("/reset/<token>", methods=["GET", "POST"])
@limiter.limit("20 per hour", methods=["POST"])
def reset(token):
    u = user_from_reset_token(token)
    if not u:
        flash("Ссылка недействительна или устарела. Запросите новую.", "err")
        return redirect(url_for("forgot"))
    if request.method == "POST":
        pw = request.form.get("password", "")
        if not PASSWORD_MIN <= len(pw) <= PASSWORD_MAX:
            flash(f"Пароль от {PASSWORD_MIN} до {PASSWORD_MAX} символов.", "err")
        else:
            u.set_password(pw)
            db.session.commit()
            flash("Пароль изменён. Войдите с новым паролем.", "ok")
            return redirect(url_for("login"))
    return render_template("reset.html")


# ---------- подключение WhatsApp ----------

@app.route("/connect")
@subscribed
def connect():
    return render_template("connect.html")


@app.route("/api/qr")
@subscribed
def api_qr():
    u = current_user
    try:
        st = evolution.state(u.instance_name)
        if st == "missing":
            evolution.create_instance(
                u.instance_name, f"{Config.WEBHOOK_BASE}/webhook/evolution/{u.webhook_token}")
            st = "close"
        if st == "open":
            u.wa_connected = True
            db.session.commit()
            return jsonify(state="open")
        data = evolution.connect(u.instance_name)
        return jsonify(state=st, qr=data.get("base64"), code=data.get("pairingCode"))
    except Exception as e:
        logging.exception("qr failed")
        return jsonify(state="error", error=str(e)), 502


@app.route("/api/wa-state")
@login_required
def api_state():
    try:
        st = evolution.state(current_user.instance_name)
    except Exception:
        st = "error"
    current_user.wa_connected = st == "open"
    db.session.commit()
    return jsonify(state=st)


@app.route("/disconnect", methods=["POST"])
@login_required
def disconnect():
    try:
        evolution.logout(current_user.instance_name)
    except Exception:
        pass
    current_user.wa_connected = False
    db.session.commit()
    return redirect(url_for("connect"))


# ---------- чаты ----------

@app.route("/chats")
@subscribed
def chats():
    return render_template("chats.html", chats=Chat.query.filter_by(user_id=current_user.id).all())


@app.route("/api/groups")
@subscribed
def api_groups():
    try:
        groups = evolution.fetch_groups(current_user.instance_name)
    except Exception as e:
        return jsonify(error=str(e)), 502
    have = {c.jid for c in Chat.query.filter_by(user_id=current_user.id)}
    return jsonify(groups=[g for g in groups if g["jid"] not in have])


def _enabled_chats(user):
    return Chat.query.filter_by(user_id=user.id, enabled=True).count()


@app.route("/chats/add", methods=["POST"])
@subscribed
def chats_add():
    jid, name = request.form.get("jid", ""), request.form.get("name", "")[:255]
    if not jid.endswith("@g.us") or len(jid) > 128:
        abort(400)
    if not Chat.query.filter_by(user_id=current_user.id, jid=jid).first():
        if _enabled_chats(current_user) >= Config.MAX_CHATS:
            flash(f"Можно отслеживать до {Config.MAX_CHATS} чатов. Отключите или удалите ненужные.", "err")
            return redirect(url_for("chats"))
        db.session.add(Chat(user_id=current_user.id, jid=jid, name=name or jid,
                            instruction=request.form.get("instruction", "")[:INSTRUCTION_MAX]))
        db.session.commit()
    return redirect(url_for("chats"))


@app.route("/chats/<int:cid>", methods=["POST"])
@subscribed
def chats_edit(cid):
    c = Chat.query.filter_by(id=cid, user_id=current_user.id).first_or_404()
    if request.form.get("action") == "delete":
        db.session.delete(c)  # вместе с сохранёнными сообщениями
    else:
        enable = bool(request.form.get("enabled"))
        if enable and not c.enabled and _enabled_chats(current_user) >= Config.MAX_CHATS:
            flash(f"Можно отслеживать до {Config.MAX_CHATS} чатов.", "err")
            return redirect(url_for("chats"))
        c.instruction = request.form.get("instruction", "")[:INSTRUCTION_MAX]
        c.enabled = enable
    db.session.commit()
    return redirect(url_for("chats"))


# ---------- лента / календарь ----------

@app.route("/dashboard")
@subscribed
def dashboard():
    u = current_user
    digests = (Digest.query.filter_by(user_id=u.id).order_by(Digest.created_at.desc()).limit(20).all())
    chat_names = {c.id: c.name for c in Chat.query.filter_by(user_id=u.id)}
    upcoming = (Event.query.filter_by(user_id=u.id, done=False)
                .filter(Event.due >= datetime.now() - timedelta(days=1))
                .order_by(Event.due).limit(8).all())
    return render_template("dashboard.html", digests=digests, chat_names=chat_names, upcoming=upcoming)


@app.route("/messages/<int:cid>")
@subscribed
def messages(cid):
    c = Chat.query.filter_by(id=cid, user_id=current_user.id).first_or_404()
    ms = Message.query.filter_by(chat_id=c.id).order_by(Message.ts.desc()).limit(100).all()
    return render_template("messages.html", chat=c, msgs=ms)


@app.route("/calendar")
@subscribed
def calendar():
    evs = (Event.query.filter_by(user_id=current_user.id)
           .filter(Event.due >= datetime.now() - timedelta(days=30))
           .order_by(Event.due).all())
    return render_template("calendar.html", events=evs, icons=pipeline.KIND_ICON,
                           ics=f"{Config.BASE_URL}/ics/{current_user.ics_token}.ics")


@app.route("/events/add", methods=["POST"])
@subscribed
def event_add():
    title = request.form.get("title", "").strip()[:250]
    kind = request.form.get("kind", "event")
    if not title:
        flash("Укажите, что нужно сделать.", "err")
        return redirect(url_for("calendar"))
    if kind not in pipeline.VALID_KINDS:
        kind = "event"
    time_s = request.form.get("time", "").strip()
    try:
        d = datetime.strptime(request.form.get("date", "") + " " + (time_s or "09:00"), "%Y-%m-%d %H:%M")
    except ValueError:
        flash("Неверная дата или время.", "err")
        return redirect(url_for("calendar"))
    db.session.add(Event(user_id=current_user.id, title=title, kind=kind, due=d,
                         all_day=not time_s, details=(request.form.get("details") or "")[:1000] or None))
    db.session.commit()
    return redirect(url_for("calendar"))


@app.route("/events/<int:eid>/<action>", methods=["POST"])
@subscribed
def event_action(eid, action):
    if action not in ("done", "delete"):
        abort(404)
    e = Event.query.filter_by(id=eid, user_id=current_user.id).first_or_404()
    if action == "done":
        e.done = not e.done
    else:
        db.session.delete(e)
    db.session.commit()
    return redirect(url_for("calendar"))


@app.route("/ics/<token>.ics")
def ics(token):
    """Подписка из Google/Apple Calendar по секретной ссылке."""
    u = User.query.filter_by(ics_token=token).first_or_404()
    esc = lambda s: (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")
    out = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Esther//RU", "X-WR-CALNAME:Эстер"]
    for e in Event.query.filter_by(user_id=u.id).all():
        out += ["BEGIN:VEVENT", f"UID:esther-{e.id}@esther", f"DTSTAMP:{datetime.utcnow():%Y%m%dT%H%M%SZ}"]
        if e.all_day:
            out += [f"DTSTART;VALUE=DATE:{e.due:%Y%m%d}", f"DTEND;VALUE=DATE:{(e.due + timedelta(days=1)):%Y%m%d}"]
        else:
            out += [f"DTSTART:{e.due:%Y%m%dT%H%M%S}", f"DTEND:{(e.due + timedelta(hours=1)):%Y%m%dT%H%M%S}"]
        out += [f"SUMMARY:{esc(e.title)}", f"DESCRIPTION:{esc(e.details)}", "END:VEVENT"]
    out.append("END:VCALENDAR")
    return Response("\r\n".join(out), mimetype="text/calendar")


@app.route("/calendar/rotate-ics", methods=["POST"])
@subscribed
def ics_rotate():
    """Новая секретная ссылка календаря; старая сразу перестаёт работать."""
    current_user.ics_token = secrets.token_urlsafe(24)
    db.session.commit()
    flash("Ссылка на календарь обновлена. Подпишитесь по новой ссылке, старая больше не работает.", "ok")
    return redirect(url_for("calendar"))


@app.route("/account/export")
@login_required
def account_export():
    """Выгрузка всех данных пользователя в JSON (право на доступ к данным)."""
    u = current_user
    iso = lambda d: d.isoformat() if d else None
    chats = Chat.query.filter_by(user_id=u.id).all()
    data = {
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "account": {"email": u.email, "created_at": iso(u.created_at), "timezone": u.timezone,
                    "digest_time": u.digest_time, "remind_hours_before": u.remind_hours_before,
                    "phone": u.phone, "subscription_status": u.sub_status},
        "chats": [{
            "name": c.name, "instruction": c.instruction, "enabled": c.enabled,
            "messages": [{"time": iso(m.ts), "sender": m.sender, "kind": m.kind, "original": m.original,
                          "language": m.lang, "translation": m.translation}
                         for m in Message.query.filter_by(chat_id=c.id).order_by(Message.ts)],
        } for c in chats],
        "events": [{"title": e.title, "kind": e.kind, "due": iso(e.due), "all_day": e.all_day,
                    "details": e.details, "amount": e.amount, "done": e.done}
                   for e in Event.query.filter_by(user_id=u.id).order_by(Event.due)],
        "digests": [{"day": iso(d.day), "summary": d.summary, "important": fromjson(d.important),
                     "terms": fromjson(d.terms)}
                    for d in Digest.query.filter_by(user_id=u.id).order_by(Digest.created_at)],
    }
    return Response(json.dumps(data, ensure_ascii=False, indent=2), mimetype="application/json",
                    headers={"Content-Disposition": "attachment; filename=esther-export.json"})


# ---------- настройки / оплата ----------

@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    u = current_user
    if request.method == "POST":
        tz = request.form.get("timezone", "")
        digest_time = request.form.get("digest_time", "")
        phone = re.sub(r"\D", "", request.form.get("phone", ""))
        errors = []
        if tz not in available_timezones():
            errors.append("Неизвестный часовой пояс.")
        try:
            datetime.strptime(digest_time, "%H:%M")
        except ValueError:
            errors.append("Время разбора должно быть в формате ЧЧ:ММ.")
        try:
            hours = int(request.form.get("remind_hours_before") or 18)
        except ValueError:
            errors.append("Количество часов для напоминания должно быть числом.")
        if phone and not 8 <= len(phone) <= 15:
            errors.append("Номер WhatsApp: от 8 до 15 цифр с кодом страны.")
        if errors:
            for e in errors:
                flash(e, "err")
        else:
            u.timezone, u.digest_time = tz, digest_time
            u.remind_hours_before = max(1, min(72, hours))
            u.phone = phone or None
            db.session.commit()
            flash("Сохранено.", "ok")
    return render_template("settings.html")


@app.route("/account/delete", methods=["POST"])
@login_required
def account_delete():
    """Удаление аккаунта: подписка Stripe, инстанс WhatsApp и все данные."""
    u = current_user._get_current_object()  # после logout_user() прокси указывал бы на анонима
    try:
        billing.cancel_subscriptions(u)
    except Exception:
        logging.exception("stripe cancel failed on account delete")
        flash("Не удалось отменить подписку. Аккаунт не удалён, попробуйте позже или напишите в поддержку.", "err")
        return redirect(url_for("settings"))
    try:
        evolution.delete_instance(u.instance_name)
    except Exception:
        logging.exception("evolution delete failed on account delete")
    logout_user()
    db.session.delete(u)
    db.session.commit()
    return redirect(url_for("index"))


@app.route("/billing")
@login_required
def billing_page():
    return render_template("billing.html")


@app.route("/billing/checkout", methods=["POST"])
@login_required
def billing_checkout():
    if current_user.stripe_customer_id and current_user.sub_status in billing.LIVE_STATUSES:
        return redirect(billing.portal_url(current_user))  # подписка уже есть, второй раз не оформляем
    return redirect(billing.checkout_url(current_user))


@app.route("/billing/portal", methods=["POST"])
@login_required
def billing_portal():
    if not current_user.stripe_customer_id:
        return redirect(url_for("billing_page"))
    return redirect(billing.portal_url(current_user))


@app.route("/webhook/stripe", methods=["POST"])
@csrf.exempt  # подлинность проверяется подписью Stripe
def stripe_hook():
    try:
        billing.handle_event(request.get_data(), request.headers.get("Stripe-Signature", ""))
    except Exception:
        logging.exception("stripe webhook")
        return "bad", 400
    return "ok"


# ---------- вебхук Evolution ----------

@app.route("/webhook/evolution/<token>", methods=["POST"])
@csrf.exempt  # подлинность проверяется секретным токеном в URL
def evo_hook(token):
    u = User.query.filter_by(webhook_token=token).first()
    if not u:
        abort(404)
    if u.has_access:
        try:
            pipeline.handle_webhook(u, request.get_json(silent=True) or {})
        except Exception:
            logging.exception("webhook handling")
    return "ok"


if __name__ == "__main__":
    app.run(debug=True, port=8000)
