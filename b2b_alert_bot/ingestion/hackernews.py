"""Hacker News Algolia Search API connector for monthly 'Who is hiring' and 'Freelancer' threads."""

import json
import logging
import re
from typing import List, Optional, Tuple, Dict, Any

from b2b_alert_bot.db import canonicalize_url, compute_lead_hash
from b2b_alert_bot.ingestion.base import BaseConnector, clean_html, parse_timestamp
from b2b_alert_bot.schema import Lead, extract_core_tech_stack


logger = logging.getLogger(__name__)

HN_ALGOLIA_BASE = "https://hn.algolia.com/api/v1"
WHOISHIRING_SEARCH_URL = (
    f"{HN_ALGOLIA_BASE}/search_by_date?tags=story,author_whoishiring&hitsPerPage=10"
)
FREELANCE_SEARCH_URL = (
    f"{HN_ALGOLIA_BASE}/search_by_date?query=Freelancer%3F%20Seeking%20freelancer&tags=story&hitsPerPage=5"
)


class HackerNewsConnector(BaseConnector):
    """Connector for Hacker News monthly hiring threads via Algolia Search API."""

    source_name: str = "hackernews"

    def __init__(
        self,
        whoishiring_url: str = WHOISHIRING_SEARCH_URL,
        freelance_url: str = FREELANCE_SEARCH_URL,
        **kwargs
    ):
        super().__init__(**kwargs)
        self.whoishiring_url = whoishiring_url
        self.freelance_url = freelance_url

    def fetch_live(self) -> List[Lead]:
        """Discover latest monthly hiring stories and ingest top-level job comments."""
        all_leads: List[Lead] = []
        target_stories: List[Dict[str, Any]] = []

        # 1. Discover latest "Who is hiring?" story
        res_hiring = self.fetch_url(self.whoishiring_url)
        if res_hiring:
            try:
                data = json.loads(res_hiring)
                for hit in data.get("hits", []):
                    title = hit.get("title", "")
                    if "Ask HN: Who is hiring?" in title:
                        target_stories.append({"id": str(hit["objectID"]), "title": title, "type": "whoishiring"})
                        break
            except Exception as e:
                logger.error(f"Error parsing HN whoishiring discovery: {e}")

        # 2. Discover latest "Freelancer? Seeking freelancer?" story
        res_freelance = self.fetch_url(self.freelance_url)
        if res_freelance:
            try:
                data = json.loads(res_freelance)
                for hit in data.get("hits", []):
                    title = hit.get("title", "")
                    if "Freelancer? Seeking freelancer?" in title:
                        target_stories.append({"id": str(hit["objectID"]), "title": title, "type": "freelance"})
                        break
            except Exception as e:
                logger.error(f"Error parsing HN freelance discovery: {e}")

        # 3. Retrieve comments for identified stories
        for story in target_stories:
            story_id = story["id"]
            story_type = story["type"]
            comments_url = f"{HN_ALGOLIA_BASE}/search?tags=comment,story_{story_id}&hitsPerPage=100"
            raw_comments = self.fetch_url(comments_url)
            if raw_comments:
                leads = self._parse_comments_payload(raw_comments, story_id, story_type)
                all_leads.extend(leads)

        return all_leads

    def _parse_header_line(self, line: str) -> Tuple[str, str, str]:
        """Parse pipe-delimited first line '{Company} | {Role} | {Location} | ...'."""
        line = line.strip()
        # Remove 'SEEKING FREELANCER' prefix if present
        line = re.sub(r"(?i)^SEEKING FREELANCER\s*[-|:]?\s*", "", line).strip()

        parts = [p.strip() for p in line.split("|") if p.strip()]
        if not parts:
            return "Unknown", "Contract Opportunity", ""

        company = parts[0]
        title = parts[1] if len(parts) > 1 else "Contract Opportunity"

        # Look for rate or comp in remaining pipe parts
        comp = ""
        for p in parts[2:]:
            if any(sym in p for sym in ["$", "€", "£", "/hr", "/mo", "k", "USD"]):
                comp = p
                break

        return company, title, comp

    def _extract_compensation(self, text: str) -> str:
        """Extract compensation or salary snippets from comment text."""
        if not text:
            return ""
        patterns = [
            r"(?i)(?:salary|compensation|rate|budget|pay|range):\s*([^\n\<\>]+)",
            (
                r"([\$€£]\s*\d{1,3}(?:,\d{3})*(?:\.\d+)?"
                r"(?:\s*-\s*[\$€£]?\s*\d{1,3}(?:,\d{3})*(?:\.\d+)?)?"
                r"(?:\s*(?:/|per)\s*(?:hr|hour|h|mo|month|yr|year))?)"
            ),
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

    def _parse_comments_payload(
        self,
        content: str,
        expected_story_id: Optional[str] = None,
        story_type: str = "whoishiring"
    ) -> List[Lead]:
        """Parse Algolia comments response or raw comment list."""
        if not content or not content.strip():
            return []

        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to decode HN JSON: {e}")
            return []

        # Support either Algolia response {"hits": [...]} or raw list [...]
        comments: List[Dict[str, Any]]
        if isinstance(data, dict):
            comments = data.get("hits", [])
        elif isinstance(data, list):
            comments = data
        else:
            return []

        leads: List[Lead] = []

        for comment in comments:
            if not isinstance(comment, dict):
                continue

            comment_id = str(comment.get("objectID") or "")
            story_id = str(comment.get("story_id") or "")
            parent_id = str(comment.get("parent_id") or "")

            # CRITICAL RULE 1: Top-level comment isolation
            # Only top-level comments represent job postings; nested replies are discussions
            if expected_story_id and story_id != expected_story_id:
                # If expected_story_id was specified, ensure it belongs to that story
                continue

            if parent_id and story_id and parent_id != story_id:
                # Discard nested replies
                continue

            raw_html_body = comment.get("comment_text") or ""
            if not raw_html_body or not raw_html_body.strip():
                continue

            plain_text = clean_html(raw_html_body)
            upper_text = plain_text.upper()

            # CRITICAL RULE 2: Freelance thread filtering
            # Discard any 'SEEKING WORK' comments (freelancers pitching themselves)
            if "SEEKING WORK" in upper_text:
                continue

            # If it's a freelance thread, it MUST be 'SEEKING FREELANCER'
            if story_type == "freelance" and "SEEKING FREELANCER" not in upper_text:
                continue

            # Parse company, title, and comp from first line
            lines = [line_str.strip() for line_str in plain_text.splitlines() if line_str.strip()]
            first_line = lines[0] if lines else ""
            company, title, comp_part = self._parse_header_line(first_line)

            # Extract compensation snippet
            raw_comp = comp_part or self._extract_compensation(plain_text)

            # HN comment canonical permalink
            item_url = f"https://news.ycombinator.com/item?id={comment_id}" if comment_id else ""
            if not item_url:
                continue

            canonical_url = canonicalize_url(item_url)
            lead_id = compute_lead_hash(self.source_name, canonical_url, title, company)

            raw_date = comment.get("created_at")
            iso_published_at = parse_timestamp(str(raw_date)) if raw_date else None

            tech_stack = extract_core_tech_stack(f"{title} {plain_text}")

            raw_metadata = {
                "hn_object_id": comment_id,
                "hn_author": comment.get("author"),
                "story_id": story_id,
                "parent_id": parent_id,
                "story_type": story_type
            }

            lead = Lead(
                id=lead_id,
                title=title,
                source=self.source_name,
                client=company,
                url=canonical_url,
                raw_compensation=raw_comp,
                description=plain_text,
                published_at=iso_published_at,
                core_tech_stack=tech_stack,
                raw_metadata=raw_metadata
            )

            try:
                lead.validate()
                leads.append(lead)
            except ValueError as ve:
                logger.warning(f"Discarding invalid Hacker News lead: {ve}")

        return leads

    def parse(self, content: str) -> List[Lead]:
        """Unified parse method for offline fixture or string testing."""
        return self._parse_comments_payload(content)
