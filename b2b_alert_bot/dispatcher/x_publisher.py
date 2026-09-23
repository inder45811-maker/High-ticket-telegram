"""Autonomous X (formerly Twitter) Publisher for B2B Deal Alerts.

Publishes high-converting, 280-character deal cards to X via the official X API v2 (POST /2/tweets)
using OAuth 1.0a User Context authentication.

Features:
- Strict 280-character budget calculation with clean truncation.
- Transatlantic peak window gating (London BST & New York EDT).
- Algorithmic cooldown protection (configurable, default 2.0 hours).
- SQLite deduplication and post history logging.
"""

import os
import sys
import uuid
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from b2b_alert_bot.schema import EnrichedLead
from b2b_alert_bot.db import Database

logger = logging.getLogger("b2b_alert_bot.x_publisher")

DEFAULT_WHOP_URL = "https://whop.com/t-7e74/high-ticket-contract-alerts"
DEFAULT_COOLDOWN_HOURS = 2.0
DEFAULT_WINDOW_START_UTC = 7
DEFAULT_WINDOW_END_UTC = 21

X_TEMPLATES = [
    "ARBITRAGE_RADAR_DROP",
    "STEAL_THE_PITCH",
    "PLATFORM_TAX_ROAST",
    "SPEED_ASYMMETRY",
]


def format_x_deal_teaser(
    enriched: EnrichedLead,
    whop_url: str = DEFAULT_WHOP_URL,
    template: str = "ARBITRAGE_RADAR_DROP",
    max_chars: int = 280,
) -> str:
    """Format an EnrichedLead into a high-converting X deal alert using algorithm-optimized archetypes."""
    badge = getattr(enriched, "budget_badge", "💰 $2,000+ HIGH TICKET")
    title = getattr(enriched, "clean_title", None) or (enriched.lead.title.strip() if enriched.lead and enriched.lead.title else "Senior Specialist")
    deliverables = getattr(enriched, "scope_bullet", "Lead core technical architecture.")
    skills = getattr(enriched, "skills_bullet", "")
    winning_angle = getattr(enriched, "winning_angle", "Lead with case studies, not a generic resume.")

    if skills and len(skills) > 25:
        skills = skills[:22] + "..."
    if winning_angle and len(winning_angle) > 55:
        winning_angle = winning_angle[:52] + "..."

    def clean_title(max_len: int) -> str:
        if len(title) > max_len:
            return title[:max_len - 3].rstrip() + "..."
        return title

    if template == "STEAL_THE_PITCH":
        # Archetype 2: The Bookmark Magnet (10x Algorithm Weight!)
        lines = [
            f"How to win a {badge} contract (no resume):",
            "",
            f"🎯 {clean_title(35)}",
            f"💡 Pitch: \"{winning_angle[:42]}\"",
            "",
            "🔖 Bookmark to pitch later",
            f"ApexRadar: {whop_url}",
            "#ApexRadar #freelance"
        ]
    elif template == "PLATFORM_TAX_ROAST":
        # Archetype 3: The Platform Tax Roast (High Retweet & Quote-Tweet Viral Hook)
        lines = [
            "Upwork charges 20% platform tax.",
            "On a $10,000 deal, that's $2,000 lost.",
            "",
            "Direct contract on our radar:",
            f"💰 {badge}",
            f"🎯 {clean_title(38)}",
            "",
            f"0% fees. Apply direct:\n{whop_url}",
            "#ApexRadar #b2b"
        ]
    elif template == "SPEED_ASYMMETRY":
        # Archetype 4: The 15-Minute Rule (Urgency & Direct Apply Conversion)
        lines = [
            f"Client posted a {badge} contract.",
            "",
            "< 15 mins: ~65% interview rate",
            "> 12 hrs: < 4% (buried in bids)",
            "",
            f"🎯 {clean_title(38)}",
            f"⚡ Direct ApexRadar stream:\n{whop_url}",
            "#ApexRadar #remotework"
        ]
    else:
        # Default: ARBITRAGE_RADAR_DROP (High CTR Curiosity Drop)
        stack_part = f"\n🛠️ Stack: {skills[:20]}" if skills else ""
        lines = [
            "🚨 HIGH-TICKET DEAL DROP",
            "",
            f"💰 Budget: {badge}",
            f"🎯 Role: {clean_title(36)}{stack_part}",
            "",
            f"⚡ Direct ApexRadar stream:\n{whop_url}",
            "#ApexRadar #freelance"
        ]

    draft = "\n".join(lines)
    if len(draft) <= max_chars:
        return draft

    # Emergency title reduction if needed
    overflow = len(draft) - max_chars
    tight_title = clean_title(max(15, 35 - overflow - 3))
    if template == "STEAL_THE_PITCH":
        lines[2] = f"🎯 {tight_title}"
    elif template == "PLATFORM_TAX_ROAST":
        lines[5] = f"🎯 {tight_title}"
    elif template == "SPEED_ASYMMETRY":
        lines[5] = f"🎯 {tight_title}"
    else:
        lines[3] = f"🎯 Role: {tight_title}"

    draft = "\n".join(lines)
    if len(draft) <= max_chars:
        return draft

    return draft[:max_chars - 3] + "..."


