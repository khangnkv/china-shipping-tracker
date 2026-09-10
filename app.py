import os
import re
import json
import sqlite3
import secrets
import hashlib
import hmac
import base64
import logging
from datetime import datetime, date
from functools import wraps

import requests
import segno
from dotenv import load_dotenv
from flask import Flask, request, session, redirect, url_for, render_template, flash, g
from flask_wtf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix

load_dotenv()  # loads .env into the environment on any platform/shell

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("shipping-tracker")

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "tracker.db"))
FLASK_ENV = os.environ.get("FLASK_ENV", "production")
IS_DEV = FLASK_ENV == "development"

SECRET_KEY = os.environ.get("SECRET_KEY")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
if not SECRET_KEY or not ADMIN_PASSWORD:
    raise RuntimeError(
        "SECRET_KEY and ADMIN_PASSWORD must be set in the environment "
        "(see .env.example) -- refusing to start with insecure defaults."
    )

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
# Public base URL of this app (no trailing slash), used to build the /track QR
# target. Falls back to the request host when unset so local dev still works.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
# Optional LINE Official Account id (e.g. "@parcelping"). If set, the public
# /track page shows a "Contact shop on LINE" button; otherwise it's hidden.
LINE_CONTACT = os.environ.get("LINE_CONTACT", "").strip()

# Timestamp (ISO) of the last inbound LINE webhook, for the diagnostics page.
LAST_WEBHOOK_AT = None

LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

VALID_MODES = {"รถ", "เรือ"}
ALLOWED_IMAGE_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}

# The 8-stage pipeline (internal code order). Legs 1-3 are the "China leg"
# (china_tracking_no + 17TRACK link; the daily LOT match sets at_china_wh);
# legs 4-8 are advanced manually per order on the detail page.
STATUS_ORDER = [
    "ordered", "to_china_wh", "at_china_wh", "cross_border",
    "th_customs", "at_th_wh", "out_for_delivery", "delivered",
]

# Internal status -> friendly customer-facing label. Used by the public /track
# page and LINE replies so customers never see internal state names.
CUSTOMER_STATUS = {
    "ordered": "Ordered",
    "to_china_wh": "In transit to China warehouse",
    "at_china_wh": "At China warehouse",
    "cross_border": "Cross-border transit",
    "th_customs": "Thailand customs",
    "at_th_wh": "At Thailand warehouse",
    "out_for_delivery": "Out for delivery",
    "delivered": "Delivered",
}

CUSTOMER_STATUS_TH = {
    "ordered": "สั่งซื้อแล้ว",
    "to_china_wh": "กำลังส่งไปคลังจีน",
    "at_china_wh": "ถึงคลังจีนแล้ว",
    "cross_border": "ขนส่งข้ามพรมแดน",
    "th_customs": "ศุลกากรไทย",
    "at_th_wh": "ถึงคลังไทยแล้ว",
    "out_for_delivery": "กำลังจัดส่ง",
    "delivered": "จัดส่งสำเร็จ",
}

# Legacy 4-status codes -> new codes, applied once by init_db to existing rows.
LEGACY_STATUS_MAP = {
    "pending": "ordered",
    "arrived_awaiting_info": "at_china_wh",
    "info_submitted": "at_china_wh",
    "shipped": "delivered",
}

app = Flask(__name__)
# Behind Caddy (TLS terminates at the proxy): trust one hop of X-Forwarded-* so
# Flask sees https + the real host -> correct secure cookies and generated URLs.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key = SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=not IS_DEV,
    MAX_CONTENT_LENGTH=5 * 1024 * 1024,  # 5 MB (allows product image uploads)
)

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "static", "uploads")

csrf = CSRFProtect(app)
limiter = Limiter(key_func=get_remote_address, app=app, default_limits=["200 per hour"])


@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


