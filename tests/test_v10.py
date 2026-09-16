"""Impeccable polish pass: submit-loading feedback, count-up stat, the
current-stage pulse, and the interactive-card hover treatment."""
from conftest import login


def test_login_form_has_loading_text(app_module, client):
    resp = client.get("/admin/login")
    assert b'data-loading-text="Signing in' in resp.data


def test_feedback_form_has_translated_loading_text(app_module, client):
    resp = client.get("/feedback")
    assert b'data-loading-text="Sending' in resp.data


def test_request_form_loading_text_differs_fresh_vs_revisit(app_module, client):
    fresh = client.get("/request")
    assert b'data-loading-text="Submitting' in fresh.data

    client.post("/request", data={"name": "Loading Check", "phone": "0855559999", "item_description": "x", "agree_terms": "on"}, follow_redirects=True)
    with app_module.app.app_context():
        code = app_module.get_db().execute("SELECT request_code FROM order_requests").fetchone()["request_code"]
    revisit = client.get(f"/request/{code}")
    assert b'data-loading-text="Saving' in revisit.data


def test_order_detail_forms_have_loading_text(app_module, client):
    login(client)
    client.post("/admin/orders/new", data={"name": "Loading Order", "mode": "รถ", "lot": "80001"}, follow_redirects=True)
    with app_module.app.app_context():
        order_id = app_module.get_db().execute("SELECT id FROM orders WHERE tracking_lot=80001").fetchone()["id"]
    resp = client.get(f"/admin/orders/{order_id}")
    assert resp.data.count(b"data-loading-text") == 2  # customer-edit form + main order-info form (status form intentionally excluded)


def test_track_lookup_result_card_has_interactive_hover_class(app_module, client):
    login(client)
    client.post("/admin/orders/new", data={"name": "Two Track", "phone": "0855550001", "mode": "รถ", "lot": "80002"}, follow_redirects=True)
    client.post("/admin/orders/new", data={"name": "Two Track", "phone": "0855550001", "mode": "เรือ", "lot": "80003"}, follow_redirects=True)
    resp = client.post("/track", data={"phone": "0855550001"}, follow_redirects=True)
    assert b"card-interactive" in resp.data


def test_track_current_stage_has_pulse_class(app_module, client):
    login(client)
    client.post("/admin/orders/new", data={"name": "Pulse Check", "mode": "รถ", "lot": "80004"}, follow_redirects=True)
    with app_module.app.app_context():
        link_code = app_module.get_db().execute("SELECT link_code FROM orders WHERE tracking_lot=80004").fetchone()["link_code"]
    resp = client.get(f"/track/{link_code}")
    assert b"stage-pulse" in resp.data


def test_landing_has_count_up_stat(app_module, client):
    resp = client.get("/")
    assert b'data-count-to="100"' in resp.data
    assert b">100<" in resp.data  # correct value present even without JS
