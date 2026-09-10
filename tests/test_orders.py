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
