from datetime import datetime


def test_parse_lot_list_extracts_numbers_per_mode(app_module):
    text = "LOT รถ : 11278, 11279, 11285\nLOT เรือ : ไม่มีตู้เรือเข้าไทย"
    result = app_module.parse_lot_list(text)
    assert result["รถ"] == {11278, 11279, 11285}
    assert result["เรือ"] == set()


def test_parse_lot_list_ignores_unrelated_lines(app_module):
    text = "not a lot line\nLOT รถ : 100"
    result = app_module.parse_lot_list(text)
    assert result["รถ"] == {100}
    assert result["เรือ"] == set()


def test_apply_matching_updates_ordered_order(app_module):
    with app_module.app.app_context():
        db = app_module.get_db()
        now = datetime.utcnow().isoformat()
        db.execute(
            "INSERT INTO customers (name, phone, created_at) VALUES (?, ?, ?)",
            ("Test Customer", "0800000000", now),
        )
        customer_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute(
            """INSERT INTO orders (customer_id, tracking_mode, tracking_lot, status,
               link_code, created_at, updated_at) VALUES (?, ?, ?, 'ordered', ?, ?, ?)""",
            (customer_id, "รถ", 11278, "ABC123", now, now),
        )
        db.commit()

        changes = app_module.apply_matching({"รถ": {11278}, "เรือ": set()})

        assert len(changes) == 1
        assert changes[0]["new_status"] == "at_china_wh"

        order = db.execute("SELECT * FROM orders WHERE customer_id = ?", (customer_id,)).fetchone()
        assert order["status"] == "at_china_wh"

        log = db.execute("SELECT * FROM status_log WHERE order_id = ?", (order["id"],)).fetchone()
        assert log is not None
        assert log["status"] == "at_china_wh"
