"""Autonomous LinkedIn Publisher for B2B Deal Alerts with Dynamic Template Rotation.

Learned Optimization Architecture:
- Rotates between 4 psychological copywriting archetypes:
  1. CURIOSITY_DEAL_TEASER: High-budget deal drop with redacted client and 3-bullet intelligence.
  2. PLATFORM_TAX_CONTRAST: Industry contrast attacking Upwork's 20% cut vs 0% direct feeds.
  3. SPEED_ADVANTAGE_RULE: Speed asymmetry breakdown (15-min law vs proposal burial).
  4. WINNING_ANGLE_TEARDOWN: High-value consultant pitch tear-down for technical buyers.
- Enforces algorithmic cooldown (default 4 hours) to prevent audience fatigue and maximize reach.
- Records template history to guarantee diverse, non-repetitive feed distribution.
"""

import os
import sys
import json
import uuid
import logging
import subprocess
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

from b2b_alert_bot.schema import EnrichedLead
from b2b_alert_bot.db import Database

def _load_dotenv() -> None:
    for env_path in [
        os.path.join(os.getcwd(), ".env"),
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env"),
        os.path.expanduser("~/.env"),
    ]:
        if os.path.isfile(env_path):
            try:
                with open(env_path, "r", encoding="utf-8") as f:
                    for line in f:
                        clean_line = line.strip()
                        if clean_line and not clean_line.startswith("#") and "=" in clean_line:
                            k, v = clean_line.split("=", 1)
                            k = k.strip()
                            v = v.strip().strip('"').strip("'")
                            if k and k not in os.environ:
                                os.environ[k] = v
            except Exception:
                pass
            break

_load_dotenv()

logger = logging.getLogger("b2b_alert_bot.linkedin")

DEFAULT_WHOP_URL = "https://whop.com/t-7e74/high-ticket-contract-alerts"
DEFAULT_COOLDOWN_HOURS = 4.0

# Transatlantic Timing Optimization (Targeting London, UK & New York, US High-Ticket B2B):
# - Default active window: 07:00 UTC (08:00 BST London start) to 21:00 UTC (17:00 EDT New York close)
# - Dead zone (21:00 - 07:00 UTC): Low engagement velocity; suppressed by LinkedIn distribution algorithm.
DEFAULT_WINDOW_START_UTC = 7
DEFAULT_WINDOW_END_UTC = 21

# Prime Transatlantic High-Converting Windows (UTC):
# - Window 1 (Golden Overlap): 11:00 - 14:30 UTC (London lunch / NY morning executive review)
# - Window 2 (Global Midday): 16:00 - 18:30 UTC (NY lunch / London commute / SF morning start)

TEMPLATES = [
    "CURIOSITY_DEAL_TEASER",
    "PLATFORM_TAX_CONTRAST",
    "SPEED_ADVANTAGE_RULE",
    "WINNING_ANGLE_TEARDOWN",
]


