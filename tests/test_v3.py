import json
from datetime import datetime

from conftest import login


def _make_order(app_module, status="ordered", link_code="TRK001", agency_id=None):
    with app_module.app.app_context():
        db = app_module.get_db()
        now = datetime.utcnow().isoformat()
        db.execute("INSERT INTO customers (name, phone, created_at) VALUES ('C','0800000000',?)", (now,))
        cid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute(
            "INSERT INTO orders (customer_id, tracking_mode, tracking_lot, agency_id, status, link_code, created_at, updated_at) "
            "VALUES (?, 'รถ', 11278, ?, ?, ?, ?, ?)",
            (cid, agency_id, status, link_code, now, now),
        )
        oid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.commit()
    return oid


# ---- durations -------------------------------------------------------------

def test_order_durations_computes_legs():
    import app
    st = {
        "ordered": "2026-01-01T00:00:00",
        "at_china_wh": "2026-01-04T00:00:00",   # +3d china leg
        "at_th_wh": "2026-01-10T00:00:00",      # +6d cross-border
        "delivered": "2026-01-12T00:00:00",     # +2d last mile, +11d total
    }
    d = app.order_durations("2026-01-01T00:00:00", st)
    assert round(d["total"], 1) == 11.0
    assert round(d["china_leg"], 1) == 3.0
    assert round(d["cross_border"], 1) == 6.0
    assert round(d["last_mile"], 1) == 2.0


def test_order_durations_missing_stage_is_none():
    import app
    d = app.order_durations("2026-01-01T00:00:00", {"ordered": "2026-01-01T00:00:00"})
    assert d["total"] is None and d["china_leg"] is None


# ---- legacy migration ------------------------------------------------------

def test_legacy_status_migration(app_module):
    with app_module.app.app_context():
        db = app_module.get_db()
        now = datetime.utcnow().isoformat()
        db.execute("INSERT INTO customers (name, created_at) VALUES ('Old', ?)", (now,))
        cid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute(
            "INSERT INTO orders (customer_id, tracking_mode, tracking_lot, status, link_code, created_at, updated_at) "
            "VALUES (?, 'รถ', 1, 'pending', 'OLD001', ?, ?)",
            (cid, now, now),
        )
        db.commit()
    app_module.init_db()  # runs the idempotent remap
    with app_module.app.app_context():
        row = app_module.get_db().execute("SELECT status FROM orders WHERE link_code='OLD001'").fetchone()
    assert row["status"] == "ordered"


# ---- parse_customer --------------------------------------------------------

def test_parse_customer_parses_mocked_response(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "ANTHROPIC_API_KEY", "test-key")

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"content": [{"text": json.dumps({"name": "สมชาย", "phone": "0812345678", "address": "123 ถ.สุขุมวิท"})}]}

    monkeypatch.setattr(app_module.requests, "post", lambda *a, **k: FakeResp())
    out = app_module.parse_customer("สมชาย 0812345678 123 ถ.สุขุมวิท")
    assert out == {"name": "สมชาย", "phone": "0812345678", "address": "123 ถ.สุขุมวิท"}


def test_parse_customer_no_key_returns_none(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "ANTHROPIC_API_KEY", "")
    assert app_module.parse_customer("anything") is None


# ---- agencies --------------------------------------------------------------

def test_agency_add_and_toggle(app_module, client):
    login(client)
    client.post("/admin/agencies", data={"action": "add", "name": "Kerry-CN"}, follow_redirects=True)
    with app_module.app.app_context():
        a = app_module.get_db().execute("SELECT * FROM agencies WHERE name='Kerry-CN'").fetchone()
    assert a is not None and a["active"] == 1

    client.post("/admin/agencies", data={"action": "toggle", "agency_id": a["id"]}, follow_redirects=True)
    with app_module.app.app_context():
        a2 = app_module.get_db().execute("SELECT * FROM agencies WHERE id=?", (a["id"],)).fetchone()
    assert a2["active"] == 0


# ---- status advance --------------------------------------------------------