class XAutoPublisher:
    """Autonomous X / Twitter publisher utilizing X API v2 (POST /2/tweets)."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        access_token: Optional[str] = None,
        access_token_secret: Optional[str] = None,
        webhook_url: Optional[str] = None,
        whop_url: Optional[str] = None,
        cooldown_hours: Optional[float] = None,
        window_start_utc: Optional[int] = None,
        window_end_utc: Optional[int] = None,
        enabled: Optional[bool] = None,
        dry_run: bool = False,
    ):
        self.api_key = (api_key or os.environ.get("X_API_KEY", "")).strip().strip('"').strip("'")
        self.api_secret = (api_secret or os.environ.get("X_API_SECRET", "")).strip().strip('"').strip("'")
        self.access_token = (access_token or os.environ.get("X_ACCESS_TOKEN", "")).strip().strip('"').strip("'")
        self.access_token_secret = (access_token_secret or os.environ.get("X_ACCESS_TOKEN_SECRET", "")).strip().strip('"').strip("'")
        self.webhook_url = (webhook_url or os.environ.get("X_WEBHOOK_URL", "")).strip().strip('"').strip("'")
        self.buffer_token = os.environ.get("BUFFER_ACCESS_TOKEN", "").strip().strip('"').strip("'")
        self.whop_url = (whop_url or os.environ.get("X_WHOP_URL", DEFAULT_WHOP_URL)).strip().strip('"').strip("'")
        self._cached_buffer_channel_id = None

        env_cooldown = os.environ.get("X_COOLDOWN_HOURS")
        self.cooldown_hours = (
            cooldown_hours if cooldown_hours is not None
            else (float(env_cooldown) if env_cooldown else DEFAULT_COOLDOWN_HOURS)
        )

        env_start = os.environ.get("X_WINDOW_START_UTC")
        self.window_start_utc = (
            window_start_utc if window_start_utc is not None
            else (int(env_start) if env_start else DEFAULT_WINDOW_START_UTC)
        )

        env_end = os.environ.get("X_WINDOW_END_UTC")
        self.window_end_utc = (
            window_end_utc if window_end_utc is not None
            else (int(env_end) if env_end else DEFAULT_WINDOW_END_UTC)
        )

        has_direct_creds = bool(self.api_key and self.api_secret and self.access_token and self.access_token_secret)
        has_webhook = bool(self.webhook_url)
        has_buffer = bool(self.buffer_token)
        has_creds = has_direct_creds or has_webhook or has_buffer
        env_enabled = os.environ.get("X_AUTO_PUBLISH", "false").lower() in ("true", "1", "yes")

        self.enabled = enabled if enabled is not None else (env_enabled and has_creds)
        self.dry_run = dry_run or (os.environ.get("DRY_RUN", "").lower() in ("true", "1", "yes"))

        if self.enabled and not has_creds and not self.dry_run:
            logger.warning("XAutoPublisher enabled but missing API credentials, Buffer token, or Webhook URL. Auto-publish disabled.")
            self.enabled = False

    def is_within_publish_window(self, now_dt: Optional[datetime] = None) -> tuple[bool, str]:
        """Verify current UTC time is within audience active hours."""
        now = now_dt or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        if self.window_start_utc <= self.window_end_utc:
            in_window = (self.window_start_utc <= now.hour < self.window_end_utc)
        else:
            in_window = (now.hour >= self.window_start_utc or now.hour < self.window_end_utc)

        if in_window:
            return True, f"Within active transatlantic hours ({now.strftime('%H:%M')} UTC)"

        return False, (
            f"Outside active transatlantic window ({self.window_start_utc:02d}:00-{self.window_end_utc:02d}:00 UTC). "
            f"Current: {now.strftime('%H:%M')} UTC (Overnight Dead Zone)"
        )

    def publish_tweet(self, text: str) -> Dict[str, Any]:
        """Publish a tweet to X via free Webhook bridge (Make/Zapier) or direct X API v2."""
        if self.dry_run:
            mock_id = f"tweet_{uuid.uuid4().hex[:12]}"
            logger.info("[DRY RUN] X tweet simulated: %s", mock_id)
            return {"ok": True, "dry_run": True, "tweet_id": mock_id}

        # Method A: Zero-Cost Free Buffer Integration (Official Free Partner to X)
        if self.buffer_token:
            return self._publish_via_buffer(text)

        # Method B: Zero-Cost Free Webhook Bridge (Make.com / Zapier / IFTTT)
        if self.webhook_url:
            try:
                import requests
                res = requests.post(
                    self.webhook_url,
                    json={"text": text, "message": text, "content": text},
                    headers={"Content-Type": "application/json"},
                    timeout=15,
                )
                if res.status_code in (200, 201, 202, 204):
                    hook_id = f"hook_{uuid.uuid4().hex[:10]}"
                    logger.info("Successfully dispatched tweet via Free Webhook bridge: %s", hook_id)
                    return {"ok": True, "tweet_id": hook_id, "method": "webhook"}
                else:
                    logger.warning("Webhook dispatch failed [%d]: %s", res.status_code, res.text)
                    return {"ok": False, "status_code": res.status_code, "error": res.text}
            except Exception as e:
                logger.error("Exception during Webhook publishing: %s", e)
                return {"ok": False, "error": str(e)}

        # Method C: Direct X API v2 (OAuth 1.0a)
        if not (self.api_key and self.api_secret and self.access_token and self.access_token_secret):
            return {"ok": False, "error": "Missing X API credentials, Buffer token, or Webhook URL"}

        try:
            from requests_oauthlib import OAuth1Session

            oauth = OAuth1Session(
                client_key=self.api_key,
                client_secret=self.api_secret,
                resource_owner_key=self.access_token,
                resource_owner_secret=self.access_token_secret,
            )

            res = oauth.post(
                "https://api.twitter.com/2/tweets",
                json={"text": text},
                headers={"Content-Type": "application/json"},
                timeout=15,
            )

            if res.status_code in (200, 201):
                data = res.json().get("data", {})
                tweet_id = data.get("id", "unknown")
                logger.info("Successfully published tweet to X API: %s", tweet_id)
                return {"ok": True, "tweet_id": tweet_id, "data": data}
            else:
                logger.warning("X API publish failed [%d]: %s", res.status_code, res.text)
                return {"ok": False, "status_code": res.status_code, "error": res.text}

        except Exception as e:
            logger.error("Exception during X publishing: %s", e)
            return {"ok": False, "error": str(e)}

    def _publish_via_buffer(self, text: str) -> Dict[str, Any]:
        """Publish tweet to connected X channel via Buffer free GraphQL API."""
        try:
            import requests

            headers = {
                "Authorization": f"Bearer {self.buffer_token}",
                "Content-Type": "application/json",
            }

            # 1. Fetch connected X channel id if not cached
            if not getattr(self, "_cached_buffer_channel_id", None):
                env_chan = os.environ.get("BUFFER_CHANNEL_ID")
                if env_chan:
                    self._cached_buffer_channel_id = env_chan
                else:
                    query_org = """
                    query {
                      account {
                        organizations {
                          id
                        }
                      }
                    }
                    """
                    r = requests.post("https://api.buffer.com", json={"query": query_org}, headers=headers, timeout=15)
                    if r.status_code != 200:
                        return {"ok": False, "error": f"Buffer account query failed: {r.text}"}

                    orgs = r.json().get("data", {}).get("account", {}).get("organizations", [])
                    if not orgs:
                        return {"ok": False, "error": "No Buffer organizations found"}
                    org_id = orgs[0].get("id")

                    query_chans = """
                    query Channels($orgId: String!) {
                      channels(input: { organizationId: $orgId }) {
                        id
                        service
                        name
                      }
                    }
                    """
                    r2 = requests.post(
                        "https://api.buffer.com",
                        json={"query": query_chans, "variables": {"orgId": org_id}},
                        headers=headers,
                        timeout=15,
                    )
                    chans = r2.json().get("data", {}).get("channels", [])
                    twitter_chan = None
                    for ch in chans:
                        if ch.get("service") in ("twitter", "x"):
                            twitter_chan = ch.get("id")
                            break

                    if not twitter_chan:
                        return {"ok": False, "error": "No connected X/Twitter channel found in Buffer"}
                    self._cached_buffer_channel_id = twitter_chan

            # 2. Publish post immediately
            mutation = """
            mutation CreatePost($input: CreatePostInput!) {
              createPost(input: $input) {
                ... on PostActionSuccess {
                  post {
                    id
                    status
                  }
                }
                ... on MutationError {
                  message
                }
              }
            }
            """
            vars_data = {
                "input": {
                    "channelId": self._cached_buffer_channel_id,
                    "text": text,
                    "schedulingType": "automatic",
                    "mode": "shareNow",
                }
            }
            r = requests.post(
                "https://api.buffer.com",
                json={"query": mutation, "variables": vars_data},
                headers=headers,
                timeout=15,
            )
            data = r.json().get("data", {}).get("createPost", {})
            if "post" in data:
                tweet_id = data["post"].get("id", "buf_ok")
                logger.info("Successfully published tweet via Buffer: %s", tweet_id)
                return {"ok": True, "tweet_id": tweet_id, "method": "buffer"}
            else:
                msg = data.get("message", str(data))
                logger.warning("Buffer publish failed: %s", msg)
                return {"ok": False, "error": msg}

        except Exception as e:
            logger.error("Exception during Buffer publishing: %s", e)
            return {"ok": False, "error": str(e)}

    def _select_next_template(self, db: Database) -> str:
        """Select next copywriting archetype in rotation based on historical X post count."""
        try:
            conn = db._get_connection()
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM x_posts;")
            count = cur.fetchone()[0]
            return X_TEMPLATES[count % len(X_TEMPLATES)]
        except Exception:
            return X_TEMPLATES[0]

    def maybe_publish_lead(self, enriched: EnrichedLead, db: Database) -> Optional[Dict[str, Any]]:
        """Evaluate lead and autonomously publish tweet if criteria, cooldown, and timing permit."""
        if not self.enabled:
            return None

        if not getattr(enriched, "is_high_ticket", False):
            return None

        lead_id = getattr(enriched.lead, "id", None)
        if not lead_id:
            return None

        # 1. Deduplication check
        if db.is_lead_posted_to_x(lead_id):
            logger.debug("Lead %s already published to X. Skipping.", lead_id)
            return None

        # 2. Timing window check
        in_win, reason = self.is_within_publish_window()
        if not in_win:
            logger.info("X tweet skipped: %s. Lead: %s", reason, lead_id)
            return None

        # 3. Cooldown check
        last_time = db.get_last_x_post_time()
        if last_time:
            now = datetime.now(timezone.utc)
            if last_time.tzinfo is None:
                last_time = last_time.replace(tzinfo=timezone.utc)
            elapsed_hours = (now - last_time).total_seconds() / 3600.0
            if elapsed_hours < self.cooldown_hours:
                logger.info(
                    "X cooldown active (%.2f hours elapsed < %.1f hours threshold). Skipping lead %s.",
                    elapsed_hours, self.cooldown_hours, lead_id
                )
                return None

        # 4. Autonomous Jev Multi-Template Campaign Evaluation
        candidates = {}
        for tmpl in X_TEMPLATES:
            candidates[tmpl] = format_x_deal_teaser(
                enriched,
                whop_url=self.whop_url,
                template=tmpl,
            )

        chosen_template = self._select_next_template(db)
        tweet_text = candidates[chosen_template]

        try:
            from b2b_alert_bot.jev.bridge import optimize_campaign_with_jev
            deal_info = {
                "title": enriched.lead.title if enriched.lead and enriched.lead.title else "High-Ticket Lead",
                "budget": getattr(enriched, "budget_badge", "High-Ticket")
            }
            jev_opt = optimize_campaign_with_jev(candidates, platform="x", deal_info=deal_info)
            if jev_opt and jev_opt.get("success"):
                winner_key = jev_opt.get("winner_key")
                if winner_key and winner_key in candidates:
                    chosen_template = winner_key
                    tweet_text = candidates[winner_key]
                logger.info("Jev Autonomous X Winner: %s", jev_opt.get("audit_summary"))
        except Exception as e:
            logger.debug("Jev X campaign optimization fallback: %s", e)

        logger.info("Publishing autonomous X deal alert [Jev Chosen Template: %s] for lead %s", chosen_template, lead_id)

        # 5. Dispatch
        result = self.publish_tweet(tweet_text)
        if result.get("ok"):
            tweet_id = result.get("tweet_id", "unknown")
            post_id = f"x_{uuid.uuid4().hex[:12]}"
            db.record_x_post(
                post_id=post_id,
                lead_id=lead_id,
                tweet_id=tweet_id,
                tweet_text=tweet_text,
            )
            logger.info("Successfully recorded autonomous X post %s for lead %s", tweet_id, lead_id)
            return result
        else:
            logger.warning("Failed to publish autonomous X post for lead %s: %s", lead_id, result.get("error"))
            return result
