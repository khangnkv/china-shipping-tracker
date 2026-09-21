import sqlite3

import pytest

from conftest import login


def test_optional_lot_clearable_fields_unlink_and_delete(app_module, client, tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "UPLOAD_DIR", str(tmp_path))  # never touch the real static/uploads
    login(client)

    # (a) an order with no LOT is created, and stores NULL.
    client.post(
        "/admin/orders/new",
        data={"name": "No Lot", "phone": "0866660001", "address": "1 Some St", "mode": "รถ", "lot": ""},
        follow_redirects=True,
    )
    with app_module.app.app_context():
        order = app_module.get_db().execute("SELECT * FROM orders").fetchone()
    assert order["tracking_lot"] is None

    # (b) an admin edit that submits blanks really blanks them (name stays required).
    client.post(
        f"/admin/orders/{order['id']}",
        data={"action": "edit_customer", "cust_name": "No Lot", "cust_phone": "", "cust_address": "", "cust_other_contact": ""},
        follow_redirects=True,
    )
    with app_module.app.app_context():
        cust = app_module.get_db().execute("SELECT * FROM customers WHERE id = ?", (order["customer_id"],)).fetchone()
    assert cust["phone"] is None and cust["phone_normalized"] is None and cust["address"] is None

    # (c) unlink sets line_user_id to NULL.
    with app_module.app.app_context():
        app_module.get_db().execute("UPDATE customers SET line_user_id = 'U-wrong' WHERE id = ?", (order["customer_id"],))
        app_module.get_db().commit()
    client.post(f"/admin/customers/{order['customer_id']}/unlink-line", follow_redirects=True)
    with app_module.app.app_context():
        cust = app_module.get_db().execute("SELECT line_user_id FROM customers WHERE id = ?", (order["customer_id"],)).fetchone()
    assert cust["line_user_id"] is None

    # (d) deleting an order also removes its status_log rows and image files.
    (tmp_path / f"{order['link_code']}.jpg").write_bytes(b"x")
    (tmp_path / f"{order['link_code']}_thumb.jpg").write_bytes(b"x")
    with app_module.app.app_context():
        app_module.get_db().execute(
            "INSERT INTO status_log (order_id, status, created_at) VALUES (?, 'ordered', '2026-01-01T00:00:00')", (order["id"],)
        )
        app_module.get_db().commit()
    client.post(f"/admin/orders/{order['id']}/delete", follow_redirects=True)
    with app_module.app.app_context():
        db = app_module.get_db()
        assert db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM status_log WHERE order_id = ?", (order["id"],)).fetchone()[0] == 0
    assert not (tmp_path / f"{order['link_code']}.jpg").exists()
    assert not (tmp_path / f"{order['link_code']}_thumb.jpg").exists()


def test_init_db_makes_tracking_lot_nullable_and_keeps_data(app_module, tmp_path, monkeypatch):
    """A tracker.db from before LOT was optional: NOT NULL, plus a column that
    was added later by ALTER. The rebuild must keep every row/column/constraint."""
    legacy = tmp_path / "legacy.db"
    con = sqlite3.connect(legacy)
    con.executescript(
        """
        CREATE TABLE customers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, phone TEXT,
                                line_user_id TEXT UNIQUE, created_at TEXT NOT NULL);
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER NOT NULL, tracking_mode TEXT NOT NULL,
            tracking_lot INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending', link_code TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, FOREIGN KEY (customer_id) REFERENCES customers(id));
        CREATE TABLE status_log (id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL, status TEXT NOT NULL,
                                 note TEXT, created_at TEXT NOT NULL, FOREIGN KEY (order_id) REFERENCES orders(id));
        INSERT INTO customers (name, created_at) VALUES ('Legacy', '2026-01-01');
        INSERT INTO orders (customer_id, tracking_mode, tracking_lot, link_code, created_at, updated_at)
            VALUES (1, 'รถ', 11092, 'AAA111', '2026-01-01', '2026-01-01'), (1, 'เรือ', 11093, 'BBB222', '2026-01-01', '2026-01-01');
        INSERT INTO status_log (order_id, status, created_at) VALUES (1, 'ordered', '2026-01-01');
        DELETE FROM orders WHERE id = 2;  -- AUTOINCREMENT counter is now ahead of max(id)
        """
    )
    con.execute("ALTER TABLE orders ADD COLUMN item_cost REAL")
    con.execute("UPDATE orders SET item_cost = 12.5")
    con.commit()
    con.close()

    monkeypatch.setattr(app_module, "DB_PATH", str(legacy))
    app_module.init_db()
    app_module.init_db()  # idempotent: second run is a no-op

    con = sqlite3.connect(legacy)
    con.row_factory = sqlite3.Row
    lot = next(r for r in con.execute("PRAGMA table_info(orders)") if r["name"] == "tracking_lot")
    assert lot["notnull"] == 0
    row = con.execute("SELECT * FROM orders").fetchone()
    assert (row["id"], row["tracking_lot"], row["link_code"], row["item_cost"]) == (1, 11092, "AAA111", 12.5)
    assert con.execute("SELECT COUNT(*) FROM status_log WHERE order_id = 1").fetchone()[0] == 1
    assert [r["table"] for r in con.execute("PRAGMA foreign_key_list(orders)")] == ["customers"]

    con.execute("INSERT INTO orders (customer_id, tracking_mode, link_code, created_at, updated_at) VALUES (1, 'รถ', 'CCC333', 'n', 'n')")
    assert con.execute("SELECT id, tracking_lot FROM orders WHERE link_code = 'CCC333'").fetchone()[:] == (3, None)  # NULL LOT ok, id 2 not reused
    with pytest.raises(sqlite3.IntegrityError):  # link_code is still UNIQUE
        con.execute("INSERT INTO orders (customer_id, tracking_mode, link_code, created_at, updated_at) VALUES (1, 'รถ', 'CCC333', 'n', 'n')")
    with pytest.raises(sqlite3.IntegrityError):  # ...and NOT NULL
        con.execute("INSERT INTO orders (customer_id, tracking_mode, link_code, created_at, updated_at) VALUES (1, 'รถ', NULL, 'n', 'n')")
    con.close()
