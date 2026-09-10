import importlib
import os
import sys

os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-prod")
os.environ.setdefault("ADMIN_PASSWORD", "test-admin-password")
os.environ.setdefault("LINE_CHANNEL_SECRET", "test-line-channel-secret")
os.environ.setdefault("FLASK_ENV", "development")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest


@pytest.fixture
def app_module(tmp_path, monkeypatch):
    """Fresh `app` module instance backed by a temp SQLite DB per test."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db_path))

    import app as app_module

    importlib.reload(app_module)
    app_module.init_db()
    app_module.app.config["TESTING"] = True
    # CSRF is exercised explicitly in test_csrf.py; disabled elsewhere so
    # tests can focus on the behavior under test without fetching tokens.
    app_module.app.config["WTF_CSRF_ENABLED"] = False
    return app_module


@pytest.fixture
def client(app_module):
    with app_module.app.test_client() as c:
        yield c


def login(client, password="test-admin-password"):
    return client.post("/admin/login", data={"password": password}, follow_redirects=True)
