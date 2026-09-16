"""Phase 5: customer convenience -- proactive LINE notifications at the three
previously-silent moments (order created, quote priced, request converted),
self-service phone-based /track lookup, ETA display, and LINE CTA coverage
on landing/terms."""
from conftest import login


def _link_customer_by_name(app_module, name, line_user_id="Utest123"):
    with app_module.app.app_context():
        db = app_module.get_db()
        db.execute("UPDATE customers SET line_user_id = ? WHERE name = ?", (line_user_id, name))
        db.commit()
        return db.execute("SELECT id FROM customers WHERE name = ?", (name,)).fetchone()["id"]


# ---- LINE notifications -----------------------------------------------------

def test_new_order_notifies_linked_customer(app_module, client, monkeypatch):
    login(client)
    client.post(
        "/admin/orders/new",
        data={"name": "Push Me", "phone": "0811110001", "mode": "รถ", "lot": "40001"},
        follow_redirects=True,
    )
    _link_customer_by_name(app_module, "Push Me")

    pushes = []
    monkeypatch.setattr(app_module, "line_push", lambda uid, text: pushes.append((uid, text)) or True)
    client.post(
        "/admin/orders/new",
        data={"name": "Push Me", "phone": "0811110001", "mode": "รถ", "lot": "40002"},
        follow_redirects=True,
    )
    assert len(pushes) == 1
    assert "/track/" in pushes[0][1]


def test_new_order_no_push_when_not_linked(app_module, client, monkeypatch):
    login(client)
    pushes = []
    monkeypatch.setattr(app_module, "line_push", lambda uid, text: pushes.append((uid, text)) or True)
    client.post(
        "/admin/orders/new",
        data={"name": "No Link", "mode": "รถ", "lot": "40003"},
        follow_redirects=True,
    )
    assert pushes == []


def test_quote_request_notifies_linked_customer_once_priced(app_module, client, monkeypatch):
    login(client)
    client.post(
        "/request",
        data={"name": "Quote Me", "phone": "0811110002", "item_description": "phone case", "agree_terms": "on"},
        follow_redirects=True,
    )
    _link_customer_by_name(app_module, "Quote Me")
    with app_module.app.app_context():
        req_id = app_module.get_db().execute("SELECT id FROM order_requests").fetchone()["id"]

    pushes = []
    monkeypatch.setattr(app_module, "line_push", lambda uid, text: pushes.append((uid, text)) or True)
    client.post(
        f"/admin/requests/{req_id}/quote",
        data={"item_cost": "500", "shipping_cost": "100"},
        follow_redirects=True,
    )
    assert len(pushes) == 1
    assert "quote is ready" in pushes[0][1]


def test_quote_request_no_push_when_item_cost_blank(app_module, client, monkeypatch):
    login(client)
    client.post(
        "/request",
        data={"name": "No Cost Yet", "phone": "0811110003", "item_description": "thing", "agree_terms": "on"},
        follow_redirects=True,
    )
    _link_customer_by_name(app_module, "No Cost Yet")
    with app_module.app.app_context():
        req_id = app_module.get_db().execute("SELECT id FROM order_requests").fetchone()["id"]

    pushes = []
    monkeypatch.setattr(app_module, "line_push", lambda uid, text: pushes.append((uid, text)) or True)
    client.post(f"/admin/requests/{req_id}/quote", data={"shipping_cost": "100"}, follow_redirects=True)
    assert pushes == []


def test_convert_request_notifies_linked_customer(app_module, client, monkeypatch):
    login(client)
    client.post(
        "/request",
        data={"name": "Convert Me", "phone": "0811110004", "item_description": "widget", "agree_terms": "on"},
        follow_redirects=True,
    )
    _link_customer_by_name(app_module, "Convert Me")
    with app_module.app.app_context():
        req_id = app_module.get_db().execute("SELECT id FROM order_requests").fetchone()["id"]

    pushes = []
    monkeypatch.setattr(app_module, "line_push", lambda uid, text: pushes.append((uid, text)) or True)
    client.post(
        "/admin/requests",
        data={"request_id": req_id, "mode": "รถ", "lot": "40004"},
        follow_redirects=True,
    )
    assert len(pushes) == 1
    assert "confirmed" in pushes[0][1]
    assert "/track/" in pushes[0][1]


