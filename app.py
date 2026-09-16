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
from flask_wtf.csrf import CSRFError
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

# Theme: cookie holds an explicit "light"/"dark" choice; "auto" (the default,
# same as no cookie at all) means "follow the OS" via the @media query in
# tailwind.input.css -- see inject_theme_and_locale() and set_theme() below.
THEME_COOKIE = "theme"
VALID_THEMES = {"light", "dark", "auto"}
LOCALE_COOKIE = "locale"
VALID_LOCALES = {"en", "th", "vi", "zh", "my"}
LOCALE_LABELS = {"en": "EN", "th": "ไทย", "vi": "Tiếng Việt", "zh": "中文", "my": "မြန်မာ"}
COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # 1 year; cleared by the browser/user clearing cookies

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


# ---------- Friendly error pages ----------
# Replaces Werkzeug/Flask's raw default pages (which drop the visitor out of
# SINEX's chrome entirely -- no header, no way back, no LINE contact) at the
# three points most likely to be hit mid-form: a too-large upload, a stale/
# resubmitted form, or an unhandled server error. "Go back" relies on the
# browser's own form-state restoration on back-navigation (bfcache/history)
# to recover typed values -- Flask itself gets no chance to see the body on
# a 413 (the request is rejected before it's read), so there is nothing
# server-side to re-populate a template with in that case.

def _error_page(code, title_key, body_key):
    return render_template("error.html", code=code, title=t(title_key), body=t(body_key)), code


@app.errorhandler(413)
def too_large(e):
    return _error_page(413, "error.413_title", "error.413_body")


@app.errorhandler(CSRFError)
def csrf_error(e):
    return _error_page(400, "error.csrf_title", "error.csrf_body")


@app.errorhandler(500)
def server_error(e):
    logger.error("unhandled server error: %s", e, exc_info=True)
    return _error_page(500, "error.500_title", "error.500_body")


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

        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            contact TEXT,
            message TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'web',   -- 'web' or 'line'
            handled INTEGER NOT NULL DEFAULT 0,
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
    _add_columns(db, "customers", [
        "address TEXT", "phone_normalized TEXT",
        # v6: other_contact (LINE handle/email/FB) moved here from being
        # request-only, so it's remembered across all of a customer's
        # orders; info_source_note records the raw text + phone the last
        # "paste customer info" auto-extraction produced, so an admin can
        # review/correct it if the extraction got something wrong.
        "other_contact TEXT", "info_source_note TEXT",
    ])
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
    # Budget/quote workflow: customer states a free-text budget (and an
    # optional alternate contact) on /request; the admin prices the item
    # against it on /admin/requests. See _quote_status().
    _add_columns(db, "order_requests", [
        "budget TEXT", "other_contact TEXT", "item_cost REAL", "shipping_cost REAL",
        "reference_image TEXT", "terms_agreed_at TEXT",
    ])

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
app.jinja_env.globals["LOCALE_LABELS"] = LOCALE_LABELS
app.jinja_env.globals["VALID_LOCALES"] = VALID_LOCALES
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
    unhandled_feedback = db.execute("SELECT COUNT(*) FROM feedback WHERE handled = 0").fetchone()[0]
    return {
        "pending_requests_count": pending_requests,
        "flagged_threads_count": flagged_threads,
        "unhandled_feedback_count": unhandled_feedback,
    }


@app.context_processor
def inject_theme_and_locale():
    """Every template gets theme_pref (the raw cookie value, for highlighting
    the active option in the switch), theme_attr (None for "auto" -- leaves
    data-theme unset so the CSS @media query decides -- or "light"/"dark" to
    force it), and locale ("en"/"th", customer-facing pages only)."""
    theme_pref = request.cookies.get(THEME_COOKIE, "auto")
    if theme_pref not in VALID_THEMES:
        theme_pref = "auto"
    locale = request.cookies.get(LOCALE_COOKIE, "en")
    if locale not in VALID_LOCALES:
        locale = "en"
    return {
        "theme_pref": theme_pref,
        "theme_attr": theme_pref if theme_pref in ("light", "dark") else None,
        "locale": locale,
    }


@app.route("/set-theme/<mode>")
def set_theme(mode):
    if mode not in VALID_THEMES:
        mode = "auto"
    resp = redirect(request.referrer or url_for("landing"))
    if mode == "auto":
        resp.delete_cookie(THEME_COOKIE, path="/")
    else:
        resp.set_cookie(THEME_COOKIE, mode, max_age=COOKIE_MAX_AGE, samesite="Lax", secure=not IS_DEV, path="/")
    return resp


@app.route("/set-locale/<lang>")
def set_locale(lang):
    if lang not in VALID_LOCALES:
        lang = "en"
    resp = redirect(request.referrer or url_for("landing"))
    resp.set_cookie(LOCALE_COOKIE, lang, max_age=COOKIE_MAX_AGE, samesite="Lax", secure=not IS_DEV, path="/")
    return resp


