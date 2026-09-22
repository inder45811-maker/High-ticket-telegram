# Project: B2B High-Ticket Contract & Lead Alert System

## Architecture
Modular Python asynchronous service architecture with zero mandatory external SaaS dependencies for local/offline testing:
- `b2b_alert_bot.ingestion`: Ingests leads from 5 sources (WWR RSS, RemoteOK JSON, Jobspresso RSS, HN Algolia API, Reddit r/forhire Atom), normalizes to standard schema, deduplicates via SHA-256 in SQLite WAL database.
- `b2b_alert_bot.enrichment`: Evaluates compensation against >= $2,000 fixed / monthly or >= $50/hr hourly, suppresses funding false positives, produces 3-bullet deal cards (Scope, Skills, Winning Angle) with deterministic NLP fallback.
- `b2b_alert_bot.dispatcher`: Formats mobile-optimized HTML Telegram deal cards with budget badge and apply button, rate-limits with token bucket, handles retries with exponential backoff, supports offline dry-run mode.
- `b2b_alert_bot.webhook`: Lightweight HTTP webhook receiver (FastAPI/aiohttp) for Whop events (`membership.went_valid`, `membership.went_invalid`), verifies HMAC-SHA256 signatures, issues single-use Telegram invites, manages member revocation via ban/unban kick.
- `b2b_alert_bot.cli`: CLI entrypoint to run ingestion poll, single alert test, webhook daemon, and full automated loop.
- `docs`: Turnkey store assets (`STOREFRONT_COPY.md`), deployment guide (`DEPLOYMENT.md`), and viral marketing playbook (`MARKETING_PLAYBOOK.md`).

## Code Layout
```
/root/teamwork_projects/b2b_alert_bot/
├── b2b_alert_bot/
│   ├── __init__.py
│   ├── schema.py              # Normalized Lead, Budget, Enrichment schemas
│   ├── db.py                  # SQLite WAL storage, schema, dedup hashing
│   ├── ingestion/
│   │   ├── __init__.py
│   │   ├── base.py            # BaseConnector with resilient HTTP client & retries
│   │   ├── weworkremotely.py  # WWR 7 category RSS connector
│   │   ├── remoteok.py        # RemoteOK JSON connector (element 0 skip)
│   │   ├── jobspresso.py      # Jobspresso /jobs/feed/ RSS connector
│   │   ├── hackernews.py      # HN Algolia search_by_date connector
│   │   └── reddit.py          # Reddit r/forhire Atom connector with browser UA
│   ├── enrichment/
│   │   ├── __init__.py
│   │   ├── compensation.py    # Regex & heuristic parser for $2k+ / $50+/hr
│   │   ├── funding_filter.py  # Suppression window for funding rounds ($2M seed)
│   │   ├── deal_card.py       # 3-bullet deal card generator (LLM & NLP fallback)
│   │   └── engine.py          # Unified enrichment pipeline
│   ├── dispatcher/
│   │   ├── __init__.py
│   │   ├── formatter.py       # HTML mobile deal card & budget badge formatter
│   │   ├── rate_limiter.py    # Token bucket rate limiter (1 msg/s chat, 30/s global)
│   │   └── telegram_bot.py    # Telegram API client with dry-run mode & retry queue
│   ├── webhook/
│   │   ├── __init__.py
│   │   ├── security.py        # Whop HMAC-SHA256 signature verification
│   │   ├── handler.py         # membership.went_valid & went_invalid handlers
│   │   └── server.py          # Lightweight webhook HTTP server
│   └── main.py                # Unified CLI runner & scheduled daemon
├── tests/
│   ├── test_ingestion.py      # Unit & fixture tests for R1 connectors
│   ├── test_enrichment.py     # Unit & fixture tests for R2 parser & deal cards
│   ├── test_dispatcher.py     # Unit & fixture tests for R3 Telegram formatting & queue
│   ├── test_webhook.py        # Unit & fixture tests for R4 Whop HMAC & member access
│   ├── test_e2e.py            # End-to-end multi-tier integration test suite
│   └── fixtures/              # Offline mock XML, JSON, and webhook payloads
├── STOREFRONT_COPY.md         # Whop storefront copy & 3-tier pricing strategy
├── DEPLOYMENT.md              # Phone-friendly zero-cost cloud deployment guide
├── MARKETING_PLAYBOOK.md      # 10 organic viral deal-teaser templates
├── requirements.txt           # Python dependencies
└── README.md                  # Project overview and developer quickstart
```

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| 1 | WWR Connector | Parse 7 category RSS feeds into standardized leads | M1 | survey_1 |
| 2 | RemoteOK Connector | Parse RemoteOK JSON API, bypass legal element 0, extract salary | M1 | survey_1 |
| 3 | Jobspresso Connector | Parse /jobs/feed/ RSS, split company/location, extract description | M1 | survey_1 |
| 4 | Hacker News Connector | Query Algolia search_by_date for monthly hiring/freelance threads | M1 | survey_1 |
| 5 | Reddit r/forhire Connector | Parse Atom feed with desktop UA, filter [Hiring] posts | M1 | survey_1 |
| 6 | Standardized Lead Schema | Normalized dataclass/dict with title, client, budget, tech, url | M1 | survey_1 |
| 7 | Deduplication Store | SQLite WAL store with deterministic SHA-256 hash deduplication | M1 | survey_1 |
| 8 | Compensation Parser | Multi-pattern regex for fixed, hourly, monthly, annual rates | M2 | survey_2 |
| 9 | High-Value Filter | Validate compensation >= $2,000 or >= $50/hr with currency normalization | M2 | survey_2 |
| 10 | Funding Context Filter | Suppress non-job funding amounts ($2M Seed round) | M2 | survey_2 |
| 11 | Deal Card Generator | 3-bullet deal cards (Scope, Skills, Winning Angle) with NLP fallback | M2 | survey_2 |
| 12 | Persistent Alert State | Track processed/alerted status in database to avoid duplicate notifications | M2 | survey_2 |
| 13 | Mobile HTML Formatter | Crash-resistant HTML message layout with budget badge & emoji | M3 | survey_3 |
| 14 | One-Click Apply Button | InlineKeyboardMarkup with direct URL button | M3 | survey_3 |
| 15 | Rate-Limited Dispatch Queue | Token bucket (1 msg/s chat) with retry on HTTP 429 retry_after | M3 | survey_3 |
| 16 | Dry-Run Dispatcher Mode | Full mock mode for local testing without Telegram bot token | M3 | survey_3 |
| 17 | Whop HMAC Verification | HMAC-SHA256 signature verification for webhook payloads | M4 | survey_2 |
| 18 | Single-Use Invite Link | Telegram createChatInviteLink with member_limit=1 on went_valid | M4 | survey_2 |
| 19 | Access Revocation Workflow | Telegram banChatMember + unbanChatMember kick on went_invalid | M4 | survey_2 |
| 20 | Webhook HTTP Service | Lightweight endpoint POST /webhooks/whop responding 200 OK | M4 | survey_2 |
| 21 | Whop Storefront Kit | STOREFRONT_COPY.md with 3-tier pricing ($49/$79/$99), FAQ, guarantee | M5 | survey_3 |
| 22 | Zero-Cost Deployment Guide | DEPLOYMENT.md with 15-min phone-friendly cloud setup | M5 | survey_3 |
| 23 | Organic Marketing Playbook | MARKETING_PLAYBOOK.md with 10 viral deal-teaser templates | M5 | survey_3 |
| 24 | E2E Testing Suite | Tiers 1-4 opaque-box test suite verifying all R1-R5 criteria | E2E-Track | survey_1,2,3 |
| 25 | Adversarial Coverage Hardening | Tier 5 adversarial stress testing and edge-case coverage | M6 | survey_1,2,3 |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| E2E | E2E Testing Track | Test harness, mock servers, offline fixtures, Tiers 1-4 tests, TEST_READY.md | none | IN_PROGRESS |
| M1 | Lead Ingestion & Connectors | Connectors for 5 feeds, normalized schema, SQLite deduplication store | none | IN_PROGRESS |
| M2 | AI Enrichment & High-Value Filter | Compensation parsing, $2k+ / $50+/hr filter, 3-bullet deal cards, NLP fallback | M1 | PLANNED |
| M3 | Telegram Alert Dispatcher | Mobile HTML formatter, budget badges, apply button, rate limiter, dry-run mode | M2 | PLANNED |
| M4 | Whop Subscription & Webhooks | HMAC verification, went_valid invite link, went_invalid revocation kick | M3 | PLANNED |
| M5 | Turnkey Business Assets | STOREFRONT_COPY.md, DEPLOYMENT.md, MARKETING_PLAYBOOK.md | none | IN_PROGRESS |
| M6 | Final Verification & Hardening | 100% E2E test pass (Tiers 1-4) + Tier 5 adversarial hardening + Forensic Audit | E2E, M1-M5 | PLANNED |

