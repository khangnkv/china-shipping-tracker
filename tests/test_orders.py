from conftest import login


def test_new_order_rejects_non_numeric_lot(app_module, client):
    login(client)

    resp = client.post(
        "/admin/orders/new",
        data={"name": "Bad Lot", "mode": "รถ", "lot": "not-a-number"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"valid customer name" in resp.data

    with app_module.app.app_context():
        db = app_module.get_db()
        count = db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    assert count == 0


def test_repeat_customer_by_phone_reuses_customer_row(app_module, client):
    """A second order placed with the same phone number must attach to the
    same customer, not create a duplicate -- prerequisite for order history
    and phone-based LINE linking (see app.py new_order())."""
    login(client)

    client.post(
        "/admin/orders/new",
        data={"name": "Somchai", "phone": "081-234-5678", "mode": "รถ", "lot": "11001"},
        follow_redirects=True,
    )
    client.post(
        "/admin/orders/new",
        data={"name": "Somchai J", "phone": "0812345678", "mode": "เรือ", "lot": "11002"},
        follow_redirects=True,
    )

    with app_module.app.app_context():
        db = app_module.get_db()
        customers = db.execute("SELECT * FROM customers").fetchall()
        orders = db.execute("SELECT customer_id FROM orders").fetchall()

    assert len(customers) == 1
    assert len({o["customer_id"] for o in orders}) == 1
    # Name on file is updated to the most recent submission.
    assert customers[0]["name"] == "Somchai J"


def test_new_order_blank_phone_never_merges(app_module, client):
    """Two orders with no phone number must NOT be merged into one customer --
    only a shared phone number is a safe match key."""
    login(client)

    client.post(
        "/admin/orders/new",
        data={"name": "No Phone A", "mode": "รถ", "lot": "12001"},
        follow_redirects=True,
    )
    client.post(
        "/admin/orders/new",
        data={"name": "No Phone B", "mode": "รถ", "lot": "12002"},
        follow_redirects=True,
    )

    with app_module.app.app_context():
        db = app_module.get_db()
        count = db.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
    assert count == 2


def test_order_detail_edit_updates_date_mode_lot(app_module, client):
    """Order date, mode, and LOT are mistakes-happen fields and must be
    editable after creation (see app.py order_detail())."""
    login(client)
    client.post(
        "/admin/orders/new",
        data={"name": "Edit Me", "mode": "รถ", "lot": "13001"},
        follow_redirects=True,
    )
    with app_module.app.app_context():
        db = app_module.get_db()
        order_id = db.execute("SELECT id FROM orders").fetchone()["id"]

    resp = client.post(
        f"/admin/orders/{order_id}",
        data={"order_date": "2026-01-15", "tracking_mode": "เรือ", "tracking_lot": "13099"},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    with app_module.app.app_context():
        db = app_module.get_db()
        order = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    assert order["created_at"].startswith("2026-01-15")
    assert order["tracking_mode"] == "เรือ"
    assert order["tracking_lot"] == 13099


def test_order_detail_rejects_invalid_date(app_module, client):
    login(client)
    client.post(
        "/admin/orders/new",
        data={"name": "Bad Date", "mode": "รถ", "lot": "14001"},
        follow_redirects=True,
    )
    with app_module.app.app_context():
        db = app_module.get_db()
        order_id = db.execute("SELECT id FROM orders").fetchone()["id"]
        original_created_at = db.execute(
            "SELECT created_at FROM orders WHERE id = ?", (order_id,)
        ).fetchone()["created_at"]

    resp = client.post(
        f"/admin/orders/{order_id}",
        data={"order_date": "not-a-date"},
        follow_redirects=True,
    )
    assert b"Invalid order date" in resp.data

    with app_module.app.app_context():
        db = app_module.get_db()
        order = db.execute("SELECT created_at FROM orders WHERE id = ?", (order_id,)).fetchone()
    assert order["created_at"] == original_created_at