# ---- Self-service phone lookup on /track ------------------------------------

def test_track_lookup_get_shows_form(app_module, client):
    resp = client.get("/track")
    assert resp.status_code == 200
    assert b'name="phone"' in resp.data


def test_track_lookup_invalid_phone_shows_error(app_module, client):
    resp = client.post("/track", data={"phone": "abc"}, follow_redirects=True)
    assert b"valid phone number" in resp.data


def test_track_lookup_no_match_shows_message(app_module, client):
    resp = client.post("/track", data={"phone": "0899999999"}, follow_redirects=True)
    assert b"couldn" in resp.data.lower()


def test_track_lookup_single_match_redirects_to_track_page(app_module, client):
    login(client)
    client.post(
        "/admin/orders/new",
        data={"name": "Single Match", "phone": "0811110005", "mode": "รถ", "lot": "40005"},
        follow_redirects=True,
    )
    with app_module.app.app_context():
        link_code = app_module.get_db().execute("SELECT link_code FROM orders WHERE tracking_lot=40005").fetchone()["link_code"]

    resp = client.post("/track", data={"phone": "0811110005"})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/track/{link_code}")


def test_track_lookup_multiple_matches_lists_both(app_module, client):
    login(client)
    client.post(
        "/admin/orders/new",
        data={"name": "Two Orders", "phone": "0811110006", "mode": "รถ", "lot": "40006"},
        follow_redirects=True,
    )
    client.post(
        "/admin/orders/new",
        data={"name": "Two Orders", "phone": "0811110006", "mode": "เรือ", "lot": "40007"},
        follow_redirects=True,
    )
    resp = client.post("/track", data={"phone": "0811110006"}, follow_redirects=True)
    assert resp.status_code == 200
    assert resp.data.count(b'class="font-mono text-xs"') >= 2


# ---- Estimated delivery ------------------------------------------------------

def test_estimated_delivery_truck_and_boat_ranges(app_module, client):
    with app_module.app.app_context():
        order_truck = {"tracking_mode": "รถ", "created_at": "2026-01-01T00:00:00"}
        order_boat = {"tracking_mode": "เรือ", "created_at": "2026-01-01T00:00:00"}
        eta_truck = app_module.estimated_delivery(order_truck)
        eta_boat = app_module.estimated_delivery(order_boat)
    assert eta_truck == {"low": "08 Jan", "high": "15 Jan"}
    assert eta_boat == {"low": "11 Jan", "high": "31 Jan"}


def test_estimated_delivery_none_for_unknown_mode(app_module, client):
    with app_module.app.app_context():
        assert app_module.estimated_delivery({"tracking_mode": "?", "created_at": "2026-01-01T00:00:00"}) is None


def test_track_page_shows_eta_for_in_progress_order(app_module, client):
    login(client)
    client.post(
        "/admin/orders/new",
        data={"name": "ETA Check", "mode": "รถ", "lot": "40008"},
        follow_redirects=True,
    )
    with app_module.app.app_context():
        link_code = app_module.get_db().execute("SELECT link_code FROM orders WHERE tracking_lot=40008").fetchone()["link_code"]
    resp = client.get(f"/track/{link_code}")
    assert b"Estimated delivery" in resp.data


# ---- LINE CTA coverage on landing/terms --------------------------------------

def test_landing_shows_line_cta_when_contact_configured(app_module, client, monkeypatch):
    monkeypatch.setitem(app_module.app.jinja_env.globals, "LINE_CONTACT", "@testhandle")
    resp = client.get("/")
    assert b"line.me/ti/p/~testhandle" in resp.data


def test_landing_hides_line_cta_when_contact_blank(app_module, client):
    resp = client.get("/")
    assert b"line.me/ti/p/~" not in resp.data


def test_terms_shows_line_cta_when_contact_configured(app_module, client, monkeypatch):
    monkeypatch.setitem(app_module.app.jinja_env.globals, "LINE_CONTACT", "@testhandle")
    resp = client.get("/terms")
    assert b"line.me/ti/p/~testhandle" in resp.data


def test_track_not_found_links_to_phone_lookup(app_module, client):
    resp = client.get("/track/NOSUCH")
    assert resp.status_code == 404
    assert b'href="/track"' in resp.data
