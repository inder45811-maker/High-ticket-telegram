# B2B High-Ticket Contract Alert System — Zero-Cost Cloud Deployment Guide

> **15-Minute Mobile-First Deployment & Operations Manual**  
> Complete step-by-step instructions to configure, launch, and monitor the automated B2B Contract Alert Radar **100% from a smartphone browser** (iOS Safari or Android Chrome) using zero-cost cloud infrastructure (GitHub Actions Free Tier + Render / Railway Free Tiers).

---

## 📋 Executive Overview & Cost Architecture

The B2B Contract Alert System is architected for zero infrastructure overhead and maximum operational resilience:

| Component | Target Infrastructure | Monthly Cost | Phone-Friendly Setup |
| :--- | :--- | :--- | :--- |
| **Ingestion, Filter & Dispatcher** | **GitHub Actions Cron** | **$0.00** (2,000 free minutes/mo) | 📱 100% in Mobile Browser |
| **Webhook Access Manager (Whop)** | **Render.com / Railway.app** | **$0.00** (Free Tier Web Service) | 📱 100% in Mobile Browser |
| **Alert Delivery Channel** | **Telegram Bot API** | **$0.00** (Official Free Bot API) | 📱 100% in Telegram Mobile App |
| **Subscriber Billing & Invites** | **Whop Marketplace** | **3% transaction fee only** | 📱 100% in Whop Mobile Dashboard |

*Total fixed monthly operational cost: **$0.00 / month**.*

---

## ⏱️ 15-Minute Mobile Browser Deployment Walkthrough

Follow these 4 phases sequentially from your smartphone. You will not need a terminal, laptop, or desktop computer.

```
┌─────────────────┐     ┌──────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│ Phase 1:        │     │ Phase 2:         │     │ Phase 3:         │     │ Phase 4:        │
│ Telegram Setup  │ ──> │ GitHub Secrets   │ ──> │ 1st Run Test     │ ──> │ Webhook Server  │
│ (3 Minutes)     │     │ (4 Minutes)      │     │ (2 Minutes)      │     │ (6 Minutes)     │
└─────────────────┘     └──────────────────┘     └──────────────────┘     └─────────────────┘
```

---

### 📱 Phase 1: Telegram Bot & VIP Channel Setup (3 Minutes)

1. **Create the Telegram Bot**:
   - Open the **Telegram app** on your phone.
   - In the search bar at the top, type `@BotFather` and select the verified bot with the blue checkmark.
   - Tap **Start** (or send `/start`), then send `/newbot`.
   - Choose a display name for your bot (e.g., `B2B Contract Radar`).
   - Choose a username ending in `bot` (e.g., `B2BDealAlertBot`).
   - BotFather will immediately return an API token formatted like:
     ```
     7198273645:AAH_XYZ987654321AbcDefGhIjKlMnOpQrS
     ```
   - **Long-press and copy this token** — this is your `TELEGRAM_BOT_TOKEN`.

2. **Create Your Private Telegram Channel**:
   - In Telegram, tap the **New Message / Compose** icon (pencil in top-right or bottom-right).
   - Select **New Channel**.
   - Set Channel Name to `B2B High-Ticket Deals [VIP]` (or your chosen brand name).
   - Tap **Next** and select **Private Channel**.
   - Tap **Done**.

3. **Add Bot as Administrator**:
   - In your new channel, tap the channel name at the top to open Channel Info.
   - Tap **Administrators** -> **Add Administrator**.
   - Search for your bot username (`@B2BDealAlertBot`) and select it.
   - Ensure the permission **"Post Messages"** is toggled ON (green).
   - Tap **Done** / checkmark to confirm.

4. **Obtain the Channel Chat ID**:
   - Post any test message in the channel (e.g., `test`).
   - Forward that test message to `@userinfobot` or `@username_to_id_bot`.
   - The helper bot will instantly reply with the numeric ID. For private channels, this begins with `-100` (e.g., `-1002345678901`).
   - Copy this number — this is your `TELEGRAM_CHAT_ID` (or `TELEGRAM_CHANNEL_ID`).

