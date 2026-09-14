"""Phase 3: bot/human handoff window, LLM pre-filter, inbox image replies, local_dt filter."""
from datetime import datetime, timedelta

from conftest import login
from test_webhook import make_order, message_event, post_webhook


# ---- _human_recently_active --------------------------------------------------

def test_human_recently_active_true_for_fresh_admin_reply(app_module, client):
    with app_module.app.app_context():
        db = app_module.get_db()
        db.execute(
            "INSERT INTO customers (name, phone, created_at) VALUES ('C','0800000000',?)",
            (datetime.utcnow().isoformat(),),
        )
        cid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        app_module._log_message(cid, "out", "hi from admin", source="admin")
        assert app_module._human_recently_active(cid) is True


def test_human_recently_active_false_for_bot_reply(app_module, client):
    with app_module.app.app_context():
        db = app_module.get_db()
        db.execute(
            "INSERT INTO customers (name, phone, created_at) VALUES ('C','0800000001',?)",
            (datetime.utcnow().isoformat(),),
        )
        cid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        app_module._log_message(cid, "out", "bot reply", source="bot")
        assert app_module._human_recently_active(cid) is False


def test_human_recently_active_false_after_window_expires(app_module, client):
    with app_module.app.app_context():
        db = app_module.get_db()
        db.execute(
            "INSERT INTO customers (name, phone, created_at) VALUES ('C','0800000002',?)",
            (datetime.utcnow().isoformat(),),
        )
        cid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        old = (datetime.utcnow() - timedelta(minutes=20)).isoformat()
        db.execute(
            "INSERT INTO messages (customer_id, direction, text, created_at, source) VALUES (?, 'out', 'old', ?, 'admin')",
            (cid, old),
        )
        db.commit()
        assert app_module._human_recently_active(cid) is False


# ---- webhook suppression window -----------------------------------------------

def test_webhook_suppresses_bot_reply_within_15min_of_admin_reply(app_module, client, monkeypatch):
    calls = []
    monkeypatch.setattr(app_module, "line_reply", lambda *a, **k: calls.append(a) or True)
    make_order(app_module, name="HandoffCust", link_code="HAN001", phone="0812340001")
    post_webhook(client, "test-line-channel-secret", message_event("U1", "HAN001"))  # link
    calls.clear()

    with app_module.app.app_context():
        db = app_module.get_db()
        customer = db.execute("SELECT * FROM customers WHERE line_user_id = ?", ("U1",)).fetchone()
        app_module._log_message(customer["id"], "out", "human reply", source="admin")

    resp = post_webhook(client, "test-line-channel-secret", message_event("U1", "MY ORDERS"))
    assert resp.status_code == 200
    assert calls == []  # nothing sent -- full silence while human is handling it

    with app_module.app.app_context():
        db = app_module.get_db()
        logged = db.execute(
            "SELECT * FROM messages WHERE customer_id = ? AND direction = 'in' ORDER BY id DESC LIMIT 1",
            (customer["id"],),
        ).fetchone()
    assert logged["text"] == "MY ORDERS"  # still logged for the Inbox thread


def test_webhook_resumes_after_15min_window_expires(app_module, client, monkeypatch):
    calls = []
    monkeypatch.setattr(app_module, "line_reply", lambda *a, **k: calls.append(a) or True)
    make_order(app_module, name="ResumeCust", link_code="RES001", phone="0812340002")
    post_webhook(client, "test-line-channel-secret", message_event("U1", "RES001"))  # link
    calls.clear()

    with app_module.app.app_context():
        db = app_module.get_db()
        customer = db.execute("SELECT * FROM customers WHERE line_user_id = ?", ("U1",)).fetchone()
        old = (datetime.utcnow() - timedelta(minutes=20)).isoformat()
        db.execute(
            "INSERT INTO messages (customer_id, direction, text, created_at, source) VALUES (?, 'out', 'old human reply', ?, 'admin')",
            (customer["id"], old),
        )
        db.commit()

    resp = post_webhook(client, "test-line-channel-secret", message_event("U1", "MY ORDERS"))
    assert resp.status_code == 200
    assert len(calls) == 1  # bot replies normally again


# ---- Bilingual fast-path phrase matching (Phase 3d) --------------------------

def test_bilingual_phrase_matches_english_and_thai():
    import app as app_module
    assert app_module._match_bilingual_phrase("what's my status?") == "status"
    assert app_module._match_bilingual_phrase("พัสดุถึงไหนแล้ว") == "status"
    assert app_module._match_bilingual_phrase("Hello!") == "greeting"
    assert app_module._match_bilingual_phrase("สวัสดีครับ") == "greeting"
    assert app_module._match_bilingual_phrase("ขอบคุณค่ะ") == "thanks"
    assert app_module._match_bilingual_phrase("some totally unrelated gibberish 999") is None


