# Deploying to a RackNerd Ubuntu VPS (24/7 + HTTPS for LINE)

The app must be reachable at a **public HTTPS URL** or LINE webhooks, LINE push
messages, and the order QR code will not work. This runbook uses
**gunicorn + systemd** (always-on, auto-restart, starts on boot) behind
**Caddy** (automatic TLS).

Assumes Ubuntu 22.04/24.04 and that you can SSH in as root (or a sudo user).
Replace `yourdomain.com` and the paths/user if you change them.

---

## 0. Before you start — get the code onto the box

The repo root **is** the app (app.py, deploy/, etc. at top level), so it drops
straight into `/opt/shipping`.

Option A (git) — clone directly into the app dir (do this after step 1 creates it):

```bash
git clone https://github.com/YOUR_USER/china-shipping-tracker.git /opt/shipping
```

Option B (scp) from your machine — copy the repo *contents* into `/opt/shipping`:

```bash
scp -r china-shipping-tracker/* root@YOUR_VPS_IP:/opt/shipping/
```

Commit the built `static/app.css` so the VPS needs no Node. Only rebuild CSS if
you change template classes:
`npx tailwindcss@3.4.1 -i tailwind.input.css -o static/app.css --minify`.

---

## 1. System packages + a service user

```bash
apt update && apt install -y python3-venv python3-pip git ufw
# Caddy (official repo):
apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list
apt update && apt install -y caddy

# dedicated non-root user that owns the app
adduser --system --group --home /opt/shipping shipping || true
mkdir -p /opt/shipping /var/log/shipping /opt/shipping/backups
chown -R shipping:shipping /opt/shipping /var/log/shipping
```

## 2. Python environment

```bash
cd /opt/shipping
sudo -u shipping python3 -m venv venv
sudo -u shipping venv/bin/pip install --upgrade pip
sudo -u shipping venv/bin/pip install -r requirements.txt
```

## 3. Production `.env`  (`/opt/shipping/.env`, chmod 600, owned by shipping)

```dotenv
SECRET_KEY=<run: python -c "import secrets;print(secrets.token_hex(32))">
ADMIN_PASSWORD=<a long random password — NOT admin1234>
LINE_CHANNEL_ACCESS_TOKEN=<from LINE console>
LINE_CHANNEL_SECRET=<from LINE console>
ANTHROPIC_API_KEY=<optional: enables title translation + customer paste-split>
PUBLIC_BASE_URL=https://yourdomain.com     # REQUIRED so QR + /track links are public
LINE_CONTACT=@yourshop                      # optional; shows a LINE button on /track
FLASK_ENV=production                         # enables secure cookies
PORT=5000
```

```bash
chmod 600 /opt/shipping/.env && chown shipping:shipping /opt/shipping/.env
```

## 4. systemd service

```bash
cp /opt/shipping/deploy/shipping.service /etc/systemd/system/shipping.service
systemctl daemon-reload
systemctl enable --now shipping
systemctl status shipping           # should be active (running)
curl -sf http://127.0.0.1:5000/healthz && echo OK
```

## 5. Caddy (TLS reverse proxy)

Edit `deploy/Caddyfile`, replace `yourdomain.com`, then:

```bash
cp /opt/shipping/deploy/Caddyfile /etc/caddy/Caddyfile
systemctl restart caddy
```

## 6. Firewall

```bash
ufw allow 22/tcp && ufw allow 80/tcp && ufw allow 443/tcp && ufw --force enable
```

## 7. Daily DB backup (cron)

```bash
( crontab -l 2>/dev/null; echo '15 3 * * * sqlite3 /opt/shipping/tracker.db ".backup /opt/shipping/backups/tracker-$(date +\%F).db"' ) | crontab -
```
(`apt install -y sqlite3` if the CLI is missing.)

---

## 8. Buy + point a domain

1. **Buy** at Cloudflare Registrar (at-cost), Namecheap, or Porkbun.
2. **DNS:** add an **A record** for `@` (and `www`) → your RackNerd IPv4
   (find it in the RackNerd panel). Optional `AAAA` → IPv6.
   - If your DNS is on Cloudflare, set the record to **DNS only (grey cloud)** so
     Caddy can complete the ACME challenge and issue the cert itself.
3. Wait for propagation: `dig +short yourdomain.com` should show your VPS IP.
4. Visit `https://yourdomain.com` — Caddy issues TLS on first hit. `/healthz` → `ok`.

---

## 9. Point LINE at it + verify

In the LINE Developers console → your Messaging API channel:
- **Webhook URL** = `https://yourdomain.com/webhook` → click **Verify** (expect success).
- **Use webhook** = ON. In the OA Manager, turn **Auto-reply** and **Greeting** OFF
  so they don't collide with the bot.

---

## 10. Test matrix — run until all pass (see the app's **LINE** page for diagnostics)

Open `https://yourdomain.com/admin/line-status` — confirm token/secret **Set**,
`PUBLIC_BASE_URL` is your https domain, and "last inbound webhook" updates after
the Verify in step 9.

| # | Test | Expected |
|---|------|----------|
| A | LINE console **Verify** | success; line-status shows a recent webhook |
| B | Phone: add OA as friend, send an order's tracking code | reply "You're linked!"; the order's customer now shows **Linked** |
| C | Send a wrong code / a reused code | "please send the code" / "already been used" |
| D | Admin: advance a linked order's stage, then **Send LINE notifications** | phone receives the friendly status; **no** internal LOT/mode/agency/China no. |
| E | Advance to **Out for delivery** with a carrier + tracking no. | message includes the local carrier + tracking number |
| F | LINE page → **Send test message** to that customer | test push arrives (isolates push from status logic) |
| G | Scan the QR on the order-detail page | opens `https://yourdomain.com/track/<code>`, shows the 8-stage timeline + item + local tracking; unknown code → "Order not found" |

### Faster iteration before DNS is ready (optional)
Run a temporary public tunnel from the VPS (or your PC) and point the LINE webhook
at it while you iterate:
```bash
# one-off, no account needed:
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /usr/local/bin/cloudflared && chmod +x /usr/local/bin/cloudflared
cloudflared tunnel --url http://localhost:5000
```
Set `PUBLIC_BASE_URL` to the printed `https://<name>.trycloudflare.com`, restart
`shipping`, set that as the LINE Webhook URL, and run tests A–G. Switch both back to
the real domain once DNS is live.

---

## Updating the app later

```bash
cd /opt/shipping && sudo -u shipping git pull   # or scp the new files
sudo -u shipping venv/bin/pip install -r requirements.txt
systemctl restart shipping
```

## Troubleshooting
- `journalctl -u shipping -n 50 --no-pager` — app logs.
- `journalctl -u caddy -n 50 --no-pager` — TLS/proxy logs.
- Webhook Verify fails → DNS not propagated, port 443 blocked, or `LINE_CHANNEL_SECRET`
  wrong (the app fails closed on a bad/missing secret).
- Push fails but Verify works → check `LINE_CHANNEL_ACCESS_TOKEN`; see error.log.