---

### 🐙 Phase 2: GitHub Repository & Secrets Setup (4 Minutes)

1. **Fork the Repository on Mobile**:
   - Open **Safari** or **Chrome** on your phone.
   - Navigate to your repository URL: `https://github.com/your-username/b2b_alert_bot`.
   - In the top right, tap the **Fork** button -> tap **Create fork**.

2. **Configure Repository Secrets**:
   - In your forked repository, tap **Settings** (gear icon near the top navigation tab).
   - Scroll down to the left sidebar menu (or hamburger menu on mobile) and tap **Secrets and variables** -> **Actions**.
   - Under "Repository secrets", tap the green **New repository secret** button.
   - Add the following secrets one by one:

   | Secret Name | Value to Paste |
   | :--- | :--- |
   | `TELEGRAM_BOT_TOKEN` | Token from `@BotFather` (e.g., `7198273645:AAH_...`) |
   | `TELEGRAM_CHAT_ID` | Channel ID from Phase 1 (e.g., `-1002345678901`) |
   | `OPENAI_API_KEY` | *(Optional)* OpenAI API Key `sk-...`. If left empty, the engine uses built-in NLP heuristics. |
   | `MIN_BUDGET_USD` | `2000` *(Default minimum fixed contract value)* |
   | `MIN_HOURLY_USD` | `50` *(Default minimum hourly contract rate)* |

---

### 🚀 Phase 3: Trigger First Run & Verify on Phone (2 Minutes)

1. **Execute Manual Workflow Dispatch**:
   - In your GitHub mobile browser tab, tap the **Actions** tab.
   - Under "All workflows", tap **Lead Ingestion & Telegram Dispatcher** (or `alert_cron.yml`).
   - Tap the blue banner that says **"Run workflow"** -> tap the green **"Run workflow"** button.
   - Refresh the page after 5 seconds: a running workflow with a yellow spinning circle will appear.

2. **Verify Live Notification on Telegram**:
   - Switch back to the **Telegram app**.
   - Within 30 to 60 seconds, your private channel will receive formatted deal cards featuring:
     * High-visibility budget badge (e.g., `💰 $4,500 FIXED` or `⏱️ $75/HR`)
     * 3-bullet executive summary (Scope, Skills, Winning Angle)
     * Active `[🚀 Apply on Source]` button.
   - Tap the button to verify it opens the original application source.

---

### 🌐 Phase 4: Whop Webhook Server on Render (Free Tier) (6 Minutes)

To automatically generate single-use Telegram invites when users subscribe on Whop, deploy the lightweight webhook receiver on Render.com:

1. **Sign Up & Connect GitHub on Render**:
   - Open mobile browser and go to `https://render.com`.
   - Tap **Get Started for Free** and log in with your **GitHub account**.
   - On the Render Dashboard, tap **+ New** -> **Web Service**.
   - Select your `b2b_alert_bot` repository from GitHub.

2. **Configure Web Service Settings**:
   - **Name:** `b2b-contract-webhook`
   - **Region:** Choose the region closest to you (e.g., `Oregon (US West)` or `Frankfurt (EU Central)`).
   - **Branch:** `main`
   - **Runtime:** `Python 3`
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python -m b2b_alert_bot.main --server --port $PORT`
   - **Instance Type:** `Free` ($0.00/month)

3. **Add Environment Variables on Render**:
   - Scroll down to the **Environment Variables** section and tap **Add Environment Variable**:
     * `TELEGRAM_BOT_TOKEN`: Your `@BotFather` bot token.
     * `TELEGRAM_CHAT_ID`: Your Telegram channel ID (e.g., `-1002345678901`).
     * `WHOP_WEBHOOK_SECRET`: Copy from Whop Dashboard (see Step 4 below).
     * `PORT`: `10000` (Render default).
   - Tap **Create Web Service** at the bottom. Render will build and deploy your app in ~90 seconds.
   - Copy your public service URL: `https://b2b-contract-webhook.onrender.com`.

