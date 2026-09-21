"""Phase 6: request->order conversion no longer drops data, customer info
is editable/non-destructively updated everywhere, other_contact lives on
the customer record and pre-fills on LINE reorder, info-source notes, and
the expanded language set (vi/zh/my)."""
from io import BytesIO

from PIL import Image

from conftest import login


def _submit_request(client, name="Convert Me", phone="0855556666", **extra):
    data = {"name": name, "phone": phone, "item_description": "Cool thing", "agree_terms": "on"}
    data.update(extra)
    return client.post("/request", data=data, follow_redirects=True)


# ---- Bug A: convert branch carries reference_image / item_cost / shipping_cost ----

def test_convert_carries_photo_and_quote_into_the_order(app_module, client):
    login(client)
    buf = BytesIO()
    Image.new("RGB", (40, 40), "green").save(buf, format="JPEG")
    buf.seek(0)
    client.post(
        "/request",
        data={"name": "Photo Convert", "phone": "0855551111", "item_description": "Thing",
              "agree_terms": "on", "reference_image": (buf, "item.jpg")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    with app_module.app.app_context():
        req = app_module.get_db().execute("SELECT * FROM order_requests").fetchone()
    assert req["reference_image"] is not None

    client.post(f"/admin/requests/{req['id']}/quote", data={"item_cost": "800", "shipping_cost": "150"}, follow_redirects=True)
    resp = client.post("/admin/requests", data={"request_id": req["id"], "mode": "รถ", "lot": "70001"}, follow_redirects=True)
    assert resp.status_code == 200
    assert b"House ship" in resp.data

    with app_module.app.app_context():
        db = app_module.get_db()
        updated_req = db.execute("SELECT * FROM order_requests WHERE id = ?", (req["id"],)).fetchone()
        order = db.execute("SELECT * FROM orders WHERE id = ?", (updated_req["converted_order_id"],)).fetchone()
    assert order["item_image"] == req["reference_image"]
    assert order["item_cost"] == 800.0
    assert order["house_ship_fee"] == 150.0
    assert order["payment_amount"] is None  # quoted, not yet actually received


# ---- Bug B: a blank field on resubmit never erases a previously-good value ----

def test_find_or_create_customer_blank_address_does_not_erase_existing(app_module, client):
    with app_module.app.app_context():
        cid1 = app_module._find_or_create_customer("Somchai", "0899990000", "123 Real St")
        app_module.get_db().commit()
        cid2 = app_module._find_or_create_customer("Somchai", "0899990000", "")  # blank on resubmit
        app_module.get_db().commit()
        row = app_module.get_db().execute("SELECT address FROM customers WHERE id = ?", (cid1,)).fetchone()
    assert cid1 == cid2
    assert row["address"] == "123 Real St"


def test_view_request_edit_blank_address_does_not_erase_existing(app_module, client):
    _submit_request(client, name="Edit Blank", phone="0855552222", address="Original Address")
    with app_module.app.app_context():
        req = app_module.get_db().execute("SELECT * FROM order_requests").fetchone()
    client.post(
        f"/request/{req['request_code']}",
        data={"name": "Edit Blank", "phone": "0855552222", "address": "", "item_description": "Cool thing"},
        follow_redirects=True,
    )
    with app_module.app.app_context():
        cust = app_module.get_db().execute("SELECT address FROM customers WHERE id = ?", (req["customer_id"],)).fetchone()
    assert cust["address"] == "Original Address"


# ---- Feature C: other_contact lives on the customer, pre-fills on LINE reorder ----

def test_other_contact_persists_to_customer_on_request_submit(app_module, client):
    _submit_request(client, other_contact="@somchai_line")
    with app_module.app.app_context():
        req = app_module.get_db().execute("SELECT * FROM order_requests").fetchone()
        cust = app_module.get_db().execute("SELECT other_contact FROM customers WHERE id = ?", (req["customer_id"],)).fetchone()
    assert cust["other_contact"] == "@somchai_line"


def test_line_reorder_prefills_other_contact_from_customer(app_module, client, monkeypatch):
    from test_webhook import make_order, message_event, post_webhook

    monkeypatch.setattr(app_module, "line_reply", lambda *a, **k: True)
    cid = make_order(app_module, name="ReorderCust", link_code="REO002", phone="0855553333")
    with app_module.app.app_context():
        app_module.get_db().execute("UPDATE customers SET other_contact = ? WHERE id = ?", ("@reorder_line", cid))
        app_module.get_db().commit()

    post_webhook(client, "test-line-channel-secret", message_event("U1", "REO002"))  # link first
    post_webhook(client, "test-line-channel-secret", message_event("U1", "NEW ORDER"))
    post_webhook(client, "test-line-channel-secret", message_event("U1", "Another gadget"))

    with app_module.app.app_context():
        req = app_module.get_db().execute(
            "SELECT * FROM order_requests WHERE customer_id = ?", (cid,)
        ).fetchone()
    assert req["other_contact"] == "@reorder_line"


# ---- Feature D: customer info editable on order_detail, non-destructive ----

def test_order_detail_customer_edit_updates_customer_row(app_module, client):
    login(client)
    client.post("/admin/orders/new", data={"name": "Typo Name", "phone": "0855554444", "mode": "รถ", "lot": "70002"}, follow_redirects=True)
    with app_module.app.app_context():
        order = app_module.get_db().execute("SELECT id, customer_id FROM orders WHERE tracking_lot=70002").fetchone()

    resp = client.post(
        f"/admin/orders/{order['id']}",
        data={"action": "edit_customer", "cust_name": "Correct Name", "cust_phone": "0855554445", "cust_address": "New Address"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    with app_module.app.app_context():
        cust = app_module.get_db().execute("SELECT * FROM customers WHERE id = ?", (order["customer_id"],)).fetchone()
    assert cust["name"] == "Correct Name"
    assert cust["phone"] == "0855554445"
    assert cust["address"] == "New Address"


def test_order_detail_customer_edit_blank_address_clears(app_module, client):
    """Admin edit is deliberate: a blank field CLEARS it (the public intake
    paths, tested above, are the ones that never erase a stored value)."""
    login(client)
    client.post("/admin/orders/new", data={"name": "Keep Addr", "phone": "0855555555", "address": "Keep This", "mode": "รถ", "lot": "70003"}, follow_redirects=True)
    with app_module.app.app_context():
        order = app_module.get_db().execute("SELECT id, customer_id FROM orders WHERE tracking_lot=70003").fetchone()

    client.post(
        f"/admin/orders/{order['id']}",
        data={"action": "edit_customer", "cust_name": "Keep Addr", "cust_phone": "0855555555", "cust_address": ""},
        follow_redirects=True,
    )
    with app_module.app.app_context():
        cust = app_module.get_db().execute("SELECT address FROM customers WHERE id = ?", (order["customer_id"],)).fetchone()
    assert cust["address"] is None


def test_order_detail_shows_copy_all_button_and_customer_fields(app_module, client):
    login(client)
    client.post("/admin/orders/new", data={"name": "Copy Check", "phone": "0855556000", "mode": "รถ", "lot": "70004"}, follow_redirects=True)
    with app_module.app.app_context():
        order_id = app_module.get_db().execute("SELECT id FROM orders WHERE tracking_lot=70004").fetchone()["id"]
    resp = client.get(f"/admin/orders/{order_id}")
    assert b"data-copy-all" in resp.data
    assert b'name="cust_name"' in resp.data
    assert b'name="cust_phone"' in resp.data
    assert b'name="cust_other_contact"' in resp.data


# ---- Feature E: info-source note ----

def test_info_source_note_recorded_on_new_order_with_pasted_blob(app_module, client):
    login(client)
    client.post(
        "/admin/orders/new",
        data={"name": "Blob Cust", "phone": "0855557777", "mode": "รถ", "lot": "70005",
              "source_blob": "คุณสมชาย 085-555-7777 123 ถ.สุขุมวิท"},
        follow_redirects=True,
    )
    with app_module.app.app_context():
        cust = app_module.get_db().execute("SELECT info_source_note FROM customers WHERE phone_normalized='0855557777'").fetchone()
    assert cust["info_source_note"] is not None
    assert "0855557777" in cust["info_source_note"] or "085-555-7777" in cust["info_source_note"]


def test_no_info_source_note_when_no_blob_pasted(app_module, client):
    login(client)
    client.post("/admin/orders/new", data={"name": "No Blob", "phone": "0855558888", "mode": "รถ", "lot": "70006"}, follow_redirects=True)
    with app_module.app.app_context():
        cust = app_module.get_db().execute("SELECT info_source_note FROM customers WHERE phone_normalized='0855558888'").fetchone()
    assert cust["info_source_note"] is None


# ---- Feature F: expanded languages ----

def test_all_five_languages_available_and_render(app_module, client):
    for lang, needle in [("vi", "Tiếng Việt"), ("zh", "中文"), ("my", "မြန်မာ")]:
        resp = client.get(f"/set-locale/{lang}", follow_redirects=True)
        assert resp.status_code == 200
        resp2 = client.get("/")
        assert resp2.status_code == 200
    assert app_module.VALID_LOCALES == {"en", "th", "vi", "zh", "my"}


def test_language_dropdown_lists_all_locales(app_module, client):
    resp = client.get("/")
    for code in ["en", "th", "vi", "zh", "my"]:
        assert f'/set-locale/{code}'.encode() in resp.data
