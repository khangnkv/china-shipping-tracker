import base64
import hashlib
import hmac
import json
from datetime import datetime


def sign(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def make_order(app_module, name="Cust", link_code="LINK01"):
    with app_module.app.app_context():
        db = app_module.get_db()
        now = datetime.utcnow().isoformat()
        db.execute(
            "INSERT INTO customers (name, phone, created_at) VALUES (?, ?, ?)",
            (name, "0800000000", now),
        )
        customer_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute(
            """INSERT INTO orders (customer_id, tracking_mode, tracking_lot, status,
               link_code, created_at, updated_at) VALUES (?, ?, ?, 'ordered', ?, ?, ?)""",
            (customer_id, "รถ", 11278, link_code, now, now),
        )
        db.commit()
    return customer_id


def message_event(user_id, text):
    return {
        "events": [
            {
                "type": "message",
                "replyToken": "dummy-reply-token",
                "source": {"userId": user_id},
                "message": {"type": "text", "text": text},
            }
        ]
    }


def post_webhook(client, secret, payload):
    body = json.dumps(payload).encode("utf-8")
    signature = sign(body, secret)
    return client.post(
        "/webhook",
        data=body,
        headers={"X-Line-Signature": signature, "Content-Type": "application/json"},
    )


def test_webhook_rejects_missing_signature(client):
    body = json.dumps(message_event("U1", "LINK01")).encode("utf-8")
    resp = client.post("/webhook", data=body, headers={"Content-Type": "application/json"})
    assert resp.status_code == 400


def test_webhook_fails_closed_when_secret_unset(app_module, client, monkeypatch):
    monkeypatch.setattr(app_module, "LINE_CHANNEL_SECRET", "")
    body = json.dumps(message_event("U1", "LINK01")).encode("utf-8")
    resp = client.post(
        "/webhook",
        data=body,
        headers={"X-Line-Signature": "anything", "Content-Type": "application/json"},
    )
    assert resp.status_code == 400


def test_webhook_links_new_customer_with_valid_code(app_module, client, monkeypatch):
    monkeypatch.setattr(app_module, "line_reply", lambda *a, **k: True)
    make_order(app_module, link_code="LINK01")

    resp = post_webhook(client, "test-line-channel-secret", message_event("U1", "LINK01"))
    assert resp.status_code == 200

    with app_module.app.app_context():
        db = app_module.get_db()
        customer = db.execute("SELECT * FROM customers WHERE line_user_id = ?", ("U1",)).fetchone()
    assert customer is not None


def test_webhook_blocks_link_code_reuse_by_different_user(app_module, client, monkeypatch):
    replies = []
    monkeypatch.setattr(app_module, "line_reply", lambda token, text: replies.append(text) or True)
    make_order(app_module, link_code="LINK01")

    resp1 = post_webhook(client, "test-line-channel-secret", message_event("U1", "LINK01"))
    assert resp1.status_code == 200

    resp2 = post_webhook(client, "test-line-channel-secret", message_event("U2", "LINK01"))
    assert resp2.status_code == 200

    with app_module.app.app_context():
        db = app_module.get_db()
        customer = db.execute("SELECT * FROM customers WHERE line_user_id = ?", ("U1",)).fetchone()
        hijacked = db.execute("SELECT * FROM customers WHERE line_user_id = ?", ("U2",)).fetchone()

    assert customer is not None
    assert hijacked is None
    assert any("already been used" in r for r in replies)


def test_webhook_second_code_for_already_linked_line_user_does_not_500(app_module, client, monkeypatch):
    monkeypatch.setattr(app_module, "line_reply", lambda *a, **k: True)
    make_order(app_module, name="Cust1", link_code="LINK01")
    make_order(app_module, name="Cust2", link_code="LINK02")

    resp1 = post_webhook(client, "test-line-channel-secret", message_event("U1", "LINK01"))
    assert resp1.status_code == 200

    resp2 = post_webhook(client, "test-line-channel-secret", message_event("U1", "LINK02"))
    assert resp2.status_code == 200
