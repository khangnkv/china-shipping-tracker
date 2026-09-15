"""Impeccable full-app audit fixes: status badge dark-mode tokens, a11y
labels/aria on login + line_status, sr-only chart data tables, mobile card
lists on Orders/Stats."""
from conftest import login


def test_status_badge_uses_css_tokens_not_literal_tailwind_colors(app_module, client):
    login(client)
    client.post(
        "/admin/orders/new",
        data={"name": "Badge Check", "mode": "รถ", "lot": "22001"},
        follow_redirects=True,
    )
    resp = client.get("/admin/orders")
    assert b"var(--status-slate-bg)" in resp.data
    assert b"bg-slate-100 text-slate-700" not in resp.data


def test_login_password_toggle_has_aria_label(app_module, client):
    resp = client.get("/admin/login")
    assert b'aria-label="Show password"' in resp.data
    assert b'aria-pressed="false"' in resp.data


def test_line_status_select_has_label_association(app_module, client):
    login(client)
    client.post(
        "/admin/orders/new",
        data={"name": "Linked Customer", "mode": "รถ", "lot": "22004"},
        follow_redirects=True,
    )
    with app_module.app.app_context():
        db = app_module.get_db()
        db.execute("UPDATE customers SET line_user_id = 'Utest123' WHERE name = 'Linked Customer'")
        db.commit()
    resp = client.get("/admin/line-status")
    assert b'for="testCustomer"' in resp.data
    assert b'id="testCustomer"' in resp.data


def test_stats_charts_have_sr_only_tables_and_aria_labels(app_module, client):
    login(client)
    resp = client.get("/admin/stats")
    assert b'class="sr-only"' in resp.data
    assert b'aria-label="Bar chart of monthly profit in baht"' in resp.data
    assert b'aria-label="Doughnut chart of order count by shipping mode"' in resp.data


def test_orders_page_has_mobile_card_list(app_module, client):
    login(client)
    client.post(
        "/admin/orders/new",
        data={"name": "Card List Check", "mode": "รถ", "lot": "22002"},
        follow_redirects=True,
    )
    resp = client.get("/admin/orders")
    assert b'id="cardsList"' in resp.data
    assert b"Card List Check" in resp.data


def test_stats_agency_table_has_mobile_card_fallback(app_module, client):
    login(client)
    with app_module.app.app_context():
        db = app_module.get_db()
        db.execute("INSERT INTO agencies (name, active, created_at) VALUES ('CardAgency', 1, datetime('now'))")
        db.commit()
        agency_id = db.execute("SELECT id FROM agencies WHERE name = 'CardAgency'").fetchone()[0]
    client.post(
        "/admin/orders/new",
        data={"name": "Agency Stats", "mode": "รถ", "lot": "22003", "agency_id": str(agency_id)},
        follow_redirects=True,
    )
    resp = client.get("/admin/stats")
    assert resp.status_code == 200
    assert b"sm:hidden" in resp.data
