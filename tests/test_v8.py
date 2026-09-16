"""Fixes reported live: LINE carousel images not displaying (a stale
reference to a thumb that was never written, plus PNG uploads bloating to
several MB with no real transparency), a customer feedback channel, and a
tappable (not just a separate button) order image in the LINE carousel."""
import os
from io import BytesIO

from PIL import Image

from conftest import login


def _png_bytes(size=(50, 50), color=(255, 0, 0), alpha=None):
    buf = BytesIO()
    if alpha is not None:
        img = Image.new("RGBA", size, color + (alpha,))
    else:
        img = Image.new("RGB", size, color)
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


# ---- Image pipeline: JPEG conversion, transparency preserved when real ------

def test_opaque_png_upload_is_converted_to_jpeg(app_module, client):
    login(client)
    client.post("/admin/orders/new", data={"name": "Img One", "mode": "รถ", "lot": "50001"}, follow_redirects=True)
    with app_module.app.app_context():
        order_id = app_module.get_db().execute("SELECT id FROM orders WHERE tracking_lot=50001").fetchone()["id"]

    resp = client.post(
        f"/admin/orders/{order_id}",
        data={"mode": "รถ", "image": (_png_bytes(), "photo.png")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    with app_module.app.app_context():
        order = app_module.get_db().execute("SELECT item_image FROM orders WHERE id=?", (order_id,)).fetchone()
    assert order["item_image"].endswith(".jpg")
    assert os.path.exists(os.path.join(app_module.UPLOAD_DIR, os.path.basename(order["item_image"])))
    thumb = app_module.item_image_thumb(order["item_image"])
    assert os.path.exists(os.path.join(app_module.UPLOAD_DIR, os.path.basename(thumb)))


def test_transparent_png_upload_stays_png(app_module, client):
    login(client)
    client.post("/admin/orders/new", data={"name": "Img Two", "mode": "รถ", "lot": "50002"}, follow_redirects=True)
    with app_module.app.app_context():
        order_id = app_module.get_db().execute("SELECT id FROM orders WHERE tracking_lot=50002").fetchone()["id"]

    resp = client.post(
        f"/admin/orders/{order_id}",
        data={"mode": "รถ", "image": (_png_bytes(alpha=0), "logo.png")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    with app_module.app.app_context():
        order = app_module.get_db().execute("SELECT item_image FROM orders WHERE id=?", (order_id,)).fetchone()
    assert order["item_image"].endswith(".png")


def test_compress_and_save_returns_actual_extension_used(app_module, client, tmp_path):
    with app_module.app.app_context():
        out = tmp_path / "test_img"
        used_ext = app_module._compress_and_save(_png_bytes(), str(out), "png")
    assert used_ext == "jpg"
    assert os.path.exists(f"{out}.jpg")
    assert os.path.exists(f"{out}_thumb.jpg")


# ---- Carousel: skip a hero image whose file doesn't actually exist ----------

def test_carousel_skips_hero_when_thumb_file_missing(app_module, client):
    """Reproduces the live bug: item_image points at a file whose _thumb
    variant was never written (legacy data, manual edit, etc). Before the
    fix this built a URL to a 404, which LINE renders as a blank white box
    with no error anywhere -- exactly what was reported."""
    with app_module.app.app_context():
        db = app_module.get_db()
        cid = app_module._find_or_create_customer("Carousel Ghost", "0822220001", "")
        db.commit()
        db.execute(
            "INSERT INTO orders (customer_id, tracking_mode, tracking_lot, status, link_code, "
            "item_image, payment_amount, created_at, updated_at) "
            "VALUES (?, 'รถ', 1, 'ordered', 'GHOST1', 'uploads/GHOST1.jpg', 100, datetime('now'), datetime('now'))",
            (cid,),
        )
        db.commit()
        carousel = app_module._order_history_carousel(cid)
    assert carousel is not None
    bubble = carousel["contents"]["contents"][0]
    assert "hero" not in bubble


def test_carousel_includes_hero_with_tap_action_when_thumb_exists(app_module, client):
    with app_module.app.app_context():
        db = app_module.get_db()
        cid = app_module._find_or_create_customer("Carousel Real", "0822220002", "")
        db.commit()
        os.makedirs(app_module.UPLOAD_DIR, exist_ok=True)
        Image.new("RGB", (20, 20), "blue").save(os.path.join(app_module.UPLOAD_DIR, "REAL01_thumb.jpg"))
        db.execute(
            "INSERT INTO orders (customer_id, tracking_mode, tracking_lot, status, link_code, "
            "item_image, payment_amount, created_at, updated_at) "
            "VALUES (?, 'รถ', 2, 'ordered', 'REAL01', 'uploads/REAL01.jpg', 100, datetime('now'), datetime('now'))",
            (cid,),
        )
        db.commit()
        carousel = app_module._order_history_carousel(cid)
    bubble = carousel["contents"]["contents"][0]
    assert "hero" in bubble
    assert bubble["hero"]["action"]["type"] == "uri"
    assert "/track/REAL01" in bubble["hero"]["action"]["uri"]


# ---- Feedback: public submit, admin list + toggle, nav badge ---------------

def test_feedback_submit_requires_message(app_module, client):
    resp = client.post("/feedback", data={"name": "No Message"}, follow_redirects=True)
    assert b"write your feedback" in resp.data
    with app_module.app.app_context():
        count = app_module.get_db().execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
    assert count == 0


def test_feedback_submit_success(app_module, client):
    resp = client.post(
        "/feedback",
        data={"name": "Happy Customer", "contact": "0891234567", "message": "Love the tracking page!"},
        follow_redirects=True,
    )
    assert b"Thank you" in resp.data
    with app_module.app.app_context():
        row = app_module.get_db().execute("SELECT * FROM feedback").fetchone()
    assert row["message"] == "Love the tracking page!"
    assert row["source"] == "web"
    assert row["handled"] == 0


def test_admin_feedback_page_lists_and_toggles_handled(app_module, client):
    client.post("/feedback", data={"message": "Please add PromptPay"}, follow_redirects=True)
    login(client)
    resp = client.get("/admin/feedback")
    assert b"Please add PromptPay" in resp.data

    with app_module.app.app_context():
        fid = app_module.get_db().execute("SELECT id FROM feedback").fetchone()["id"]
    client.post("/admin/feedback", data={"feedback_id": fid}, follow_redirects=True)
    with app_module.app.app_context():
        row = app_module.get_db().execute("SELECT handled FROM feedback WHERE id=?", (fid,)).fetchone()
    assert row["handled"] == 1


def test_nav_shows_unhandled_feedback_badge(app_module, client):
    client.post("/feedback", data={"message": "x"}, follow_redirects=True)
    login(client)
    resp = client.get("/admin/orders")
    assert b"Feedback" in resp.data


# ---- LINE bot: feedback keyword ---------------------------------------------

def test_webhook_feedback_keyword_replies_with_link(app_module, client, monkeypatch):
    from test_webhook import make_order, message_event, post_webhook

    replies = []
    monkeypatch.setattr(app_module, "line_reply", lambda token, msg: replies.append(msg) or True)
    make_order(app_module, name="FeedbackCust", link_code="FBK001")
    post_webhook(client, "test-line-channel-secret", message_event("U1", "FBK001"))  # link first

    post_webhook(client, "test-line-channel-secret", message_event("U1", "feedback"))
    assert "/feedback" in str(replies[-1])