def test_status_advance_logs_and_moves(app_module, client, monkeypatch):
    monkeypatch.setattr(app_module, "line_push", lambda *a, **k: True)
    login(client)
    oid = _make_order(app_module, status="at_th_wh")
    client.post(
        f"/admin/orders/{oid}",
        data={"action": "advance_status", "status": "out_for_delivery",
              "local_carrier": "Flash Express", "local_tracking_no": "TH99"},
        follow_redirects=True,
    )
    with app_module.app.app_context():
        o = app_module.get_db().execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        log = app_module.get_db().execute(
            "SELECT * FROM status_log WHERE order_id=? ORDER BY id DESC LIMIT 1", (oid,)
        ).fetchone()
    assert o["status"] == "out_for_delivery"
    assert o["local_carrier"] == "Flash Express" and o["local_tracking_no"] == "TH99"
    assert log["status"] == "out_for_delivery"


def test_status_advance_rejects_invalid(app_module, client):
    login(client)
    oid = _make_order(app_module, link_code="TRK777")
    client.post(f"/admin/orders/{oid}", data={"action": "advance_status", "status": "bogus"}, follow_redirects=True)
    with app_module.app.app_context():
        o = app_module.get_db().execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone()
    assert o["status"] == "ordered"  # unchanged


# ---- track no-leak (extended) ---------------------------------------------

def test_healthz_ok(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.get_data(as_text=True) == "ok"


def _link(app_module, customer_id, uid="U-LINK"):
    with app_module.app.app_context():
        db = app_module.get_db()
        db.execute("UPDATE customers SET line_user_id = ? WHERE id = ?", (uid, customer_id))
        db.commit()


def test_advance_pushes_when_linked(app_module, client, monkeypatch):
    pushes = []
    monkeypatch.setattr(app_module, "line_push", lambda uid, text: pushes.append((uid, text)) or True)
    login(client)
    oid = _make_order(app_module, status="at_th_wh", link_code="TRKP01")
    with app_module.app.app_context():
        cid = app_module.get_db().execute("SELECT customer_id FROM orders WHERE id=?", (oid,)).fetchone()[0]
    _link(app_module, cid, "U-ADV")
    client.post(f"/admin/orders/{oid}", data={"action": "advance_status", "status": "out_for_delivery",
                                              "local_carrier": "Flash", "local_tracking_no": "TH1"}, follow_redirects=True)
    assert pushes and pushes[0][0] == "U-ADV"
    assert "Out for delivery" in pushes[0][1]


def test_notify_pushes_and_marks(app_module, client, monkeypatch):
    pushes = []
    monkeypatch.setattr(app_module, "line_push", lambda uid, text: pushes.append((uid, text)) or True)
    login(client)
    oid = _make_order(app_module, status="at_china_wh", link_code="TRKN01")
    with app_module.app.app_context():
        cid = app_module.get_db().execute("SELECT customer_id FROM orders WHERE id=?", (oid,)).fetchone()[0]
    _link(app_module, cid, "U-NOTIFY")
    client.post("/admin/notify", follow_redirects=True)
    assert pushes and pushes[0][0] == "U-NOTIFY"
    with app_module.app.app_context():
        o = app_module.get_db().execute("SELECT last_notified_status FROM orders WHERE id=?", (oid,)).fetchone()
    assert o["last_notified_status"] == "at_china_wh"


def test_track_hides_china_tracking_and_agency(app_module, client):
    with app_module.app.app_context():
        db = app_module.get_db()
        now = datetime.utcnow().isoformat()
        db.execute("INSERT INTO agencies (name, active, created_at) VALUES ('SecretAgency', 1, ?)", (now,))
        aid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    oid = _make_order(app_module, status="at_china_wh", link_code="TRK555", agency_id=aid)
    with app_module.app.app_context():
        app_module.get_db().execute(
            "UPDATE orders SET china_tracking_no='SF-SECRET-123' WHERE id=?", (oid,)
        )
        app_module.get_db().commit()
    html = client.get("/track/TRK555").get_data(as_text=True)
    assert "SF-SECRET-123" not in html
    assert "SecretAgency" not in html
    assert "11278" not in html and "รถ" not in html