4. **Connect Webhook in Whop Dashboard**:
   - Open `https://whop.com/dashboard` in your mobile browser.
   - Navigate to **Developer Settings** -> **Webhooks** -> **Add Webhook**.
   - **Webhook URL:** `https://b2b-contract-webhook.onrender.com/webhooks/whop`
   - **Subscribed Events:** Check:
     * `membership.went_valid` (New subscriber or successful renewal)
     * `membership.went_invalid` (Cancelled, disputed, or expired subscription)
   - Copy the **Webhook Secret Key** generated by Whop.
   - Paste this key into your Render environment variables as `WHOP_WEBHOOK_SECRET`.
   - Tap **Send Test Event** in Whop: verify HTTP `200 OK` response.

---

## ⚙️ Automated Cron Configuration (GitHub Actions)

The repository includes an optimized GitHub Actions cron workflow at `.github/workflows/alert_cron.yml`.

### Workflow File Structure:
```yaml
name: Lead Ingestion & Telegram Dispatcher

on:
  schedule:
    # Runs every 30 minutes (consumes ~150 mins/mo of 2,000 free minutes)
    - cron: '*/30 * * * *'
  workflow_dispatch: # Allows manual trigger from mobile GitHub app/browser

jobs:
  poll-and-dispatch:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - name: Check out repository
        uses: actions/checkout@v4

      - name: Set up Python 3.11
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'
          cache: 'pip'

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements.txt

      - name: Restore SQLite Deduplication Cache
        uses: actions/cache@v4
        with:
          path: data/
          key: deduplication-db-${{ github.run_id }}
          restore-keys: |
            deduplication-db-

      - name: Execute Lead Ingestion & Alert Dispatch
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
          MIN_BUDGET_USD: ${{ secrets.MIN_BUDGET_USD || '2000' }}
          MIN_HOURLY_USD: ${{ secrets.MIN_HOURLY_USD || '50' }}
          DATA_DIR: './data'
        run: |
          mkdir -p data
          python -m b2b_alert_bot.main --cron

      - name: Save Deduplication Cache
        uses: actions/cache@v4
        if: always()
        with:
          path: data/
          key: deduplication-db-${{ github.run_id }}
```

---

## 🔑 Complete Environment Variables Checklist

Use this checklist to ensure all environment keys are correctly configured across GitHub Actions and Render/Railway:

| Environment Variable | Required? | Default | Valid Example | Purpose & Validation Rules |
| :--- | :---: | :---: | :--- | :--- |
| `TELEGRAM_BOT_TOKEN` | **Yes** (or Dry-Run) | `None` | `7198273645:AAH_XYZ...` | Bot authentication token from `@BotFather`. Must contain `:` separating bot ID and hash. |
| `TELEGRAM_CHAT_ID` | **Yes** | `None` | `-1002345678901` | Target private channel ID. Must begin with `-100` for channels or positive int for DMs. |
| `MIN_BUDGET_USD` | No | `2000` | `2000` | Minimum fixed contract budget in USD. Leads below this threshold are discarded. |
| `MIN_HOURLY_USD` | No | `50` | `50` | Minimum hourly contract rate in USD. Hourly leads below this rate are discarded. |
| `OPENAI_API_KEY` | No | `""` | `sk-proj-abc123...` | API key for OpenAI model enrichment. If omitted, uses deterministic NLP fallback. |
| `WHOP_WEBHOOK_SECRET` | For Webhooks | `""` | `wh_sec_991a82f...` | Secret key used to verify HMAC-SHA256 signatures on incoming Whop webhooks. |
| `PORT` | For Server | `8000` | `10000` | Port for the webhook receiver HTTP server (Render auto-injects `10000`). |
| `HOST` | For Server | `0.0.0.0`| `0.0.0.0` | Binding host address for webhook server. |
| `DRY_RUN` | No | `false` | `true` | Offline simulation mode. Dispatches to mock handler without calling Telegram API. |
| `DATA_DIR` | No | `./data` | `./data` | Filepath where SQLite deduplication database (`leads.db`) is stored. |
| `LOG_LEVEL` | No | `INFO` | `DEBUG` | Logging verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR`. |

---

## 📱 Mobile Monitoring & Health Verification

You can monitor the health of your alert system from any mobile device without SSH or terminal tools.

### 1. Checking Live Health Check Endpoints
Open your Render public URL in mobile Safari or Chrome:

#### Endpoint: `GET /health`
Validates basic service liveness and runtime uptime:
```json
{
  "status": "healthy",
  "service": "b2b_alert_bot",
  "version": "1.0.0",
  "timestamp": "2026-09-20T18:00:00Z"
}
```

#### Endpoint: `GET /ready`
Validates database connection, Telegram bot connectivity, and indexing metrics:
```json
{
  "status": "ready",
  "database": "ok",
  "telegram_dispatcher": "ok",
  "total_leads_indexed": 418,
  "high_ticket_alerts_sent": 87,
  "active_subscribers": 52,
  "last_poll_timestamp": "2026-09-20T18:30:12Z"
}
```

---

### 2. Checking Run Logs on Mobile

#### A. Mobile GitHub Actions:
1. Open GitHub in mobile browser -> Go to your repo.
2. Tap **Actions**.
3. Tap on the latest workflow run (green checkmark or red cross).
4. Tap the job name: `poll-and-dispatch`.
5. Tap the step: `Execute Lead Ingestion & Alert Dispatch`.
6. Scroll down to view the real-time execution log:
   ```
   [INFO] Ingestion started: scanning 5 sources...
   [INFO] We Work Remotely: 14 leads parsed.
   [INFO] RemoteOK: 22 leads parsed.
   [INFO] Filter passed: 4 new leads >= $2,000.
   [INFO] Dispatched deal card #d4b81c to channel -1002345678901 (HTTP 200).
   [INFO] Run complete. Deduplication store updated.
   ```

#### B. Mobile Render Dashboard:
1. Open `https://dashboard.render.com` on your phone.
2. Tap `b2b-contract-webhook`.
3. Tap the **Logs** tab.
4. Real-time HTTP requests and webhook processing events stream live in the browser.

---

### 3. Mobile Telegram Status Command (`/status`)
If you send a direct message to your bot on Telegram:
- Send: `/status` (only authorized for the channel administrator).
- The bot replies with an executive operational card:
  ```
  📊 System Status: Operational
  ⏱️ Last Cron Sync: 12 minutes ago
  📦 Leads Evaluated: 342
  💎 High-Ticket Pings Today: 8
  👥 Active Channel Members: 48
  ⚡ Health Check: All 5 Feeds Normal
  ```

---

## 🚨 Mobile Emergency Runbook & Troubleshooting

### Problem 1: Telegram API Rate Limit (HTTP 429)
- **Symptom:** Workflow logs display `Telegram API Error 429: Too Many Requests. retry_after: 8`.
- **Cause:** Dispatched more than 1 message/sec into a single channel.
- **Resolution:** Built-in token bucket automatically sleeps for `retry_after + 1` seconds and reschedules the message. No manual action required.

### Problem 2: Missing Channel Administrator Rights
- **Symptom:** Logs show `403 Forbidden: bot is not a member of the channel` or `400 Bad Request: have no rights to post`.
- **Resolution:** Open Channel Info on Telegram mobile -> Administrators -> Select your bot -> Verify **Post Messages** toggle is ON.

### Problem 3: Leaked Token Rotation (1 Minute Fix)
- **Emergency Step:**
  1. Open Telegram mobile -> Message `@BotFather`.
  2. Send `/revoke` -> select your bot.
  3. Copy the brand new token.
  4. Open mobile GitHub -> Repo Settings -> Secrets -> Edit `TELEGRAM_BOT_TOKEN` -> Paste new token -> Save.
  5. Open Render mobile -> Environment -> Edit `TELEGRAM_BOT_TOKEN` -> Save Changes.

---

*Your zero-cost B2B High-Ticket Contract Radar is now 100% operational, fully automated, and manageable straight from your pocket.*