def format_deal_teaser(
    enriched: EnrichedLead,
    whop_url: str = DEFAULT_WHOP_URL,
    template: str = "CURIOSITY_DEAL_TEASER",
    recent_deals: Optional[List[str]] = None,
) -> str:
    """Format an EnrichedLead using one of 4 conversion-optimized LinkedIn archetypes."""
    badge = getattr(enriched, "budget_badge", "💰 $2,000+ HIGH TICKET")
    title = enriched.lead.title.strip() if enriched.lead and enriched.lead.title else "Senior Contract Specialist"
    deliverables = getattr(enriched, "scope_bullet", "Deliver core technical architecture and milestones.")
    skills = getattr(enriched, "skills_bullet", "Full Stack, API Architecture")
    winning_angle = getattr(enriched, "winning_angle", "Lead with concrete case studies rather than a generic resume.")

    if template == "PLATFORM_TAX_CONTRAST":
        if recent_deals and len(recent_deals) > 0:
            deals_block = "\n".join(f"• {d}" for d in recent_deals[:3])
        else:
            deals_block = f"• {badge}: {title}"

        return f"""The economics of freelance bidding sites are officially broken.

If you land a $10,000 contract on Upwork:
• Upwork takes $1,000–$2,000 in platform fees.
• You compete with 80+ race-to-the-bottom proposals.
• You spend 10+ hours a week buying "connects."

High-ticket clients aren't posting on bidding boards anymore. They post directly on private engineering portals and remote feeds where they can hire senior talent directly.

Here is what just landed on our radar:
{deals_block}

Direct client links. Direct invoicing. 0% platform tax.

Grab your access link to our 24/7 Telegram stream:
{whop_url}"""

    elif template == "SPEED_ADVANTAGE_RULE":
        return f"""A client posted a {badge} contract for "{title}" minutes ago.

While 95% of freelancers haven't even seen the post yet, the first 3 qualified applicants will book calls before lunch.

Why speed is your only unfair advantage in 2026:
• Apply in < 15 mins: ~65% interview rate
• Apply in 2 hours: ~28% interview rate
• Apply after 12 hours: < 4% interview rate (buried under 100+ proposals)

If you're still manually refreshing 10 job boards once a day, you have already lost.

We automated the entire radar. The moment a verified $2k+ fixed or $50+/hr contract goes live, our private bot pings with direct client links and pitch intelligence.

Automate your deal flow:
{whop_url}"""

    elif template == "WINNING_ANGLE_TEARDOWN":
        return f"""How to win a {badge} contract without sending a generic 4-paragraph resume:

Opportunity on our radar:
🎯 Role: {title}
🏢 Client: [REDACTED — Verified Private Tech Feed]
📋 Key Deliverables: {deliverables}
🛠️ Required Stack: {skills}

💡 The Winning Pitch Angle:
"{winning_angle}"

Founders and CTOs don't read 100 proposals. They read the first 5 qualified messages that address their specific architectural risk.

Our private Telegram bot streams 10-20 verified high-ticket contracts every week with pre-computed winning pitch angles.

Join the private stream here:
{whop_url}"""

    else:
        # Default: CURIOSITY_DEAL_TEASER
        return f"""🚨 NEW HIGH-TICKET CONTRACT RADAR ALERT

💰 Budget: {badge}
🎯 Role: {title}
🏢 Client: [REDACTED — Verified Private Tech Feed]

📋 Project Scope & Intelligence:
• Deliverables: {deliverables}
• Required Skills: {skills}
• Winning Angle: {winning_angle}

⚡ Why speed is your only unfair advantage in 2026:
Contracts on public bidding boards receive 100+ proposals within 12 hours. Our automated radar streams verified $2k+ fixed and $50+/hr opportunities directly to your Telegram within 3 minutes of going live — with direct client links and 0% platform tax.

Unlock the direct client apply link & 24/7 VIP alerts here:
{whop_url}"""


