import os
import re
import io
import glob
import json
import sqlite3
import secrets
import hashlib
import hmac
import base64
import logging
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from functools import wraps

import requests
import segno
from PIL import Image, ImageOps
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

# OpenRouter (openrouter.ai) powers all LLM-assisted features: Chinese title
# translation, the admin "paste customer info" quick-fill, and the bilingual
# (Thai/English) message-intent classifier the LINE bot uses for anything
# that doesn't match a fast-path keyword. Every call site degrades cleanly
# (returns None, never raises) when this is unset.
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "anthropic/claude-haiku-4.5")
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
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

VALID_MODES = {"รถ", "เรือ"}
ALLOWED_IMAGE_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}

# Max long-edge (px) for the stored full image and the list/carousel thumbnail.
IMAGE_MAX_DIM = 1600
THUMB_MAX_DIM = 400
IMAGE_QUALITY = 80


def _normalize_phone(phone):
    """Digits-only phone key used to match repeat customers across orders.
    Returns None for blank/unusable input so we never merge unrelated
    customers on an empty string."""
    digits = re.sub(r"\D", "", phone or "")
    return digits or None

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

        CREATE TABLE IF NOT EXISTS countries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL,
            direction TEXT NOT NULL,          -- 'in' or 'out'
            text TEXT,
            created_at TEXT NOT NULL,
            needs_admin INTEGER NOT NULL DEFAULT 0,
            handled INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (customer_id) REFERENCES customers(id)
        );

        CREATE TABLE IF NOT EXISTS order_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL,
            request_code TEXT UNIQUE NOT NULL,
            item_description TEXT,
            source_link TEXT,
            status TEXT NOT NULL DEFAULT 'new',   -- 'new' or 'converted'
            converted_order_id INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (customer_id) REFERENCES customers(id)
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
    _add_columns(db, "customers", ["address TEXT", "phone_normalized TEXT"])
    # Agency routes: from/to country (FK, or free-text when not in the list),
    # two-way flag, optional external link.
    _add_columns(db, "agencies", [
        "from_country_id INTEGER", "from_country_text TEXT",
        "to_country_id INTEGER", "to_country_text TEXT",
        "two_way INTEGER NOT NULL DEFAULT 0", "url TEXT",
    ])
    # 'bot' (automated) vs 'admin' (typed by a human in /admin/inbox) --
    # only 'admin' outbound replies count toward the 15-min handoff window.
    _add_columns(db, "messages", ["source TEXT NOT NULL DEFAULT 'bot'"])

    # Backfill phone_normalized for any rows that predate the column.
    for row in db.execute(
        "SELECT id, phone FROM customers WHERE phone_normalized IS NULL AND phone IS NOT NULL AND phone != ''"
    ).fetchall():
        db.execute(
            "UPDATE customers SET phone_normalized = ? WHERE id = ?",
            (_normalize_phone(row[1]), row[0]),
        )

    # Seed the starter country list once (bullet list the admin can extend or
    # deactivate later on the Agencies page -- never re-seeded after that).
    if db.execute("SELECT COUNT(*) FROM countries").fetchone()[0] == 0:
        now = datetime.utcnow().isoformat()
        for name in ("Thailand", "China", "Vietnam"):
            db.execute(
                "INSERT INTO countries (name, active, created_at) VALUES (?, 1, ?)",
                (name, now),
            )

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

def _as_message_list(messages):
    """Accept a plain string, a single pre-built message dict, or a
    list[dict] of LINE message objects, and always return the list form
    the LINE API expects (`messages` must be a JSON array)."""
    if isinstance(messages, str):
        return [_line_text(messages)]
    if isinstance(messages, dict):
        return [messages]
    return messages


def line_push(line_user_id, messages):
    if not LINE_CHANNEL_ACCESS_TOKEN or not line_user_id:
        logger.warning("line_push skipped: missing access token or user id")
        return False
    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {"to": line_user_id, "messages": _as_message_list(messages)}
    try:
        r = requests.post(LINE_PUSH_URL, headers=headers, json=payload, timeout=10)
    except requests.RequestException as e:
        logger.error("line_push network error: %s", e)
        return False
    if r.status_code != 200:
        logger.error("line_push failed HTTP %s: %s", r.status_code, r.text[:300])
    return r.status_code == 200


def line_reply(reply_token, messages):
    if not LINE_CHANNEL_ACCESS_TOKEN or not reply_token:
        logger.warning("line_reply skipped: missing access token or reply token")
        return False
    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {"replyToken": reply_token, "messages": _as_message_list(messages)}
    try:
        r = requests.post(LINE_REPLY_URL, headers=headers, json=payload, timeout=10)
    except requests.RequestException as e:
        logger.error("line_reply network error: %s", e)
        return False
    if r.status_code != 200:
        logger.error("line_reply failed HTTP %s: %s", r.status_code, r.text[:300])
    return r.status_code == 200


# ---------- LINE message builders (Flex/quick-reply, used by the webhook) ----------

def _line_text(text, quick_replies=None):
    """quick_replies: optional [(label, message_text), ...] -- tapping sends
    message_text as if the user typed it, keeping the whole flow stateless."""
    msg = {"type": "text", "text": text}
    if quick_replies:
        msg["quickReply"] = {"items": _line_quick_reply_items(quick_replies)}
    return msg


def _line_quick_reply_items(pairs):
    return [
        {"type": "action", "action": {"type": "message", "label": label[:20], "text": text[:300]}}
        for label, text in pairs
    ]


def _line_flex_carousel(alt_text, bubbles):
    return {
        "type": "flex",
        "altText": alt_text[:400],
        "contents": {"type": "carousel", "contents": bubbles},
    }


def _line_image(original_url, preview_url):
    return {"type": "image", "originalContentUrl": original_url, "previewImageUrl": preview_url}