## Interface Contracts

### `Lead` Data Contract (M1 -> M2)
```python
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
from datetime import datetime

@dataclass
class Lead:
    id: str                         # SHA-256 hash of canonical URL
    title: str                      # Raw job title
    source: str                     # "weworkremotely", "remoteok", "jobspresso", "hackernews", "reddit"
    client: str                     # Company or hiring entity
    url: str                        # Canonical apply URL
    raw_compensation: str           # Raw compensation string or snippet
    description: str                # Full text or HTML snippet
    published_at: Optional[str]     # ISO timestamp
    core_tech_stack: List[str] = field(default_factory=list)
    raw_metadata: Dict[str, Any] = field(default_factory=dict)
```

### `EnrichedLead` Data Contract (M2 -> M3)
```python
@dataclass
class EnrichedLead:
    lead: Lead
    is_high_ticket: bool            # True if comp >= $2,000 fixed/mo or >= $50/hr
    rate_type: str                  # "fixed", "hourly", "monthly", "annual", "unknown"
    min_amount: float               # Normalized USD amount
    max_amount: float               # Normalized USD amount
    currency: str                   # "USD", "EUR", "GBP", etc.
    budget_badge: str               # e.g., "💰 $4,500 FIXED", "⏱️ $75/HR"
    scope_bullet: str               # Deliverables & scope summary
    skills_bullet: str              # Key required tech & skills
    winning_angle: str              # Actionable 1-sentence application tip
```

### `TelegramDispatcher` Contract (M3)
```python
class TelegramDispatcher:
    def format_deal_card(self, lead: EnrichedLead) -> str: ...
    def create_apply_markup(self, url: str) -> dict: ...
    async def dispatch(self, lead: EnrichedLead) -> bool: ...
```

### `WhopWebhook` Contract (M4)
```python
class WhopWebhookHandler:
    def verify_signature(self, payload: bytes, signature: str, timestamp: str = "") -> bool: ...
    async def handle_event(self, event_type: str, data: dict) -> dict: ...
```
