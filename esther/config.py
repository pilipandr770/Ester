import logging
import os
from dotenv import load_dotenv

load_dotenv()

_WEAK_SECRETS = {"", "dev", "change-me", "changeme", "secret"}


def _flag(name, default="0"):
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


class Config:
    APP_ENV = os.getenv("APP_ENV", "production").strip().lower()  # production | development
    SECRET_KEY = os.getenv("SECRET_KEY", "")
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", "sqlite:///esther.db")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")

    # Cookie и прокси
    PROXY_HOPS = int(os.getenv("PROXY_HOPS", "1"))  # сколько reverse proxy перед приложением
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = BASE_URL.startswith("https://")
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_SECURE = SESSION_COOKIE_SECURE
    WTF_CSRF_TIME_LIMIT = None  # токен живёт столько же, сколько сессия
    RATELIMIT_ENABLED = _flag("RATELIMIT_ENABLED", "1")
    # memory:// считается отдельно в каждом воркере gunicorn; для точных лимитов укажите redis://
    RATELIMIT_STORAGE_URI = os.getenv("RATELIMIT_STORAGE_URI", "memory://")

    # Почта (сброс пароля)
    SMTP_HOST = os.getenv("SMTP_HOST", "")
    SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
    SMTP_USER = os.getenv("SMTP_USER", "")
    SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
    SMTP_FROM = os.getenv("SMTP_FROM", "")
    SMTP_STARTTLS = _flag("SMTP_STARTTLS", "1")

    EVOLUTION_URL = os.getenv("EVOLUTION_URL", "http://localhost:8080")
    EVOLUTION_API_KEY = os.getenv("EVOLUTION_API_KEY", "")
    WEBHOOK_BASE = os.getenv("WEBHOOK_BASE", "http://web:8000")

    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
    MODEL_FAST = os.getenv("MODEL_FAST", "claude-haiku-5-5")
    MODEL_SMART = os.getenv("MODEL_SMART", "claude-sonnet-5-5")

    STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
    STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID", "")
    STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    STRIPE_AUTOMATIC_TAX = _flag("STRIPE_AUTOMATIC_TAX")  # нужен включённый Stripe Tax в дашборде
    TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "7"))

    # Оператор (юрлицо в Израиле) и хранение данных
    COMPANY_NAME = os.getenv("COMPANY_NAME", "")
    COMPANY_ID = os.getenv("COMPANY_ID", "")
    COMPANY_ADDRESS = os.getenv("COMPANY_ADDRESS", "")
    CONTACT_EMAIL = os.getenv("CONTACT_EMAIL", "")
    RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "30"))

    # Лимиты расходов на ИИ и очередь
    MAX_CHATS = int(os.getenv("MAX_CHATS", "10"))  # включённых групп на пользователя
    MAX_MESSAGES_PER_DAY = int(os.getenv("MAX_MESSAGES_PER_DAY", "500"))
    MAX_MEDIA_PER_DAY = int(os.getenv("MAX_MEDIA_PER_DAY", "40"))  # фото и голосовые (OCR / Whisper)
    MAX_MEDIA_MB = int(os.getenv("MAX_MEDIA_MB", "10"))
    MAX_AUDIO_SECONDS = int(os.getenv("MAX_AUDIO_SECONDS", "300"))
    JOB_MAX_ATTEMPTS = int(os.getenv("JOB_MAX_ATTEMPTS", "5"))

    SENTRY_DSN = os.getenv("SENTRY_DSN", "")
    MAX_CONTENT_LENGTH = 2 * 1024 * 1024  # вебхуки и формы маленькие
    @classmethod
    def is_production(cls):
        return cls.APP_ENV == "production"


def _placeholder(v):
    v = (v or "").strip()
    return not v or v.startswith("[") or "example.com" in v


def validate_config():
    """В production падаем на небезопасной или неполной конфигурации, иначе только предупреждаем."""
    log = logging.getLogger("esther.config")
    errors, warnings = [], []

    if Config.SECRET_KEY.strip().lower() in _WEAK_SECRETS or len(Config.SECRET_KEY) < 16:
        errors.append("SECRET_KEY не задан или слишком простой (нужно минимум 16 символов)")
    if Config.EVOLUTION_API_KEY.strip().lower() in _WEAK_SECRETS or Config.EVOLUTION_API_KEY.startswith("change-me"):
        errors.append("EVOLUTION_API_KEY не задан или оставлен значением по умолчанию")
    for name in ("COMPANY_NAME", "COMPANY_ID", "COMPANY_ADDRESS", "CONTACT_EMAIL"):
        if _placeholder(getattr(Config, name)):
            errors.append(f"{name} не заполнен: он подставляется в условия и политику конфиденциальности")
    if not Config.BASE_URL.startswith("https://"):
        warnings.append("BASE_URL без https: cookie будут без флага Secure")
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "STRIPE_SECRET_KEY",
                 "STRIPE_PRICE_ID", "STRIPE_WEBHOOK_SECRET"):
        if not getattr(Config, name):
            warnings.append(f"{name} не задан")
    if not (Config.SMTP_HOST and Config.SMTP_FROM):
        warnings.append("SMTP не настроен: письма для сброса пароля отправляться не будут")

    for w in warnings:
        log.warning("config: %s", w)
    if errors:
        msg = "Небезопасная конфигурация: " + "; ".join(errors)
        if Config.is_production():
            raise RuntimeError(msg)
        log.warning("%s (APP_ENV=%s, продолжаем)", msg, Config.APP_ENV)
