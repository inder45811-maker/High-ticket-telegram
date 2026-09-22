"""Reddit r/forhire Atom 1.0 XML connector with strict [Hiring] gating."""

import logging
import re
import xml.etree.ElementTree as ET
from typing import List, Optional

from b2b_alert_bot.db import canonicalize_url, compute_lead_hash
from b2b_alert_bot.ingestion.base import (
    BaseConnector,
    clean_html,
    parse_timestamp,
    DEFAULT_DESKTOP_UA
)
from b2b_alert_bot.schema import Lead, extract_core_tech_stack


logger = logging.getLogger(__name__)

REDDIT_FORHIRE_RSS_URL = "https://www.reddit.com/r/forhire/.rss"


class RedditConnector(BaseConnector):
    """Connector for Reddit r/forhire public Atom feed with anti-blocking headers."""

    source_name: str = "reddit"

    def __init__(self, feed_url: str = REDDIT_FORHIRE_RSS_URL, **kwargs):
        # Enforce desktop browser UA to prevent HTTP 403 blocks from Akamai/Cloudflare
        if "user_agent" not in kwargs:
            kwargs["user_agent"] = DEFAULT_DESKTOP_UA
        super().__init__(**kwargs)
        self.feed_url = feed_url

    def fetch_live(self) -> List[Lead]:
        """Fetch live Atom XML feed from r/forhire."""
        raw_xml = self.fetch_url(self.feed_url)
        if not raw_xml:
            return []
        return self.parse(raw_xml)

    def _extract_compensation(self, text: str) -> str:
        """Extract budget or hourly compensation from title or post body."""
        if not text:
            return ""
        patterns = [
            (
                r"([\$€£]\s*\d{1,3}(?:,\d{3})*(?:\.\d+)?"
                r"(?:\s*-\s*[\$€£]?\s*\d{1,3}(?:,\d{3})*(?:\.\d+)?)?"
                r"(?:\s*(?:/|per)\s*(?:hr|hour|h|mo|month|yr|year))?)"
            ),
            r"(?i)(?:budget|rate|pay):\s*([^\n\<\>]+)",
            r"(?i)\b(\d{2,3}k(?:\s*-\s*\d{2,3}k)?(?:\s*(?:/|per)\s*(?:yr|year))?)\b",
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
        """Parse Reddit r/forhire Atom 1.0 XML into standardized Lead objects."""
        if not content or not content.strip():
            return []

        leads: List[Lead] = []
        try:
            if "<!ENTITY" in content:
                logger.warning("Rejecting XML containing DTD <!ENTITY definition.")
                return []
            root = ET.fromstring(content)
        except ET.ParseError as e:
            logger.error(f"Failed to parse Reddit Atom XML: {e}")
            return []

        atom_ns = "http://www.w3.org/2005/Atom"

        # Find entries with namespace or without
        entries = root.findall(f"{{{atom_ns}}}entry")
        if not entries:
            entries = root.findall("entry")

        def _find_elem(parent: ET.Element, tag: str) -> Optional[ET.Element]:
            node = parent.find(f"{{{atom_ns}}}{tag}")
            if node is not None:
                return node
            return parent.find(tag)

        for entry in entries:
            title_node = _find_elem(entry, "title")
            raw_title = title_node.text.strip() if title_node is not None and title_node.text else ""
            if not raw_title:
                continue

            raw_title_lower = raw_title.lower()

            # CRITICAL RULE 1: Filter for [Hiring] posts
            if "[hiring]" not in raw_title_lower:
                continue

            # CRITICAL RULE 2: Exclude [For Hire] (job seekers)
            if "[for hire]" in raw_title_lower or "[forhire]" in raw_title_lower:
                continue

            # CRITICAL RULE 3: Exclude mod stickies and announcements
            if any(term in raw_title_lower for term in [
                "rules reminder", "minimum karma", "announcement", "weekly discussion", "meta"
            ]):
                continue

            # Clean [Hiring] tag from title
            clean_title = re.sub(r"(?i)\[\s*hiring\s*\]", "", raw_title).strip(" :-|")
            if not clean_title:
                clean_title = raw_title

            # Extract URL
            link_node = _find_elem(entry, "link")
            raw_url = ""
            if link_node is not None:
                raw_url = link_node.attrib.get("href", "")
            if not raw_url:
                continue

            canonical_apply_url = canonicalize_url(raw_url)

            # Extract author username
            author_node = entry.find(f"{{{atom_ns}}}author/{{{atom_ns}}}name")
            if author_node is None:
                author_node = entry.find("author/name")
            author = author_node.text.strip() if author_node is not None and author_node.text else "Anonymous"

            lead_id = compute_lead_hash(self.source_name, canonical_apply_url, clean_title, author)

            # Extract updated timestamp
            updated_node = _find_elem(entry, "updated")
            raw_date = updated_node.text.strip() if updated_node is not None and updated_node.text else None
            iso_published_at = parse_timestamp(raw_date)

            # Extract content HTML
            content_node = _find_elem(entry, "content")
            raw_content = content_node.text if content_node is not None and content_node.text else ""
            plain_description = clean_html(raw_content)

            # Extract compensation from title or description
            raw_compensation = self._extract_compensation(raw_title) or self._extract_compensation(plain_description)

            tech_stack = extract_core_tech_stack(f"{clean_title} {plain_description}")

            id_node = _find_elem(entry, "id")
            raw_metadata = {
                "author": author,
                "original_title": raw_title,
                "reddit_id": id_node.text.strip() if id_node is not None and id_node.text else ""
            }

            lead = Lead(
                id=lead_id,
                title=clean_title,
                source=self.source_name,
                client=author,
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
                logger.warning(f"Discarding invalid Reddit lead: {ve}")

        return leads