class LinkedInAutoPublisher:
    """Autonomous publisher communicating with LinkedIn REST API with adaptive template rotation."""

    def __init__(
        self,
        token: Optional[str] = None,
        author_urn: Optional[str] = None,
        whop_url: Optional[str] = None,
        cooldown_hours: Optional[float] = None,
        window_start_utc: Optional[int] = None,
        window_end_utc: Optional[int] = None,
        only_prime_windows: Optional[bool] = None,
        skip_weekends: Optional[bool] = None,
        enabled: Optional[bool] = None,
        dry_run: bool = False,
    ):
        self.token = token or os.environ.get("LINKEDIN_ACCESS_TOKEN", "")
        self.author_urn = author_urn or os.environ.get("LINKEDIN_AUTHOR_URN", "")
        self.whop_url = whop_url or os.environ.get("LINKEDIN_WHOP_URL", DEFAULT_WHOP_URL)
        
        env_cooldown = os.environ.get("LINKEDIN_COOLDOWN_HOURS")
        self.cooldown_hours = cooldown_hours if cooldown_hours is not None else (float(env_cooldown) if env_cooldown else DEFAULT_COOLDOWN_HOURS)
        
        env_window_start = os.environ.get("LINKEDIN_WINDOW_START_UTC")
        self.window_start_utc = window_start_utc if window_start_utc is not None else (int(env_window_start) if env_window_start is not None and env_window_start != "" else DEFAULT_WINDOW_START_UTC)

        env_window_end = os.environ.get("LINKEDIN_WINDOW_END_UTC")
        self.window_end_utc = window_end_utc if window_end_utc is not None else (int(env_window_end) if env_window_end is not None and env_window_end != "" else DEFAULT_WINDOW_END_UTC)

        env_prime = os.environ.get("LINKEDIN_ONLY_PRIME_WINDOWS", "false").lower() in ("true", "1", "yes")
        self.only_prime_windows = only_prime_windows if only_prime_windows is not None else env_prime

        env_skip_weekends = os.environ.get("LINKEDIN_SKIP_WEEKENDS", "false").lower() in ("true", "1", "yes")
        self.skip_weekends = skip_weekends if skip_weekends is not None else env_skip_weekends

        env_enabled = os.environ.get("LINKEDIN_AUTO_PUBLISH", "true").lower() in ("true", "1", "yes")
        self.enabled = enabled if enabled is not None else env_enabled
        self.dry_run = dry_run or (os.environ.get("DRY_RUN", "").lower() in ("true", "1", "yes"))

        if self.enabled and (not self.token or not self.author_urn):
            logger.warning("LinkedInAutoPublisher enabled but missing token or author_urn. Auto-publish disabled.")
            self.enabled = False

    def is_within_publish_window(self, now_dt: Optional[datetime] = None) -> tuple[bool, str]:
        """Check if current time is within high-ticket audience active hours in US & UK.
        
        Optimized for:
        - London / UK (BST / GMT): 08:00 - 22:00 local (07:00 - 21:00 UTC)
        - New York / US East (EDT / EST): 03:00 - 17:00 local (07:00 - 21:00 UTC)
        - Transatlantic Golden Window: 11:00 - 14:30 UTC
        - Dead Zone: 21:00 - 07:00 UTC (Audience offline; algorithmic reach drops ~85%)
        """
        now = now_dt or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        if self.skip_weekends and now.weekday() >= 5:
            return False, f"Weekend publishing disabled (Today is {now.strftime('%A')})"

        current_hour_float = now.hour + (now.minute / 60.0)

        if self.only_prime_windows:
            in_window_1 = (11.0 <= current_hour_float <= 14.5)
            in_window_2 = (16.0 <= current_hour_float <= 18.5)
            if in_window_1 or in_window_2:
                return True, f"Within prime transatlantic window ({now.strftime('%H:%M')} UTC)"
            return False, (
                f"Outside prime windows (Window 1: 11:00-14:30 UTC, Window 2: 16:00-18:30 UTC). "
                f"Current: {now.strftime('%H:%M')} UTC"
            )

        if self.window_start_utc <= self.window_end_utc:
            in_window = (self.window_start_utc <= now.hour < self.window_end_utc)
        else:
            in_window = (now.hour >= self.window_start_utc or now.hour < self.window_end_utc)

        if in_window:
            return True, f"Within active transatlantic hours ({now.strftime('%H:%M')} UTC)"

        return False, (
            f"Outside active transatlantic window ({self.window_start_utc:02d}:00-{self.window_end_utc:02d}:00 UTC). "
            f"Current: {now.strftime('%H:%M')} UTC (Overnight Dead Zone in London & New York)"
        )

    def publish_text(self, text: str) -> Dict[str, Any]:
        """Publish text commentary to LinkedIn using IPv4 and HTTP/1.1."""
        if self.dry_run:
            mock_urn = f"urn:li:share:mock_{uuid.uuid4().hex[:12]}"
            logger.info("[DRY RUN] LinkedIn post simulated: %s", mock_urn)
            return {"ok": True, "dry_run": True, "post_urn": mock_urn}

        if not self.token or not self.author_urn:
            return {"ok": False, "error": "Missing LinkedIn credentials"}

        payload = {
            "author": self.author_urn,
            "commentary": text,
            "visibility": "PUBLIC",
            "distribution": {
                "feedDistribution": "MAIN_FEED",
                "targetEntities": [],
                "thirdPartyDistributionChannels": []
            },
            "lifecycleState": "PUBLISHED"
        }

        import tempfile
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json") as tf:
                json.dump(payload, tf)
                tmp_path = tf.name

            cmd = [
                "curl", "-s", "-4", "--http1.1", "-X", "POST",
                "https://api.linkedin.com/rest/posts",
                "-H", f"Authorization: Bearer {self.token}",
                "-H", "Content-Type: application/json",
                "-H", "LinkedIn-Version: 202503",
                "-H", "X-Restli-Protocol-Version: 2.0.0",
                "-i",
                "--max-time", "30",
                "--data-binary", f"@{tmp_path}"
            ]

            res = subprocess.run(cmd, capture_output=True, text=True, timeout=35)
            output = res.stdout or ""

            if "201 Created" in output:
                post_urn = "unknown"
                for line in output.splitlines():
                    if line.lower().startswith("x-restli-id:"):
                        post_urn = line.split(":", 1)[1].strip()
                return {"ok": True, "post_urn": post_urn, "raw_headers": output}
            else:
                logger.warning("LinkedIn API publish failed: %s", output)
                return {"ok": False, "error": output}

        except Exception as e:
            logger.error("Exception during LinkedIn publishing: %s", e)
            return {"ok": False, "error": str(e)}
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    def _select_next_template(self, db: Database) -> str:
        """Select next copywriting archetype in the rotation based on publication history."""
        try:
            conn = db._get_connection()
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM linkedin_posts;")
            count = cur.fetchone()[0]
            return TEMPLATES[count % len(TEMPLATES)]
        except Exception:
            return TEMPLATES[0]

    def _get_recent_deal_summaries(self, db: Database) -> List[str]:
        """Fetch 3 recent high-ticket deals to embed in contrast posts."""
        summaries = []
        try:
            leads = db.get_leads_by_status("dispatched")
            for l in leads[-3:]:
                badge = l.raw_compensation or "High Ticket"
                summaries.append(f"{badge}: {l.title}")
        except Exception:
            pass
        return summaries

    def maybe_publish_lead(self, enriched: EnrichedLead, db: Database) -> Optional[Dict[str, Any]]:
        """Evaluate lead and autonomously publish to LinkedIn using adaptive rotation."""
        if not self.enabled:
            return None

        if not getattr(enriched, "is_high_ticket", False):
            return None

        lead_id = getattr(enriched.lead, "id", None)
        if not lead_id:
            return None

        # 1. Check if lead was already published to LinkedIn
        if db.is_lead_posted_to_linkedin(lead_id):
            logger.debug("Lead %s already published to LinkedIn. Skipping.", lead_id)
            return None

        # 2. Check audience active window (London / New York / San Francisco)
        in_window, window_reason = self.is_within_publish_window()
        if not in_window:
            logger.info("LinkedIn post held/skipped: %s. Lead: %s", window_reason, lead_id)
            return None

        # 3. Check algorithmic cooldown to protect profile and maximize reach
        last_time = db.get_last_linkedin_post_time()
        if last_time:
            now = datetime.now(timezone.utc)
            if last_time.tzinfo is None:
                last_time = last_time.replace(tzinfo=timezone.utc)
            elapsed_hours = (now - last_time).total_seconds() / 3600.0
            if elapsed_hours < self.cooldown_hours:
                logger.info(
                    "LinkedIn cooldown active (%.2f hours elapsed < %.1f hours threshold). Skipping post for lead %s.",
                    elapsed_hours, self.cooldown_hours, lead_id
                )
                return None

        # 3. Choose dynamic template based on rotation history
        chosen_template = self._select_next_template(db)
        recent_deals = self._get_recent_deal_summaries(db) if chosen_template == "PLATFORM_TAX_CONTRAST" else None

        post_text = format_deal_teaser(
            enriched=enriched,
            whop_url=self.whop_url,
            template=chosen_template,
            recent_deals=recent_deals,
        )
        logger.info(
            "Autonomous LinkedIn publishing triggered [Template: %s] for lead %s: %s",
            chosen_template, lead_id, enriched.lead.title
        )
        
        result = self.publish_text(post_text)
        if result.get("ok"):
            post_urn = result.get("post_urn", "unknown")
            post_id = f"li_{uuid.uuid4().hex[:12]}"
            db.record_linkedin_post(
                post_id=post_id,
                lead_id=lead_id,
                post_urn=post_urn,
                post_text=post_text,
            )
            logger.info("Successfully recorded autonomous LinkedIn post %s for lead %s", post_urn, lead_id)
            return result
        else:
            logger.warning("Failed to publish autonomous LinkedIn post for lead %s: %s", lead_id, result.get("error"))
            return result
