"""Фоновый воркер: вечерний дайджест и напоминания. Запускается отдельным процессом."""
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler

import evolution
import mailer
import pipeline
from app import app
from config import Config
from models import db, User, Message

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("worker")


def send_wa_alerts():
    """Письмо тем, у кого WhatsApp отключился (бан, выход с телефона, обрыв сессии)."""
    with app.app_context():
        try:
            for u in User.query.filter(User.wa_alert_pending.is_(True)).all():
                u.wa_alert_pending = False  # одно письмо на одно отключение, даже если SMTP недоступен
                db.session.commit()
                if u.has_access:
                    mailer.send_mail(
                        u.email, "Эстер: WhatsApp отключился",
                        "Эстер больше не получает сообщения: WhatsApp отключился от сервиса.\n"
                        f"Откройте {Config.BASE_URL}/connect и отсканируйте QR-код заново.")
        except Exception:
            db.session.rollback()
            log.exception("wa alerts")


def tick():
    send_wa_alerts()
    with app.app_context():
        now_utc = datetime.now(ZoneInfo("UTC"))
        for u in User.query.filter(User.wa_connected.is_(True)).all():
            if not u.has_access:
                continue

            # 1) вечерний дайджест: не раньше заданного времени, один раз в сутки
            try:
                made = pipeline.maybe_run_digest(u, now_utc)
            except Exception:
                db.session.rollback()
                log.exception("digest user %s", u.id)
                made = []
            if made and u.phone:
                try:
                    evolution.send_text(
                        u.instance_name, u.phone,
                        "📬 Эстер: вечерний разбор готов (" + ", ".join(made) + "). Откройте приложение.")
                except Exception:
                    log.exception("digest notice user %s", u.id)

            # 2) напоминания
            try:
                pipeline.send_due_reminders(u, now_utc)
            except Exception:
                db.session.rollback()
                log.exception("reminders user %s", u.id)


def jobs():
    """Обработка очереди входящих сообщений (перевод, OCR, голосовые)."""
    with app.app_context():
        try:
            pipeline.process_jobs()
        except Exception:
            db.session.rollback()
            log.exception("job queue")


def cleanup():
    """Удаляем сообщения старше RETENTION_DAYS (политика конфиденциальности)."""
    with app.app_context():
        cutoff = datetime.utcnow() - timedelta(days=Config.RETENTION_DAYS)
        n = Message.query.filter(Message.ts < cutoff).delete()
        db.session.commit()
        if n:
            log.info("retention: deleted %s messages", n)


if __name__ == "__main__":
    s = BlockingScheduler(timezone="UTC")
    s.add_job(tick, "interval", minutes=1, max_instances=1, coalesce=True)
    s.add_job(jobs, "interval", seconds=5, max_instances=1, coalesce=True)
    s.add_job(cleanup, "cron", hour=3, minute=15)
    log.info("Esther worker started")
    s.start()