# Small, hand-picked bilingual string table for the customer-facing pages
# (landing/track/request) -- NOT a full i18n framework. Covers the labels
# that exist today; anything not in the table falls back to the English
# literal already in the template.
TRANSLATIONS = {
    "landing.title": {"en": "Buy anything in China. We bring it to your door in Thailand.", "th": "ซื้อของจากจีนอะไรก็ได้ เราส่งถึงบ้านคุณในไทย", "vi": "Mua bất cứ thứ gì ở Trung Quốc. Chúng tôi giao tận cửa nhà bạn ở Thái Lan.", "zh": "在中国购买任何商品，我们直接送到您在泰国的家门口。", "my": "တရုတ်ပြည်မှာ ဘာမဆိုဝယ်ပါ။ ထိုင်းနိုင်ငံရှိ သင့်အိမ်တံခါးဝအထိ ကျွန်ုပ်တို့ ပို့ဆောင်ပေးပါမည်။"},
    "landing.subtitle": {"en": "Tell us the item and your budget — we quote the real cost before you pay a baht, then track it all the way home.", "th": "บอกเราว่าอยากได้อะไรและงบเท่าไหร่ เราจะแจ้งราคาจริงก่อนที่คุณจะจ่ายเงิน แล้วติดตามพัสดุได้จนถึงบ้าน", "vi": "Cho chúng tôi biết sản phẩm và ngân sách của bạn — chúng tôi báo giá thực trước khi bạn trả bất kỳ khoản nào, sau đó theo dõi đơn hàng đến tận nhà.", "zh": "告诉我们商品和您的预算——在您付款之前我们会提供真实报价，然后全程为您追踪包裹直到送达。", "my": "ပစ္စည်းနှင့် ဘတ်ဂျက်ကို ပြောပြပါ — ငွေမပေးချေမီ အမှန်တကယ်ကုန်ကျစရိတ်ကို ကျွန်ုပ်တို့ ခန့်မှန်းပေးပြီး၊ အိမ်အထိ ရောက်သည်အထိ ခြေရာခံပေးပါမည်။"},
    "landing.cta": {"en": "Start your order", "th": "เริ่มสั่งซื้อ", "vi": "Bắt đầu đặt hàng", "zh": "开始下单", "my": "အော်ဒါစတင်မည်"},
    "landing.cta_note": {"en": "Takes about 2 minutes — no account needed.", "th": "ใช้เวลาประมาณ 2 นาที ไม่ต้องสมัครสมาชิก", "vi": "Chỉ mất khoảng 2 phút — không cần tạo tài khoản.", "zh": "只需约2分钟，无需注册账户。", "my": "မိနစ် ၂ မိနစ်ခန့်သာကြာပါသည် — အကောင့်မလိုအပ်ပါ။"},
    "landing.how_it_works": {"en": "How it works", "th": "ขั้นตอนการสั่งซื้อ", "vi": "Cách thức hoạt động", "zh": "使用流程", "my": "အလုပ်လုပ်ပုံ"},
    "landing.step1_title": {"en": "Describe your item & budget", "th": "บอกรายละเอียดสินค้าและงบประมาณ", "vi": "Mô tả sản phẩm & ngân sách", "zh": "描述商品和预算", "my": "ပစ္စည်းနှင့် ဘတ်ဂျက်ကို ဖော်ပြပါ"},
    "landing.step1_body": {"en": "Paste a product link or describe it, and tell us roughly what you want to spend.", "th": "วางลิงก์สินค้าหรืออธิบายสินค้า พร้อมบอกงบประมาณคร่าวๆ", "vi": "Dán liên kết sản phẩm hoặc mô tả nó, và cho chúng tôi biết khoảng ngân sách bạn muốn chi.", "zh": "粘贴商品链接或描述商品，并告诉我们大致预算。", "my": "ပစ္စည်းလင့်ခ်ကို ကူးထည့်ပါ သို့မဟုတ် ဖော်ပြပါ၊ ကုန်ကျလိုသော ခန့်မှန်းငွေကိုပါ ပြောပြပါ။"},
    "landing.step2_title": {"en": "We quote the real cost", "th": "เราแจ้งราคาจริง", "vi": "Chúng tôi báo giá thực", "zh": "我们提供真实报价", "my": "အမှန်တကယ်ကုန်ကျစရိတ်ကို ခန့်မှန်းပေးမည်"},
    "landing.step2_body": {"en": "Item price + shipping, checked against your budget — no hidden fees added later.", "th": "ราคาสินค้า + ค่าส่ง เทียบกับงบของคุณ ไม่มีค่าใช้จ่ายแอบแฝงภายหลัง", "vi": "Giá sản phẩm + phí vận chuyển, đối chiếu với ngân sách của bạn — không phát sinh phí ẩn sau này.", "zh": "商品价格+运费，与您的预算核对——之后不会有隐藏费用。", "my": "ပစ္စည်းဈေးနှုန်း + ပို့ဆောင်ခ၊ သင့်ဘတ်ဂျက်နှင့်နှိုင်းယှဉ်ပြီး — နောက်ပိုင်း ဝှက်ထားသောကုန်ကျစရိတ်များ မရှိပါ။"},
    "landing.step3_title": {"en": "You confirm on LINE", "th": "ยืนยันผ่าน LINE", "vi": "Bạn xác nhận qua LINE", "zh": "您在LINE上确认", "my": "LINE မှာ အတည်ပြုပါ"},
    "landing.step3_body": {"en": "A real person messages you to confirm before anything ships.", "th": "มีเจ้าหน้าที่จริงทักมายืนยันก่อนจัดส่งทุกครั้ง", "vi": "Một nhân viên thật sẽ nhắn tin xác nhận với bạn trước khi giao hàng.", "zh": "在发货前，真人客服会与您联系确认。", "my": "ပစ္စည်းမပို့ခင် လူတကယ်က သင့်ကို မက်ဆေ့ချ်ပို့ပြီး အတည်ပြုပါမည်။"},
    "landing.step4_title": {"en": "We ship & you track", "th": "จัดส่งและติดตามสถานะได้", "vi": "Chúng tôi giao hàng & bạn theo dõi", "zh": "我们发货，您追踪", "my": "ကျွန်ုပ်တို့ပို့ဆောင်ပြီး သင်ခြေရာခံနိုင်ပါသည်"},
    "landing.step4_body": {"en": "Follow it from the China warehouse to your door, step by step.", "th": "ติดตามพัสดุตั้งแต่คลังจีนจนถึงหน้าบ้านคุณทีละขั้นตอน", "vi": "Theo dõi từ kho hàng Trung Quốc đến tận cửa nhà bạn, từng bước một.", "zh": "从中国仓库到您家门口，全程逐步追踪。", "my": "တရုတ်ကုန်လှောင်ရုံမှ သင့်အိမ်တံခါးဝအထိ အဆင့်ဆင့် ခြေရာခံနိုင်ပါသည်။"},
    "landing.trust_title": {"en": "Why customers trust SINEX", "th": "ทำไมลูกค้าไว้วางใจ SINEX", "vi": "Vì sao khách hàng tin tưởng SINEX", "zh": "为什么客户信赖SINEX", "my": "SINEX ကို ဖောက်သည်များ ဘာကြောင့်ယုံကြည်သနည်း"},
    "landing.stat_shipments_label": {"en": "shipments delivered", "th": "รายการจัดส่งสำเร็จ", "vi": "đơn hàng đã giao", "zh": "已完成配送", "my": "ပို့ဆောင်ပြီးသည့် ပစ္စည်းများ"},
    "landing.stat_since_label": {"en": "operating since", "th": "ดำเนินการตั้งแต่ปี", "vi": "hoạt động từ", "zh": "运营开始于", "my": "စတင်လည်ပတ်သည့်နှစ်"},
    "landing.trust_line_title": {"en": "A real person answers on LINE", "th": "มีคนจริงตอบแชท LINE", "vi": "Nhân viên thật trả lời trên LINE", "zh": "LINE上由真人回复", "my": "LINE မှာ လူတကယ်ဖြေကြားပေးပါသည်"},
    "landing.trust_line_body": {"en": "Not a bot maze — message us any time you have a question.", "th": "ไม่ใช่บอทวนลูป ทักมาได้ทุกเมื่อที่มีคำถาม", "vi": "Không phải mê cung chatbot — nhắn tin cho chúng tôi bất cứ khi nào bạn có câu hỏi.", "zh": "不是机器人迷宫——有任何问题随时给我们发消息。", "my": "ဘော့ချက်ဝိုင်းထဲ လမ်းမပျောက်ပါနှင့် — မေးခွန်းရှိတိုင်း မည်သည့်အချိန်မဆို မက်ဆေ့ချ်ပို့နိုင်ပါသည်။"},
    "track.title": {"en": "Tracking code", "th": "รหัสติดตามพัสดุ", "vi": "Mã theo dõi", "zh": "追踪码", "my": "ခြေရာခံကုဒ်"},
    "track.recipient": {"en": "Recipient", "th": "ผู้รับ", "vi": "Người nhận", "zh": "收件人", "my": "လက်ခံသူ"},
    "track.timeline": {"en": "Tracking timeline", "th": "ไทม์ไลน์การจัดส่ง", "vi": "Dòng thời gian vận chuyển", "zh": "物流时间线", "my": "ခြေရာခံအချိန်ဇယား"},
    "track.current": {"en": "Current status", "th": "สถานะปัจจุบัน", "vi": "Trạng thái hiện tại", "zh": "当前状态", "my": "လက်ရှိအခြေအနေ"},
    "track.parcel_details": {"en": "Parcel details", "th": "รายละเอียดพัสดุ", "vi": "Chi tiết kiện hàng", "zh": "包裹详情", "my": "ပါဆယ်အသေးစိတ်"},
    "track.china_leg": {"en": "China → warehouse tracking", "th": "เลขติดตามจากจีน → คลังสินค้า", "vi": "Theo dõi Trung Quốc → kho hàng", "zh": "中国→仓库追踪", "my": "တရုတ် → ကုန်လှောင်ရုံ ခြေရာခံမှု"},
    "track.contact_line": {"en": "Contact shop on LINE", "th": "ติดต่อร้านทาง LINE", "vi": "Liên hệ cửa hàng qua LINE", "zh": "通过LINE联系店铺", "my": "LINE မှတစ်ဆင့် ဆိုင်ကိုဆက်သွယ်ပါ"},
    "track.delivered": {"en": "Your parcel has been delivered!", "th": "พัสดุของคุณถึงมือแล้ว!", "vi": "Kiện hàng của bạn đã được giao!", "zh": "您的包裹已送达！", "my": "သင့်ပါဆယ် ရောက်ရှိပြီးပါပြီ!"},
    "track.not_found_title": {"en": "Order not found", "th": "ไม่พบคำสั่งซื้อ", "vi": "Không tìm thấy đơn hàng", "zh": "未找到订单", "my": "အော်ဒါမတွေ့ပါ"},
    "track.not_found_body": {"en": "Please check your tracking code and try again.", "th": "กรุณาตรวจสอบรหัสติดตามแล้วลองใหม่อีกครั้ง", "vi": "Vui lòng kiểm tra mã theo dõi và thử lại.", "zh": "请检查您的追踪码后重试。", "my": "ခြေရာခံကုဒ်ကို စစ်ဆေးပြီး ပြန်လည်ကြိုးစားပါ။"},
    "track.not_found_home": {"en": "Go to homepage", "th": "กลับหน้าแรก", "vi": "Về trang chủ", "zh": "返回首页", "my": "ပင်မစာမျက်နှာသို့ သွားမည်"},
    "track.lookup_cta": {"en": "Track an existing order", "th": "ติดตามคำสั่งซื้อที่มีอยู่", "vi": "Theo dõi đơn hàng hiện có", "zh": "追踪已有订单", "my": "လက်ရှိအော်ဒါကို ခြေရာခံမည်"},
    "track.lookup_title": {"en": "Find your order", "th": "ค้นหาคำสั่งซื้อของคุณ", "vi": "Tìm đơn hàng của bạn", "zh": "查找您的订单", "my": "သင့်အော်ဒါကို ရှာပါ"},
    "track.lookup_subtitle": {"en": "Enter the phone number you used when ordering.", "th": "กรอกเบอร์โทรที่คุณใช้ตอนสั่งซื้อ", "vi": "Nhập số điện thoại bạn đã dùng khi đặt hàng.", "zh": "请输入您下单时使用的电话号码。", "my": "အော်ဒါတင်စဉ်က သုံးခဲ့သည့် ဖုန်းနံပါတ်ကို ထည့်ပါ။"},
    "track.lookup_phone_label": {"en": "Phone number", "th": "เบอร์โทรศัพท์", "vi": "Số điện thoại", "zh": "电话号码", "my": "ဖုန်းနံပါတ်"},
    "track.lookup_submit": {"en": "Find my orders", "th": "ค้นหาคำสั่งซื้อของฉัน", "vi": "Tìm đơn hàng của tôi", "zh": "查找我的订单", "my": "ကျွန်ုပ်၏အော်ဒါများကို ရှာမည်"},
    "track.lookup_invalid_phone": {"en": "Please enter a valid phone number.", "th": "กรุณากรอกเบอร์โทรศัพท์ที่ถูกต้อง", "vi": "Vui lòng nhập số điện thoại hợp lệ.", "zh": "请输入有效的电话号码。", "my": "မှန်ကန်သော ဖုန်းနံပါတ်ကို ထည့်ပါ။"},
    "track.lookup_no_match": {"en": "We couldn't find any orders with that phone number.", "th": "เราไม่พบคำสั่งซื้อที่ใช้เบอร์โทรนี้", "vi": "Chúng tôi không tìm thấy đơn hàng nào với số điện thoại đó.", "zh": "未找到使用该电话号码的任何订单。", "my": "ထိုဖုန်းနံပါတ်နှင့် အော်ဒါများ မတွေ့ပါ။"},
    "track.lookup_results_title": {"en": "Your orders", "th": "คำสั่งซื้อของคุณ", "vi": "Đơn hàng của bạn", "zh": "您的订单", "my": "သင့်အော်ဒါများ"},
    "track.eta_label": {"en": "Estimated delivery", "th": "วันจัดส่งโดยประมาณ", "vi": "Dự kiến giao hàng", "zh": "预计送达", "my": "ခန့်မှန်းပို့ဆောင်ချိန်"},
    "track.eta_note": {"en": "Estimate only, not a guarantee.", "th": "เป็นเพียงการประมาณการ ไม่ใช่การรับประกัน", "vi": "Chỉ là ước tính, không phải cam kết.", "zh": "仅供参考，非承诺时间。", "my": "ခန့်မှန်းချက်သာဖြစ်ပြီး အာမခံချက်မဟုတ်ပါ။"},
    "track.qr_hint": {"en": "Save or share this code", "th": "บันทึกหรือแชร์รหัสนี้", "vi": "Lưu hoặc chia sẻ mã này", "zh": "保存或分享此二维码", "my": "ဤကုဒ်ကို သိမ်းထားပါ သို့မဟုတ် မျှဝေပါ"},
    "feedback.nav_cta": {"en": "Send feedback", "th": "ส่งความคิดเห็น", "vi": "Gửi phản hồi", "zh": "发送反馈", "my": "အကြံပြုချက်ပေးပို့ပါ"},
    "feedback.title": {"en": "Send feedback", "th": "ส่งความคิดเห็น", "vi": "Gửi phản hồi", "zh": "发送反馈", "my": "အကြံပြုချက်ပေးပို့ပါ"},
    "feedback.subtitle": {"en": "Tell us what's working, what isn't, or what you wish we had — every message gets read.", "th": "บอกเราว่าอะไรดี อะไรไม่ดี หรืออยากให้เรามีอะไรเพิ่ม — ทุกข้อความจะถูกอ่าน", "vi": "Cho chúng tôi biết điều gì đang tốt, điều gì chưa tốt, hoặc điều bạn mong muốn — mọi tin nhắn đều được đọc.", "zh": "告诉我们哪些做得好、哪些不好，或您希望我们提供什么——每条消息我们都会阅读。", "my": "ဘာကောင်းလဲ၊ ဘာမကောင်းလဲ၊ ဘာရှိစေချင်လဲ ပြောပြပါ — မက်ဆေ့ချ်တိုင်းကို ဖတ်ရှုပါသည်။"},
    "feedback.message_label": {"en": "Your feedback", "th": "ความคิดเห็นของคุณ", "vi": "Phản hồi của bạn", "zh": "您的反馈", "my": "သင့်အကြံပြုချက်"},
    "feedback.name_label": {"en": "Name", "th": "ชื่อ", "vi": "Tên", "zh": "姓名", "my": "အမည်"},
    "feedback.contact_label": {"en": "Contact", "th": "ช่องทางติดต่อ", "vi": "Liên hệ", "zh": "联系方式", "my": "ဆက်သွယ်ရန်"},
    "feedback.contact_hint": {"en": "Phone or LINE, in case we'd like to follow up (optional).", "th": "เบอร์โทรหรือไลน์ เผื่อเราอยากติดต่อกลับ (ไม่บังคับ)", "vi": "Số điện thoại hoặc LINE, phòng khi chúng tôi muốn liên hệ lại (không bắt buộc).", "zh": "电话或LINE，以便我们跟进（可选）。", "my": "ဖုန်း သို့မဟုတ် LINE၊ နောက်ဆက်တွဲမေးလိုပါက (မဖြစ်မနေမလိုအပ်)။"},
    "feedback.submit": {"en": "Send feedback", "th": "ส่งความคิดเห็น", "vi": "Gửi phản hồi", "zh": "发送反馈", "my": "အကြံပြုချက်ပေးပို့ပါ"},
    "feedback.sending": {"en": "Sending…", "th": "กำลังส่ง…", "vi": "Đang gửi…", "zh": "发送中…", "my": "ပို့နေသည်…"},
    "feedback.error_required": {"en": "Please write your feedback before sending.", "th": "กรุณาเขียนความคิดเห็นก่อนส่ง", "vi": "Vui lòng viết phản hồi trước khi gửi.", "zh": "发送前请填写您的反馈内容。", "my": "မပို့မီ သင့်အကြံပြုချက်ကို ရေးပါ။"},
    "feedback.thanks_title": {"en": "Thank you!", "th": "ขอบคุณ!", "vi": "Cảm ơn bạn!", "zh": "谢谢！", "my": "ကျေးဇူးတင်ပါသည်!"},
    "feedback.thanks_body": {"en": "Your feedback helps us improve — we read every message.", "th": "ความคิดเห็นของคุณช่วยให้เราพัฒนาได้ดีขึ้น เราอ่านทุกข้อความ", "vi": "Phản hồi của bạn giúp chúng tôi cải thiện — chúng tôi đọc mọi tin nhắn.", "zh": "您的反馈帮助我们不断改进——我们会阅读每一条消息。", "my": "သင့်အကြံပြုချက်သည် တိုးတက်အောင် ကူညီပေးပါသည် — မက်ဆေ့ချ်တိုင်းကို ဖတ်ပါသည်။"},
    "request.title": {"en": "Request an order", "th": "แจ้งความจำนงสั่งซื้อ", "vi": "Yêu cầu đặt hàng", "zh": "提交订购请求", "my": "အော်ဒါတောင်းဆိုမည်"},
    "request.title_submitted": {"en": "Your order request", "th": "คำขอสั่งซื้อของคุณ", "vi": "Yêu cầu đặt hàng của bạn", "zh": "您的订购请求", "my": "သင့်အော်ဒါတောင်းဆိုမှု"},
    "request.subtitle": {"en": "Tell us what you'd like to order and how to reach you — we'll take it from there.", "th": "บอกเราว่าอยากสั่งอะไรและติดต่อคุณได้ทางไหน ที่เหลือเราจัดการเอง", "vi": "Cho chúng tôi biết bạn muốn đặt gì và cách liên hệ với bạn — chúng tôi sẽ xử lý phần còn lại.", "zh": "告诉我们您想订购什么以及如何联系您——接下来交给我们处理。", "my": "ဘာမှာချင်လဲ၊ ဘယ်လိုဆက်သွယ်ရမလဲ ပြောပြပါ — ကျန်တာကို ကျွန်ုပ်တို့ ဆောင်ရွက်ပေးပါမည်။"},
    "request.name": {"en": "Your name", "th": "ชื่อของคุณ", "vi": "Tên của bạn", "zh": "您的姓名", "my": "သင့်အမည်"},
    "request.phone": {"en": "Phone number", "th": "เบอร์โทรศัพท์", "vi": "Số điện thoại", "zh": "电话号码", "my": "ဖုန်းနံပါတ်"},
    "request.address": {"en": "Delivery address", "th": "ที่อยู่จัดส่ง", "vi": "Địa chỉ giao hàng", "zh": "收货地址", "my": "ပို့ဆောင်ရန်လိပ်စာ"},
    "request.address_optional": {"en": "(optional for now)", "th": "(ยังไม่จำเป็นตอนนี้)", "vi": "(hiện chưa bắt buộc)", "zh": "（暂不强制）", "my": "(လောလောဆယ်မလိုအပ်သေးပါ)"},
    "request.item": {"en": "What would you like to order?", "th": "อยากสั่งอะไร?", "vi": "Bạn muốn đặt gì?", "zh": "您想订购什么？", "my": "ဘာမှာလိုပါသလဲ?"},
    "request.link": {"en": "Product link", "th": "ลิงก์สินค้า", "vi": "Liên kết sản phẩm", "zh": "商品链接", "my": "ပစ္စည်းလင့်ခ်"},
    "request.optional": {"en": "(optional)", "th": "(ไม่บังคับ)", "vi": "(không bắt buộc)", "zh": "（可选）", "my": "(မဖြစ်မနေမလို)"},
    "request.budget": {"en": "Your budget (for the item itself — shipping is separate)", "th": "งบประมาณของคุณ (ค่าสินค้าเท่านั้น ไม่รวมค่าส่ง)", "vi": "Ngân sách của bạn (chỉ tính sản phẩm — phí vận chuyển tính riêng)", "zh": "您的预算（仅商品本身——运费另计）", "my": "သင့်ဘတ်ဂျက် (ပစ္စည်းကိုယ်တိုင်အတွက်သာ — ပို့ဆောင်ခ သီးခြား)"},
    "request.budget_placeholder": {"en": "e.g. around ฿2,000 — flexible", "th": "เช่น ประมาณ ๒,๐๐๐ บาท ยืดหยุ่นได้", "vi": "vd: khoảng ฿2,000 — có thể linh hoạt", "zh": "例如：约฿2,000——可灵活调整", "my": "ဥပမာ - ฿2,000 ခန့် — ပြောင်းလွယ်ပြင်လွယ်"},
    "request.budget_hint": {"en": "This is what you're willing to pay for the item itself. Shipping is calculated separately once your item arrives at our warehouse.", "th": "นี่คืองบที่คุณยินดีจ่ายสำหรับตัวสินค้าเท่านั้น ค่าส่งจะคำนวณแยกหลังจากสินค้าถึงคลังของเรา", "vi": "Đây là số tiền bạn sẵn sàng trả cho sản phẩm. Phí vận chuyển sẽ được tính riêng khi hàng đến kho của chúng tôi.", "zh": "这是您愿意为商品本身支付的金额。运费将在商品到达我们仓库后另行计算。", "my": "ဤသည်မှာ ပစ္စည်းအတွက် သင်ပေးချေလိုသည့်ငွေပမာဏဖြစ်သည်။ ပို့ဆောင်ခကို ပစ္စည်းကျွန်ုပ်တို့ကုန်လှောင်ရုံရောက်မှ သီးခြားတွက်ချက်ပါမည်။"},
    "request.delivery_estimates_title": {"en": "Delivery time estimates", "th": "ระยะเวลาจัดส่งโดยประมาณ", "vi": "Ước tính thời gian giao hàng", "zh": "预计送货时间", "my": "ပို့ဆောင်ချိန် ခန့်မှန်းချက်"},
    "request.delivery_road": {"en": "Road (truck): 7–14 days", "th": "ทางรถ: 7–14 วัน", "vi": "Đường bộ (xe tải): 7–14 ngày", "zh": "陆运（卡车）：7–14天", "my": "လမ်းကြောင်း (ကား): ရက် ၇–၁၄ ရက်"},
    "request.delivery_boat": {"en": "Boat (ship): 10–30 days", "th": "ทางเรือ: 10–30 วัน", "vi": "Đường biển (tàu): 10–30 ngày", "zh": "海运（船运）：10–30天", "my": "ရေကြောင်း (သင်္ဘော): ရက် ၁၀–၃၀ ရက်"},
    "request.delivery_estimates_note": {"en": "Estimates only, not a guarantee — see our shipping terms.", "th": "เป็นเพียงการประมาณการ ไม่ใช่การรับประกัน — โปรดดูข้อกำหนดการจัดส่งของเรา", "vi": "Chỉ là ước tính, không phải cam kết — xem điều khoản vận chuyển của chúng tôi.", "zh": "仅为估计，非承诺——详见我们的运输条款。", "my": "ခန့်မှန်းချက်သာဖြစ်ပြီး အာမခံချက်မဟုတ်ပါ — ကျွန်ုပ်တို့၏ပို့ဆောင်မှုစည်းကမ်းများကို ကြည့်ပါ။"},
    "request.reference_image": {"en": "Reference photo", "th": "รูปภาพอ้างอิง", "vi": "Ảnh tham khảo", "zh": "参考图片", "my": "ရည်ညွှန်းဓာတ်ပုံ"},
    "request.reference_image_hint": {"en": "A screenshot or photo of the item helps us find exactly what you mean.", "th": "ภาพหน้าจอหรือรูปสินค้าจะช่วยให้เราหาสินค้าที่คุณต้องการได้ตรงขึ้น", "vi": "Ảnh chụp màn hình hoặc ảnh sản phẩm giúp chúng tôi tìm đúng thứ bạn cần.", "zh": "商品的截图或照片能帮助我们准确找到您想要的商品。", "my": "ပစ္စည်း၏ screenshot သို့မဟုတ် ဓာတ်ပုံသည် သင်ဆိုလိုသည်ကို အတိအကျရှာဖွေရန် ကူညီပေးပါသည်။"},
    "request.agree_terms_prefix": {"en": "I have read and agree to the", "th": "ฉันได้อ่านและยอมรับ", "vi": "Tôi đã đọc và đồng ý với", "zh": "我已阅读并同意", "my": "ကျွန်ုပ် ဖတ်ရှုပြီး သဘောတူပါသည်"},
    "request.agree_terms_link": {"en": "Shipping Terms & Liability Disclaimer", "th": "ข้อกำหนดการจัดส่งและข้อจำกัดความรับผิดชอบ", "vi": "Điều khoản vận chuyển & Miễn trừ trách nhiệm", "zh": "运输条款与责任免责声明", "my": "ပို့ဆောင်ရေးစည်းကမ်းနှင့် တာဝန်ကင်းလွတ်ချက်"},
    "request.agree_terms_required": {"en": "Please confirm you've read and agree to the shipping terms before submitting.", "th": "กรุณายืนยันว่าคุณได้อ่านและยอมรับข้อกำหนดการจัดส่งก่อนส่งคำขอ", "vi": "Vui lòng xác nhận bạn đã đọc và đồng ý với điều khoản vận chuyển trước khi gửi.", "zh": "提交前请确认您已阅读并同意运输条款。", "my": "မပို့မီ ပို့ဆောင်ရေးစည်းကမ်းများကို ဖတ်ရှုသဘောတူကြောင်း အတည်ပြုပါ။"},
    "request.other_contact": {"en": "Other contact", "th": "ช่องทางติดต่ออื่น", "vi": "Liên hệ khác", "zh": "其他联系方式", "my": "အခြားဆက်သွယ်ရန်"},
    "request.other_contact_placeholder": {"en": "LINE: @user, email, or a Facebook link", "th": "LINE: @user, อีเมล หรือลิงก์ Facebook", "vi": "LINE: @user, email, hoặc liên kết Facebook", "zh": "LINE: @user、邮箱或Facebook链接", "my": "LINE: @user၊ အီးမေးလ် သို့မဟုတ် Facebook လင့်ခ်"},
    "request.other_contact_hint": {"en": "In case we need to reach you a different way about pricing.", "th": "เผื่อเราต้องติดต่อคุณช่องทางอื่นเรื่องราคา", "vi": "Phòng khi chúng tôi cần liên hệ bạn theo cách khác về giá.", "zh": "以防我们需要通过其他方式联系您沟通价格。", "my": "ဈေးနှုန်းနှင့်ပတ်သက်၍ တခြားနည်းဖြင့် ဆက်သွယ်ရန်လိုအပ်ပါက။"},
    "request.submit": {"en": "Submit request", "th": "ส่งคำขอ", "vi": "Gửi yêu cầu", "zh": "提交请求", "my": "တောင်းဆိုမှုပို့မည်"},
    "request.submitting": {"en": "Submitting…", "th": "กำลังส่ง…", "vi": "Đang gửi…", "zh": "提交中…", "my": "တင်သွင်းနေသည်…"},
    "request.save": {"en": "Save changes", "th": "บันทึกการแก้ไข", "vi": "Lưu thay đổi", "zh": "保存更改", "my": "ပြောင်းလဲမှုများကို သိမ်းမည်"},
    "request.saving": {"en": "Saving…", "th": "กำลังบันทึก…", "vi": "Đang lưu…", "zh": "保存中…", "my": "သိမ်းနေသည်…"},
    "request.line_cta": {"en": "Message us on LINE", "th": "ทักแชท LINE", "vi": "Nhắn tin cho chúng tôi qua LINE", "zh": "通过LINE给我们发消息", "my": "LINE မှာ မက်ဆေ့ချ်ပို့ပါ"},
    "request.line_hint": {"en": "Send your phone number on LINE so we can text you the moment your quote is ready — no extra code needed.", "th": "ส่งเบอร์โทรของคุณทาง LINE เพื่อให้เราแจ้งเตือนทันทีที่ใบเสนอราคาของคุณพร้อม ไม่ต้องใช้รหัสเพิ่ม", "vi": "Gửi số điện thoại của bạn qua LINE để chúng tôi nhắn tin ngay khi có báo giá — không cần thêm mã.", "zh": "在LINE上发送您的电话号码，我们会在报价准备好的第一时间通知您——无需额外代码。", "my": "LINE မှာ ဖုန်းနံပါတ်ပို့ပါက ဈေးနှုန်းအဆင်သင့်ဖြစ်သည်နှင့် ချက်ချင်းအကြောင်းကြားပေးပါမည် — ကုဒ်ထပ်မလိုပါ။"},
    "request.bookmark": {"en": "Bookmark this page to check back", "th": "บันทึกหน้านี้ไว้เพื่อกลับมาดูภายหลัง", "vi": "Đánh dấu trang này để quay lại kiểm tra", "zh": "请收藏此页面以便日后查看", "my": "ပြန်စစ်ရန် ဤစာမျက်နှာကို Bookmark လုပ်ထားပါ"},
    "request.quote_title": {"en": "Your quote", "th": "ใบเสนอราคาของคุณ", "vi": "Báo giá của bạn", "zh": "您的报价", "my": "သင့်ဈေးနှုန်း"},
    "request.quote_item": {"en": "Item cost", "th": "ค่าสินค้า", "vi": "Giá sản phẩm", "zh": "商品费用", "my": "ပစ္စည်းကုန်ကျစရိတ်"},
    "request.quote_shipping": {"en": "Shipping", "th": "ค่าส่ง", "vi": "Vận chuyển", "zh": "运费", "my": "ပို့ဆောင်ခ"},
    "request.quote_shipping_note": {"en": "Calculated once your item arrives at our warehouse — not included in the budget you gave us.", "th": "คำนวณหลังจากสินค้าถึงคลังของเรา ไม่รวมอยู่ในงบที่คุณแจ้งไว้", "vi": "Được tính khi hàng đến kho của chúng tôi — không nằm trong ngân sách bạn đã cung cấp.", "zh": "在商品到达我们仓库后计算——不包含在您给出的预算内。", "my": "ပစ္စည်း ကုန်လှောင်ရုံရောက်မှ တွက်ချက်ပါမည် — သင်ပေးထားသော ဘတ်ဂျက်တွင် မပါဝင်ပါ။"},
    "request.quote_total": {"en": "Total", "th": "ยอดรวม", "vi": "Tổng cộng", "zh": "总计", "my": "စုစုပေါင်း"},
    "request.quote_budget": {"en": "Your item budget", "th": "งบค่าสินค้าของคุณ", "vi": "Ngân sách sản phẩm của bạn", "zh": "您的商品预算", "my": "သင့်ပစ္စည်းဘတ်ဂျက်"},
    "request.quote_hint": {"en": "We'll message you on LINE to confirm before shipping.", "th": "เราจะทักไลน์เพื่อยืนยันก่อนจัดส่ง", "vi": "Chúng tôi sẽ nhắn tin qua LINE để xác nhận trước khi giao hàng.", "zh": "发货前我们会在LINE上与您确认。", "my": "မပို့မီ LINE မှာ အတည်ပြုရန် မက်ဆေ့ချ်ပို့ပါမည်။"},
    "request.quote_within": {"en": "Item within budget", "th": "ค่าสินค้าอยู่ในงบ", "vi": "Sản phẩm trong ngân sách", "zh": "商品在预算内", "my": "ပစ္စည်းသည် ဘတ်ဂျက်အတွင်း"},
    "request.quote_over": {"en": "Item over budget", "th": "ค่าสินค้าเกินงบ", "vi": "Sản phẩm vượt ngân sách", "zh": "商品超出预算", "my": "ပစ္စည်းသည် ဘတ်ဂျက်ကျော်"},
    "request.quote_awaiting": {"en": "Awaiting quote", "th": "รอแจ้งราคา", "vi": "Đang chờ báo giá", "zh": "等待报价", "my": "ဈေးနှုန်းစောင့်ဆိုင်းဆဲ"},
    "request.not_found_title": {"en": "Request not found", "th": "ไม่พบคำขอ", "vi": "Không tìm thấy yêu cầu", "zh": "未找到请求", "my": "တောင်းဆိုမှုမတွေ့ပါ"},
    "request.not_found_body": {"en": "Please check your link and try again.", "th": "กรุณาตรวจสอบลิงก์แล้วลองใหม่อีกครั้ง", "vi": "Vui lòng kiểm tra liên kết và thử lại.", "zh": "请检查您的链接后重试。", "my": "လင့်ခ်ကို စစ်ဆေးပြီး ပြန်လည်ကြိုးစားပါ။"},
    "request.not_found_cta": {"en": "Submit a new request", "th": "ส่งคำขอใหม่", "vi": "Gửi yêu cầu mới", "zh": "提交新请求", "my": "တောင်းဆိုမှုအသစ်ပို့မည်"},
    "request.error_required": {"en": "This is required.", "th": "กรุณากรอกข้อมูลนี้", "vi": "Trường này là bắt buộc.", "zh": "此项为必填。", "my": "ဤအချက်လိုအပ်ပါသည်။"},
    "request.error_summary": {"en": "Please fix the highlighted fields below.", "th": "กรุณาแก้ไขช่องที่ไฮไลต์ไว้ด้านล่าง", "vi": "Vui lòng sửa các trường được đánh dấu bên dưới.", "zh": "请修正下方标记的字段。", "my": "အောက်တွင် မီးမောင်းထိုးပြထားသော အကွက်များကို ပြင်ပါ။"},
    "request.error_reattach_photo": {"en": "Please reattach your reference photo — it wasn't saved because of the error above.", "th": "กรุณาแนบรูปภาพอ้างอิงอีกครั้ง — รูปเดิมไม่ถูกบันทึกไว้เนื่องจากข้อผิดพลาดด้านบน", "vi": "Vui lòng đính kèm lại ảnh tham khảo — ảnh chưa được lưu do lỗi ở trên.", "zh": "请重新附上参考图片——由于上述错误未能保存。", "my": "ရည်ညွှန်းဓာတ်ပုံကို ပြန်တွဲပါ — အထက်ပါအမှားကြောင့် မသိမ်းဆည်းနိုင်ခဲ့ပါ။"},
    "request.wizard_next": {"en": "Next", "th": "ถัดไป", "vi": "Tiếp theo", "zh": "下一步", "my": "ရှေ့ဆက်ရန်"},
    "request.wizard_back": {"en": "Back", "th": "ย้อนกลับ", "vi": "Quay lại", "zh": "返回", "my": "နောက်သို့"},
    "terms.title": {"en": "Shipping Terms & Liability Disclaimer", "th": "ข้อกำหนดการจัดส่งและข้อจำกัดความรับผิดชอบ", "vi": "Điều khoản vận chuyển & Miễn trừ trách nhiệm", "zh": "运输条款与责任免责声明", "my": "ပို့ဆောင်ရေးစည်းကမ်းနှင့် တာဝန်ကင်းလွတ်ချက်"},
    "terms.updated_label": {"en": "Last updated", "th": "อัปเดตล่าสุด", "vi": "Cập nhật lần cuối", "zh": "最后更新", "my": "နောက်ဆုံးမွမ်းမံသည့်ရက်"},
    "terms.intro": {"en": "Please read this before submitting a request. By checking the agreement box on the request form, you confirm you understand and accept these terms.", "th": "กรุณาอ่านก่อนส่งคำขอ การติ๊กยอมรับในแบบฟอร์มถือว่าคุณเข้าใจและยอมรับข้อกำหนดเหล่านี้", "vi": "Vui lòng đọc điều này trước khi gửi yêu cầu. Khi đánh dấu vào ô đồng ý trên biểu mẫu, bạn xác nhận đã hiểu và chấp nhận các điều khoản này.", "zh": "提交请求前请阅读本条款。在请求表单上勾选同意框，即表示您已理解并接受这些条款。", "my": "တောင်းဆိုမှုမပို့မီ ဤအချက်ကို ဖတ်ရှုပါ။ တောင်းဆိုမှုပုံစံပေါ်ရှိ သဘောတူချက်ဘောက်စ်ကို အမှန်ခြစ်ခြင်းဖြင့် ဤစည်းကမ်းများကို နားလည်လက်ခံကြောင်း အတည်ပြုပါသည်။"},
    "terms.summary_title": {"en": "The short version", "th": "สรุปแบบสั้นๆ", "vi": "Tóm tắt ngắn gọn", "zh": "简要概述", "my": "အကျဉ်းချုပ်"},
    "terms.summary_1": {"en": "We buy and ship on your behalf — we're not the manufacturer or the store.", "th": "เราซื้อและจัดส่งแทนคุณ เราไม่ใช่ผู้ผลิตหรือร้านค้า", "vi": "Chúng tôi mua và vận chuyển thay mặt bạn — chúng tôi không phải nhà sản xuất hay cửa hàng.", "zh": "我们代您购买和运输——我们不是制造商或商店。", "my": "ကျွန်ုပ်တို့သည် သင့်ကိုယ်စား ဝယ်ယူပို့ဆောင်ပေးသူဖြစ်ပြီး၊ ထုတ်လုပ်သူ သို့မဟုတ် ဆိုင်မဟုတ်ပါ။"},
    "terms.summary_2": {"en": "Your budget covers the item only; shipping is priced separately once it reaches our warehouse.", "th": "งบของคุณครอบคลุมแค่ค่าสินค้า ส่วนค่าส่งจะคิดแยกหลังของถึงคลัง", "vi": "Ngân sách của bạn chỉ bao gồm sản phẩm; phí vận chuyển được tính riêng khi hàng đến kho của chúng tôi.", "zh": "您的预算仅包含商品本身；运费将在商品到达我们仓库后另行计算。", "my": "သင့်ဘတ်ဂျက်သည် ပစ္စည်းအတွက်သာဖြစ်ပြီး၊ ပို့ဆောင်ခကို ကုန်လှောင်ရုံရောက်မှ သီးခြားတွက်ချက်ပါမည်။"},
    "terms.summary_3": {"en": "Road takes 7–14 days, boat 10–30 days — real-world estimates, not promises.", "th": "ทางรถ 7–14 วัน ทางเรือ 10–30 วัน เป็นการประมาณการตามจริง ไม่ใช่คำสัญญา", "vi": "Đường bộ mất 7–14 ngày, đường biển 10–30 ngày — đây là ước tính thực tế, không phải cam kết.", "zh": "陆运需7–14天，海运需10–30天——为实际估计，非承诺。", "my": "လမ်းကြောင်းက ရက် ၇–၁၄ ရက်၊ ရေကြောင်းက ရက် ၁၀–၃၀ ရက် — လက်တွေ့ခန့်မှန်းချက်ဖြစ်ပြီး ကတိမဟုတ်ပါ။"},
    "terms.summary_4": {"en": "Item quality or damage disputes go to the original seller, not us.", "th": "ปัญหาคุณภาพหรือความเสียหายของสินค้าต้องติดต่อผู้ขายต้นทาง ไม่ใช่เรา", "vi": "Tranh chấp về chất lượng hoặc hư hỏng sản phẩm thuộc trách nhiệm của người bán gốc, không phải chúng tôi.", "zh": "商品质量或损坏纠纷应联系原卖家，与我们无关。", "my": "ပစ္စည်းအရည်အသွေး သို့မဟုတ် ပျက်စီးမှုဆိုင်ရာ အငြင်းပွားမှုများသည် မူလရောင်းချသူထံသို့သာ သက်ဆိုင်ပြီး ကျွန်ုပ်တို့နှင့်မသက်ဆိုင်ပါ။"},
    "terms.role_title": {"en": "SINEX is a forwarding agent, not the seller", "th": "SINEX เป็นตัวแทนรับส่งพัสดุ ไม่ใช่ผู้ขาย", "vi": "SINEX là đơn vị vận chuyển trung gian, không phải người bán", "zh": "SINEX是转运代理，非卖家", "my": "SINEX သည် ပို့ဆောင်ရေးကိုယ်စားလှယ်ဖြစ်ပြီး ရောင်းချသူမဟုတ်ပါ"},
    "terms.role_body": {"en": "We purchase and/or forward items on your behalf from third-party sellers in China. We are an intermediary — we do not manufacture, own, or guarantee the items themselves.", "th": "เราซื้อและ/หรือส่งต่อสินค้าให้คุณจากผู้ขายบุคคลที่สามในประเทศจีน เราเป็นตัวกลาง ไม่ได้เป็นผู้ผลิต เจ้าของ หรือผู้รับประกันตัวสินค้า", "vi": "Chúng tôi mua và/hoặc chuyển tiếp sản phẩm thay mặt bạn từ các người bán bên thứ ba tại Trung Quốc. Chúng tôi là bên trung gian — không sản xuất, sở hữu hay bảo đảm cho sản phẩm.", "zh": "我们代您从中国第三方卖家处购买和/或转运商品。我们是中间方——不生产、不拥有、也不保证商品本身。", "my": "ကျွန်ုပ်တို့သည် တရုတ်နိုင်ငံရှိ တတိယပါတီရောင်းချသူများထံမှ သင့်ကိုယ်စား ဝယ်ယူ/ပို့ဆောင်ပေးပါသည်။ ကျွန်ုပ်တို့သည် အလယ်အလတ်ကိုယ်စားလှယ်ဖြစ်ပြီး ပစ္စည်းများကို ထုတ်လုပ်ခြင်း၊ ပိုင်ဆိုင်ခြင်း သို့မဟုတ် အာမခံခြင်း မပြုလုပ်ပါ။"},
    "terms.liability_title": {"en": "No liability for item condition, damage, or quality", "th": "ไม่รับผิดชอบต่อสภาพ ความเสียหาย หรือคุณภาพของสินค้า", "vi": "Không chịu trách nhiệm về tình trạng, hư hỏng hoặc chất lượng sản phẩm", "zh": "对商品状况、损坏或质量概不负责", "my": "ပစ္စည်းအခြေအနေ၊ ပျက်စီးမှု သို့မဟုတ် အရည်အသွေးအတွက် တာဝန်မယူပါ"},
    "terms.liability_body": {"en": "Disputes about an item's quality, authenticity, or damage from manufacturing are between you and the original seller — not SINEX. We take reasonable care in handling, but we do not refund or compensate for item defects or damage that occurred before or during the seller's own shipping to our warehouse.", "th": "ข้อพิพาทเกี่ยวกับคุณภาพ ความแท้ หรือความเสียหายจากการผลิตของสินค้า เป็นเรื่องระหว่างคุณกับผู้ขายต้นทาง ไม่ใช่ SINEX เราดูแลสินค้าด้วยความระมัดระวังตามสมควร แต่จะไม่คืนเงินหรือชดเชยความเสียหายที่เกิดขึ้นก่อนหรือระหว่างการจัดส่งของผู้ขายมายังคลังของเรา", "vi": "Tranh chấp về chất lượng, tính xác thực hoặc hư hỏng do sản xuất là giữa bạn và người bán gốc — không phải SINEX. Chúng tôi xử lý hàng hóa cẩn thận hợp lý, nhưng không hoàn tiền hoặc bồi thường cho lỗi sản phẩm hoặc hư hỏng xảy ra trước hoặc trong quá trình người bán vận chuyển đến kho của chúng tôi.", "zh": "关于商品质量、真伪或生产损坏的纠纷属于您与原卖家之间的事务——与SINEX无关。我们在处理商品时会尽合理注意义务，但对于卖家运送至我们仓库之前或期间发生的商品缺陷或损坏，我们不予退款或赔偿。", "my": "ပစ္စည်းအရည်အသွေး၊ စစ်မှန်မှု သို့မဟုတ် ထုတ်လုပ်မှုကြောင့်ပျက်စီးမှုဆိုင်ရာ အငြင်းပွားမှုများသည် သင်နှင့်မူလရောင်းချသူကြား ဖြစ်ပြီး SINEX နှင့်မသက်ဆိုင်ပါ။ ကျွန်ုပ်တို့ သင့်လျော်စွာ ဂရုစိုက်ကိုင်တွယ်သော်လည်း၊ ရောင်းချသူ၏ ကျွန်ုပ်တို့ကုန်လှောင်ရုံသို့ ပို့ဆောင်ခြင်းမပြုမီ သို့မဟုတ် ပြုလုပ်နေစဉ်အတွင်း ဖြစ်ပေါ်သော ချို့ယွင်းမှု သို့မဟုတ် ပျက်စီးမှုအတွက် ငွေပြန်အမ်း သို့မဟုတ် လျော်ကြေးမပေးပါ။"},
    "terms.delay_title": {"en": "Delivery estimates are not guarantees", "th": "ระยะเวลาจัดส่งเป็นเพียงการประมาณการ ไม่ใช่การรับประกัน", "vi": "Ước tính giao hàng không phải là cam kết", "zh": "送货时间为估计，非承诺", "my": "ပို့ဆောင်ချိန်ခန့်မှန်းချက်များသည် အာမခံချက်မဟုတ်ပါ"},
    "terms.delay_body": {"en": "Typical delivery times are road (truck): 7–14 days, boat (ship): 10–30 days, counted from when your item leaves the China warehouse. These are estimates based on normal conditions — customs, weather, holidays, and carrier delays can extend them. A delay past these estimates is not, on its own, grounds for a refund or compensation from SINEX.", "th": "ระยะเวลาจัดส่งโดยทั่วไป ทางรถ 7–14 วัน ทางเรือ 10–30 วัน นับจากสินค้าออกจากคลังจีน เป็นเพียงการประมาณการภายใต้สภาวะปกติ ศุลกากร สภาพอากาศ วันหยุด และความล่าช้าของผู้ขนส่งอาจทำให้ใช้เวลานานกว่านี้ ความล่าช้าเกินกว่าที่ประมาณการไว้เพียงอย่างเดียว ไม่ถือเป็นเหตุให้ต้องคืนเงินหรือชดเชยจาก SINEX", "vi": "Thời gian giao hàng thông thường là đường bộ (xe tải): 7–14 ngày, đường biển (tàu): 10–30 ngày, tính từ khi hàng rời kho Trung Quốc. Đây là ước tính dựa trên điều kiện bình thường — hải quan, thời tiết, ngày lễ và trì hoãn của đơn vị vận chuyển có thể kéo dài thời gian này. Việc trễ hơn so với ước tính không tự nó là căn cứ để yêu cầu hoàn tiền hoặc bồi thường từ SINEX.", "zh": "一般送货时间为：陆运（卡车）7–14天，海运（船运）10–30天，自商品离开中国仓库起计算。此为正常情况下的估计——海关、天气、假期及承运商延误均可能延长此时间。仅因超出该估计时间而延迟，本身不构成向SINEX要求退款或赔偿的理由。", "my": "ပုံမှန်ပို့ဆောင်ချိန်များမှာ လမ်းကြောင်း (ကား): ရက် ၇–၁၄ ရက်၊ ရေကြောင်း (သင်္ဘော): ရက် ၁၀–၃၀ ရက်ဖြစ်ပြီး၊ ပစ္စည်းတရုတ်ကုန်လှောင်ရုံမှ ထွက်ခွာချိန်မှ တွက်ချက်သည်။ ဤသည်မှာ ပုံမှန်အခြေအနေများအောက်တွင် ခန့်မှန်းချက်များဖြစ်ပြီး — အကောက်ခွန်၊ မိုးလေဝသ၊ အားလပ်ရက်များနှင့် သယ်ယူပို့ဆောင်ရေးကြန့်ကြာမှုများက ကြာချိန်ကို တိုးနိုင်ပါသည်။ ဤခန့်မှန်းချက်များထက် နောက်ကျခြင်းသည် ၎င်းတစ်ခုတည်းဖြင့် SINEX ထံမှ ငွေပြန်အမ်းခြင်း သို့မဟုတ် လျော်ကြေးတောင်းခံရန် အကြောင်းပြချက်မဟုတ်ပါ။"},
    "terms.pricing_title": {"en": "Budget & pricing", "th": "งบประมาณและราคา", "vi": "Ngân sách & giá cả", "zh": "预算与定价", "my": "ဘတ်ဂျက်နှင့် ဈေးနှုန်း"},
    "terms.pricing_body": {"en": "The budget you give us on the request form covers the item's purchase price only. Shipping cost is calculated separately once your item is received and measured/weighed at our warehouse, and is not included in that budget figure.", "th": "งบประมาณที่คุณแจ้งในแบบฟอร์มครอบคลุมเฉพาะราคาสินค้าเท่านั้น ค่าจัดส่งจะคำนวณแยกต่างหากหลังจากสินค้าถึงคลังและมีการชั่ง/วัดขนาดแล้ว ไม่รวมอยู่ในตัวเลขงบประมาณดังกล่าว", "vi": "Ngân sách bạn cung cấp trên biểu mẫu chỉ bao gồm giá mua sản phẩm. Phí vận chuyển được tính riêng khi hàng được nhận và đo/cân tại kho của chúng tôi, không nằm trong con số ngân sách đó.", "zh": "您在请求表单中提供的预算仅涵盖商品的购买价格。运费将在商品到达我们仓库并完成测量/称重后另行计算，不包含在该预算数字内。", "my": "တောင်းဆိုမှုပုံစံတွင် သင်ပေးထားသော ဘတ်ဂျက်သည် ပစ္စည်း၀ယ်ယူစရိတ်ကိုသာ ဖော်ပြသည်။ ပို့ဆောင်ခကို ပစ္စည်း ကျွန်ုပ်တို့ကုန်လှောင်ရုံတွင် လက်ခံရရှိပြီး အလေးချိန်/အရွယ်အစားတိုင်းတာပြီးမှ သီးခြားတွက်ချက်ပြီး ထိုဘတ်ဂျက်ကိန်းတွင် မပါဝင်ပါ။"},
    "terms.payment_title": {"en": "Payment", "th": "การชำระเงิน", "vi": "Thanh toán", "zh": "付款", "my": "ငွေပေးချေမှု"},
    "terms.payment_body": {"en": "Payment is arranged directly with us over LINE (in Thai baht) once your quote is confirmed — this website does not collect any card or bank details.", "th": "การชำระเงินจะตกลงกันโดยตรงทาง LINE (เป็นเงินบาท) หลังยืนยันราคาแล้ว เว็บไซต์นี้ไม่มีการเก็บข้อมูลบัตรหรือบัญชีธนาคารใดๆ", "vi": "Thanh toán được sắp xếp trực tiếp với chúng tôi qua LINE (bằng baht Thái) sau khi báo giá được xác nhận — trang web này không thu thập thông tin thẻ hoặc ngân hàng.", "zh": "报价确认后，付款将通过LINE与我们直接安排（以泰铢结算）——本网站不收集任何银行卡或账户信息。", "my": "ဈေးနှုန်းအတည်ပြုပြီးနောက် LINE မှတစ်ဆင့် ကျွန်ုပ်တို့နှင့် တိုက်ရိုက်ငွေပေးချေမှု စီစဉ်ပါသည် (ထိုင်းဘတ်ဖြင့်) — ဤဝက်ဘ်ဆိုက်သည် ကတ် သို့မဟုတ် ဘဏ်အချက်အလက်များကို မသိမ်းဆည်းပါ။"},
    "terms.cancel_title": {"en": "Cancellation & refusal", "th": "การยกเลิกและการปฏิเสธคำขอ", "vi": "Hủy đơn & từ chối", "zh": "取消与拒绝", "my": "ပယ်ဖျက်ခြင်းနှင့် ငြင်းပယ်ခြင်း"},
    "terms.cancel_body": {"en": "We may decline to fulfil a request (e.g. a restricted or unavailable item) before purchase without penalty to either side. Once we've paid the seller on your behalf, that payment is generally non-refundable by SINEX, consistent with the seller's own terms.", "th": "เราอาจปฏิเสธคำขอได้ (เช่น สินค้าต้องห้ามหรือไม่มีจำหน่าย) ก่อนการซื้อ โดยไม่มีผลเสียต่อทั้งสองฝ่าย เมื่อเราชำระเงินให้ผู้ขายแทนคุณแล้ว การชำระเงินนั้นโดยทั่วไปจะไม่สามารถขอคืนจาก SINEX ได้ ตามเงื่อนไขของผู้ขายเอง", "vi": "Chúng tôi có thể từ chối thực hiện yêu cầu (ví dụ: sản phẩm bị hạn chế hoặc không có sẵn) trước khi mua mà không bị phạt cho cả hai bên. Sau khi chúng tôi đã thanh toán cho người bán thay mặt bạn, khoản thanh toán đó thường không được SINEX hoàn lại, phù hợp với điều khoản của chính người bán.", "zh": "在购买前，我们可能拒绝执行某项请求（例如受限或缺货商品），双方均无需承担违约责任。一旦我们代您向卖家付款，该笔款项通常SINEX将不予退还，此与卖家自身条款一致。", "my": "ကျွန်ုပ်တို့သည် ဝယ်ယူမှုမပြုမီ တောင်းဆိုမှုတစ်ခုကို ပယ်ချနိုင်ပါသည် (ဥပမာ - ကန့်သတ်ထားသော သို့မဟုတ် မရရှိနိုင်သောပစ္စည်း) ဘက်နှစ်ဘက်စလုံးအတွက် ဒဏ်ကြေးမရှိပါ။ သင့်ကိုယ်စား ရောင်းချသူထံ ငွေပေးချေပြီးသည်နှင့် ထိုငွေသည် ရောင်းချသူ၏ကိုယ်ပိုင်စည်းကမ်းများနှင့်အညီ ပုံမှန်အားဖြင့် SINEX မှ ပြန်အမ်းမည်မဟုတ်ပါ။"},
    "terms.privacy_title": {"en": "Your information", "th": "ข้อมูลของคุณ", "vi": "Thông tin của bạn", "zh": "您的信息", "my": "သင့်အချက်အလက်"},
    "terms.privacy_body": {"en": "We collect your name, phone number, address, and (optionally) a reference photo solely to process your request and communicate with you over LINE. We don't sell or share it with third parties beyond what's needed to ship your item.", "th": "เราเก็บชื่อ เบอร์โทร ที่อยู่ และรูปภาพอ้างอิง (ถ้ามี) เพื่อดำเนินการตามคำขอและติดต่อคุณทาง LINE เท่านั้น เราไม่ขายหรือแชร์ข้อมูลของคุณกับบุคคลที่สาม เว้นแต่จำเป็นต่อการจัดส่งสินค้า", "vi": "Chúng tôi thu thập tên, số điện thoại, địa chỉ và (tùy chọn) ảnh tham khảo của bạn chỉ để xử lý yêu cầu và liên lạc với bạn qua LINE. Chúng tôi không bán hoặc chia sẻ thông tin này với bên thứ ba ngoài phạm vi cần thiết để giao hàng.", "zh": "我们收集您的姓名、电话号码、地址以及（可选的）参考图片，仅用于处理您的请求并通过LINE与您沟通。除运送商品所需外，我们不会出售或与第三方共享这些信息。", "my": "ကျွန်ုပ်တို့သည် သင့်အမည်၊ ဖုန်းနံပါတ်၊ လိပ်စာနှင့် (ရွေးချယ်နိုင်သော) ရည်ညွှန်းဓာတ်ပုံကို သင့်တောင်းဆိုမှုကို လုပ်ဆောင်ရန်နှင့် LINE မှတစ်ဆင့် ဆက်သွယ်ရန်အတွက်သာ စုဆောင်းပါသည်။ ပစ္စည်းပို့ဆောင်ရန်လိုအပ်သည်အပြင် တတိယပါတီများထံ ရောင်းချခြင်း သို့မဟုတ် မျှဝေခြင်း မပြုလုပ်ပါ။"},
    "terms.law_title": {"en": "Governing terms", "th": "กฎหมายที่ใช้บังคับ", "vi": "Luật điều chỉnh", "zh": "适用法律", "my": "အုပ်ချုပ်သည့်စည်းကမ်းများ"},
    "terms.law_body": {"en": "These terms are intended to be interpreted under the laws of Thailand. If any part is found unenforceable, the rest still stands.", "th": "ข้อกำหนดนี้มีเจตนาให้ตีความตามกฎหมายไทย หากข้อใดไม่สามารถบังคับใช้ได้ ข้อกำหนดส่วนที่เหลือยังมีผลบังคับใช้ต่อไป", "vi": "Các điều khoản này được diễn giải theo luật pháp Thái Lan. Nếu bất kỳ phần nào không thể thực thi, phần còn lại vẫn có hiệu lực.", "zh": "本条款旨在依照泰国法律解释。如任何部分被认定无法执行，其余部分仍然有效。", "my": "ဤစည်းကမ်းများကို ထိုင်းနိုင်ငံဥပဒေများအောက်တွင် အဓိပ္ပာယ်ဖွင့်ဆိုရန် ရည်ရွယ်ပါသည်။ တစ်စိတ်တစ်ပိုင်းသည် အတည်မပြုနိုင်ပါက ကျန်အပိုင်းများ ဆက်လက်တည်မြဲပါသည်။"},
    "terms.contact_title": {"en": "Questions", "th": "หากมีคำถาม", "vi": "Câu hỏi", "zh": "问题咨询", "my": "မေးခွန်းများ"},
    "terms.contact_body": {"en": "Message us on LINE any time before confirming an order if anything here is unclear.", "th": "ทักแชท LINE หาเราได้ทุกเมื่อก่อนยืนยันคำสั่งซื้อ หากมีข้อสงสัย", "vi": "Nếu có điều gì chưa rõ, hãy nhắn tin cho chúng tôi qua LINE bất cứ lúc nào trước khi xác nhận đơn hàng.", "zh": "如有任何不清楚之处，请在确认订单前随时通过LINE联系我们。", "my": "ဤနေရာတွင် တစ်စုံတစ်ခုမရှင်းလင်းပါက အော်ဒါမအတည်ပြုမီ LINE မှာ မည်သည့်အချိန်မဆို ဆက်သွယ်နိုင်ပါသည်။"},
    "terms.back": {"en": "Back to request form", "th": "กลับไปหน้าแจ้งความจำนงสั่งซื้อ", "vi": "Quay lại biểu mẫu yêu cầu", "zh": "返回请求表单", "my": "တောင်းဆိုမှုပုံစံသို့ ပြန်သွားမည်"},
    "request.step_details": {"en": "Your details", "th": "ข้อมูลของคุณ", "vi": "Thông tin của bạn", "zh": "您的详细信息", "my": "သင့်အသေးစိတ်အချက်အလက်"},
    "request.step_photo": {"en": "Photo", "th": "รูปภาพ", "vi": "Ảnh", "zh": "照片", "my": "ဓာတ်ပုံ"},
    "request.step_budget": {"en": "Budget", "th": "งบประมาณ", "vi": "Ngân sách", "zh": "预算", "my": "ဘတ်ဂျက်"},
    "request.dropzone_hint": {"en": "Drag a photo here, or click to browse", "th": "ลากรูปมาวางที่นี่ หรือคลิกเพื่อเลือกไฟล์", "vi": "Kéo ảnh vào đây, hoặc nhấp để chọn", "zh": "将照片拖到此处，或点击浏览", "my": "ဓာတ်ပုံကို ဤနေရာသို့ဆွဲထည့်ပါ သို့မဟုတ် ကလစ်နှိပ်၍ ရွေးပါ"},
    "error.413_title": {"en": "That file is too large", "th": "ไฟล์มีขนาดใหญ่เกินไป", "vi": "Tệp quá lớn", "zh": "文件过大", "my": "ဖိုင်ကြီးလွန်းပါသည်"},
    "error.413_body": {"en": "Photos must be under 5 MB. Please go back and choose a smaller image.", "th": "รูปภาพต้องมีขนาดไม่เกิน 5 MB กรุณาย้อนกลับแล้วเลือกไฟล์ที่เล็กลง", "vi": "Ảnh phải dưới 5 MB. Vui lòng quay lại và chọn ảnh nhỏ hơn.", "zh": "照片必须小于5MB。请返回并选择较小的图片。", "my": "ဓာတ်ပုံများသည် 5 MB အောက်ဖြစ်ရမည်။ ပြန်သွားပြီး ပိုငယ်သောပုံကို ရွေးပါ။"},
    "error.csrf_title": {"en": "That page had expired", "th": "หน้านี้หมดอายุแล้ว", "vi": "Trang đó đã hết hạn", "zh": "该页面已过期", "my": "ထိုစာမျက်နှာ သက်တမ်းကုန်သွားပါပြီ"},
    "error.csrf_body": {"en": "Your session timed out or the form was submitted twice. Please go back and try again.", "th": "เซสชันของคุณหมดอายุ หรือมีการส่งฟอร์มซ้ำ กรุณาย้อนกลับแล้วลองใหม่อีกครั้ง", "vi": "Phiên làm việc của bạn đã hết hạn hoặc biểu mẫu đã được gửi hai lần. Vui lòng quay lại và thử lại.", "zh": "您的会话已超时，或表单被重复提交。请返回后重试。", "my": "သင့်ဆက်ရှင် သက်တမ်းကုန်သွားပါသည် သို့မဟုတ် ပုံစံကို နှစ်ကြိမ်တင်သွင်းခဲ့ပါသည်။ ပြန်သွားပြီး ထပ်ကြိုးစားပါ။"},
    "error.500_title": {"en": "Something went wrong on our end", "th": "เกิดข้อผิดพลาดจากทางเรา", "vi": "Đã có lỗi xảy ra từ phía chúng tôi", "zh": "我们这边出了点问题", "my": "ကျွန်ုပ်တို့ဘက်တွင် တစ်ခုခုမှားယွင်းသွားပါသည်"},
    "error.500_body": {"en": "Please try again in a moment. If it keeps happening, message us on LINE and we'll sort it out.", "th": "กรุณาลองใหม่อีกครั้งในอีกสักครู่ หากยังพบปัญหาอยู่ ทักแชท LINE หาเราได้เลย", "vi": "Vui lòng thử lại sau ít phút. Nếu vẫn tiếp diễn, hãy nhắn tin cho chúng tôi qua LINE để được hỗ trợ.", "zh": "请稍后再试。如果问题持续出现，请通过LINE联系我们，我们会为您处理。", "my": "ခဏနေ ထပ်ကြိုးစားပါ။ ဆက်ဖြစ်နေပါက LINE မှာ ဆက်သွယ်ပါ ကျွန်ုပ်တို့ ဖြေရှင်းပေးပါမည်။"},
    "error.go_back": {"en": "Go back", "th": "ย้อนกลับ", "vi": "Quay lại", "zh": "返回", "my": "နောက်သို့ပြန်သွားမည်"},
}


