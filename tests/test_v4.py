"""Phase 2.5: order requests, admin inbox, chat-based contact-info/reorder flows."""
from datetime import datetime

from conftest import login
from test_webhook import make_order, message_event, post_webhook


# ---- _find_or_create_customer / new_order reuse ----------------------------

def test_find_or_create_customer_dedupes_by_phone(app_module, client):
    with app_module.app.app_context():
        db = app_module.get_db()
        cid1 = app_module._find_or_create_customer("Somchai", "081-234-5678", "Addr A")
        db.commit()
        cid2 = app_module._find_or_create_customer("Somchai J", "0812345678", "Addr B")
        db.commit()
        assert cid1 == cid2
        c = db.execute("SELECT * FROM customers WHERE id = ?", (cid1,)).fetchone()
    assert c["name"] == "Somchai J"
    assert c["address"] == "Addr B"


def test_find_or_create_customer_blank_phone_never_merges(app_module, client):
    with app_module.app.app_context():
        db = app_module.get_db()
        cid1 = app_module._find_or_create_customer("A", "", "")
        db.commit()
        cid2 = app_module._find_or_create_customer("B", "", "")
        db.commit()
    assert cid1 != cid2


# ---- public /request page ---------------------------------------------------

def test_request_submission_creates_customer_and_request(app_module, client):
    resp = client.post(
        "/request",
        data={"name": "New Cust", "phone": "0891112222", "address": "Bangkok", "item_description": "A cool gadget"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    with app_module.app.app_context():
        db = app_module.get_db()
        customer = db.execute("SELECT * FROM customers WHERE phone_normalized = ?", ("0891112222",)).fetchone()
        req = db.execute("SELECT * FROM order_requests WHERE customer_id = ?", (customer["id"],)).fetchone()
    assert customer is not None
    assert req is not None
    assert req["item_description"] == "A cool gadget"
    assert req["status"] == "new"
    assert req["request_code"] in resp.get_data(as_text=True)


def test_request_reuses_existing_customer_by_phone(app_module, client):
    with app_module.app.app_context():
        cid = app_module._find_or_create_customer("Existing", "0899990000", "Somewhere")
    client.post(
        "/request",
        data={"name": "Existing", "phone": "089-999-0000", "address": "Somewhere", "item_description": "Another item"},
        follow_redirects=True,
    )
    with app_module.app.app_context():
        db = app_module.get_db()
        count = db.execute("SELECT COUNT(*) FROM customers WHERE phone_normalized = '0899990000'").fetchone()[0]
        req = db.execute("SELECT * FROM order_requests WHERE customer_id = ?", (cid,)).fetchone()
    assert count == 1
    assert req is not None


def test_request_missing_fields_rejected(app_module, client):
    resp = client.post("/request", data={"name": "", "phone": "", "item_description": ""}, follow_redirects=True)
    assert b"fill in" in resp.data
    with app_module.app.app_context():
        count = app_module.get_db().execute("SELECT COUNT(*) FROM order_requests").fetchone()[0]
    assert count == 0


def test_view_request_edit_updates_customer_and_request(app_module, client):
    client.post(
        "/request",
        data={"name": "Edit Me", "phone": "0812223333", "item_description": "First item"},
        follow_redirects=True,
    )
    with app_module.app.app_context():
        req = app_module.get_db().execute("SELECT * FROM order_requests").fetchone()
    code = req["request_code"]

    resp = client.get(f"/request/{code}")
    assert resp.status_code == 200

    client.post(
        f"/request/{code}",
        data={"name": "Edit Me Updated", "phone": "0812223333", "address": "New Addr", "item_description": "Updated item"},
        follow_redirects=True,
    )
    with app_module.app.app_context():
        db = app_module.get_db()
        updated = db.execute("SELECT * FROM order_requests WHERE request_code = ?", (code,)).fetchone()
        cust = db.execute("SELECT * FROM customers WHERE id = ?", (updated["customer_id"],)).fetchone()
    assert updated["item_description"] == "Updated item"
    assert cust["name"] == "Edit Me Updated"
    assert cust["address"] == "New Addr"


def test_view_request_not_found(app_module, client):
    resp = client.get("/request/ZZZZZZ")
    assert resp.status_code == 404


# ---- /admin/requests conversion ---------------------------------------------

def test_admin_converts_request_to_order(app_module, client):
    login(client)
    client.post(
        "/request",
        data={"name": "Convert Me", "phone": "0855556666", "item_description": "Cool thing", "source_link": "https://example.com/item"},
        follow_redirects=True,
    )
    with app_module.app.app_context():
        req = app_module.get_db().execute("SELECT * FROM order_requests").fetchone()

    resp = client.post(
        "/admin/requests",
        data={"request_id": req["id"], "mode": "รถ", "lot": "99999"},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    with app_module.app.app_context():
        db = app_module.get_db()
        updated_req = db.execute("SELECT * FROM order_requests WHERE id = ?", (req["id"],)).fetchone()
        order = db.execute("SELECT * FROM orders WHERE id = ?", (updated_req["converted_order_id"],)).fetchone()
    assert updated_req["status"] == "converted"
    assert order is not None
    assert order["tracking_lot"] == 99999
    assert order["source_link"] == "https://example.com/item"
    assert order["item_desc_en"] == "Cool thing"


def test_admin_convert_rejects_bad_mode(app_module, client):
    login(client)
    client.post("/request", data={"name": "X", "phone": "0800001111", "item_description": "Y"}, follow_redirects=True)
    with app_module.app.app_context():
        req = app_module.get_db().execute("SELECT * FROM order_requests").fetchone()

    resp = client.post("/admin/requests", data={"request_id": req["id"], "mode": "bogus", "lot": "1"}, follow_redirects=True)
    assert b"valid mode" in resp.data
    with app_module.app.app_context():
        still_new = app_module.get_db().execute("SELECT status FROM order_requests WHERE id = ?", (req["id"],)).fetchone()
    assert still_new["status"] == "new"


# ---- LINE chat: NEW ORDER reorder flow --------------------------------------

def test_webhook_new_order_flow_creates_request_without_reasking_contact(app_module, client, monkeypatch):
    monkeypatch.setattr(app_module, "line_reply", lambda *a, **k: True)
    make_order(app_module, name="ReorderCust", link_code="REO001", phone="0877778888")
    post_webhook(client, "test-line-channel-secret", message_event("U1", "REO001"))  # link first

    post_webhook(client, "test-line-channel-secret", message_event("U1", "NEW ORDER"))
    post_webhook(client, "test-line-channel-secret", message_event("U1", "Another cool gadget, size L"))

    with app_module.app.app_context():
        db = app_module.get_db()
        customer = db.execute("SELECT * FROM customers WHERE line_user_id = ?", ("U1",)).fetchone()
        reqs = db.execute("SELECT * FROM order_requests WHERE customer_id = ?", (customer["id"],)).fetchall()
    assert len(reqs) == 1
    assert reqs[0]["item_description"] == "Another cool gadget, size L"


# ---- LINE chat: contact-info blob capture -----------------------------------

def test_webhook_contact_info_blob_updates_customer_and_shipping_info(app_module, client, monkeypatch):
    # Phase 3d: contact-info capture now goes through the unified
    # classify_intent() call (OpenRouter) instead of a standalone
    # parse_customer()-based pre-filter.
    monkeypatch.setattr(app_module, "line_reply", lambda *a, **k: True)
    monkeypatch.setattr(app_module, "OPENROUTER_API_KEY", "fake-key-for-test")
    monkeypatch.setattr(app_module, "_match_bilingual_phrase", lambda text: None)
    monkeypatch.setattr(
        app_module, "classify_intent",
        lambda text: {
            "intent": "contact_info", "name": "Parsed Name", "phone": "0899998888",
            "address": "123 Parsed St", "item_description": "", "source_link": "",
        },
    )
    make_order(app_module, name="BlobCust", link_code="BLB001", phone="0899998888")
    post_webhook(client, "test-line-channel-secret", message_event("U1", "BLB001"))  # link first

    post_webhook(client, "test-line-channel-secret", message_event("U1", "Parsed Name 0899998888 123 Parsed St"))

    with app_module.app.app_context():
        db = app_module.get_db()
        customer = db.execute("SELECT * FROM customers WHERE line_user_id = ?", ("U1",)).fetchone()
        order = db.execute("SELECT * FROM orders WHERE customer_id = ?", (customer["id"],)).fetchone()
    assert customer["name"] == "Parsed Name"
    assert customer["address"] == "123 Parsed St"
    assert order["shipping_info"] == "Parsed Name 0899998888 123 Parsed St"


def test_webhook_contact_info_blob_skipped_without_api_key(app_module, client, monkeypatch):
    monkeypatch.setattr(app_module, "line_reply", lambda *a, **k: True)
    monkeypatch.setattr(app_module, "OPENROUTER_API_KEY", "")  # no key configured
    make_order(app_module, name="NoKeyCust", link_code="NOK001", phone="0866665555")
    post_webhook(client, "test-line-channel-secret", message_event("U1", "NOK001"))

    resp = post_webhook(client, "test-line-channel-secret", message_event("U1", "some random text"))
    assert resp.status_code == 200  # falls through to the safe fallback, no crash

    with app_module.app.app_context():
        db = app_module.get_db()
        customer = db.execute("SELECT * FROM customers WHERE line_user_id = ?", ("U1",)).fetchone()
        order = db.execute("SELECT * FROM orders WHERE customer_id = ?", (customer["id"],)).fetchone()
    assert customer["name"] == "NoKeyCust"  # unchanged
    assert order["shipping_info"] is None


# ---- SUPPORT keyword ---------------------------------------------------------

def test_webhook_support_keyword_flags_needs_admin(app_module, client, monkeypatch):
    monkeypatch.setattr(app_module, "line_reply", lambda *a, **k: True)
    make_order(app_module, name="SupportCust", link_code="SUP001")
    post_webhook(client, "test-line-channel-secret", message_event("U1", "SUP001"))

    post_webhook(client, "test-line-channel-secret", message_event("U1", "SUPPORT"))
    with app_module.app.app_context():
        row = app_module.get_db().execute(
            "SELECT * FROM messages WHERE direction='in' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row["needs_admin"] == 1


# ---- Admin inbox reply -------------------------------------------------------

def test_admin_inbox_reply_clears_needs_admin(app_module, client, monkeypatch):
    monkeypatch.setattr(app_module, "line_push", lambda *a, **k: True)
    login(client)
    with app_module.app.app_context():
        db = app_module.get_db()
        now = datetime.utcnow().isoformat()
        db.execute(
            "INSERT INTO customers (name, phone, line_user_id, created_at) VALUES (?, ?, ?, ?)",
            ("InboxCust", "0800001234", "U-INBOX", now),
        )
        cid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute(
            "INSERT INTO messages (customer_id, direction, text, created_at, needs_admin) VALUES (?, 'in', ?, ?, 1)",
            (cid, "help me please", now),
        )
        db.commit()

    resp = client.get("/admin/inbox")
    assert resp.status_code == 200
    assert b"InboxCust" in resp.data

    client.post("/admin/inbox", data={"customer_id": cid, "text": "Sure, here's the answer!"}, follow_redirects=True)

    with app_module.app.app_context():
        db = app_module.get_db()
        flagged = db.execute("SELECT COUNT(*) FROM messages WHERE customer_id = ? AND needs_admin = 1", (cid,)).fetchone()[0]
        out_row = db.execute("SELECT * FROM messages WHERE customer_id = ? AND direction = 'out' ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
    assert flagged == 0
    assert out_row["text"] == "Sure, here's the answer!"


def test_admin_requests_page_loads(app_module, client):
    login(client)
    resp = client.get("/admin/requests")
    assert resp.status_code == 200


def test_nav_badge_counts_reflect_pending_state(app_module, client):
    login(client)
    client.post("/request", data={"name": "Badge", "phone": "0811119999", "item_description": "x"}, follow_redirects=True)
    resp = client.get("/admin/orders")
    assert b"Requests" in resp.data
    # badge shows "1" somewhere near the Requests link
    assert b'>1</span>' in resp.data or b'>1<' in resp.data
