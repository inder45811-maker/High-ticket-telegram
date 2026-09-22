"""Autonomous X (Twitter) Founder Signal Ingestion Connector.

Ingests and evaluates real-time founder "WTB" / hiring tweets from X/Twitter.
Detects direct contract requests, founder DMs, and high-urgency hiring tweets,
formatting them into standardized Lead objects with direct 1-click DM links.
"""

import logging
import re
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any

from b2b_alert_bot.db import canonicalize_url, compute_lead_hash
from b2b_alert_bot.ingestion.base import BaseConnector, DEFAULT_DESKTOP_UA
from b2b_alert_bot.schema import Lead, extract_core_tech_stack

logger = logging.getLogger(__name__)

FOUNDER_HIRING_PATTERNS = [
    r"(?i)\b(?:looking for|need|hiring)\s+(?:a|an|senior)?\s*(?:contractor|developer|engineer|freelancer|architect|designer)\b",
    r"(?i)\b(?:paid gig|paid contract|b2b contract|budget available|dm portfolio|dm me)\b",
    r"(?i)\b(?:contract|project)\s*(?:budget|rate):\s*[\$€£]?\s*\d+",
]


class XSignalConnector(BaseConnector):
    """Connector for X/Twitter Founder Contract Signals."""

    source_name: str = "x_signal"

    def __init__(self, api_url: Optional[str] = None, **kwargs):
        if "user_agent" not in kwargs:
            kwargs["user_agent"] = DEFAULT_DESKTOP_UA
        super().__init__(**kwargs)
        self.api_url = api_url

    def fetch_live(self) -> List[Lead]:
        """Retrieve live X/Twitter founder contract signals."""
        if not self.api_url:
            return []
        raw = self.fetch_url(self.api_url)
        if not raw:
            return []
        return self.parse(raw)

    def _extract_compensation(self, text: str) -> str:
        """Extract explicit budget or hourly rate from tweet body."""
        if not text:
            return ""
        patterns = [
            r"([\$€£]\s*\d{1,3}(?:,\d{3})*(?:\.\d+)?(?:\s*-\s*[\$€£]?\s*\d{1,3}(?:,\d{3})*(?:\.\d+)?)?(?:\s*(?:/|per)\s*(?:hr|hour|h|mo|month|yr|year))?)",
            r"(?i)(?:budget|rate|paying):\s*([^\n\<\>]+)",
            r"(?i)\b(\d{2,3}k(?:\s*-\s*\d{2,3}k)?(?:\s*(?:/|per)\s*(?:yr|year))?)\b",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                snippet = match.group(1 if match.lastindex == 1 else 0).strip()
                clean_snippet = re.sub(r"[ \t]+", " ", snippet).strip(" .,;")
                if len(clean_snippet) <= 80:
                    return clean_snippet
        return ""

    def parse_tweet(self, tweet_data: Dict[str, Any]) -> Optional[Lead]:
        """Convert single raw tweet JSON into a validated Lead."""
        text = tweet_data.get("text", "") or tweet_data.get("full_text", "")
        if not text or not text.strip():
            return None

        # Quick intent pre-filter
        is_signal = any(re.search(pat, text) for pat in FOUNDER_HIRING_PATTERNS)
        if not is_signal:
            return None

        author_username = (
            tweet_data.get("author", {}).get("username") or
            tweet_data.get("user", {}).get("screen_name") or
            tweet_data.get("username") or
            "Founder"
        )
        tweet_id = str(tweet_data.get("id") or tweet_data.get("id_str") or "")
        tweet_url = f"https://x.com/{author_username}/status/{tweet_id}" if tweet_id else f"https://x.com/{author_username}"

        canonical_url = canonicalize_url(tweet_url)

        # Title: First sentence or up to 80 chars
        first_line = text.split("\n")[0].strip()
        title = first_line[:90] if len(first_line) > 90 else first_line
        title = re.sub(r"https?://\S+", "", title).strip(" :-|;,")
        if not title:
            title = f"Contract Opportunity via @{author_username}"

        raw_comp = self._extract_compensation(text)
        tech_stack = extract_core_tech_stack(text)
        lead_id = compute_lead_hash(self.source_name, canonical_url, title, author_username)

        lead = Lead(
            id=lead_id,
            title=title,
            source=self.source_name,
            client=f"@{author_username} (X Founder)",
            url=canonical_url,
            raw_compensation=raw_comp,
            description=text,
            published_at=tweet_data.get("created_at") or datetime.now(timezone.utc).isoformat(),
            core_tech_stack=tech_stack,
            raw_metadata={"tweet_id": tweet_id, "username": author_username}
        )

        try:
            lead.validate()
            return lead
        except ValueError as ve:
            logger.warning(f"Discarding invalid tweet lead: {ve}")
            return None

    def parse(self, content: Any) -> List[Lead]:
        """Parse JSON array of tweets or string payload."""
        import json
        if not content:
            return []

        if isinstance(content, str):
            try:
                data = json.loads(content)
            except Exception:
                return []
        elif isinstance(content, (list, dict)):
            data = content
        else:
            return []

        tweets = data if isinstance(data, list) else data.get("data", [data])
        leads = []
        for t in tweets:
            if isinstance(t, dict):
                lead = self.parse_tweet(t)
                if lead:
                    leads.append(lead)
        return leads
