# ApexRadar — Real-Time B2B Contract Intelligence

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Architecture: Modular Services](https://img.shields.io/badge/architecture-modular_async-green.svg)](#architecture)
[![Test Suite: 100% Passing](https://img.shields.io/badge/tests-210%20passed-brightgreen.svg)](#testing--verification)
[![License: Proprietary](https://img.shields.io/badge/license-commercial-purple.svg)](#)

> **Turnkey, automated B2B high-ticket contract radar and recurring subscription business.**  
> Continuously monitors open web feeds, filters opportunities with compensation $\ge \$2,000$ fixed or $\ge \$50$/hr, generates 3-bullet executive deal cards with winning pitch angles, dispatches mobile-optimized alerts to private Telegram channels, and automates member onboarding/offboarding via Whop webhooks.

---

## 📋 Table of Contents
1. [Market Benchmark & Reference Models](#market-benchmark--reference-models)
2. [System Overview & Architecture](#system-overview--architecture)
3. [Requirements Mapping (R1–R5)](#requirements-mapping-r1r5)
4. [Code Layout](#code-layout)
5. [Quickstart Guide](#quickstart-guide)
6. [CLI & Daemon Reference](#cli--daemon-reference)
7. [Environment Variables](#environment-variables)
8. [Testing & Verification](#testing--verification)
9. [Turnkey Business Launch Assets](#turnkey-business-launch-assets)

---

## 💼 Market Benchmark & Reference Models

| Reference Model | Monthly Revenue | Pricing Model | Key Differentiator |
| :--- | :--- | :--- | :--- |
| **SolidGigs** | $\sim$\$36,000/mo MRR | \$39–\$49/month | Curated leads, 0% platform commission, saves 50+ hours/month |
| **Remote Rocketship** | $\sim$\$6,500/mo MRR | \$19–\$29/month | Direct-from-source indexing ahead of aggregator job boards |
| **Whop Telegram Signal Hubs** | \$10k–\$50k/mo MRR | \$49–\$99/month | Zero-friction automated onboarding and offboarding via bot |

This project provides an end-to-end, self-hosted implementation replicating these economics with **zero mandatory external SaaS dependencies** for testing and execution.

---

## 🏗️ System Overview & Architecture

```
                                  B2B CONTRACT RADAR ARCHITECTURE
                                  
  [ Ingestion Sources ]
  ├── We Work Remotely (7 RSS categories)
  ├── RemoteOK (JSON REST API)
  ├── Jobspresso (/jobs/feed/ RSS)
  ├── Hacker News (Algolia API Hiring threads)
  └── Reddit r/forhire (Desktop-UA Atom feed)
               │
               ▼
  [ b2b_alert_bot.ingestion ]
  └── BaseConnector (desktop UA, conditional GET, exponential backoff, jitter)
               │
               ▼
  [ b2b_alert_bot.db ] ──(Deduplication Engine)
  └── SQLite WAL Mode + SHA-256 Canonical URL Hashing + LRU Cache
               │
               ▼
  [ b2b_alert_bot.enrichment ]
  ├── CompensationParser (regex heuristics, FX rates: EUR, GBP, CAD, AUD)
  ├── High-Value Filter (enforces >= $2,000 fixed/mo or >= $50/hr)
  ├── FundingFilter (suppresses $2M seed/funding round false positives)
  └── DealCardGenerator (3 bullets: Scope, Skills, Winning Angle + NLP fallback)
               │
               ▼
  [ b2b_alert_bot.dispatcher ]
  ├── TelegramFormatter (mobile HTML layout, budget badge, apply button)
  ├── TelegramRateLimiter (token bucket: 1 msg/s chat, 30 msgs/s global)
  └── TelegramDispatcher (dry-run simulation, 429 retry_after & 5xx backoff)
               │
               ▼
  [ Private Telegram VIP Channel ]
```

### Integrated Webhook Billing Engine
```
  [ Whop Marketplace / Stripe ]
               │  HMAC-SHA256 Signed Webhook Payload
               ▼
  [ b2b_alert_bot.webhook ]
  ├── WhopWebhookServer (ThreadingHTTPServer: POST /webhooks/whop, GET /health, GET /ready)
  ├── Security Layer (HMAC-SHA256 constant-time check, replay protection, whsec_ prefix)
  └── WhopWebhookHandler
        ├── On membership.went_valid:
        │     └── Telegram createChatInviteLink (member_limit=1, expire_date=72h)
        └── On membership.went_invalid:
              └── Telegram banChatMember + unbanChatMember (neutral reset ejection)
```

---

## 📑 Requirements Mapping (R1–R5)

| Req | Description | Implementation Module | Primary Deliverables |
| :--- | :--- | :--- | :--- |
| **R1** | **Lead Ingestion & Connectors** | `b2b_alert_bot.ingestion`<br>`b2b_alert_bot.db` | 5 source connectors (WWR, RemoteOK, Jobspresso, HN, Reddit), `Lead` dataclass, SQLite WAL store, URL canonicalizer, SHA-256 deduplication. |
| **R2** | **AI Enrichment & High-Value Filter** | `b2b_alert_bot.enrichment` | Compensation parser ($\ge \$2\text{k}$ fixed / $\ge \$50$/hr), funding round filter, 3-bullet deal cards (Scope, Skills, Winning Angle), deterministic NLP classifier. |
| **R3** | **Telegram Alert Dispatcher** | `b2b_alert_bot.dispatcher` | Mobile HTML formatter, high-visibility badges (`💰 $4,500 FIXED`, `⏱️ $75/HR`), 1-click apply inline keyboard, token bucket rate limiter, dry-run simulation mode. |
| **R4** | **Whop Subscription & Webhooks** | `b2b_alert_bot.webhook` | HMAC-SHA256 signature verification, `membership.went_valid` single-use invite generator, `membership.went_invalid` member revocation kick, zero-dependency HTTP server. |
| **R5** | **Turnkey Business Assets** | Docs & Guides | `STOREFRONT_COPY.md` (3-tier Whop pricing), `DEPLOYMENT.md` (15-min phone cloud deployment), `MARKETING_PLAYBOOK.md` (10 organic deal teaser templates). |

---

## 📁 Code Layout

```
/root/teamwork_projects/b2b_alert_bot/
├── b2b_alert_bot/
│   ├── __init__.py            # Package root exports
│   ├── schema.py              # Normalized Lead and EnrichedLead schemas
│   ├── db.py                  # SQLite WAL persistence & SHA-256 deduplication
│   ├── ingestion/             # R1: Feed connectors
│   │   ├── base.py            # BaseConnector with HTTP retries & conditional GET
│   │   ├── weworkremotely.py  # WWR 7-category RSS connector
│   │   ├── remoteok.py        # RemoteOK REST API connector (disclaimer bypass)
│   │   ├── jobspresso.py      # Jobspresso RSS feed connector
│   │   ├── hackernews.py      # Hacker News Algolia monthly hiring connector
│   │   └── reddit.py          # Reddit r/forhire Atom connector (desktop UA)
│   ├── enrichment/            # R2: Filtering & deal card generation
│   │   ├── compensation.py    # Compensation parser & FX normalizer
│   │   ├── funding_filter.py  # 50-character funding exclusion window
│   │   ├── deal_card.py       # 3-bullet deal cards & NLP archetype classifier
│   │   └── engine.py          # Unified EnrichmentEngine pipeline
│   ├── dispatcher/            # R3: Telegram formatting & alert dispatch
│   │   ├── formatter.py       # Mobile HTML message & budget badge formatter
│   │   ├── rate_limiter.py    # Token bucket rate limiter (1 msg/s chat, 30/s global)
│   │   └── telegram_bot.py    # TelegramDispatcher with offline dry-run mode
│   ├── webhook/               # R4: Whop webhook lifecycle & access manager
│   │   ├── security.py        # HMAC-SHA256 signature verifier (standard & legacy)
│   │   ├── handler.py         # went_valid invite link & went_invalid revocation
│   │   └── server.py          # Lightweight HTTP server (/webhooks/whop, /health, /ready)
│   └── main.py                # Unified CLI runner & scheduled daemon service
├── tests/
│   ├── test_ingestion.py      # 31 unit tests for R1 connectors
│   ├── test_enrichment.py     # 46 unit tests for R2 parser & deal cards
│   ├── test_dispatcher.py     # 20 unit tests for R3 Telegram dispatch & rate limits
│   ├── test_webhook.py        # 30 unit tests for R4 Whop HMAC & access manager
│   ├── test_cli.py            # 24 unit tests for CLI commands, daemon & status
│   ├── test_e2e.py            # 59 multi-tier integration tests (Tiers 1–4)
│   └── fixtures/              # Offline mock XML, JSON, and Atom test payloads
├── STOREFRONT_COPY.md         # R5: Ready-to-paste Whop sales copy & 3-tier pricing
├── DEPLOYMENT.md              # R5: 15-minute phone-friendly cloud deployment guide
├── MARKETING_PLAYBOOK.md      # R5: 10 organic viral deal-teaser templates
├── requirements.txt           # Minimal production dependencies
└── README.md                  # Complete system documentation
```

---

## 🚀 Quickstart Guide

### 1. Prerequisites
- Python 3.10+
- `pip` package manager

### 2. Installation
```bash
# Clone the repository
git clone https://github.com/your-username/b2b_alert_bot.git
cd b2b_alert_bot

# Install requirements
pip install -r requirements.txt
```

### 3. Immediate 1-Minute Offline Dry-Run Verification
Execute a complete poll cycle using built-in offline fixtures without needing Telegram tokens or external network access:
```bash
python3 -m b2b_alert_bot.main poll --dry-run --fixtures-dir tests/fixtures
```
You will see all 5 sources ingested, high-ticket opportunities evaluated, deal cards formatted, and synthetic alerts dispatched:
```
============================================================
           B2B ALERT BOT — POLL CYCLE COMPLETE
============================================================
  Sources Queried:       5
  Total Ingested (New):  23
  Duplicates Skipped:    0
  High-Ticket Filtered:  15 (>= $2,000 fixed/mo or >= $50/hr)
  Below Threshold:       8
  Alerts Dispatched:     15 (Mode: DRY RUN)
============================================================
```

### 4. Check System Database Status
```bash
python3 -m b2b_alert_bot.main status
```

---

## 💻 CLI & Daemon Reference

The application entrypoint is `b2b_alert_bot/main.py`. It supports subcommands as well as environment-friendly shortcut flags.

### 1. `poll` — Single Poll & Dispatch Cycle
Ingests from connectors, deduplicates in SQLite, enriches leads, and dispatches deal alerts.
```bash
# Live run with default thresholds ($2,000 fixed or $50/hr)
python3 -m b2b_alert_bot.main poll

# Dry-run mode using offline fixtures
python3 -m b2b_alert_bot.main poll --dry-run --fixtures-dir tests/fixtures

# Custom budget thresholds ($5,000 fixed / $75/hr) and source whitelist
python3 -m b2b_alert_bot.main poll --min-fixed 5000 --min-hourly 75 --sources weworkremotely remoteok

# Alternate shortcut flag
python3 -m b2b_alert_bot.main --cron
```

**Options for `poll`:**
- `--dry-run`: Run dispatcher in simulation mode without contacting Telegram API.
- `--db <path>`: SQLite leads database filepath (default: `leads.db` or `DATA_DIR/leads.db`).
- `--chat-id <id>`: Telegram channel/group chat ID (default: `TELEGRAM_CHAT_ID` env var).
- `--bot-token <token>`: Telegram Bot token (default: `TELEGRAM_BOT_TOKEN` env var).
- `--min-fixed <float>`: Minimum fixed contract budget in USD (default: `2000.0`).
- `--min-hourly <float>`: Minimum hourly contract rate in USD (default: `50.0`).
- `--fixtures-dir <path>`: Directory containing sample XML/JSON/Atom files for offline polling.
- `--sources <name...>`: Whitelist specific sources (`weworkremotely`, `remoteok`, `jobspresso`, `hackernews`, `reddit`).

---

### 2. `daemon` — Scheduled Periodic Polling Service
Runs the poll cycle on a recurring schedule with signal handling (`SIGINT`, `SIGTERM`) for graceful termination.
```bash
# Run continuous daemon polling every 30 minutes (default)
python3 -m b2b_alert_bot.main daemon

# Custom 15-minute polling interval
python3 -m b2b_alert_bot.main daemon --interval 15

# Daemon with dry-run mode
python3 -m b2b_alert_bot.main daemon --interval 30 --dry-run
```

**Options for `daemon`:**
- `--interval <minutes>`: Polling interval in minutes (default: `30.0`).
- `--interval-seconds <seconds>`: Explicit polling interval in seconds.
- All options from `poll` (`--db`, `--min-fixed`, `--min-hourly`, `--dry-run`, etc.).
- `--max-cycles <int>`: Terminate after running $N$ cycles (useful in automated testing).

---

### 3. `webhook` — Whop Webhook HTTP Receiver
Runs the standalone HTTP webhook server to handle member access provisioning and revocation.
```bash
# Start webhook server on port 8080
python3 -m b2b_alert_bot.main webhook --port 8080

# Bind specific host and secret
python3 -m b2b_alert_bot.main webhook --host 0.0.0.0 --port 10000 --secret whsec_your_secret

# Shortcut flag
python3 -m b2b_alert_bot.main --server --port 8080
```

**Endpoints provided:**
- `POST /webhooks/whop`: Handles `membership.went_valid` and `membership.went_invalid` with HMAC-SHA256 signature verification.
- `GET /health`: Health probe returning `{"status": "healthy", ...}` (HTTP 200).
- `GET /ready`: Readiness probe verifying database connectivity (HTTP 200).

---

### 4. `serve` / `all` — Unified Concurrent Server & Daemon
Runs both the Whop webhook HTTP server and the scheduled polling loop concurrently in a single process.
```bash
python3 -m b2b_alert_bot.main serve --port 8080 --interval 30
```
On receiving `SIGINT` (Ctrl+C) or `SIGTERM`, both the polling daemon and HTTP server shut down cleanly.

---

### 5. `status` — Database & Operational Metrics
Prints real-time statistics on leads ingested, high-ticket leads identified, alerts dispatched, and active subscribers.
```bash
# Formatted human-readable output
python3 -m b2b_alert_bot.main status

# Machine-readable JSON output (ideal for monitoring scripts)
python3 -m b2b_alert_bot.main status --json
```

---

## 🔑 Environment Variables

All settings can be configured via environment variables or CLI flags:

| Environment Variable | Required? | Default | Valid Example | Description |
| :--- | :---: | :---: | :--- | :--- |
| `TELEGRAM_BOT_TOKEN` | Yes (or Dry-Run) | `""` | `7198273645:AAH_XYZ...` | Bot token from `@BotFather`. Required for live Telegram messaging and invite creation. |
| `TELEGRAM_CHAT_ID` | Yes | `""` | `-1002345678901` | Private channel ID for alert posts. Begins with `-100`. |
| `TELEGRAM_CHANNEL_ID` | No | `TELEGRAM_CHAT_ID` | `-1002345678901` | Channel ID for single-use invite links and member management. |
| `WHOP_WEBHOOK_SECRET` | For Webhooks | `""` | `whsec_991a82f...` | Secret key from Whop Developer Dashboard for HMAC-SHA256 signature validation. |
| `MIN_BUDGET_USD` | No | `2000` | `2500` | Minimum fixed contract budget in USD. |
| `MIN_HOURLY_USD` | No | `50` | `60` | Minimum hourly contract rate in USD. |
| `DATA_DIR` | No | `.` | `./data` | Directory where SQLite databases (`leads.db`, `subscribers.db`) are stored. |
| `PORT` | For Server | `8080` | `10000` | Port for the webhook receiver HTTP server. |
| `HOST` | For Server | `0.0.0.0` | `0.0.0.0` | Binding host address. |
| `POLL_INTERVAL_MINUTES` | No | `30` | `15` | Polling frequency for daemon mode. |
| `DRY_RUN` | No | `false` | `true` | When `true`, simulates Telegram dispatching offline without API calls. |
| `LOG_LEVEL` | No | `INFO` | `DEBUG` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |

---

## 🧪 Testing & Verification

The project includes an exhaustive, automated multi-tier test suite covering **100% of functional requirements** with offline test fixtures:

### Run Complete Test Suite
```bash
python3 -m pytest tests/ -v
```
*Expected Result: 210 passed in ~6 seconds.*

### Test Suite Breakdown
```bash
# 1. CLI & Daemon tests (24 tests)
python3 -m pytest tests/test_cli.py -v

# 2. Feed Ingestion & Connector tests (31 tests)
python3 -m pytest tests/test_ingestion.py -v

# 3. AI Compensation & Deal Card Enrichment tests (46 tests)
python3 -m pytest tests/test_enrichment.py -v

# 4. Telegram Formatter, Rate Limiting & Dispatcher tests (20 tests)
python3 -m pytest tests/test_dispatcher.py -v

# 5. Whop HMAC & Webhook Lifecycle tests (30 tests)
python3 -m pytest tests/test_webhook.py -v

# 6. Multi-Tier End-to-End Integration tests (59 tests across Tiers 1-4)
python3 -m pytest tests/test_e2e.py -v
```

---

## 📦 Turnkey Business Launch Assets

The repository contains the complete business operations package to launch, market, and monetize this system:

1. **[Whop Storefront Sales Copy (`STOREFRONT_COPY.md`)](STOREFRONT_COPY.md):**
   - High-converting sales headline, sub-headline, and pain-point positioning.
   - 3-tier pricing strategy: **\$49 Early Bird**, **\$79 Standard VIP**, **\$99 Pro Arbitrage**.
   - Feature bullet list, Objection-handling FAQ, and 30-Day Risk-Free Guarantee copy.

2. **[Zero-Cost Cloud Deployment Guide (`DEPLOYMENT.md`)](DEPLOYMENT.md):**
   - 15-minute phone-friendly cloud setup guide (iOS Safari or Android Chrome).
   - Ingestion cron via GitHub Actions (free tier: 2,000 minutes/month).
   - Webhook server hosting via Render.com / Railway.app free tier (\$0.00/mo).
   - Mobile monitoring and health check procedures.

3. **[Organic Viral Marketing Playbook (`MARKETING_PLAYBOOK.md`)](MARKETING_PLAYBOOK.md):**
   - 10 battle-tested organic "Deal Teaser" post templates for LinkedIn, X (Twitter), and TikTok/Reels.
   - Screen framing and visual blur instructions to drive subscriber curiosity.
   - Direct Call-to-Action hooks designed to convert views into paid recurring Whop memberships without ad spend.
