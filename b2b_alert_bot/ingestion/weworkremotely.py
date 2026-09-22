"""We Work Remotely (WWR) RSS 2.0 connector for high-value contract feeds."""

import logging
import re
import xml.etree.ElementTree as ET
from typing import List, Optional, Tuple

from b2b_alert_bot.db import canonicalize_url, compute_lead_hash
from b2b_alert_bot.ingestion.base import BaseConnector, clean_html, parse_timestamp
from b2b_alert_bot.schema import Lead, extract_core_tech_stack


logger = logging.getLogger(__name__)


WWR_FEED_URLS = [
    "https://weworkremotely.com/categories/remote-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss",
    "https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-front-end-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-management-and-finance-jobs.rss",
    "https://weworkremotely.com/categories/remote-product-jobs.rss",
]


class WeWorkRemotelyConnector(BaseConnector):
    """Connector for We Work Remotely RSS 2.0 feeds across 7 key remote tech categories."""

    source_name: str = "weworkremotely"

    def __init__(self, feed_urls: Optional[List[str]] = None, **kwargs):
        super().__init__(**kwargs)
        self.feed_urls = feed_urls or WWR_FEED_URLS

    def fetch_live(self) -> List[Lead]:
        """Fetch and aggregate leads across all configured WWR RSS category feeds."""
        all_leads: List[Lead] = []
        seen_ids = set()

        for feed_url in self.feed_urls:
            raw_xml = self.fetch_url(feed_url)
            if not raw_xml:
                continue
            leads = self.parse(raw_xml)
            for lead in leads:
                if lead.id not in seen_ids:
                    seen_ids.add(lead.id)
                    all_leads.append(lead)

        return all_leads

    def _split_title(self, raw_title: str) -> Tuple[str, str]:
        """Split '{Company}: {Position Title}' into isolated company and position strings."""
        if not raw_title:
            return "Unknown", "Untitled Listing"

        parts = raw_title.split(":", 1)
        if len(parts) == 2:
            company = parts[0].strip()
            title = parts[1].strip()
            return company or "Unknown", title or raw_title.strip()

        return "Unknown", raw_title.strip()

    def _extract_compensation(self, text: str) -> str:
        """Extract compensation or salary snippets from job description."""
        if not text:
            return ""

        patterns = [
            r"(?i)(?:salary|compensation|rate|budget|pay|range):\s*([^\n\<\>]+)",
            (
                r"([\$€£]\s*\d{1,3}(?:,\d{3})*(?:\.\d+)?"
                r"(?:\s*-\s*[\$€£]?\s*\d{1,3}(?:,\d{3})*(?:\.\d+)?)?"
                r"(?:\s*(?:/|per)\s*(?:hr|hour|h|mo|month|yr|year))?)"
            ),
        ]

        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                snippet = match.group(1 if match.lastindex == 1 else 0).strip()
                clean_snippet = re.sub(r"[ \t]+", " ", snippet).strip(" .,;")
                if len(clean_snippet) <= 100:
                    return clean_snippet
        return ""

    def parse(self, content: str) -> List[Lead]:
        """Parse WWR RSS 2.0 XML payload into standardized Lead objects."""
        if not content or not content.strip():
            return []

        leads: List[Lead] = []
        try:
            if "<!ENTITY" in content:
                logger.warning("Rejecting XML containing DTD <!ENTITY definition.")
                return []

            root = ET.fromstring(content)
        except ET.ParseError as e:
            logger.error(f"Failed to parse WWR RSS XML: {e}")
            return []

        channel = root.find("channel")
        if channel is None:
            channel = root

        for item in channel.findall("item"):
            title_node = item.find("title")
            raw_title = title_node.text.strip() if title_node is not None and title_node.text else ""
            if not raw_title:
                continue

            company, title = self._split_title(raw_title)

            # Extract URL
            link_node = item.find("link")
            guid_node = item.find("guid")
            raw_url = ""
            if link_node is not None and link_node.text:
                raw_url = link_node.text.strip()
            elif guid_node is not None and guid_node.text:
                raw_url = guid_node.text.strip()

            if not raw_url:
                continue

            canonical_apply_url = canonicalize_url(raw_url)
            lead_id = compute_lead_hash(self.source_name, canonical_apply_url, title, company)

            # Extract dates
            pubdate_node = item.find("pubDate")
            raw_date = pubdate_node.text.strip() if pubdate_node is not None and pubdate_node.text else None
            iso_published_at = parse_timestamp(raw_date)

            # Extract region and category metadata
            region_node = item.find("region")
            region = region_node.text.strip() if region_node is not None and region_node.text else ""
            category_node = item.find("category")
            category = category_node.text.strip() if category_node is not None and category_node.text else ""

            # Extract description
            desc_node = item.find("description")
            raw_desc = desc_node.text if desc_node is not None and desc_node.text else ""
            plain_description = clean_html(raw_desc)

            # Extract compensation snippet
            raw_compensation = self._extract_compensation(plain_description)

            # Extract core tech stack
            tech_stack = extract_core_tech_stack(f"{title} {plain_description}")

            raw_metadata = {
                "region": region,
                "category": category,
                "original_title": raw_title
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
                core_tech_stack=tech_stack,
                raw_metadata=raw_metadata
            )

            try:
                lead.validate()
                leads.append(lead)
            except ValueError as ve:
                logger.warning(f"Discarding invalid WWR lead: {ve}")

        return leads
