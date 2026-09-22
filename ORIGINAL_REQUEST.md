# Original User Request

## Initial Request — 2026-09-20T18:01:23Z

Build a turnkey, automated B2B High-Ticket Contract & Lead Alert business (inspired by SolidGigs and top-earning Whop communities) that ingests public contract feeds, enriches and filters for $2,000+ opportunities using AI, dispatches formatted deal cards to a private Telegram channel, and integrates with Whop for automated recurring subscription billing.

Working directory: ~/teamwork_projects/b2b_alert_bot
Integrity mode: development

## Market Benchmarks & Reference Models
- **SolidGigs:** ~$36k/mo MRR ($435k ARR bootstrapped). Key differentiator: curated leads, 0% platform commission, saves 50+ hours/mo.
- **Remote Rocketship:** ~$6.5k/mo MRR solo-founder. Key differentiator: direct-from-source scraping ahead of aggregator boards.
- **Whop Telegram Signal Hubs:** $49–$99/mo recurring pricing. Zero-friction automated onboarding and offboarding via Whop Telegram bot.

## Requirements

### R1. Lead Ingestion & Reliable Source Connectors
Implement connectors for reliable, open-access contract feeds that do not require brittle browser automation or risk IP bans. Include:
- Public remote freelance/contract RSS feeds (e.g., We Work Remotely, RemoteOK, Jobspresso).
- Tech & agency contract portals (e.g., Hacker News "Who is hiring / Freelance" APIs, Reddit r/forhire RSS).
- Standardized data schema: title, source, client/poster, budget/rate, core tech stack, original application URL, and unique hash for deduplication.

### R2. AI Enrichment & High-Value Filtering
Process each ingested opportunity through an enrichment filter:
- Parse and validate compensation, rejecting any listing below $2,000 (or equivalent hourly rate of $50+/hr).
- Generate a 3-bullet executive deal card: (1) Scope & Deliverables, (2) Required Skills, and (3) "Winning Angle" (a 1-sentence tip on how the applicant should pitch this specific client).
- Deduplicate listings using persistent state to prevent repeated notifications.

### R3. Telegram Alert Dispatcher
Format enriched leads into clean, mobile-optimized Telegram messages featuring:
- High-visibility header with budget badge (e.g., `💰 $4,500 FIXED` or `⏱️ $75/HR`).
- Bulleted executive summary and winning angle.
- Direct, one-click apply button/link.
- Built-in dispatch queue with rate-limiting and retry logic to comply with Telegram API limits.

### R4. Whop Subscription & Webhook Access Manager
Provide a lightweight webhook service that interfaces with Whop/Stripe:
- On `membership.went_valid` (new subscriber): Generate and deliver a single-use Telegram private channel invite link.
- On `membership.went_invalid` (cancellation, dispute, or failed renewal): Automatically revoke access and kick the user from the channel.
- Handle webhook signature verification to prevent spoofed access requests.

### R5. Turnkey Store Assets & Zero-Cost Mobile Operations Playbook
Produce the complete business launch package:
- **Whop Storefront Kit:** Complete headline, feature bullets, FAQ, and 3-tier pricing strategy ($49 early-bird, $79 standard, $99 pro).
- **Zero-Cost Deployment Guide:** Step-by-step instructions to deploy the engine on a free/low-cost cloud tier (e.g., Railway, Render, or GitHub Actions cron) configurable and monitorable 100% from a mobile phone browser.
- **Organic Viral Playbook:** 10 battle-tested "Deal Teaser" post templates for LinkedIn, X, and TikTok/Reels designed to convert audience views into paid subscribers without ad spend.

## Acceptance Criteria

### Ingestion & Filtering Engine
- [ ] Connectors successfully parse sample RSS and API feeds into the standardized lead schema.
- [ ] Budget filter discards items under $2,000 and retains items at or above $2,000 with 100% accuracy on test fixtures.
- [ ] Deduplication engine prevents duplicate records from triggering alerts across consecutive runs.

### Enrichment & Dispatch
- [ ] Deal card generator produces required 3-bullet format with budget badge and application link.
- [ ] Bot dispatcher successfully delivers formatted messages in dry-run/mock mode without raising Telegram API errors.

### Subscription & Webhook Handling
- [ ] Webhook receiver verifies incoming signatures and responds with HTTP 200 for valid events.
- [ ] Mock `went_valid` event triggers single-use invite generation.
- [ ] Mock `went_invalid` event triggers membership revocation logic.

### Business & Operations Deliverables
- [ ] `DEPLOYMENT.md` provides a verified, phone-friendly deployment checklist requiring under 15 minutes to configure.
- [ ] `STOREFRONT_COPY.md` contains ready-to-paste sales copy and pricing tiers for Whop.
- [ ] `MARKETING_PLAYBOOK.md` contains 10 organic deal-teaser templates with image framing instructions.
