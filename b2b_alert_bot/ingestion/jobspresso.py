"""Jobspresso dedicated job board RSS 2.0 connector."""

import html
import logging
import re
import xml.etree.ElementTree as ET
from typing import List, Tuple

from b2b_alert_bot.db import canonicalize_url, compute_lead_hash
from b2b_alert_bot.ingestion.base import BaseConnector, clean_html, parse_timestamp
from b2b_alert_bot.schema import Lead, extract_core_tech_stack


logger = logging.getLogger(__name__)

JOBSPRESSO_FEED_URL = "https://jobspresso.co/jobs/feed/"


class JobspressoConnector(BaseConnector):
    """Connector for Jobspresso /jobs/feed/ RSS 2.0 with Dublin Core metadata."""

    source_name: str = "jobspresso"

    def __init__(self, feed_url: str = JOBSPRESSO_FEED_URL, **kwargs):
        super().__init__(**kwargs)
        self.feed_url = feed_url

    def fetch_live(self) -> List[Lead]:
        """Query Jobspresso RSS feed and parse active job listings."""
        raw_xml = self.fetch_url(self.feed_url)
        if not raw_xml:
            return []
        return self.parse(raw_xml)

    def _parse_creator(self, creator_text: str) -> Tuple[str, str]:
        """Extract company name and location from '<dc:creator>' (e.g. 'Hopper<br>⚲&nbsp;Canada')."""
        if not creator_text:
            return "Unknown", ""

        unescaped = html.unescape(creator_text)
        parts = re.split(r"<br\s*/?>", unescaped, flags=re.IGNORECASE)
        company = re.sub(r"<[^>]+>", "", parts[0]).strip()

        location = ""
        if len(parts) > 1:
            raw_loc = re.sub(r"<[^>]+>", "", parts[1])
            location = raw_loc.replace("⚲", "").strip()

        return company or "Unknown", location

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
        """Parse Jobspresso RSS 2.0 XML into standardized Lead objects."""
        if not content or not content.strip():
            return []

        leads: List[Lead] = []
        try:
            if "<!ENTITY" in content:
                logger.warning("Rejecting XML containing DTD <!ENTITY definition.")
                return []
            root = ET.fromstring(content)
        except ET.ParseError as e:
            logger.error(f"Failed to parse Jobspresso RSS XML: {e}")
            return []

        channel = root.find("channel")
        if channel is None:
            channel = root

        # XML namespace definitions
        dc_namespace = "http://purl.org/dc/elements/1.1/"
        content_namespace = "http://purl.org/rss/1.0/modules/content/"

        for item in channel.findall("item"):
            title_node = item.find("title")
            raw_title = title_node.text.strip() if title_node is not None and title_node.text else ""
            if not raw_title:
                continue

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

            # Extract company and location from dc:creator
            creator_node = item.find(f"{{{dc_namespace}}}creator")
            creator_text = creator_node.text if creator_node is not None and creator_node.text else ""
            company, location = self._parse_creator(creator_text)

            lead_id = compute_lead_hash(self.source_name, canonical_apply_url, raw_title, company)

            # Extract date
            pubdate_node = item.find("pubDate")
            raw_date = pubdate_node.text.strip() if pubdate_node is not None and pubdate_node.text else None
            iso_published_at = parse_timestamp(raw_date)

            # Prefer full content:encoded over summary description
            encoded_node = item.find(f"{{{content_namespace}}}encoded")
            desc_node = item.find("description")
            raw_body = ""
            if encoded_node is not None and encoded_node.text:
                raw_body = encoded_node.text
            elif desc_node is not None and desc_node.text:
                raw_body = desc_node.text

            plain_description = clean_html(raw_body)
            raw_compensation = self._extract_compensation(plain_description)
            tech_stack = extract_core_tech_stack(f"{raw_title} {plain_description}")

            raw_metadata = {
                "location": location,
                "raw_creator": creator_text
            }

            lead = Lead(
                id=lead_id,
                title=raw_title,
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
                logger.warning(f"Discarding invalid Jobspresso lead: {ve}")

        return leads
