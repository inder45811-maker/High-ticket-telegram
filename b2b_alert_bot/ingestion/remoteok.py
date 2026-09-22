"""RemoteOK JSON REST API connector for remote engineering and contract opportunities."""

import json
import logging
from typing import List, Any

from b2b_alert_bot.db import canonicalize_url, compute_lead_hash
from b2b_alert_bot.ingestion.base import BaseConnector, clean_html, parse_timestamp
from b2b_alert_bot.schema import Lead, extract_core_tech_stack


logger = logging.getLogger(__name__)

REMOTEOK_API_URL = "https://remoteok.com/api"


class RemoteOKConnector(BaseConnector):
    """Connector for RemoteOK official public JSON API."""

    source_name: str = "remoteok"

    def __init__(self, api_url: str = REMOTEOK_API_URL, **kwargs):
        super().__init__(**kwargs)
        self.api_url = api_url

    def fetch_live(self) -> List[Lead]:
        """Query RemoteOK API and parse active listings."""
        raw_json = self.fetch_url(self.api_url)
        if not raw_json:
            return []
        return self.parse(raw_json)

    def _format_salary(self, salary_min: Any, salary_max: Any) -> str:
        """Format annual salary range into readable string snippet."""
        try:
            s_min = float(salary_min) if salary_min else 0.0
        except (ValueError, TypeError):
            s_min = 0.0

        try:
            s_max = float(salary_max) if salary_max else 0.0
        except (ValueError, TypeError):
            s_max = 0.0

        if s_min > 0 and s_max > 0:
            return f"${int(s_min):,} - ${int(s_max):,}/yr"
        elif s_min > 0:
            return f"${int(s_min):,}/yr"
        elif s_max > 0:
            return f"${int(s_max):,}/yr"
        return ""

    def parse(self, content: str) -> List[Lead]:
        """Parse RemoteOK JSON payload into standardized Lead objects."""
        if not content or not content.strip():
            return []

        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse RemoteOK JSON: {e}")
            return []

        if not isinstance(data, list):
            logger.warning("RemoteOK JSON root is not an array.")
            return []

        leads: List[Lead] = []

        for idx, item in enumerate(data):
            if not isinstance(item, dict):
                continue

            # CRITICAL RULE: Element 0 is typically a legal / metadata notice:
            # {"last_updated": 1789862431, "legal": "API Terms of Service..."}
            # Skip any item containing 'legal' or lacking 'id' / 'position'
            if "legal" in item or "position" not in item or "id" not in item:
                logger.debug(f"Skipping non-job metadata element at index {idx}.")
                continue

            company = str(item.get("company") or "").strip() or "Unknown"
            title = str(item.get("position") or "").strip()
            if not title:
                continue

            raw_url = item.get("apply_url") or item.get("url") or ""
            if not raw_url:
                continue

            canonical_apply_url = canonicalize_url(str(raw_url))
            lead_id = compute_lead_hash(self.source_name, canonical_apply_url, title, company)

            raw_date = item.get("date")
            iso_published_at = parse_timestamp(str(raw_date)) if raw_date else None

            raw_desc = item.get("description") or ""
            plain_description = clean_html(str(raw_desc))

            raw_compensation = self._format_salary(item.get("salary_min"), item.get("salary_max"))

            # Extract tags from RemoteOK tags array and fallback text taxonomy
            tags: List[str] = []
            raw_tags = item.get("tags")
            if isinstance(raw_tags, list):
                tags = [str(t).strip() for t in raw_tags if t and isinstance(t, str)]

            text_tech = extract_core_tech_stack(f"{title} {plain_description}")
            # Merge and deduplicate preserving case
            combined_tech = list(dict.fromkeys(tags + text_tech))

            raw_metadata = {
                "remoteok_id": item.get("id"),
                "location": item.get("location"),
                "salary_min": item.get("salary_min"),
                "salary_max": item.get("salary_max"),
                "tags": tags
            }

            lead = Lead(
                id=lead_id,
                title=title,
                source=self.source_name,
                client=company,
                url=canonical_apply_url,
                raw_compensation=raw_compensation,
                description=plain_description,
                published_at=iso_published_at,
                core_tech_stack=combined_tech,
                raw_metadata=raw_metadata
            )

            try:
                lead.validate()
                leads.append(lead)
            except ValueError as ve:
                logger.warning(f"Discarding invalid RemoteOK lead: {ve}")

        return leads