def test_webhook_greeting_gets_friendly_reply_not_status_dump(app_module, client, monkeypatch):
    replies = []
    monkeypatch.setattr(app_module, "line_reply", lambda token, msg: replies.append(msg) or True)
    make_order(app_module, name="GreetCust", link_code="GRE001", phone="0812340010")
    post_webhook(client, "test-line-channel-secret", message_event("U1", "GRE001"))
    replies.clear()

    post_webhook(client, "test-line-channel-secret", message_event("U1", "Hello, is this the correct chat"))
    assert len(replies) == 1
    reply_text = replies[0]["text"] if isinstance(replies[0], dict) else replies[0]
    assert "GRE001" not in reply_text  # never dumps the tracking code/status for a plain greeting


def test_webhook_classify_intent_called_once_for_ambiguous_message(app_module, client, monkeypatch):
    calls = []
    monkeypatch.setattr(app_module, "line_reply", lambda *a, **k: True)
    monkeypatch.setattr(app_module, "OPENROUTER_API_KEY", "fake-key")
    monkeypatch.setattr(app_module, "classify_intent", lambda text: calls.append(text) or {
        "intent": "other", "name": "", "phone": "", "address": "", "item_description": "", "source_link": "",
    })
    make_order(app_module, name="AmbigCust", link_code="AMB001", phone="0812340011")
    post_webhook(client, "test-line-channel-secret", message_event("U1", "AMB001"))

    post_webhook(client, "test-line-channel-secret", message_event("U1", "some totally ambiguous free-form text"))
    assert calls == ["some totally ambiguous free-form text"]  # exactly one call


def test_webhook_new_order_via_ai_single_shot(app_module, client, monkeypatch):
    monkeypatch.setattr(app_module, "line_reply", lambda *a, **k: True)
    monkeypatch.setattr(app_module, "OPENROUTER_API_KEY", "fake-key")
    monkeypatch.setattr(app_module, "classify_intent", lambda text: {
        "intent": "new_order", "name": "", "phone": "", "address": "",
        "item_description": "Red bag from this link", "source_link": "https://example.com/bag",
    })
    make_order(app_module, name="AIOrderCust", link_code="AIO001", phone="0812340012")
    post_webhook(client, "test-line-channel-secret", message_event("U1", "AIO001"))

    with app_module.app.app_context():
        customer = app_module.get_db().execute("SELECT * FROM customers WHERE line_user_id = ?", ("U1",)).fetchone()

    post_webhook(client, "test-line-channel-secret", message_event("U1", "I want to order a red bag from this link https://example.com/bag"))

    with app_module.app.app_context():
        req = app_module.get_db().execute(
            "SELECT * FROM order_requests WHERE customer_id = ?", (customer["id"],)
        ).fetchone()
    assert req is not None
    assert req["item_description"] == "Red bag from this link"
    assert req["source_link"] == "https://example.com/bag"


# ---- Inbox image replies --------------------------------------------------------

def test_inbox_reply_with_image_sends_image_message(app_module, client, monkeypatch, tmp_path):
    pushed = []
    monkeypatch.setattr(app_module, "line_push", lambda uid, messages: pushed.append(messages) or True)
    login(client)
    with app_module.app.app_context():
        db = app_module.get_db()
        now = datetime.utcnow().isoformat()
        db.execute(
            "INSERT INTO customers (name, phone, line_user_id, created_at) VALUES (?, ?, ?, ?)",
            ("ImgCust", "0800009999", "U-IMG", now),
        )
        cid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.commit()

    from PIL import Image
    import io
    img = Image.new("RGB", (100, 100), color=(10, 20, 30))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    buf.seek(0)

    from io import BytesIO
    resp = client.post(
        "/admin/inbox",
        data={"customer_id": cid, "text": "here's a photo", "image": (buf, "test.jpg")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert len(pushed) == 1
    sent_messages = pushed[0]
    types = [m["type"] for m in sent_messages]
    assert "image" in types
    assert "text" in types
    img_msg = next(m for m in sent_messages if m["type"] == "image")
    assert img_msg["originalContentUrl"].startswith("http")
    assert img_msg["previewImageUrl"].startswith("http")

    with app_module.app.app_context():
        out_row = app_module.get_db().execute(
            "SELECT * FROM messages WHERE customer_id = ? AND direction='out' ORDER BY id DESC LIMIT 1", (cid,)
        ).fetchone()
    assert out_row["source"] == "admin"
    assert out_row["text"].startswith("[image]")


# ---- local_dt filter ------------------------------------------------------------

def test_local_dt_converts_utc_to_bangkok():
    import app as app_module
    # 2026-09-14T09:27:00 UTC -> Asia/Bangkok is UTC+7 -> 16:27
    assert app_module.local_dt("2026-09-14T09:27:00") == "14 Sep, 16:27"


def test_local_dt_handles_blank():
    import app as app_module
    assert app_module.local_dt("") == ""
    assert app_module.local_dt(None) == ""