TALK_TO_HUMAN_QUICK_REPLY = [("💬 Talk to support", "HUMAN")]


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


@app.context_processor
def _nav_badge_counts():
    """Pending-count badges for the admin nav (Requests / Inbox). Cheap
    COUNT queries, only meaningful once logged in, harmless elsewhere."""
    if not session.get("logged_in"):
        return {}
    db = get_db()
    pending_requests = db.execute("SELECT COUNT(*) FROM order_requests WHERE status = 'new'").fetchone()[0]
    flagged_threads = db.execute(
        "SELECT COUNT(DISTINCT customer_id) FROM messages WHERE needs_admin = 1"
    ).fetchone()[0]
    return {"pending_requests_count": pending_requests, "flagged_threads_count": flagged_threads}


def _openrouter_chat_json(system_prompt, user_content, max_tokens=300):
    """POST a system+user message to OpenRouter (OpenAI-compatible chat
    completions), expecting the assistant's reply to be a JSON object.
    Returns the parsed dict, or None on any failure -- no key configured,
    network error, non-JSON reply. Callers never see an exception; they just
    fall back to whatever behavior they already had without the AI."""
    if not OPENROUTER_API_KEY or not user_content.strip():
        return None
    try:
        r = requests.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": PUBLIC_BASE_URL or "https://openrouter.ai",
                "X-Title": "China Shipping Tracker",
            },
            json={
                "model": OPENROUTER_MODEL,
                "max_tokens": max_tokens,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
            },
            timeout=15,
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"].strip()
        # Some models (Claude via OpenRouter included) wrap JSON in a
        # markdown code fence despite being told to return raw JSON --
        # strip ```json ... ``` / ``` ... ``` before parsing.
        if content.startswith("```"):
            content = re.sub(r"^```[a-zA-Z]*\n?", "", content)
            content = re.sub(r"\n?```$", "", content)
        return json.loads(content.strip())
    except (requests.RequestException, KeyError, ValueError, IndexError) as e:
        logger.error("OpenRouter call failed: %s", e)
        return None


def translate_title(zh):
    """Chinese product title -> {'en': str, 'th': str} via OpenRouter.
    Returns None if no API key or the call/parse fails -- caller keeps the
    hand-entered values instead. Never raises."""
    data = _openrouter_chat_json(
        "Translate the product title to a few words in English and Thai. "
        'Return ONLY compact JSON: {"en": "...", "th": "..."}',
        zh, max_tokens=200,
    )
    if data is None:
        return None
    return {"en": str(data.get("en", "")), "th": str(data.get("th", ""))}


def parse_customer(text):
    """Free-form customer blob (Thai/EN) -> {'name','phone','address'} via
    OpenRouter. Returns None if no API key or the call/parse fails. Never
    raises."""
    data = _openrouter_chat_json(
        "Split this customer contact blob (Thai or English) into its parts. "
        'Return ONLY compact JSON: {"name": "...", "phone": "...", "address": "..."}. '
        "Use an empty string for any part that is not present.",
        text, max_tokens=400,
    )
    if data is None:
        return None
    return {
        "name": str(data.get("name", "")),
        "phone": str(data.get("phone", "")),
        "address": str(data.get("address", "")),
    }


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


def _clear_old_images(link_code):
    """Remove any previously saved full/thumb files for this order, regardless
    of extension -- prevents stale leftovers when a re-upload changes format."""
    for pattern in (f"{link_code}.*", f"{link_code}_thumb.*"):
        for path in glob.glob(os.path.join(UPLOAD_DIR, pattern)):
            try:
                os.remove(path)
            except OSError:
                pass


def _compress_and_save(fileobj, base_path_no_ext, ext):
    """Resize+re-encode an uploaded/fetched image and write both a full
    (<=IMAGE_MAX_DIM px) and a thumbnail (<=THUMB_MAX_DIM px) version to disk,
    so list/carousel views never load a full-size photo. Raises on a genuinely
    unreadable file -- caller decides how to surface that to the user."""
    img = ImageOps.exif_transpose(Image.open(fileobj))  # respect phone camera orientation
    if ext == "jpg" and img.mode in ("RGBA", "P", "LA"):
        img = img.convert("RGB")

    save_kwargs = {
        "jpg": {"format": "JPEG", "quality": IMAGE_QUALITY, "optimize": True},
        "png": {"format": "PNG", "optimize": True},
        "webp": {"format": "WEBP", "quality": IMAGE_QUALITY},
    }[ext]

    def _resized(max_dim):
        w, h = img.size
        if max(w, h) <= max_dim:
            return img.copy()
        scale = max_dim / max(w, h)
        return img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)

    _resized(IMAGE_MAX_DIM).save(f"{base_path_no_ext}.{ext}", **save_kwargs)
    _resized(THUMB_MAX_DIM).save(f"{base_path_no_ext}_thumb.{ext}", **save_kwargs)


def item_image_thumb(item_image):
    """'uploads/ABC123.jpg' -> 'uploads/ABC123_thumb.jpg' (or None)."""
    if not item_image:
        return None
    root, ext = os.path.splitext(item_image)
    return f"{root}_thumb{ext}"


app.jinja_env.globals["item_image_thumb"] = item_image_thumb

BANGKOK_TZ = ZoneInfo("Asia/Bangkok")


def local_dt(iso_str, fmt="%d %b, %H:%M"):
    """UTC ISO timestamp (as stored everywhere in this app) -> human-readable
    Asia/Bangkok local time, e.g. '14 Sep, 16:27'."""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str).replace(tzinfo=ZoneInfo("UTC"))
    except ValueError:
        return iso_str
    return dt.astimezone(BANGKOK_TZ).strftime(fmt)


app.jinja_env.filters["local_dt"] = local_dt


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


