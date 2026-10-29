"""Отправка писем (сброс пароля) через SMTP."""
import logging
import smtplib
from email.message import EmailMessage

from config import Config

log = logging.getLogger("esther.mail")


def configured():
    return bool(Config.SMTP_HOST and Config.SMTP_FROM)


def send_mail(to, subject, body):
    """Возвращает True, если письмо передано SMTP-серверу."""
    if not configured():
        log.warning("SMTP не настроен, письмо для %s не отправлено", to)
        return False
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = Config.SMTP_FROM, to, subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(Config.SMTP_HOST, Config.SMTP_PORT, timeout=15) as s:
            if Config.SMTP_STARTTLS:
                s.starttls()
            if Config.SMTP_USER:
                s.login(Config.SMTP_USER, Config.SMTP_PASSWORD)
            s.send_message(msg)
        return True
    except Exception:
        log.exception("не удалось отправить письмо")
        return False
