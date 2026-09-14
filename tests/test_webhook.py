import base64
import hashlib
import hmac
import json
from datetime import datetime


def sign(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def make_order(app_module, name="Cust", link_code="LINK01", phone="0800000000"):
    with app_module.app.app_context():
        db = app_module.get_db()
        now = datetime.utcnow().isoformat()
        db.execute(
            "INSERT INTO customers (name, phone, phone_normalized, created_at) VALUES (?, ?, ?, ?)",
            (name, phone, app_module._normalize_phone(phone), now),
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


# ---- Phase 2: phone-number registration ------------------------------------

def test_webhook_links_new_customer_by_phone(app_module, client, monkeypatch):
    monkeypatch.setattr(app_module, "line_reply", lambda *a, **k: True)
    cid = make_order(app_module, name="PhoneCust", link_code="PH0001", phone="081-234-5678")

    resp = post_webhook(client, "test-line-channel-secret", message_event("U1", "0812345678"))
    assert resp.status_code == 200

    with app_module.app.app_context():
        customer = app_module.get_db().execute(
            "SELECT * FROM customers WHERE line_user_id = ?", ("U1",)
        ).fetchone()
    assert customer is not None
    assert customer["id"] == cid


def test_webhook_phone_already_linked_refuses_second_user(app_module, client, monkeypatch):
    replies = []
    monkeypatch.setattr(app_module, "line_reply", lambda token, msg: replies.append(msg) or True)
    make_order(app_module, name="PhoneCust", link_code="PH0002", phone="0899998888")

    resp1 = post_webhook(client, "test-line-channel-secret", message_event("U1", "0899998888"))
    assert resp1.status_code == 200
    resp2 = post_webhook(client, "test-line-channel-secret", message_event("U2", "0899998888"))
    assert resp2.status_code == 200

    with app_module.app.app_context():
        db = app_module.get_db()
        u1 = db.execute("SELECT * FROM customers WHERE line_user_id = ?", ("U1",)).fetchone()
        u2 = db.execute("SELECT * FROM customers WHERE line_user_id = ?", ("U2",)).fetchone()
    assert u1 is not None
    assert u2 is None
    # Second reply carries the "already linked" text (as a text message dict).
    assert any(isinstance(r, dict) and "already linked" in r.get("text", "") for r in replies)


# ---- Phase 2: order history carousel ----------------------------------------

def test_webhook_my_orders_returns_flex_carousel(app_module, client, monkeypatch):
    replies = []
    monkeypatch.setattr(app_module, "line_reply", lambda token, msg: replies.append(msg) or True)
    make_order(app_module, name="CarouselCust", link_code="CAR001")
    post_webhook(client, "test-line-channel-secret", message_event("U1", "CAR001"))  # link first

    replies.clear()
    resp = post_webhook(client, "test-line-channel-secret", message_event("U1", "MY ORDERS"))
    assert resp.status_code == 200
    assert len(replies) == 1
    flex = replies[0][0]
    assert flex["type"] == "flex"
    assert flex["contents"]["type"] == "carousel"
    assert len(flex["contents"]["contents"]) == 1  # one order on file


# ---- Phase 2: escalation ------------------------------------------------------

def test_webhook_human_keyword_flags_needs_admin(app_module, client, monkeypatch):
    monkeypatch.setattr(app_module, "line_reply", lambda *a, **k: True)
    make_order(app_module, name="EscalateCust", link_code="ESC001")
    post_webhook(client, "test-line-channel-secret", message_event("U1", "ESC001"))  # link first

    resp = post_webhook(client, "test-line-channel-secret", message_event("U1", "HUMAN"))
    assert resp.status_code == 200

    with app_module.app.app_context():
        db = app_module.get_db()
        row = db.execute(
            "SELECT * FROM messages WHERE direction='in' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row["needs_admin"] == 1
    assert row["text"] == "HUMAN"


def test_webhook_zero_orders_fallback_flags_needs_admin(app_module, client, monkeypatch):
    monkeypatch.setattr(app_module, "line_reply", lambda *a, **k: True)
    with app_module.app.app_context():
        db = app_module.get_db()
        now = datetime.utcnow().isoformat()
        db.execute(
            "INSERT INTO customers (name, phone, line_user_id, created_at) VALUES (?, ?, ?, ?)",
            ("NoOrdersCust", "", "U-NOORDERS", now),
        )
        db.commit()

    # Message deliberately doesn't match any bilingual fast-path phrase (a
    # genuine greeting/status/etc. no longer escalates -- see test_v5.py) so
    # this exercises the true dead-end: no match, no AI key, zero orders.
    resp = post_webhook(client, "test-line-channel-secret", message_event("U-NOORDERS", "zzz completely unrelated message 999"))
    assert resp.status_code == 200

    with app_module.app.app_context():
        row = app_module.get_db().execute(
            "SELECT * FROM messages WHERE direction='in' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row["needs_admin"] == 1


def test_webhook_casual_message_with_orders_does_not_flag_needs_admin(app_module, client, monkeypatch):
    monkeypatch.setattr(app_module, "line_reply", lambda *a, **k: True)
    make_order(app_module, name="CasualCust", link_code="CAS001")
    post_webhook(client, "test-line-channel-secret", message_event("U1", "CAS001"))  # link first

    resp = post_webhook(client, "test-line-channel-secret", message_event("U1", "thanks so much!"))
    assert resp.status_code == 200

    with app_module.app.app_context():
        row = app_module.get_db().execute(
            "SELECT * FROM messages WHERE direction='in' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row["needs_admin"] == 0


# ---- Phase 2: LINE message transport helpers --------------------------------

def test_as_message_list_wraps_every_shape():
    import app as app_module
    # A bare string (existing call sites) wraps into a single text message.
    assert app_module._as_message_list("hi") == [{"type": "text", "text": "hi"}]
    # A single pre-built message dict must also be wrapped -- LINE's API
    # requires `messages` to always be a JSON array, even for one message.
    one = app_module._line_text("hi")
    assert app_module._as_message_list(one) == [one]
    # A list is passed through unchanged.
    many = [app_module._line_text("a"), app_module._line_text("b")]
    assert app_module._as_message_list(many) == many


def test_quick_reply_labels_fit_line_20_char_limit():
    """LINE truncates/rejects quick-reply labels over 20 chars -- lock in the
    exact labels the bot sends so a future edit can't silently regress this."""
    import app as app_module
    labels = [
        "📱 Register by phone", "🔑 I have a code",
        "📦 My Orders", app_module.TALK_TO_HUMAN_QUICK_REPLY[0][0],
    ]
    for label in labels:
        assert len(label) <= 20, label
