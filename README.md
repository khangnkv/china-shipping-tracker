# China shipping tracker — v1 (manual paste + LINE)

A minimal working system:
- Admin panel to add orders and paste today's LOT list
- Auto-matches orders against the LOT list (pending vs arrived)
- Customers link themselves via a one-time code sent in LINE chat
- LINE push notifications for status changes
- Customers reply in LINE to submit shipping info once arrived

This is intentionally simple: SQLite (one file, no server to install),
no background jobs, no LIFF forms yet. Get this working end-to-end first,
then layer on automation later.

## Part 1 — Create your LINE Official Account + Messaging API channel

1. Go to https://developers.line.biz/console/ and log in with a LINE account
   (personal account is fine; you can add teammates later).
2. Create a **Provider** (this is just an org name, e.g. your business name).
3. Inside the provider, create a new **Channel** → choose **Messaging API**.
   Fill in the basic info (name, category, description, icon). This
   automatically creates a LINE Official Account with the same name.
4. Once created, open the channel and go to the **Messaging API** tab:
   - Note the **Channel secret** (under Basic settings tab).
   - Click **Issue** under Channel access token (long-lived) — copy this token.
   - Both of these go into your `.env` file (see Part 3).
5. Still on the Messaging API tab:
   - Turn **Use webhook** ON.
   - You'll set the **Webhook URL** once your server is live (Part 3) —
     it will be `https://yourdomain.com/webhook`.
   - Turn OFF **Auto-reply messages** and **Greeting messages** in the
     linked LINE Official Account Manager (https://manager.line.biz) —
     otherwise LINE's canned replies will interfere with your bot's replies.
6. In LINE Official Account Manager, under **Settings → Response settings**,
   set "Chat" to disabled and "Webhooks" to enabled, so all messages route
   to your app instead of a human chat inbox.
7. Get your Official Account's QR code / LINE ID from the Official Account
   Manager home page — this is what you'll give customers to add as a friend.

## Styling / frontend

The UI is server-rendered Jinja templates styled with Tailwind, compiled once
into `static/app.css` (committed — the running app needs no build step). After
editing any template class, rebuild it:

```bash
npx tailwindcss@3.4.1 -i tailwind.input.css -o static/app.css --minify
```

`tailwind.config.js` scans `templates/**/*.html`. Dynamic status-badge classes
live as complete class strings in `templates/_macros.html` so they survive the
CSS purge — never build a class name by string concatenation.

## Order pipeline & agencies (v3)

Orders move through an 8-stage pipeline; the customer sees friendly EN/TH labels
on `/track`, never the internal LOT/mode/agency/China tracking number:

`Ordered → In transit to China warehouse → At China warehouse → Cross-border
transit → Thailand customs → At Thailand warehouse → Out for delivery → Delivered`

- **China leg:** paste the China tracking number on the order detail page — it
  renders a **17TRACK** deep link. Your daily **Paste LOT list** match flips
  matched orders to *At China warehouse*.
- **After the warehouse:** advance each order manually with **Update status** on
  the detail page. Choosing *Out for delivery* reveals the local carrier + tracking
  number (e.g. Flash Express) that the customer then sees on `/track`.
- **Customer tracking number** = the generated `link_code`, shown on order create
  and detail. Give it to the customer; they can also type it to the LINE bot.
- **Faster entry:** paste a raw customer blob and click *Split into fields* to fill
  name/phone/address (needs `ANTHROPIC_API_KEY`); drag, paste, or upload the product
  image, or try *Fetch image from link* (best-effort — Xianyu/Taobao often block).
- **Agencies:** add them under **Agencies**, pick one per order. **Stats** then
  compares agencies by avg transit days (total + per leg), profit, margin, and
  ฿/day efficiency, and shows your latest-used agency.

## Part 2 — Run it locally first (sanity check)

```bash
cd china-shipping-tracker
python3 -m venv venv
source venv/bin/activate      # Windows PowerShell: venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env
# edit .env: SECRET_KEY and ADMIN_PASSWORD are REQUIRED, the app refuses to
# start without them. Set FLASK_ENV=development for local testing.
python app.py
```

The app loads `.env` itself (via `python-dotenv`) on startup — no need to
`export`/`set` the variables into your shell manually, on any OS.

Visit `http://localhost:5000` → it redirects to `/admin/login`. Log in with
your `ADMIN_PASSWORD`. Try adding a test order — you'll see a link code.
The LINE webhook won't work yet since LINE can't reach `localhost`; that's
expected until you deploy.

### Run the tests

```bash
pip install -r requirements-dev.txt
pytest -v
```

Tests spin up the app against a temporary SQLite file (no real `.env`
values or network calls needed) and cover LOT-list parsing/matching, login
+ rate limiting, CSRF protection, order-input validation, and the LINE
webhook (signature verification and the link-code linking flow).

## Part 3 — Deploy on your cloud server

> **For a full copy-paste production runbook** (RackNerd/Ubuntu VPS: gunicorn +
> systemd + Caddy auto-TLS, domain purchase & DNS, and a LINE + QR test matrix),
> see [`deploy/DEPLOY.md`](deploy/DEPLOY.md). The summary below is the short version.

LINE requires the webhook URL to be **public HTTPS**. Steps below assume a
Linux server (Ubuntu/Debian) with a domain pointed at it. Adjust for your
actual setup — if you tell me your OS and whether you have a domain, I can
give exact commands.

1. Copy the project to the server (`scp -r china-shipping-tracker user@yourserver:~`)
   or `git clone` if you push it to a repo.
2. On the server:
   ```bash
   sudo apt update && sudo apt install -y python3-venv python3-pip
   cd china-shipping-tracker
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env
   nano .env   # fill in real SECRET_KEY, ADMIN_PASSWORD, LINE token + secret
   ```
3. Run it with gunicorn (production WSGI server) instead of `python app.py`:
   ```bash
   gunicorn -w 2 -b 127.0.0.1:5000 app:app
   ```
   (`.env` is loaded automatically by the app itself, same as local dev.)
   For it to survive reboots/crashes, wrap this in a systemd service —
   ask me and I'll write the unit file for your exact paths.
4. Put a reverse proxy with HTTPS in front of it. Easiest option is **Caddy**
   (auto-issues and renews TLS certificates for you):
   ```bash
   sudo apt install -y caddy
   ```
   `/etc/caddy/Caddyfile`:
   ```
   yourdomain.com {
       reverse_proxy 127.0.0.1:5000
   }
   ```
   `sudo systemctl restart caddy`. (If you'd rather use nginx + certbot,
   that works too — just ask.)
5. Back in the LINE Developers Console → Messaging API tab, set **Webhook
   URL** to `https://yourdomain.com/webhook` and click **Verify** — it
   should return success once your server is reachable.

## Part 4 — Day-to-day usage

1. Log in at `https://yourdomain.com/admin/login`.
2. When a new customer orders, add them under **Orders** → note the
   generated **link code**.
3. Tell the customer: "Add [your LINE OA] as a friend and send this code: `ABC123`"
   — once they do, their LINE account is linked and future updates go
   straight to their LINE chat.
4. Each day after the shipping agent's site updates (~12:00), open
   **Paste LOT list**, copy the LOT block from the agent's site, paste it in,
   and click **Match against orders**. Orders found in the list flip to
   "arrived, awaiting info".
5. Click **Send LINE notifications now** — pending customers get a "still
   waiting" message, newly-arrived customers get a message asking them to
   reply with their shipping address.
6. When a customer replies with their address, it's saved automatically
   and shown in the Orders table under "Shipping info" — you then submit it
   on the agent's site manually (the one step that still needs a human).

## What's deliberately left out of v1 (for later)

- No LIFF form yet — shipping info is collected as a plain text reply,
  which works but isn't validated (no separate address/phone fields).
- No automatic daily scrape — you paste the LOT list yourself.
- No multi-admin roles — one shared `ADMIN_PASSWORD` for now.
- No support yet for multiple shipping agents/routes as separate entities —
  every order assumes the one agent's LOT format. Worth adding once this
  is working reliably.