# ---------- DB helpers ----------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        # WAL = concurrent readers while one writer runs; busy_timeout waits out
        # brief write locks instead of failing under the 2 gunicorn workers.
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA busy_timeout=5000")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT,
            line_user_id TEXT UNIQUE,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL,
            tracking_mode TEXT NOT NULL,      -- 'รถ' or 'เรือ'
            tracking_lot INTEGER NOT NULL,
            tracking_sub TEXT,                -- e.g. '18' from 11092/18, informational only
            status TEXT NOT NULL DEFAULT 'pending',  -- pending / arrived_awaiting_info / info_submitted / shipped
            shipping_info TEXT,
            link_code TEXT UNIQUE NOT NULL,
            last_notified_status TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (customer_id) REFERENCES customers(id)
        );

        CREATE TABLE IF NOT EXISTS status_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            note TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (order_id) REFERENCES orders(id)
        );

        CREATE TABLE IF NOT EXISTS agencies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        """
    )
    # CREATE TABLE IF NOT EXISTS won't add columns to an existing tracker.db,
    # so migrate the v2 + v3 columns idempotently.
    _add_columns(db, "orders", [
        "source_link TEXT", "item_image TEXT", "item_title_zh TEXT",
        "item_desc_en TEXT", "item_desc_th TEXT",
        "payment_amount REAL", "item_cost REAL",
        "china_ship_fee REAL", "house_ship_fee REAL",
        # v3 deep tracking + agency
        "agency_id INTEGER", "china_tracking_no TEXT",
        "local_carrier TEXT", "local_tracking_no TEXT",
    ])
    _add_columns(db, "customers", ["address TEXT"])

    # One-time remap of legacy 4-status codes to the new 8-stage pipeline.
    for old, new in LEGACY_STATUS_MAP.items():
        db.execute("UPDATE orders SET status = ? WHERE status = ?", (new, old))
        db.execute("UPDATE status_log SET status = ? WHERE status = ?", (new, old))
    db.commit()
    db.close()


def _add_columns(db, table, cols):  # cols: ["name TYPE", ...]
    existing = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
    for col in cols:
        if col.split()[0] not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {col}")


init_db()  # idempotent; must run regardless of entrypoint (dev server, flask run, gunicorn)


# ---------- Auth ----------

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


@app.route("/admin/login", methods=["GET", "POST"])
@limiter.limit("5 per minute")
def login():
    if request.method == "POST":
        submitted = request.form.get("password", "")
        if hmac.compare_digest(submitted, ADMIN_PASSWORD):
            session["logged_in"] = True
            return redirect(url_for("orders_page"))
        flash("Wrong password")
    return render_template("login.html")


@app.route("/admin/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------- LOT list parsing ----------

def parse_lot_list(text):
    """
    Parses lines like:
      LOT รถ : 11278, 11279, 11285
      LOT เรือ : ไม่มีตู้เรือเข้าไทย
    Returns dict: {'รถ': set(int), 'เรือ': set(int)}
    An empty set means 'nothing currently in the system for this mode'.
    """
    result = {"รถ": set(), "เรือ": set()}
    for line in text.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        mode_part, rest = line.split(":", 1)
        mode = "รถ" if "รถ" in mode_part else ("เรือ" if "เรือ" in mode_part else None)
        if mode is None:
            continue
        numbers = re.findall(r"\d+", rest)
        result[mode] = set(int(n) for n in numbers)
    return result


def apply_matching(lot_data):
    """
    Compares not-yet-arrived orders (China leg) against today's lot_data and
    flips matches to 'at_china_wh'. Returns the list of orders that newly arrived.
    """
    db = get_db()
    changes = []
    open_orders = db.execute(
        "SELECT * FROM orders WHERE status IN ('ordered', 'to_china_wh')"
    ).fetchall()

    for order in open_orders:
        mode = order["tracking_mode"]
        lot = order["tracking_lot"]
        found = lot in lot_data.get(mode, set())
        if found:
            now = datetime.utcnow().isoformat()
            db.execute(
                "UPDATE orders SET status = 'at_china_wh', updated_at = ? WHERE id = ?",
                (now, order["id"]),
            )
            db.execute(
                "INSERT INTO status_log (order_id, status, note, created_at) VALUES (?, ?, ?, ?)",
                (order["id"], "at_china_wh", "Matched in today's LOT list", now),
            )
            changes.append({"order": order, "new_status": "at_china_wh"})
    db.commit()
    return changes


# ---------- LINE helpers ----------

def line_push(line_user_id, text):
    if not LINE_CHANNEL_ACCESS_TOKEN or not line_user_id:
        logger.warning("line_push skipped: missing access token or user id")
        return False
    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {"to": line_user_id, "messages": [{"type": "text", "text": text}]}
    try:
        r = requests.post(LINE_PUSH_URL, headers=headers, json=payload, timeout=10)
    except requests.RequestException as e:
        logger.error("line_push network error: %s", e)
        return False
    if r.status_code != 200:
        logger.error("line_push failed HTTP %s: %s", r.status_code, r.text[:300])
    return r.status_code == 200


def line_reply(reply_token, text):
    if not LINE_CHANNEL_ACCESS_TOKEN or not reply_token:
        logger.warning("line_reply skipped: missing access token or reply token")
        return False
    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {"replyToken": reply_token, "messages": [{"type": "text", "text": text}]}
    try:
        r = requests.post(LINE_REPLY_URL, headers=headers, json=payload, timeout=10)
    except requests.RequestException as e:
        logger.error("line_reply network error: %s", e)
        return False
    if r.status_code != 200:
        logger.error("line_reply failed HTTP %s: %s", r.status_code, r.text[:300])
    return r.status_code == 200


def verify_line_signature(body, signature):
    if not LINE_CHANNEL_SECRET:
        # Fail closed: without a configured secret we cannot verify anything,
        # so refuse the request rather than accepting unsigned webhooks.
        return False
    hash_ = hmac.new(LINE_CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(hash_).decode("utf-8")
    return hmac.compare_digest(expected, signature or "")


# ---------- Order helpers (profit, translation, QR) ----------

def order_profit(row):
    """Returns {'profit': float, 'margin': float|None} for an order row.
    margin is None when payment_amount is missing/zero (shown as '—')."""
    payment = row["payment_amount"] or 0
    profit = payment - (row["item_cost"] or 0) - (row["china_ship_fee"] or 0) - (row["house_ship_fee"] or 0)
    margin = (profit / payment * 100) if payment else None
    return {"profit": profit, "margin": margin}


app.jinja_env.globals["order_profit"] = order_profit
app.jinja_env.globals["CUSTOMER_STATUS"] = CUSTOMER_STATUS
app.jinja_env.globals["CUSTOMER_STATUS_TH"] = CUSTOMER_STATUS_TH
app.jinja_env.globals["LINE_CONTACT"] = LINE_CONTACT
app.jinja_env.globals["STATUS_ORDER"] = STATUS_ORDER


def translate_title(zh):
    """Chinese product title -> {'en': str, 'th': str} via Claude Haiku.
    Returns None if no API key or the call/parse fails -- caller keeps the
    hand-entered values instead. Never raises."""
    if not ANTHROPIC_API_KEY or not zh:
        return None
    try:
        r = requests.post(
            ANTHROPIC_URL,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 200,
                "system": (
                    "Translate the product title to a few words in English and Thai. "
                    'Return ONLY compact JSON: {"en": "...", "th": "..."}'
                ),
                "messages": [{"role": "user", "content": zh}],
            },
            timeout=15,
        )
        r.raise_for_status()
        text = r.json()["content"][0]["text"]
        data = json.loads(text)
        return {"en": str(data.get("en", "")), "th": str(data.get("th", ""))}
    except (requests.RequestException, KeyError, ValueError, IndexError) as e:
        logger.error("translate_title failed: %s", e)
        return None


def parse_customer(text):
    """Free-form customer blob (Thai/EN) -> {'name','phone','address'} via Claude
    Haiku. Returns None if no API key or the call/parse fails. Never raises."""
    if not ANTHROPIC_API_KEY or not text.strip():
        return None
    try:
        r = requests.post(
            ANTHROPIC_URL,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 400,
                "system": (
                    "Split this customer contact blob (Thai or English) into its parts. "
                    'Return ONLY compact JSON: {"name": "...", "phone": "...", "address": "..."}. '
                    "Use an empty string for any part that is not present."
                ),
                "messages": [{"role": "user", "content": text}],
            },
            timeout=15,
        )
        r.raise_for_status()
        data = json.loads(r.json()["content"][0]["text"])
        return {
            "name": str(data.get("name", "")),
            "phone": str(data.get("phone", "")),
            "address": str(data.get("address", "")),
        }
    except (requests.RequestException, KeyError, ValueError, IndexError) as e:
        logger.error("parse_customer failed: %s", e)
        return None


def active_agencies():
    return get_db().execute(
        "SELECT * FROM agencies WHERE active = 1 ORDER BY name"
    ).fetchall()


def _days_between(a, b):
    """Whole+fractional days between two ISO strings, or None if either missing."""
    if not a or not b:
        return None
    try:
        return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 86400
    except (ValueError, TypeError):
        return None


def order_durations(created_at, stage_times):
    """Per-leg durations in days from a {status: first_ts} map.
    stage_times should include 'ordered' (use the order's created_at)."""
    ordered = stage_times.get("ordered") or created_at
    return {
        "total": _days_between(ordered, stage_times.get("delivered")),
        "china_leg": _days_between(ordered, stage_times.get("at_china_wh")),
        "cross_border": _days_between(stage_times.get("at_china_wh"), stage_times.get("at_th_wh")),
        "last_mile": _days_between(stage_times.get("at_th_wh"), stage_times.get("delivered")),
    }


def qr_svg(url):
    """Inline SVG QR code for a URL (no external files, no Pillow)."""
    return segno.make(url, error="m").svg_inline(scale=4, border=2)


# ---------- Admin routes ----------

@app.route("/healthz")
def healthz():
    return "ok", 200


@app.route("/")
def index():
    return redirect(url_for("orders_page"))


@app.route("/admin/orders", methods=["GET"])
@login_required
def orders_page():
    db = get_db()
    orders = db.execute(
        """
        SELECT orders.*, customers.name AS customer_name, customers.phone AS customer_phone,
               customers.line_user_id AS line_user_id
        FROM orders JOIN customers ON orders.customer_id = customers.id
        ORDER BY orders.created_at DESC
        """
    ).fetchall()

    # Lightweight KPI strip (presentation only, derived from the rows above).
    month_prefix = datetime.utcnow().strftime("%Y-%m")
    kpis = {
        "in_transit": sum(1 for o in orders if o["status"] != "shipped"),
        "shipped_month": sum(
            1 for o in orders
            if o["status"] == "shipped" and (o["updated_at"] or "").startswith(month_prefix)
        ),
        "profit_month": sum(
            order_profit(o)["profit"] for o in orders
            if (o["created_at"] or "").startswith(month_prefix)
        ),
    }
    return render_template("orders.html", orders=orders, kpis=kpis, agencies=active_agencies())


@app.route("/admin/parse-customer", methods=["POST"])
@login_required
def parse_customer_route():
    """Split a pasted customer blob into name/phone/address (JSON) for the form."""
    result = parse_customer(request.form.get("text", ""))
    if result is None:
        return {"ok": False}, 200
    return {"ok": True, **result}, 200


@app.route("/admin/orders/new", methods=["POST"])
@login_required
def new_order():
    name = request.form.get("name", "").strip()
    phone = request.form.get("phone", "").strip()
    address = request.form.get("address", "").strip()
    mode = request.form.get("mode", "").strip()
    lot_raw = request.form.get("lot", "").strip()
    agency_id = request.form.get("agency_id", "").strip() or None

    if not name or mode not in VALID_MODES or not lot_raw.isdigit():
        flash("Please fill in a valid customer name, mode, and numeric LOT number.")
        return redirect(url_for("orders_page"))
    lot = int(lot_raw)

    db = get_db()
    now = datetime.utcnow().isoformat()
    cur = db.execute(
        "INSERT INTO customers (name, phone, address, created_at) VALUES (?, ?, ?, ?)",
        (name, phone, address, now),
    )
    customer_id = cur.lastrowid

    link_code = secrets.token_hex(3).upper()  # e.g. 'A1B2C3'
    db.execute(
        """
        INSERT INTO orders (customer_id, tracking_mode, tracking_lot, agency_id,
                             status, link_code, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'ordered', ?, ?, ?)
        """,
        (customer_id, mode, lot, agency_id, link_code, now, now),
    )
    db.commit()
    flash(f"Order created. Customer tracking number: {link_code}")
    return redirect(url_for("orders_page"))


def _parse_money(raw):
    """'' -> None, otherwise float; invalid -> None (field left unchanged upstream)."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


@app.route("/admin/orders/<int:order_id>", methods=["GET", "POST"])
@login_required
def order_detail(order_id):
    db = get_db()
    order = db.execute(
        "SELECT orders.*, customers.name AS customer_name, customers.phone AS customer_phone, "
        "customers.line_user_id AS line_user_id "
        "FROM orders JOIN customers ON orders.customer_id = customers.id WHERE orders.id = ?",
        (order_id,),
    ).fetchone()
    if order is None:
        flash("Order not found.")
        return redirect(url_for("orders_page"))

    if request.method == "POST":
        now = datetime.utcnow().isoformat()

        # Manual stage advance (legs 4-8). Writes a status_log row, moves the
        # order, captures local carrier/tracking when going out for delivery,
        # and pushes the friendly status to the customer if they're linked.
        if request.form.get("action") == "advance_status":
            new_status = request.form.get("status", "").strip()
            if new_status not in STATUS_ORDER:
                flash("Invalid status.")
                return redirect(url_for("order_detail", order_id=order_id))
            note = request.form.get("note", "").strip()
            if new_status == "out_for_delivery":
                carrier = request.form.get("local_carrier", "").strip()
                tracking = request.form.get("local_tracking_no", "").strip()
                db.execute(
                    "UPDATE orders SET local_carrier = ?, local_tracking_no = ? WHERE id = ?",
                    (carrier, tracking, order_id),
                )
            db.execute("UPDATE orders SET status = ?, updated_at = ? WHERE id = ?", (new_status, now, order_id))
            db.execute(
                "INSERT INTO status_log (order_id, status, note, created_at) VALUES (?, ?, ?, ?)",
                (order_id, new_status, note or None, now),
            )
            db.commit()
            if order["line_user_id"]:
                line_push(order["line_user_id"], f"[{order['link_code']}] Update: {CUSTOMER_STATUS.get(new_status, new_status)}")
            flash(f"Status updated to “{CUSTOMER_STATUS.get(new_status, new_status)}”.")
            return redirect(url_for("order_detail", order_id=order_id))

        source_link = request.form.get("source_link", "").strip()
        title_zh = request.form.get("item_title_zh", "").strip()
        desc_en = request.form.get("item_desc_en", "").strip()
        desc_th = request.form.get("item_desc_th", "").strip()

        # Auto-translate only when the Chinese title changed and no manual EN/TH
        # override was typed this submit -- never blocks the save if it fails.
        if title_zh and title_zh != (order["item_title_zh"] or "") and not (desc_en or desc_th):
            t = translate_title(title_zh)
            if t:
                desc_en, desc_th = t["en"], t["th"]

        # Image upload (optional). Stored as static/uploads/<link_code>.<ext>.
        item_image = order["item_image"]
        file = request.files.get("image")
        if file and file.filename:
            ext = ALLOWED_IMAGE_TYPES.get(file.mimetype)
            if not ext:
                flash("Image must be JPEG, PNG, or WEBP.")
                return redirect(url_for("order_detail", order_id=order_id))
            os.makedirs(UPLOAD_DIR, exist_ok=True)
            fname = f"{order['link_code']}.{ext}"
            file.save(os.path.join(UPLOAD_DIR, fname))
            item_image = f"uploads/{fname}"

        db.execute(
            "UPDATE orders SET source_link = ?, item_image = ?, item_title_zh = ?, "
            "item_desc_en = ?, item_desc_th = ?, payment_amount = ?, item_cost = ?, "
            "china_ship_fee = ?, house_ship_fee = ?, agency_id = ?, china_tracking_no = ?, "
            "local_carrier = ?, local_tracking_no = ?, updated_at = ? WHERE id = ?",
            (
                source_link, item_image, title_zh, desc_en, desc_th,
                _parse_money(request.form.get("payment_amount")),
                _parse_money(request.form.get("item_cost")),
                _parse_money(request.form.get("china_ship_fee")),
                _parse_money(request.form.get("house_ship_fee")),
                request.form.get("agency_id", "").strip() or None,
                request.form.get("china_tracking_no", "").strip(),
                request.form.get("local_carrier", "").strip(),
                request.form.get("local_tracking_no", "").strip(),
                now, order_id,
            ),
        )
        db.commit()
        flash("Saved.")
        return redirect(url_for("order_detail", order_id=order_id))

    base = PUBLIC_BASE_URL or request.host_url.rstrip("/")
    track_url = f"{base}/track/{order['link_code']}"
    china_no = order["china_tracking_no"]
    china_track_url = f"https://t.17track.net/en#nums={china_no}" if china_no else None
    return render_template(
        "order_detail.html", order=order, track_url=track_url, qr=qr_svg(track_url),
        agencies=active_agencies(), china_track_url=china_track_url,
    )


@app.route("/admin/orders/<int:order_id>/fetch-image", methods=["POST"])
@login_required
def fetch_image(order_id):
    """Best-effort: pull an og:image from the order's source_link. Xianyu/Taobao
    block bots, so this frequently fails -- degrade gracefully, never block."""
    db = get_db()
    order = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if order is None:
        flash("Order not found.")
        return redirect(url_for("orders_page"))
    link = (order["source_link"] or "").strip()
    if not link:
        flash("Add a source link first, then fetch.")
        return redirect(url_for("order_detail", order_id=order_id))
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; ShippingTracker/1.0)"}
        html = requests.get(link, headers=headers, timeout=10).text
        m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
        if not m:
            flash("Couldn't find an image on that page — paste or drag one instead.")
            return redirect(url_for("order_detail", order_id=order_id))
        img_url = m.group(1)
        resp = requests.get(img_url, headers=headers, timeout=10)
        ext = ALLOWED_IMAGE_TYPES.get(resp.headers.get("Content-Type", "").split(";")[0].strip())
        if not ext:
            flash("Fetched image isn't a supported type — paste or drag one instead.")
            return redirect(url_for("order_detail", order_id=order_id))
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        fname = f"{order['link_code']}.{ext}"
        with open(os.path.join(UPLOAD_DIR, fname), "wb") as f:
            f.write(resp.content)
        db.execute("UPDATE orders SET item_image = ? WHERE id = ?", (f"uploads/{fname}", order_id))
        db.commit()
        flash("Image fetched from link.")
    except requests.RequestException:
        flash("Couldn't reach that link (site may block bots) — paste or drag an image instead.")
    return redirect(url_for("order_detail", order_id=order_id))


@app.route("/admin/agencies", methods=["GET", "POST"])
@login_required
def agencies_page():
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            name = request.form.get("name", "").strip()
            if name:
                try:
                    db.execute(
                        "INSERT INTO agencies (name, active, created_at) VALUES (?, 1, ?)",
                        (name, datetime.utcnow().isoformat()),
                    )
                    db.commit()
                except sqlite3.IntegrityError:
                    flash("That agency already exists.")
        elif action == "toggle":
            db.execute(
                "UPDATE agencies SET active = 1 - active WHERE id = ?",
                (request.form.get("agency_id"),),
            )
            db.commit()
        return redirect(url_for("agencies_page"))
    agencies = db.execute("SELECT * FROM agencies ORDER BY active DESC, name").fetchall()
    return render_template("agencies.html", agencies=agencies)


@app.route("/admin/match", methods=["GET", "POST"])
@login_required
def match_page():
    changes = None
    if request.method == "POST":
        text = request.form["lot_text"]
        lot_data = parse_lot_list(text)
        changes = apply_matching(lot_data)
    return render_template("match.html", changes=changes)


@app.route("/admin/notify", methods=["POST"])
@login_required
def notify():
    """Push the current friendly status to every linked customer whose status has
    changed since we last notified them (never leaks internal mode/lot/agency)."""
    db = get_db()
    pending = db.execute(
        "SELECT orders.*, customers.line_user_id FROM orders "
        "JOIN customers ON orders.customer_id = customers.id "
        "WHERE customers.line_user_id IS NOT NULL "
        "AND (orders.last_notified_status IS NULL OR orders.last_notified_status != orders.status)"
    ).fetchall()

    sent = 0
    for order in pending:
        label = CUSTOMER_STATUS.get(order["status"], order["status"])
        text = f"[{order['link_code']}] Update: {label}"
        if order["status"] == "out_for_delivery" and order["local_tracking_no"]:
            text += f"\n{order['local_carrier'] or 'Courier'} tracking: {order['local_tracking_no']}"
        if line_push(order["line_user_id"], text):
            db.execute(
                "UPDATE orders SET last_notified_status = ? WHERE id = ?",
                (order["status"], order["id"]),
            )
            sent += 1

    db.commit()
    flash(f"Sent {sent} LINE messages.")
    return redirect(url_for("orders_page"))


@app.route("/admin/line-status", methods=["GET"])
@login_required
def line_status():
    db = get_db()
    linked = db.execute(
        "SELECT customers.id, customers.name, orders.link_code "
        "FROM customers JOIN orders ON orders.customer_id = customers.id "
        "WHERE customers.line_user_id IS NOT NULL ORDER BY customers.name"
    ).fetchall()
    diag = {
        "token_set": bool(LINE_CHANNEL_ACCESS_TOKEN),
        "secret_set": bool(LINE_CHANNEL_SECRET),
        "public_base_url": PUBLIC_BASE_URL or "(unset — falls back to request host)",
        "last_webhook_at": LAST_WEBHOOK_AT or "never",
        "linked_count": len(linked),
    }
    return render_template("line_status.html", diag=diag, linked=linked)


@app.route("/admin/line-test", methods=["POST"])
@login_required
def line_test():
    """Push a fixed test message to a chosen linked customer to prove push works
    independently of the status pipeline."""
    db = get_db()
    customer = db.execute(
        "SELECT * FROM customers WHERE id = ? AND line_user_id IS NOT NULL",
        (request.form.get("customer_id"),),
    ).fetchone()
    if not customer:
        flash("Pick a linked customer first.")
    elif line_push(customer["line_user_id"], "✅ Test message from Shipping Tracker — your LINE is connected."):
        flash(f"Test message sent to {customer['name']}.")
    else:
        flash("Push failed — check the access token and server logs.")
    return redirect(url_for("line_status"))


# ---------- Analytics ----------

def _avg(values):
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


@app.route("/admin/stats", methods=["GET"])
@login_required
def stats():
    db = get_db()
    orders = db.execute(
        "SELECT orders.*, agencies.name AS agency_name "
        "FROM orders LEFT JOIN agencies ON orders.agency_id = agencies.id"
    ).fetchall()

    # Per-order first-timestamp per stage (for durations).
    stage_times_by_order = {}
    for row in db.execute("SELECT order_id, status, created_at FROM status_log ORDER BY created_at"):
        stage_times_by_order.setdefault(row["order_id"], {}).setdefault(row["status"], row["created_at"])

    total_profit = total_revenue = 0.0
    margins, total_days = [], []
    by_mode = {}          # mode -> {'count', 'profit'}
    monthly = {}          # 'YYYY-MM' -> profit
    status_counts = {}
    # agency name -> accumulators
    agg = {}
    latest_agency = (None, "")  # (updated_at, name)

    for o in orders:
        p = order_profit(o)
        total_profit += p["profit"]
        total_revenue += o["payment_amount"] or 0
        if p["margin"] is not None:
            margins.append(p["margin"])
        m = by_mode.setdefault(o["tracking_mode"], {"count": 0, "profit": 0.0})
        m["count"] += 1
        m["profit"] += p["profit"]
        monthly[(o["created_at"] or "")[:7]] = monthly.get((o["created_at"] or "")[:7], 0.0) + p["profit"]
        status_counts[o["status"]] = status_counts.get(o["status"], 0) + 1

        st = stage_times_by_order.get(o["id"], {})
        st.setdefault("ordered", o["created_at"])
        d = order_durations(o["created_at"], st)
        if d["total"] is not None:
            total_days.append(d["total"])

        aname = o["agency_name"] or "Unassigned"
        a = agg.setdefault(aname, {
            "count": 0, "profit": 0.0, "margins": [],
            "total": [], "china_leg": [], "cross_border": [], "last_mile": [],
        })
        a["count"] += 1
        a["profit"] += p["profit"]
        if p["margin"] is not None:
            a["margins"].append(p["margin"])
        for leg in ("total", "china_leg", "cross_border", "last_mile"):
            a[leg].append(d[leg])
        if o["agency_name"] and (latest_agency[0] is None or (o["updated_at"] or "") > latest_agency[0]):
            latest_agency = (o["updated_at"], o["agency_name"])

    # Collapse agency accumulators into display rows + efficiency.
    agencies = []
    for name, a in agg.items():
        avg_total = _avg(a["total"])
        eff = (a["profit"] / avg_total) if avg_total else None  # profit per transit-day
        agencies.append({
            "name": name, "count": a["count"], "profit": a["profit"],
            "avg_margin": _avg(a["margins"]),
            "avg_total": avg_total,
            "avg_china": _avg(a["china_leg"]),
            "avg_cross": _avg(a["cross_border"]),
            "avg_last": _avg(a["last_mile"]),
            "efficiency": eff,
        })
    agencies.sort(key=lambda r: (r["avg_total"] is None, r["avg_total"] or 0))  # fastest first

    return render_template(
        "stats.html",
        total_profit=total_profit,
        total_revenue=total_revenue,
        avg_margin=_avg(margins),
        avg_transit=_avg(total_days),
        status_counts=status_counts,
        by_mode=by_mode,
        monthly=dict(sorted(monthly.items())),
        agencies=agencies,
        latest_agency=latest_agency[1] or None,
    )


# ---------- Public tracking ----------

@app.route("/track/<link_code>", methods=["GET"])
@csrf.exempt
def track(link_code):
    db = get_db()
    order = db.execute("SELECT * FROM orders WHERE link_code = ?", (link_code.upper(),)).fetchone()
    if order is None:
        return render_template("track.html", order=None, stage_times=None), 404
    logs = db.execute(
        "SELECT status, created_at FROM status_log WHERE order_id = ? ORDER BY created_at",
        (order["id"],),
    ).fetchall()
    # stage -> first timestamp it was reached (for the timeline). 'ordered' is the
    # order's own creation time since no log row is written for it.
    stage_times = {"ordered": order["created_at"]}
    for row in logs:
        stage_times.setdefault(row["status"], row["created_at"])
    return render_template("track.html", order=order, stage_times=stage_times)


# ---------- LINE webhook ----------

# LINE delivers webhooks to whatever URL is set in the Developers Console.
# Accept both common paths so the app works whether that URL ends in
# /webhook or /callback -- a mismatch here just yields a silent 404.
@app.route("/webhook", methods=["POST"])
@app.route("/callback", methods=["POST"])
@csrf.exempt
def webhook():
    global LAST_WEBHOOK_AT
    LAST_WEBHOOK_AT = datetime.utcnow().isoformat()
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data()

    if not verify_line_signature(body, signature):
        logger.warning("webhook rejected: invalid or missing X-Line-Signature")
        return "invalid signature", 400

    data = request.get_json(silent=True) or {}
    db = get_db()

    for event in data.get("events", []):
        if event.get("type") != "message":
            continue
        message = event.get("message", {})
        if message.get("type") != "text":
            continue

        # Group/room events and some system events have no userId; skip them
        # rather than raising a KeyError that 500s the whole webhook.
        line_user_id = event.get("source", {}).get("userId")
        reply_token = event.get("replyToken")
        if not line_user_id:
            continue
        text = (message.get("text") or "").strip()

        customer = db.execute(
            "SELECT * FROM customers WHERE line_user_id = ?", (line_user_id,)
        ).fetchone()

        if customer is None:
            # try to link this LINE account using the code they sent
            order = db.execute(
                "SELECT * FROM orders WHERE link_code = ?", (text.upper(),)
            ).fetchone()
            if order:
                target_customer = db.execute(
                    "SELECT * FROM customers WHERE id = ?", (order["customer_id"],)
                ).fetchone()
                if target_customer and target_customer["line_user_id"]:
                    # Code already used to link a (possibly different) LINE
                    # account -- refuse to silently re-link/hijack it.
                    line_reply(reply_token, "This code has already been used to link an account.")
                else:
                    try:
                        db.execute(
                            "UPDATE customers SET line_user_id = ? WHERE id = ?",
                            (line_user_id, order["customer_id"]),
                        )
                        db.commit()
                        line_reply(reply_token, "You're linked! We'll send shipment updates here.")
                    except sqlite3.IntegrityError:
                        # This LINE account is already linked to a different customer.
                        db.rollback()
                        line_reply(reply_token, "This LINE account is already linked to another order.")
            else:
                line_reply(
                    reply_token,
                    "Welcome! Please send the registration code we gave you to link your order.",
                )
            continue

        # Linked customer typing one of their own tracking codes -> reply with
        # the friendly status for that order. Scoped to their own orders so a
        # code can't be used to peek at someone else's shipment.
        code_order = db.execute(
            "SELECT * FROM orders WHERE link_code = ? AND customer_id = ?",
            (text.upper(), customer["id"]),
        ).fetchone()
        if code_order:
            label = CUSTOMER_STATUS.get(code_order["status"], code_order["status"])
            line_reply(reply_token, f"[{code_order['link_code']}] Status: {label}")
            continue

        # Linked customer sent something that isn't a tracking code: reply with the
        # friendly status of their most recent order, else a generic acknowledgement.
        latest = db.execute(
            "SELECT * FROM orders WHERE customer_id = ? ORDER BY updated_at DESC LIMIT 1",
            (customer["id"],),
        ).fetchone()
        if latest:
            label = CUSTOMER_STATUS.get(latest["status"], latest["status"])
            line_reply(reply_token, f"[{latest['link_code']}] Status: {label}")
        else:
            line_reply(reply_token, "Thanks for your message! We'll get back to you if needed.")

    return "OK", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=IS_DEV)
