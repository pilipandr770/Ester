import os
import re
import sys
import tempfile

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

_db = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ.update(
    APP_ENV="development",
    SECRET_KEY="test-secret-key-0123456789",
    DATABASE_URL="sqlite:///" + _db.replace("\\", "/"),
    STRIPE_WEBHOOK_SECRET="whsec_test",
)

from app import app as flask_app, limiter  # noqa: E402
from models import db  # noqa: E402


@pytest.fixture()
def app():
    flask_app.config.update(TESTING=True)
    with flask_app.app_context():
        db.drop_all()
        db.create_all()
        limiter.reset()
        yield flask_app
        db.session.remove()


@pytest.fixture()
def client(app):
    return app.test_client()


from tests.helpers import post, csrf_token  # noqa: E402,F401


@pytest.fixture()
def make_user(app):
    from datetime import datetime, timedelta
    from models import User

    def _make(email="a@example.org", password="password123", **fields):
        u = User(email=email, trial_ends_at=datetime.utcnow() + timedelta(days=7), **fields)
        u.set_password(password)
        db.session.add(u)
        db.session.commit()
        u.instance_name = f"esther_{u.id}_test"
        db.session.commit()
        return u
    return _make


@pytest.fixture()
def logged_in(client, make_user):
    u = make_user()
    r = post(client, "/login", {"email": u.email, "password": "password123"})
    assert r.status_code == 302
    return client, u