def t(key):
    return TRANSLATIONS.get(key, {}).get(g_locale_or_en(), TRANSLATIONS.get(key, {}).get("en", key))


def g_locale_or_en():
    locale = request.cookies.get(LOCALE_COOKIE, "en")
    return locale if locale in VALID_LOCALES else "en"


app.jinja_env.globals["t"] = t


def _budget_number(text):
    """First number found in a free-text budget string ('~฿1,500' -> 1500.0),
    or None when nothing parses -- the customer's budget is intentionally
    free-text, so this is a best-effort read, not a hard requirement."""
    if not text:
        return None
    match = re.search(r"[\d,]+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def quote_status(item_cost, budget_text):
    """'awaiting' (no item price yet), 'within_budget', 'over_budget', or
    'quoted' (priced, but the budget text didn't contain a comparable
    number -- e.g. "flexible" -- so the admin's own judgement stands).

    Compares ONLY the item cost against the customer's stated budget --
    the budget is explicitly "for the item itself" (see TRANSLATIONS'
    request.budget/quote_shipping_note copy); shipping is quoted
    separately once the item arrives at the warehouse and is never folded
    into this comparison."""
    if item_cost is None:
        return "awaiting"
    budget_num = _budget_number(budget_text)
    if budget_num is None:
        return "quoted"
    return "within_budget" if item_cost <= budget_num else "over_budget"


app.jinja_env.globals["quote_status"] = quote_status
app.jinja_env.globals["budget_number"] = _budget_number

# Same day ranges already quoted to customers on /request and /terms --
# reused here so /track can restate the estimate against the order's own
# actual dates instead of only ever showing the generic range.
DELIVERY_ESTIMATE_DAYS = {"รถ": (7, 14), "เรือ": (10, 30)}


def estimated_delivery(order):
    """{'low': ..., 'high': ...} formatted dates for an order's estimated
    delivery window, or None when the mode isn't recognized or the order has
    no creation date. Caller decides whether it still makes sense to show
    (e.g. skip once the order is already delivered)."""
    days = DELIVERY_ESTIMATE_DAYS.get(order["tracking_mode"])
    if not days or not order["created_at"]:
        return None
    created = datetime.fromisoformat(order["created_at"])
    return {
        "low": (created + timedelta(days=days[0])).strftime("%d %b"),
        "high": (created + timedelta(days=days[1])).strftime("%d %b"),
    }


app.jinja_env.globals["estimated_delivery"] = estimated_delivery


def translate_url(text):
    """Genuinely-free Google Translate deep link (no API key/cost) for the
    admin to read a Thai/mixed-language message -- separate from the paid
    OpenRouter path used for the bot's own bilingual replies."""
    from urllib.parse import quote as urlquote
    return f"https://translate.google.com/?sl=auto&tl=en&text={urlquote(text or '')}&op=translate"


app.jinja_env.globals["translate_url"] = translate_url


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


def _has_real_transparency(img):
    """True only when the image actually USES transparency (a visible
    transparent/semi-transparent pixel), not just when it happens to carry
    an alpha channel -- a phone photo re-saved as PNG is RGB in substance,
    and keeping it as PNG for that reason alone produces a multi-MB file
    for no visual benefit (a real 1920x1440 photo measured ~2.4MB as PNG
    vs. ~200KB as JPEG at the same quality)."""
    if img.mode == "P":
        return "transparency" in img.info
    if img.mode in ("RGBA", "LA"):
        return img.getchannel("A").getextrema()[0] < 255
    return False


def _compress_and_save(fileobj, base_path_no_ext, ext):
    """Resize+re-encode an uploaded/fetched image and write both a full
    (<=IMAGE_MAX_DIM px) and a thumbnail (<=THUMB_MAX_DIM px) version to disk,
    so list/carousel views never load a full-size photo. Raises on a genuinely
    unreadable file -- caller decides how to surface that to the user.

    Returns the extension actually used to save the files, which may differ
    from the `ext` passed in: any upload without real transparency (the
    overwhelming common case -- product photos, screenshots) is always
    stored as JPEG regardless of its original format. This keeps files
    small and, critically, keeps every order's image within the JPEG/PNG
    set LINE's Flex "image" component can actually render -- a .webp
    upload used to silently lose its LINE carousel photo entirely."""
    img = ImageOps.exif_transpose(Image.open(fileobj))  # respect phone camera orientation
    if not _has_real_transparency(img):
        ext = "jpg"
    if ext == "jpg" and img.mode != "RGB":
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
    return ext


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
def landing():
    """Customer-facing front door -- trust/marketing page with a CTA into
    /request. The admin dashboard now lives at /admin (redirect below); it
    used to be what '/' did, back when this app had no public front door."""
    return render_template("landing.html")


@app.route("/admin")
def admin_index():
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


def _find_or_create_customer(name, phone, address, other_contact=None):
    """Repeat customers (matched by normalized phone) get attached to their
    existing customer_id instead of a fresh row, so order history / LINE
    linking / "my orders" / reorder-without-re-entering-info all see one
    identity. A blank phone always creates a new customer (nothing to match
    against). Shared by new_order() (admin), the public /request intake
    (Phase 2.5), and order_detail()'s customer-info edit, so every intake
    path dedupes identically.

    A blank address/other_contact on THIS call never overwrites a
    previously-stored value -- only a non-blank value replaces what's
    there. (Previously any blank silently nulled out a real address; the
    LINE "NEW ORDER" reorder flow never re-asks for it, so a repeat
    customer reordering by chat would have wiped their own address.)"""
    db = get_db()
    now = datetime.utcnow().isoformat()

    phone_norm = _normalize_phone(phone)
    if phone_norm:
        existing = db.execute(
            "SELECT id, address, other_contact FROM customers WHERE phone_normalized = ?", (phone_norm,)
        ).fetchone()
        if existing:
            new_address = address.strip() if address and address.strip() else existing["address"]
            new_contact = other_contact.strip() if other_contact and other_contact.strip() else existing["other_contact"]
            db.execute(
                "UPDATE customers SET name = ?, address = ?, other_contact = ? WHERE id = ?",
                (name, new_address, new_contact, existing["id"]),
            )
            return existing["id"]

    cur = db.execute(
        "INSERT INTO customers (name, phone, phone_normalized, address, other_contact, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (name, phone, phone_norm, address, other_contact, now),
    )
    return cur.lastrowid


def _customer_line_id(customer_id):
    """line_user_id for a customer, or None if they're not linked (or don't
    exist) -- shared by every place that needs to know whether a proactive
    push is even possible before trying one."""
    row = get_db().execute("SELECT line_user_id FROM customers WHERE id = ?", (customer_id,)).fetchone()
    return row["line_user_id"] if row else None


def _record_info_source(customer_id, raw_text, extracted_phone):
    """Records what a "paste customer info" auto-extraction (parse_customer())
    actually produced, so an admin can review/correct it later from the
    order screen if the AI got something wrong. Overwrites -- this is the
    latest source only, not a full history, proportionate to "let me check
    and fix it" rather than a compliance audit trail. No-op on a blank
    raw_text (nothing was actually pasted this time)."""
    raw_text = (raw_text or "").strip()
    if not raw_text:
        return
    snippet = raw_text if len(raw_text) <= 300 else raw_text[:300] + "…"
    note = f"Pasted {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC: \"{snippet}\" → phone: {extracted_phone or '(none found)'}"
    get_db().execute("UPDATE customers SET info_source_note = ? WHERE id = ?", (note, customer_id))


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
    _record_info_source(customer_id, request.form.get("source_blob", ""), phone)

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
    line_id = _customer_line_id(customer_id)
    if line_id:
        track_url = f"{_public_base_url()}/track/{link_code}"
        line_push(
            line_id,
            f"Your order has been created! Track it here: {track_url}\n"
            f"สั่งซื้อของคุณถูกสร้างแล้ว! ติดตามได้ที่ลิงก์ด้านบน",
        )
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
        "customers.address AS customer_address, customers.other_contact AS customer_other_contact, "
        "customers.info_source_note AS customer_info_source_note, "
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

        # Customer info correction -- a separate small form/action so it has
        # its own validation and never gets entangled with the big order-info
        # save below. Always edits THIS order's own customer row directly (by
        # customer_id, same pattern already used in view_request()'s edit
        # branch) -- deliberately NOT routed through _find_or_create_customer()'s
        # phone-matching, which is for deciding whether a NEW submission
        # belongs to an existing customer, not for correcting one you already
        # know. A blank address/other_contact here never erases a
        # previously-good value (same non-destructive-update fix as Bug B).
        if request.form.get("action") == "edit_customer":
            cust_name = request.form.get("cust_name", "").strip()
            cust_phone = request.form.get("cust_phone", "").strip()
            cust_address = request.form.get("cust_address", "").strip()
            cust_other_contact = request.form.get("cust_other_contact", "").strip()
            if not cust_name or not cust_phone:
                flash("Customer name and phone are required.")
                return redirect(url_for("order_detail", order_id=order_id))
            new_address = cust_address if cust_address else (order["customer_address"] or None)
            new_contact = cust_other_contact if cust_other_contact else (order["customer_other_contact"] or None)
            db.execute(
                "UPDATE customers SET name = ?, phone = ?, phone_normalized = ?, address = ?, other_contact = ? WHERE id = ?",
                (cust_name, cust_phone, _normalize_phone(cust_phone), new_address, new_contact, order["customer_id"]),
            )
            _record_info_source(order["customer_id"], request.form.get("cust_source_blob", ""), cust_phone)
            db.commit()
            flash("Customer info updated.")
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
                saved_ext = _compress_and_save(file, os.path.join(UPLOAD_DIR, order["link_code"]), ext)
            except Exception as e:
                logger.error("image compression failed: %s", e)
                flash("Couldn't process that image — try a different file.")
                return redirect(url_for("order_detail", order_id=order_id))
            item_image = f"uploads/{order['link_code']}.{saved_ext}"

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
            saved_ext = _compress_and_save(io.BytesIO(resp.content), os.path.join(UPLOAD_DIR, order["link_code"]), ext)
        except Exception as e:
            logger.error("image compression failed (fetch): %s", e)
            flash("Fetched image but couldn't process it — paste or drag one instead.")
            return redirect(url_for("order_detail", order_id=order_id))
        db.execute(
            "UPDATE orders SET item_image = ? WHERE id = ?",
            (f"uploads/{order['link_code']}.{saved_ext}", order_id),
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
        # Carry over what the request already had -- the customer's uploaded
        # reference photo and the quote the admin already priced -- instead
        # of silently dropping them and making the admin redo the work.
        # item_image: no file move needed, _save_request_reference_image()
        # already wrote both the full and _thumb files under the req_<code>
        # name via _compress_and_save(), and item_image_thumb() resolves
        # correctly regardless of filename prefix. shipping_cost lands in
        # house_ship_fee (the request only ever collects one combined
        # shipping figure; china_ship_fee is left for the admin to split out
        # if they want a different breakdown). payment_amount stays blank --
        # a quote isn't the same as payment actually received.
        cur = db.execute(
            "INSERT INTO orders (customer_id, tracking_mode, tracking_lot, agency_id, status, "
            "link_code, source_link, item_desc_en, item_image, item_cost, house_ship_fee, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'ordered', ?, ?, ?, ?, ?, ?, ?, ?)",
            (req["customer_id"], mode, int(lot_raw), agency_id, link_code,
             req["source_link"], req["item_description"], req["reference_image"],
             req["item_cost"], req["shipping_cost"], now, now),
        )
        order_id = cur.lastrowid
        db.execute(
            "UPDATE order_requests SET status = 'converted', converted_order_id = ?, updated_at = ? WHERE id = ?",
            (order_id, now, req["id"]),
        )
        db.commit()
        if req["shipping_cost"] is not None:
            flash(f"Note: the quoted shipping (฿{req['shipping_cost']:,.0f}) was carried into \"House ship\" — rebalance with China ship if needed.")
        line_id = _customer_line_id(req["customer_id"])
        if line_id:
            track_url = f"{_public_base_url()}/track/{link_code}"
            line_push(
                line_id,
                f"Your order is confirmed! Track it here: {track_url}\n"
                f"คำสั่งซื้อของคุณได้รับการยืนยันแล้ว! ติดตามได้ที่ลิงก์ด้านบน",
            )
        flash(f"Order created from request. Customer tracking number: {link_code}")
        return redirect(url_for("order_detail", order_id=order_id))

    pending = db.execute(
        "SELECT order_requests.*, customers.name AS customer_name, customers.phone AS customer_phone, "
        "customers.address AS customer_address, customers.info_source_note AS customer_info_source_note "
        "FROM order_requests "
        "JOIN customers ON order_requests.customer_id = customers.id "
        "WHERE order_requests.status = 'new' ORDER BY order_requests.created_at"
    ).fetchall()
    return render_template("requests.html", pending=pending, agencies=active_agencies())


@app.route("/admin/requests/<int:request_id>/quote", methods=["POST"])
@login_required
def quote_request(request_id):
    """Price a request: item cost (compared against the customer's stated
    budget via quote_status()) and shipping cost (quoted separately, not
    part of the budget comparison -- shipping is only known once the item
    reaches the warehouse). Shown on both this queue and the customer's own
    /request/<code> revisit page."""
    db = get_db()
    req = db.execute(
        "SELECT id, customer_id, request_code, budget FROM order_requests WHERE id = ? AND status = 'new'",
        (request_id,),
    ).fetchone()
    if req is None:
        flash("Request not found or already processed.")
        return redirect(url_for("requests_page"))
    item_cost = _parse_money(request.form.get("item_cost"))
    db.execute(
        "UPDATE order_requests SET item_cost = ?, shipping_cost = ?, updated_at = ? WHERE id = ?",
        (item_cost, _parse_money(request.form.get("shipping_cost")),
         datetime.utcnow().isoformat(), request_id),
    )
    db.commit()
    line_id = _customer_line_id(req["customer_id"])
    if line_id and item_cost is not None:
        verdict = quote_status(item_cost, req["budget"])
        verdict_text = {
            "within_budget": "within your stated budget",
            "over_budget": "above your stated budget — message us if you'd like to adjust anything",
        }.get(verdict, "ready")
        request_url = f"{_public_base_url()}/request/{req['request_code']}"
        line_push(
            line_id,
            f"Your quote is ready — the item is {verdict_text}. View details: {request_url}\n"
            f"ใบเสนอราคาของคุณพร้อมแล้ว ดูรายละเอียดได้ที่ลิงก์ด้านบน",
        )
    flash("Quote saved.")
    return redirect(url_for("requests_page"))


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


@app.route("/admin/feedback", methods=["GET", "POST"])
@login_required
def feedback_admin_page():
    db = get_db()
    if request.method == "POST":
        db.execute(
            "UPDATE feedback SET handled = 1 - handled WHERE id = ?",
            (request.form.get("feedback_id"),),
        )
        db.commit()
        return redirect(url_for("feedback_admin_page"))
    entries = db.execute("SELECT * FROM feedback ORDER BY handled ASC, created_at DESC").fetchall()
    return render_template("feedback_admin.html", entries=entries)


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
    track_url = f"{_public_base_url()}/track/{order['link_code']}"
    return render_template(
        "track.html", order=order, stage_times=stage_times, china_track_url=china_track_url,
        qr=qr_svg(track_url),
    )


@app.route("/track", methods=["GET", "POST"])
@limiter.limit("20 per hour")
def track_lookup():
    """Self-service phone-based lookup for a customer who lost their tracking
    link/code -- the LINE bot already supports this (phone matching in the
    webhook); the web had no equivalent, so a lost link was previously a
    dead end unless the customer went and texted the bot instead."""
    if request.method != "POST":
        return render_template("track.html", order=None, lookup=True, lookup_results=None, lookup_error=None)

    phone_norm = _normalize_phone(request.form.get("phone", ""))
    if not phone_norm:
        return render_template(
            "track.html", order=None, lookup=True, lookup_results=None,
            lookup_error=t("track.lookup_invalid_phone"),
        )

    db = get_db()
    orders = db.execute(
        "SELECT orders.link_code, orders.status, orders.created_at, orders.item_desc_en, orders.item_title_zh "
        "FROM orders JOIN customers ON orders.customer_id = customers.id "
        "WHERE customers.phone_normalized = ? ORDER BY orders.created_at DESC",
        (phone_norm,),
    ).fetchall()
    if len(orders) == 1:
        return redirect(url_for("track", link_code=orders[0]["link_code"]))
    return render_template("track.html", order=None, lookup=True, lookup_results=orders, lookup_error=None)


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
        "budget": (request_row["budget"] if request_row else ""),
        "other_contact": (request_row["other_contact"] if request_row else ""),
    }


def _save_request_reference_image(file, request_code):
    """Optional reference photo on a request -- same compress/validate path
    as an order's product image. Returns the relative static path, or None
    if no (valid) file was given. Silently skips an unreadable/wrong-type
    file rather than failing the whole submission over an optional field."""
    if not file or not file.filename:
        return None
    ext = ALLOWED_IMAGE_TYPES.get(file.mimetype)
    if not ext:
        return None
    base = os.path.join(UPLOAD_DIR, f"req_{request_code}")
    try:
        saved_ext = _compress_and_save(file, base, ext)
    except Exception:
        logger.warning("Could not process reference image for request %s", request_code, exc_info=True)
        return None
    return f"uploads/req_{request_code}.{saved_ext}"


@app.route("/terms", methods=["GET"])
def terms_page():
    return render_template("terms.html")


@app.route("/feedback", methods=["GET", "POST"])
@limiter.limit("10 per hour")
def feedback_page():
    """Open feedback box -- deliberately not tied to a customer/order, so it
    also works for someone who hasn't ordered yet. Read (and marked handled)
    from /admin/feedback; nothing here is customer-visible again afterward."""
    if request.method == "POST":
        message = request.form.get("message", "").strip()
        if not message:
            flash(t("feedback.error_required"))
            return render_template("feedback.html", values=request.form, submitted=False)
        db = get_db()
        db.execute(
            "INSERT INTO feedback (name, contact, message, source, created_at) VALUES (?, ?, ?, 'web', ?)",
            (request.form.get("name", "").strip() or None,
             request.form.get("contact", "").strip() or None,
             message, datetime.utcnow().isoformat()),
        )
        db.commit()
        return render_template("feedback.html", values=None, submitted=True)
    return render_template("feedback.html", values={"name": "", "contact": "", "message": ""}, submitted=False)


@app.route("/request", methods=["GET", "POST"])
@limiter.limit("10 per hour")
def new_request():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        address = request.form.get("address", "").strip()
        item_description = request.form.get("item_description", "").strip()
        source_link = request.form.get("source_link", "").strip()
        budget = request.form.get("budget", "").strip()
        other_contact = request.form.get("other_contact", "").strip()
        agreed_terms = request.form.get("agree_terms") == "on"
        photo_file = request.files.get("reference_image")
        # A file input can't be re-populated by the server on re-render (browsers
        # block it for security), so if one was attached this submit and we're
        # about to bounce the form back, tell the customer explicitly rather
        # than silently dropping their photo.
        had_photo = bool(photo_file and photo_file.filename)

        field_errors = {}
        if not name:
            field_errors["name"] = t("request.error_required")
        if not phone:
            field_errors["phone"] = t("request.error_required")
        if not item_description:
            field_errors["item_description"] = t("request.error_required")
        if field_errors:
            flash(t("request.error_summary"))
            return render_template(
                "request.html", request_row=None, values=request.form, submitted=False,
                field_errors=field_errors, photo_reattach=had_photo, wizard_start_step=1,
            )
        if not agreed_terms:
            flash(t("request.agree_terms_required"))
            return render_template(
                "request.html", request_row=None, values=request.form, submitted=False,
                field_errors={}, photo_reattach=had_photo, wizard_start_step=3,
            )

        db = get_db()
        now = datetime.utcnow().isoformat()
        customer_id = _find_or_create_customer(name, phone, address, other_contact)
        request_code = secrets.token_hex(3).upper()
        reference_image = _save_request_reference_image(request.files.get("reference_image"), request_code)
        db.execute(
            "INSERT INTO order_requests (customer_id, request_code, item_description, source_link, "
            "budget, other_contact, reference_image, terms_agreed_at, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)",
            (customer_id, request_code, item_description, source_link, budget or None, other_contact or None,
             reference_image, now, now, now),
        )
        db.commit()
        return redirect(url_for("view_request", request_code=request_code))

    return render_template(
        "request.html", request_row=None, values=_request_form_values(), submitted=False,
        field_errors={}, photo_reattach=False, wizard_start_step=1,
    )


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
        budget = request.form.get("budget", "").strip()
        other_contact = request.form.get("other_contact", "").strip()
        if not name or not phone or not item_description:
            flash("Please fill in your name, phone number, and what you'd like to order.")
            return redirect(url_for("view_request", request_code=request_code))

        now = datetime.utcnow().isoformat()
        # A blank address/other_contact here must never erase a previously-good
        # value (same fix as _find_or_create_customer()) -- fall back to what's
        # already on file rather than nulling it out.
        current = db.execute(
            "SELECT address, other_contact FROM customers WHERE id = ?", (req["customer_id"],)
        ).fetchone()
        new_address = address if address else (current["address"] or None)
        new_contact = other_contact if other_contact else (current["other_contact"] or None)
        db.execute(
            "UPDATE customers SET name = ?, phone = ?, phone_normalized = ?, address = ?, other_contact = ? WHERE id = ?",
            (name, phone, _normalize_phone(phone), new_address, new_contact, req["customer_id"]),
        )
        db.execute(
            "UPDATE order_requests SET item_description = ?, source_link = ?, budget = ?, other_contact = ?, "
            "updated_at = ? WHERE id = ?",
            (item_description, source_link, budget or None, other_contact or None, now, req["id"]),
        )
        db.commit()
        flash("Updated.")
        return redirect(url_for("view_request", request_code=request_code))

    values = {
        "name": req["customer_name"], "phone": req["customer_phone"], "address": req["customer_address"] or "",
        "item_description": req["item_description"] or "", "source_link": req["source_link"] or "",
        "budget": req["budget"] or "", "other_contact": req["other_contact"] or "",
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
    "feedback": ["feedback", "suggestion", "complaint", "ข้อเสนอแนะ", "ฟีดแบ็ก", "ติชม"],
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
        "feedback (wants to give feedback/suggestions about the SERVICE itself, not about one order), "
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
        # LINE's Flex "image" component requires JPEG/PNG over HTTPS (now
        # guaranteed by _compress_and_save() converting everything without
        # real transparency to JPEG -- see its docstring). item_image_thumb()
        # is still a pure filename transform with no filesystem check, so
        # also confirm the thumb file actually exists before building a URL
        # to it: a dangling reference here (a legacy upload that predates
        # the compression pipeline, a manual DB edit) used to silently
        # produce a live-but-404 image URL -- LINE renders that as a blank
        # white box with no error anywhere, exactly the reported bug.
        thumb = item_image_thumb(o["item_image"])
        thumb_path = os.path.join(os.path.dirname(__file__), "static", thumb) if thumb else None
        if thumb and thumb.lower().endswith((".jpg", ".jpeg", ".png")) and thumb_path and os.path.exists(thumb_path):
            bubble["hero"] = {
                "type": "image", "url": f"{base}/static/{thumb}",
                "size": "full", "aspectRatio": "1:1", "aspectMode": "cover",
                # Tap the photo itself to open tracking, not just the footer
                # button -- a fully tappable image tile, not a flat picture.
                "action": {"type": "uri", "uri": f"{base}/track/{o['link_code']}"},
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
                "You can:\n📦 Type MY ORDERS to see your shipments\n🛒 Type NEW ORDER to request another\n"
                "💬 Talk to support anytime\n📝 Type FEEDBACK to tell us what to improve",
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

        def _handle_feedback():
            _reply(
                "We'd love your feedback! Tap here: "
                f"{_public_base_url()}/feedback\n"
                "เราอยากรับฟังความคิดเห็นของคุณ กดลิงก์ด้านบนได้เลย"
            )

        def _handle_new_order(item_description=None, source_link=None):
            if item_description:
                now = datetime.utcnow().isoformat()
                req_code = secrets.token_hex(3).upper()
                # other_contact is pulled from the customer record, not
                # re-asked -- they're already linked/identified, and (Phase
                # 6) other_contact now lives on customers precisely so a
                # returning customer never has to give it again.
                db.execute(
                    "INSERT INTO order_requests (customer_id, request_code, item_description, source_link, "
                    "other_contact, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'new', ?, ?)",
                    (customer["id"], req_code, item_description, source_link or None,
                     customer["other_contact"], now, now),
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
            "feedback": _handle_feedback,
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
