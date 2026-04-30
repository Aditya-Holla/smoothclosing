# Deploying SmoothClosing Dashboard

Hosted setup: **Render** (Docker web service + persistent disk) behind
**Cloudflare Access** (email-based login for 2-3 users).

Total cost: ~$25/month for Render Standard, $0 for Cloudflare Access free tier.

---

## Prerequisites

- The repo is on GitHub (private repo is fine).
- A Render account: https://render.com
- A Cloudflare account with a domain you control (any domain — we'll use a
  subdomain like `dashboard.smoothclosing.com`).

---

## Step 1 — Create the Render service

1. Push the latest code to GitHub. The repo must contain `Dockerfile`,
   `render.yaml`, and `.dockerignore` (already added).
2. In Render: **New → Blueprint** → connect your repo → Render reads
   `render.yaml` and creates the service + 5GB disk automatically.
3. Render will fail the first build because secrets aren't set yet — that's
   expected. Continue to Step 2.

## Step 2 — Add secrets

In the Render dashboard for the new service, go to **Environment** and fill
in every variable that's currently in your local `.env`:

```
ANTHROPIC_API_KEY=sk-ant-...
RC_CLIENT_ID=...
RC_CLIENT_SECRET=...
RC_JWT_TOKEN=...
RC_FROM_NUMBER=...
SENDER_NAME=...
SKIPGENIE_EMAIL=...
SKIPGENIE_PASSWORD=...
RENTCAST_API_KEY=...
GOOGLE_SHEET_ID=...
DISPOSITIONS_SHEET_ID=...
```

Hit **Save Changes** — Render will redeploy.

## Step 3 — Seed the data disk

The persistent disk at `/data` starts empty. Upload starter files via
Render's **Shell** tab (a web SSH into the running container):

```bash
# In Render Shell:
cd /data
mkdir -p input_pdfs
# To upload files, use Render's file upload UI in the Shell tab,
# or scp from your laptop (Render Pro+ only) — easiest is to commit
# starter CSVs into the repo and copy on first boot.
```

The minimum to get the dashboard usable:

- `/data/leads.csv` — your existing accumulated leads (or empty file with
  the header row).
- `/data/sms_history.csv` — your texting history (so dedupe works).
- `/data/input_pdfs/` — directory for new PDFs to process.

Upload your local files (`scp`, the Render Shell, or paste into the editor).

## Step 4 — Verify it's running

Render gives the service a URL like `https://smoothclosing-dashboard.onrender.com`.
Open it. You should see the dashboard.

**Don't share that URL yet** — it's wide open to the internet. Continue to Step 5.

## Step 5 — Lock it down with Cloudflare Access

Cloudflare Access (free tier: up to 50 users) gates the URL behind email
login. Only the people you whitelist can reach it.

1. **In Cloudflare**: pick a domain you own and add a CNAME record:
   - Name: `dashboard` (or whatever subdomain you want)
   - Target: `smoothclosing-dashboard.onrender.com`
   - Proxy: **Proxied** (orange cloud)

2. **In Render**: settings → Custom Domains → add `dashboard.yourdomain.com`.
   Render will verify ownership. This may take a few minutes.

3. **In Cloudflare Zero Trust** (separate dashboard, sign up for free):
   - Access → Applications → Add an application → Self-hosted
   - Application name: `SmoothClosing Dashboard`
   - Application domain: `dashboard.yourdomain.com`
   - Add a policy:
     - Name: `Authorized team`
     - Action: Allow
     - Include → Emails → list the 2-3 team emails
   - Save

4. Now visiting `https://dashboard.yourdomain.com` shows a Cloudflare login
   page. Only allow-listed emails can reach the dashboard.

## Step 6 — Test

Open `https://dashboard.yourdomain.com` in a private window. Confirm:

- Cloudflare prompts for email
- After login, the dashboard loads
- The chat tab works
- SMS dry-run works (don't send real texts on first test)

---

## Day-2 ops

**Code updates:** push to `main` on GitHub → Render auto-redeploys (~3 min).

**Data updates:** files written by the dashboard land in `/data` and persist
across deploys. To pull a CSV down for analysis, use Render Shell + `cat` or
the file-download UI.

**Adding/removing users:** Cloudflare Zero Trust → Access → edit the policy
email list. Takes effect immediately.

**Logs:** Render dashboard → service → Logs tab. Streamlit + script output
both stream there.

**Restart / rollback:** Render dashboard → Manual Deploy → "Clear cache and
deploy" or "Roll back to previous deploy."

---

## Costs

| Item | Cost |
|---|---|
| Render Standard plan (web service + 2GB RAM) | $25/mo |
| Render persistent disk (5GB) | included |
| Cloudflare Access (under 50 users) | $0 |
| Cloudflare DNS | $0 |
| **Total** | **~$25/mo** |

Could drop to ~$7/mo on Render Starter if Playwright cold-starts within
512MB RAM, but skip-trace runs may OOM. Standard is the safer pick.

---

## Known limitations of the hosted version

- **No CAD scraping from the hosted dashboard** by default. CAD scraper
  works via Playwright but isn't surfaced in the web UI; run locally.
- **Scheduled runs** (e.g. nightly skip-trace) aren't set up. Add a Render
  Cron Job if you want this — point it at a small Python entrypoint that
  imports and calls the relevant script.
- **Single instance only** — don't scale to multiple replicas. The dashboard
  reads/writes shared CSVs that aren't safe for concurrent writes.