def _find_or_create_customer(name, phone, address):
    """Repeat customers (matched by normalized phone) get attached to their
    existing customer_id instead of a fresh row, so order history / LINE
    linking / "my orders" / reorder-without-re-entering-info all see one
    identity. A blank phone always creates a new customer (nothing to match
    against). Shared by new_order() (admin) and the public /request intake
    (Phase 2.5) so both paths dedupe identically."""
    db = get_db()
    now = datetime.utcnow().isoformat()

    phone_norm = _normalize_phone(phone)
    if phone_norm:
        existing = db.execute(
            "SELECT id FROM customers WHERE phone_normalized = ?", (phone_norm,)
        ).fetchone()
        if existing:
            db.execute(
                "UPDATE customers SET name = ?, address = ? WHERE id = ?",
                (name, address or None, existing["id"]),
            )
            return existing["id"]

    cur = db.execute(
        "INSERT INTO customers (name, phone, phone_normalized, address, created_at) VALUES (?, ?, ?, ?, ?)",
        (name, phone, phone_norm, address, now),
    )
    return cur.lastrowid


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
    customer_id = _find_or_create_customer(name, phone, address)

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

        # Order date / mode / LOT are mistakes-happen fields admins need to be
        # able to correct after creation (unlike v1, which only let price/
        # tracking/image be edited post-creation). Validate before touching
        # anything else so a bad value never partially saves the form.
        order_date_raw = request.form.get("order_date", "").strip()
        created_at = order["created_at"]
        if order_date_raw:
            try:
                datetime.strptime(order_date_raw, "%Y-%m-%d")
            except ValueError:
                flash("Invalid order date.")
                return redirect(url_for("order_detail", order_id=order_id))
            # Keep the existing time-of-day, just replace the date part, so
            # created_at-ordered lists don't all collapse to midnight.
            time_part = created_at[10:] if created_at and len(created_at) > 10 else "T00:00:00"
            created_at = order_date_raw + time_part

        tracking_mode = request.form.get("tracking_mode", "").strip() or order["tracking_mode"]
        if tracking_mode not in VALID_MODES:
            flash("Invalid tracking mode.")
            return redirect(url_for("order_detail", order_id=order_id))

        tracking_lot_raw = request.form.get("tracking_lot", "").strip()
        if tracking_lot_raw:
            if not tracking_lot_raw.isdigit():
                flash("LOT number must be numeric.")
                return redirect(url_for("order_detail", order_id=order_id))
            tracking_lot = int(tracking_lot_raw)
        else:
            tracking_lot = order["tracking_lot"]

        # Auto-translate only when the Chinese title changed and no manual EN/TH
        # override was typed this submit -- never blocks the save if it fails.
        if title_zh and title_zh != (order["item_title_zh"] or "") and not (desc_en or desc_th):
            t = translate_title(title_zh)
            if t:
                desc_en, desc_th = t["en"], t["th"]

        # Image upload (optional): compressed to a capped full size + a small
        # thumbnail, both stored under static/uploads/<link_code>[_thumb].<ext>.
        item_image = order["item_image"]
        file = request.files.get("image")
        if file and file.filename:
            ext = ALLOWED_IMAGE_TYPES.get(file.mimetype)
            if not ext:
                flash("Image must be JPEG, PNG, or WEBP.")
                return redirect(url_for("order_detail", order_id=order_id))
            os.makedirs(UPLOAD_DIR, exist_ok=True)
            _clear_old_images(order["link_code"])
            try:
                _compress_and_save(file, os.path.join(UPLOAD_DIR, order["link_code"]), ext)
            except Exception as e:
                logger.error("image compression failed: %s", e)
                flash("Couldn't process that image — try a different file.")
                return redirect(url_for("order_detail", order_id=order_id))
            item_image = f"uploads/{order['link_code']}.{ext}"

        db.execute(
            "UPDATE orders SET source_link = ?, item_image = ?, item_title_zh = ?, "
            "item_desc_en = ?, item_desc_th = ?, payment_amount = ?, item_cost = ?, "
            "china_ship_fee = ?, house_ship_fee = ?, agency_id = ?, china_tracking_no = ?, "
            "local_carrier = ?, local_tracking_no = ?, shipping_info = ?, created_at = ?, "
            "tracking_mode = ?, tracking_lot = ?, updated_at = ? WHERE id = ?",
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
                request.form.get("shipping_info", "").strip() or None,
                created_at, tracking_mode, tracking_lot,
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
        _clear_old_images(order["link_code"])
        try:
            _compress_and_save(io.BytesIO(resp.content), os.path.join(UPLOAD_DIR, order["link_code"]), ext)
        except Exception as e:
            logger.error("image compression failed (fetch): %s", e)
            flash("Fetched image but couldn't process it — paste or drag one instead.")
            return redirect(url_for("order_detail", order_id=order_id))
        db.execute(
            "UPDATE orders SET item_image = ? WHERE id = ?",
            (f"uploads/{order['link_code']}.{ext}", order_id),
        )
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
            from_country_id = request.form.get("from_country_id", "").strip() or None
            from_country_text = request.form.get("from_country_text", "").strip() or None
            to_country_id = request.form.get("to_country_id", "").strip() or None
            to_country_text = request.form.get("to_country_text", "").strip() or None
            # "Other…" in the select posts the sentinel value "__other__" and
            # the real value in the paired free-text field.
            if from_country_id == "__other__":
                from_country_id = None
            if to_country_id == "__other__":
                to_country_id = None
            two_way = 1 if request.form.get("two_way") else 0
            url = request.form.get("url", "").strip() or None
            if name:
                try:
                    db.execute(
                        "INSERT INTO agencies (name, active, created_at, from_country_id, from_country_text, "
                        "to_country_id, to_country_text, two_way, url) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            name, datetime.utcnow().isoformat(),
                            from_country_id, from_country_text,
                            to_country_id, to_country_text,
                            two_way, url,
                        ),
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
        elif action == "add_country":
            # Internal, admin-only bullet list of countries agencies can route
            # between -- extendable/removable independent of the fixed
            # China->Thailand status pipeline (which doesn't change).
            cname = request.form.get("country_name", "").strip()
            if cname:
                try:
                    db.execute(
                        "INSERT INTO countries (name, active, created_at) VALUES (?, 1, ?)",
                        (cname, datetime.utcnow().isoformat()),
                    )
                    db.commit()
                except sqlite3.IntegrityError:
                    flash("That country already exists.")
        elif action == "toggle_country":
            # Soft remove (like agency toggle) rather than delete, so existing
            # agencies that reference this country don't dangle.
            db.execute(
                "UPDATE countries SET active = 1 - active WHERE id = ?",
                (request.form.get("country_id"),),
            )
            db.commit()
        return redirect(url_for("agencies_page"))

    agencies = db.execute(
        "SELECT agencies.*, fc.name AS from_country_name, tc.name AS to_country_name "
        "FROM agencies "
        "LEFT JOIN countries fc ON agencies.from_country_id = fc.id "
        "LEFT JOIN countries tc ON agencies.to_country_id = tc.id "
        "ORDER BY agencies.active DESC, agencies.name"
    ).fetchall()
    countries = db.execute("SELECT * FROM countries ORDER BY active DESC, name").fetchall()
    return render_template("agencies.html", agencies=agencies, countries=countries)


@app.route("/admin/requests", methods=["GET", "POST"])
@login_required
def requests_page():
    """Queue of order_requests submitted via the public /request page or the
    LINE 'NEW ORDER' chat flow (Phase 2.5). Converting one creates a real
    order -- mode/LOT/agency are only knowable from the physical LOT list,
    so that part still needs an admin."""
    db = get_db()
    if request.method == "POST":
        req = db.execute(
            "SELECT * FROM order_requests WHERE id = ? AND status = 'new'",
            (request.form.get("request_id"),),
        ).fetchone()
        if req is None:
            flash("Request not found or already processed.")
            return redirect(url_for("requests_page"))

        mode = request.form.get("mode", "").strip()
        lot_raw = request.form.get("lot", "").strip()
        agency_id = request.form.get("agency_id", "").strip() or None
        if mode not in VALID_MODES or not lot_raw.isdigit():
            flash("Please pick a valid mode and numeric LOT number.")
            return redirect(url_for("requests_page"))

        now = datetime.utcnow().isoformat()
        link_code = secrets.token_hex(3).upper()
        cur = db.execute(
            "INSERT INTO orders (customer_id, tracking_mode, tracking_lot, agency_id, status, "
            "link_code, source_link, item_desc_en, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'ordered', ?, ?, ?, ?, ?)",
            (req["customer_id"], mode, int(lot_raw), agency_id, link_code,
             req["source_link"], req["item_description"], now, now),
        )
        order_id = cur.lastrowid
        db.execute(
            "UPDATE order_requests SET status = 'converted', converted_order_id = ?, updated_at = ? WHERE id = ?",
            (order_id, now, req["id"]),
        )
        db.commit()
        flash(f"Order created from request. Customer tracking number: {link_code}")
        return redirect(url_for("order_detail", order_id=order_id))

    pending = db.execute(
        "SELECT order_requests.*, customers.name AS customer_name, customers.phone AS customer_phone, "
        "customers.address AS customer_address FROM order_requests "
        "JOIN customers ON order_requests.customer_id = customers.id "
        "WHERE order_requests.status = 'new' ORDER BY order_requests.created_at"
    ).fetchall()
    return render_template("requests.html", pending=pending, agencies=active_agencies())


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
        "openrouter_key_set": bool(OPENROUTER_API_KEY),
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


@app.route("/admin/inbox", methods=["GET", "POST"])
@login_required
def inbox_page():
    """Optional in-app view of LINE conversations (Phase 2.5) -- purely
    additive: replying from the official LINE app still works exactly as
    before. History only exists from Phase 2 onward (nothing retroactive)."""
    db = get_db()
    if request.method == "POST":
        customer = db.execute(
            "SELECT * FROM customers WHERE id = ?", (request.form.get("customer_id"),)
        ).fetchone()
        text = request.form.get("text", "").strip()
        file = request.files.get("image")
        has_image = bool(file and file.filename)

        if not customer or not customer["line_user_id"] or not (text or has_image):
            flash("Pick a linked customer and enter a reply or attach an image.")
            return redirect(url_for("inbox_page"))

        messages = []
        log_text = text
        if has_image:
            ext = ALLOWED_IMAGE_TYPES.get(file.mimetype)
            if not ext:
                flash("Image must be JPEG, PNG, or WEBP.")
                return redirect(url_for("inbox_page"))
            os.makedirs(UPLOAD_DIR, exist_ok=True)
            fname_base = f"inbox_{secrets.token_hex(6)}"
            try:
                _compress_and_save(file, os.path.join(UPLOAD_DIR, fname_base), ext)
            except Exception as e:
                logger.error("inbox image compression failed: %s", e)
                flash("Couldn't process that image — try a different file.")
                return redirect(url_for("inbox_page"))
            base = _public_base_url()
            messages.append(_line_image(
                f"{base}/static/uploads/{fname_base}.{ext}",
                f"{base}/static/uploads/{fname_base}_thumb.{ext}",
            ))
            log_text = f"[image] {text}".strip()
        if text:
            messages.append(_line_text(text))

        if line_push(customer["line_user_id"], messages):
            _log_message(customer["id"], "out", log_text, source="admin")
            db.execute(
                "UPDATE messages SET needs_admin = 0, handled = 1 WHERE customer_id = ? AND needs_admin = 1",
                (customer["id"],),
            )
            db.commit()
        else:
            flash("Send failed — check the LINE access token and server logs.")
        return redirect(url_for("inbox_page"))

    threads = db.execute(
        """
        SELECT customers.id AS customer_id, customers.name, customers.phone, customers.line_user_id,
               MAX(messages.created_at) AS last_at,
               SUM(CASE WHEN messages.needs_admin = 1 THEN 1 ELSE 0 END) AS flagged_count
        FROM messages JOIN customers ON messages.customer_id = customers.id
        GROUP BY customers.id
        ORDER BY flagged_count > 0 DESC, last_at DESC
        """
    ).fetchall()
    history = {}
    for t in threads:
        history[t["customer_id"]] = db.execute(
            "SELECT * FROM messages WHERE customer_id = ? ORDER BY created_at", (t["customer_id"],)
        ).fetchall()
    return render_template("inbox.html", threads=threads, history=history)


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
        return render_template("track.html", order=None, stage_times=None, china_track_url=None), 404
    logs = db.execute(
        "SELECT status, created_at FROM status_log WHERE order_id = ? ORDER BY created_at",
        (order["id"],),
    ).fetchall()
    # stage -> first timestamp it was reached (for the timeline). 'ordered' is the
    # order's own creation time since no log row is written for it.
    stage_times = {"ordered": order["created_at"]}
    for row in logs:
        stage_times.setdefault(row["status"], row["created_at"])
    # China-leg tracking number IS shown to the customer (with its 17TRACK
    # link) -- only the internal agency/route stays hidden.
    china_no = order["china_tracking_no"]
    china_track_url = f"https://t.17track.net/en#nums={china_no}" if china_no else None
    return render_template(
        "track.html", order=order, stage_times=stage_times, china_track_url=china_track_url,
    )


# ---------- Public order requests ----------
# Self-service intake: a customer submits contact info + what they want to
# order, without needing tracking_mode/tracking_lot (only knowable once the
# physical LOT list arrives) -- an admin converts it into a real order via
# /admin/requests. Mirrors the /track/<link_code> pattern: standalone public
# page, no login, its own short code for revisiting.

def _request_form_values(request_row=None, customer_row=None):
    """Defaults for the request form, either blank or pre-filled from an
    existing (still-'new') request for the edit view."""
    return {
        "name": (customer_row["name"] if customer_row else ""),
        "phone": (customer_row["phone"] if customer_row else ""),
        "address": (customer_row["address"] if customer_row else ""),
        "item_description": (request_row["item_description"] if request_row else ""),
        "source_link": (request_row["source_link"] if request_row else ""),
    }


@app.route("/request", methods=["GET", "POST"])
@limiter.limit("10 per hour")
def new_request():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        address = request.form.get("address", "").strip()
        item_description = request.form.get("item_description", "").strip()
        source_link = request.form.get("source_link", "").strip()

        if not name or not phone or not item_description:
            flash("Please fill in your name, phone number, and what you'd like to order.")
            return render_template("request.html", request_row=None, values=request.form, submitted=False)

        db = get_db()
        now = datetime.utcnow().isoformat()
        customer_id = _find_or_create_customer(name, phone, address)
        request_code = secrets.token_hex(3).upper()
        db.execute(
            "INSERT INTO order_requests (customer_id, request_code, item_description, source_link, "
            "status, created_at, updated_at) VALUES (?, ?, ?, ?, 'new', ?, ?)",
            (customer_id, request_code, item_description, source_link, now, now),
        )
        db.commit()
        return redirect(url_for("view_request", request_code=request_code))

    return render_template("request.html", request_row=None, values=_request_form_values(), submitted=False)


@app.route("/request/<request_code>", methods=["GET", "POST"])
@limiter.limit("30 per hour")
def view_request(request_code):
    db = get_db()
    req = db.execute(
        "SELECT order_requests.*, customers.name AS customer_name, customers.phone AS customer_phone, "
        "customers.address AS customer_address FROM order_requests "
        "JOIN customers ON order_requests.customer_id = customers.id "
        "WHERE request_code = ?",
        (request_code.upper(),),
    ).fetchone()
    if req is None:
        return render_template("request.html", request_row=None, values=None, not_found=True), 404

    if request.method == "POST":
        if req["status"] != "new":
            flash("This request has already been processed and can no longer be edited.")
            return redirect(url_for("view_request", request_code=request_code))
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        address = request.form.get("address", "").strip()
        item_description = request.form.get("item_description", "").strip()
        source_link = request.form.get("source_link", "").strip()
        if not name or not phone or not item_description:
            flash("Please fill in your name, phone number, and what you'd like to order.")
            return redirect(url_for("view_request", request_code=request_code))

        now = datetime.utcnow().isoformat()
        db.execute(
            "UPDATE customers SET name = ?, phone = ?, phone_normalized = ?, address = ? WHERE id = ?",
            (name, phone, _normalize_phone(phone), address or None, req["customer_id"]),
        )
        db.execute(
            "UPDATE order_requests SET item_description = ?, source_link = ?, updated_at = ? WHERE id = ?",
            (item_description, source_link, now, req["id"]),
        )
        db.commit()
        flash("Updated.")
        return redirect(url_for("view_request", request_code=request_code))

    values = {
        "name": req["customer_name"], "phone": req["customer_phone"], "address": req["customer_address"] or "",
        "item_description": req["item_description"] or "", "source_link": req["source_link"] or "",
    }
    return render_template("request.html", request_row=req, values=values, submitted=True)


# ---------- LINE webhook helpers ----------

def _looks_like_phone(text):
    digits = _normalize_phone(text)
    return bool(digits and 8 <= len(digits) <= 12)


def _public_base_url():
    return PUBLIC_BASE_URL or request.host_url.rstrip("/")


# ---------- Bilingual (Thai/English) intent dispatch ----------
# Fast path: cheap substring matching, no API call, works with no key at all.
# These are a superset of the old English-only exact-match keywords, so
# nothing regresses -- they now just also match natural Thai phrasing and
# don't require an exact/whole-string match.
BILINGUAL_PHRASES = {
    "status": ["status", "tracking", "where is my order", "สถานะ", "ของถึงไหน", "พัสดุถึงไหน"],
    "history": ["my orders", "order history", "ออเดอร์ของฉัน", "รายการสั่งซื้อ", "ดูออเดอร์"],
    "new_order": ["new order", "order again", "สั่งใหม่", "อยากสั่งของ"],
    "help": ["help", "menu", "ช่วยด้วย", "เมนู"],
    "human": ["human", "agent", "support", "คุยกับคน", "ติดต่อแอดมิน", "แอดมิน", "พนักงาน"],
    "greeting": ["hello", "hi ", "hey", "สวัสดี", "หวัดดี"],
    "thanks": ["thanks", "thank you", "thx", "ขอบคุณ", "ขอบใจ"],
}

INTENT_CATEGORIES = set(BILINGUAL_PHRASES) | {"contact_info", "other"}

# Exact text of the bot's own "what would you like to order?" prompt -- the
# reorder follow-up check looks for OUR last reply being this, rather than
# the customer's previous message being a literal trigger phrase, so it
# works the same whether that prompt was reached via the fast path or the
# AI classifier below.
NEW_ORDER_PROMPT_TEXT = "What would you like to order? Paste the product link or describe the item."


def _match_bilingual_phrase(text):
    """Case-insensitive substring match against BILINGUAL_PHRASES. Returns
    the matched intent name, or None. No API call -- this is the fast,
    free path that handles the common cases in either language."""
    lowered = text.lower()
    for intent, phrases in BILINGUAL_PHRASES.items():
        if any(p in lowered for p in phrases):
            return intent
    return None


def classify_intent(text):
    """Bilingual (Thai/English/mixed) message understanding for anything
    that doesn't match a fast-path phrase -- one OpenRouter call per
    genuinely ambiguous message, not per message. Returns a dict:
      {"intent": one of INTENT_CATEGORIES,
       "name": "", "phone": "", "address": "",
       "item_description": "", "source_link": ""}
    (extraction fields populated only when clearly present), or None if no
    API key or the call/parse fails. Never raises."""
    data = _openrouter_chat_json(
        "You read a customer message to a China->Thailand shipping tracker's "
        "support chatbot. The message may be Thai, English, or mixed. "
        "Classify its intent and extract any structured info present. "
        'Return ONLY compact JSON: {"intent": "...", "name": "", "phone": "", '
        '"address": "", "item_description": "", "source_link": ""}. '
        "intent must be exactly one of: status (asking about their order/tracking), "
        "history (wants to see all their orders), "
        "new_order (wants to place or start a new order -- extract item_description/"
        "source_link if they described what they want), "
        "contact_info (giving their name/phone/address -- extract those fields), "
        "human (wants to talk to a person, OR sounds frustrated/upset/complaining), "
        "greeting (hello with no specific ask), "
        "thanks (acknowledgment/thank you), "
        "other (anything else). "
        "Leave extraction fields blank unless clearly present in the message.",
        text, max_tokens=300,
    )
    if data is None or data.get("intent") not in INTENT_CATEGORIES:
        return None
    return {
        "intent": data["intent"],
        "name": str(data.get("name", "")),
        "phone": str(data.get("phone", "")),
        "address": str(data.get("address", "")),
        "item_description": str(data.get("item_description", "")),
        "source_link": str(data.get("source_link", "")),
    }


def _log_message(customer_id, direction, text, needs_admin=0, source="bot"):
    db = get_db()
    db.execute(
        "INSERT INTO messages (customer_id, direction, text, created_at, needs_admin, source) VALUES (?, ?, ?, ?, ?, ?)",
        (customer_id, direction, text, datetime.utcnow().isoformat(), needs_admin, source),
    )
    db.commit()


HUMAN_HANDOFF_WINDOW_MINUTES = 15


def _human_recently_active(customer_id):
    """True if an admin personally replied (via /admin/inbox) to this
    customer within the last HUMAN_HANDOFF_WINDOW_MINUTES -- while true, the
    bot stays completely silent rather than risk contradicting/interrupting
    an in-progress human conversation. Can only see replies sent through the
    Inbox; a reply typed directly in the official LINE app is invisible to
    this backend and won't extend the window."""
    cutoff = (datetime.utcnow() - timedelta(minutes=HUMAN_HANDOFF_WINDOW_MINUTES)).isoformat()
    row = get_db().execute(
        "SELECT 1 FROM messages WHERE customer_id = ? AND direction = 'out' AND source = 'admin' "
        "AND created_at > ? LIMIT 1",
        (customer_id, cutoff),
    ).fetchone()
    return row is not None


def _order_history_carousel(customer_id):
    """Flex carousel of a customer's last 10 orders (LINE's hard cap is 12
    bubbles). Each bubble links out to the existing public /track page rather
    than replying with a text detail -- no new postback-event handling needed,
    and the customer gets the full timeline + China tracking there."""
    db = get_db()
    orders = db.execute(
        "SELECT * FROM orders WHERE customer_id = ? ORDER BY created_at DESC LIMIT 10",
        (customer_id,),
    ).fetchall()
    if not orders:
        return None

    base = _public_base_url()
    bubbles = []
    for o in orders:
        title = o["item_desc_en"] or o["item_desc_th"] or f"Order {o['link_code']}"
        status_label = CUSTOMER_STATUS.get(o["status"], o["status"])
        paid = f"฿{o['payment_amount']:,.0f}" if o["payment_amount"] else "—"
        bubble = {
            "type": "bubble",
            "body": {
                "type": "box",
                "layout": "vertical",
                "spacing": "sm",
                "contents": [
                    {"type": "text", "text": title[:60], "weight": "bold", "size": "sm", "wrap": True},
                    {"type": "text", "text": status_label, "size": "xs", "color": "#0d9488"},
                    {
                        "type": "box", "layout": "horizontal", "margin": "md",
                        "contents": [
                            {"type": "text", "text": "Paid", "size": "xs", "color": "#94a3b8", "flex": 1},
                            {"type": "text", "text": paid, "size": "xs", "align": "end", "flex": 1},
                        ],
                    },
                    {
                        "type": "box", "layout": "horizontal",
                        "contents": [
                            {"type": "text", "text": "Date", "size": "xs", "color": "#94a3b8", "flex": 1},
                            {"type": "text", "text": (o["created_at"] or "")[:10], "size": "xs", "align": "end", "flex": 1},
                        ],
                    },
                ],
            },
            "footer": {
                "type": "box", "layout": "vertical",
                "contents": [{
                    "type": "button", "style": "primary", "color": "#0f172a", "height": "sm",
                    "action": {"type": "uri", "label": "View tracking", "uri": f"{base}/track/{o['link_code']}"},
                }],
            },
        }
        # LINE's Flex "image" component requires JPEG/PNG over HTTPS -- skip
        # the hero for .webp thumbnails rather than risk a broken image.
        thumb = item_image_thumb(o["item_image"])
        if thumb and thumb.lower().endswith((".jpg", ".jpeg", ".png")):
            bubble["hero"] = {
                "type": "image", "url": f"{base}/static/{thumb}",
                "size": "full", "aspectRatio": "1:1", "aspectMode": "cover",
            }
        bubbles.append(bubble)

    return _line_flex_carousel(f"Your {len(bubbles)} order(s)", bubbles)


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

        # ---- Unlinked LINE user: link by code, link by phone, or guide them ----
        if customer is None:
            linked_customer_id = None

            # Always try an exact link_code match first (cheap, authoritative
            # DB lookup -- no need to pre-guess the shape of a real code).
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
                        linked_customer_id = order["customer_id"]
                    except sqlite3.IntegrityError:
                        # This LINE account is already linked to a different customer.
                        db.rollback()
                        line_reply(reply_token, "This LINE account is already linked to another order.")

            elif _looks_like_phone(text):
                matches = db.execute(
                    "SELECT * FROM customers WHERE phone_normalized = ?", (_normalize_phone(text),)
                ).fetchall()
                unlinked = [c for c in matches if not c["line_user_id"]]
                if any(c["line_user_id"] for c in matches):
                    line_reply(reply_token, _line_text(
                        "That phone number is already linked to another LINE account. "
                        "If this is you, tap below and we'll help.",
                        quick_replies=TALK_TO_HUMAN_QUICK_REPLY,
                    ))
                elif len(unlinked) == 1:
                    try:
                        db.execute(
                            "UPDATE customers SET line_user_id = ? WHERE id = ?",
                            (line_user_id, unlinked[0]["id"]),
                        )
                        db.commit()
                        linked_customer_id = unlinked[0]["id"]
                    except sqlite3.IntegrityError:
                        db.rollback()
                        line_reply(reply_token, "This LINE account is already linked to another order.")
                elif len(unlinked) > 1:
                    # Duplicate customer records predating the Phase 1 fix --
                    # ambiguous, ask for the unambiguous code instead.
                    line_reply(reply_token, "Found more than one account with that number — please send the tracking code we gave you instead.")
                else:
                    line_reply(reply_token, "We couldn't find an order with that phone number — please double-check it, or send your tracking code instead.")

            elif text.strip().upper() == "REGISTER":
                line_reply(reply_token, "Please type the phone number you gave us when ordering.")

            else:
                line_reply(reply_token, _line_text(
                    "Welcome! To link your shipment, either:\n"
                    "📱 send the phone number you gave us when ordering, or\n"
                    "🔑 send the tracking code we gave you.",
                    quick_replies=[("📱 Register by phone", "REGISTER"), ("🔑 I have a code", "I'll type my code now.")],
                ))

            if linked_customer_id:
                messages = [_line_text("You're linked! We'll send shipment updates here.")]
                carousel = _order_history_carousel(linked_customer_id)
                if carousel:
                    messages.append(carousel)
                line_reply(reply_token, messages)
            continue

        # ---- Linked customer: log the inbound message, then dispatch ----
        _log_message(customer["id"], "in", text)

        # An admin personally answered this customer in the last 15 minutes
        # (via /admin/inbox) -- stay completely silent so the bot doesn't
        # interrupt/contradict an in-progress human conversation. The message
        # is still logged above so it shows up in the Inbox thread. Normal
        # bot dispatch resumes automatically once the window expires.
        if _human_recently_active(customer["id"]):
            continue

        def _reply(messages, needs_admin_text=None):
            line_reply(reply_token, messages)
            out_text = messages if isinstance(messages, str) else "(rich message)"
            _log_message(customer["id"], "out", out_text)
            if needs_admin_text is not None:
                db.execute(
                    "UPDATE messages SET needs_admin = 1 WHERE id = (SELECT id FROM messages WHERE customer_id = ? AND direction = 'in' ORDER BY id DESC LIMIT 1)",
                    (customer["id"],),
                )
                db.commit()

        # ---- Intent handlers, shared by the bilingual fast path (no API
        # call) and the AI classifier fallback below, so each intent is
        # handled identically regardless of how it was recognized. ----

        def _handle_status():
            latest = db.execute(
                "SELECT * FROM orders WHERE customer_id = ? ORDER BY updated_at DESC LIMIT 1",
                (customer["id"],),
            ).fetchone()
            if latest:
                label = CUSTOMER_STATUS.get(latest["status"], latest["status"])
                _reply(_line_text(f"[{latest['link_code']}] Status: {label}", quick_replies=TALK_TO_HUMAN_QUICK_REPLY))
            else:
                _reply("You don't have any orders on file yet.")

        def _handle_history():
            carousel = _order_history_carousel(customer["id"])
            _reply([carousel] if carousel else "You don't have any orders on file yet.")

        def _handle_help():
            _reply(_line_text(
                "You can:\n📦 Type MY ORDERS to see your shipments\n🛒 Type NEW ORDER to request another\n💬 Talk to support anytime",
                quick_replies=[("📦 My Orders", "MY ORDERS")] + TALK_TO_HUMAN_QUICK_REPLY,
            ))

        def _handle_human():
            _reply("A human will follow up shortly.", needs_admin_text=text)

        def _handle_greeting():
            _reply(_line_text(
                "Hi! 👋 I can check your order status, show your order history, or connect you to support.",
                quick_replies=[("📦 My Orders", "MY ORDERS")] + TALK_TO_HUMAN_QUICK_REPLY,
            ))

        def _handle_thanks():
            _reply("You're welcome! 🙂")

        def _handle_new_order(item_description=None, source_link=None):
            if item_description:
                now = datetime.utcnow().isoformat()
                req_code = secrets.token_hex(3).upper()
                db.execute(
                    "INSERT INTO order_requests (customer_id, request_code, item_description, source_link, "
                    "status, created_at, updated_at) VALUES (?, ?, ?, ?, 'new', ?, ?)",
                    (customer["id"], req_code, item_description, source_link or None, now, now),
                )
                db.commit()
                _reply("Got it — we'll set this up and confirm once it ships.")
            else:
                _reply(NEW_ORDER_PROMPT_TEXT)

        def _handle_contact_info(name, phone, address):
            db.execute(
                "UPDATE customers SET name = ?, address = ? WHERE id = ?",
                (name or customer["name"], address or customer["address"], customer["id"]),
            )
            latest_order = db.execute(
                "SELECT * FROM orders WHERE customer_id = ? ORDER BY updated_at DESC LIMIT 1",
                (customer["id"],),
            ).fetchone()
            if latest_order and not latest_order["shipping_info"]:
                db.execute("UPDATE orders SET shipping_info = ? WHERE id = ?", (text, latest_order["id"]))
            db.commit()
            _reply("Thanks — we've saved your details.")

        def _handle_unclear():
            # Safe fallback: never guess/dump status. Escalate only when
            # there's truly nothing else to offer (no orders at all) -- the
            # one genuine dead-end, matching the original fallback trigger.
            has_orders = db.execute(
                "SELECT 1 FROM orders WHERE customer_id = ? LIMIT 1", (customer["id"],)
            ).fetchone() is not None
            menu = _line_text(
                "Sorry, I didn't quite understand that. Here's what I can help with:",
                quick_replies=[("📦 My Orders", "MY ORDERS")] + TALK_TO_HUMAN_QUICK_REPLY,
            )
            _reply(menu, needs_admin_text=(None if has_orders else text))

        INTENT_HANDLERS = {
            "status": _handle_status, "history": _handle_history, "help": _handle_help,
            "human": _handle_human, "greeting": _handle_greeting, "thanks": _handle_thanks,
        }

        upper_text = text.strip().upper()

        # Own tracking code -> friendly status (checked first: deterministic,
        # exact, and language-agnostic). Scoped to their own orders so a code
        # can't be used to peek at someone else's shipment.
        code_order = db.execute(
            "SELECT * FROM orders WHERE link_code = ? AND customer_id = ?",
            (upper_text, customer["id"]),
        ).fetchone()
        if code_order:
            label = CUSTOMER_STATUS.get(code_order["status"], code_order["status"])
            _reply(f"[{code_order['link_code']}] Status: {label}")
            continue

        # Bilingual fast path (no API call) -- also serves as an escape hatch
        # from the reorder follow-up below, checked first so typing e.g.
        # "help" while the bot is waiting for an item description doesn't get
        # captured as the item description.
        fast_intent = _match_bilingual_phrase(text)
        if fast_intent in INTENT_HANDLERS:
            INTENT_HANDLERS[fast_intent]()
            continue
        if fast_intent == "new_order":
            _handle_new_order()
            continue

        # Reorder follow-up: if OUR last reply was the "what would you like
        # to order?" prompt, treat THIS message as the item description --
        # works the same whether that prompt was reached via the fast path
        # above or the AI classifier below, and reuses already-logged
        # history as implicit state (2.1's stateless design) instead of a
        # dedicated state table.
        last_out = db.execute(
            "SELECT text FROM messages WHERE customer_id = ? AND direction = 'out' ORDER BY id DESC LIMIT 1",
            (customer["id"],),
        ).fetchone()
        if last_out and last_out["text"] == NEW_ORDER_PROMPT_TEXT:
            _handle_new_order(item_description=text)
            continue

        # No fast-path match: ask the bilingual AI classifier (one call per
        # genuinely ambiguous message) if a key is configured, else fall
        # back straight to the safe menu -- never a guessed status dump.
        result = classify_intent(text) if OPENROUTER_API_KEY else None
        if result is None:
            _handle_unclear()
            continue

        intent = result["intent"]
        if intent in INTENT_HANDLERS:
            INTENT_HANDLERS[intent]()
        elif intent == "new_order":
            _handle_new_order(result["item_description"] or None, result["source_link"] or None)
        elif intent == "contact_info":
            _handle_contact_info(result["name"], result["phone"], result["address"])
        else:
            _handle_unclear()

    return "OK", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=IS_DEV)
