"""Reddit Atom 1.0 XML and OAuth JSON connector with multi-subreddit radar and semantic intent gating."""

from datetime import datetime, timezone
import json
import logging
import os
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

from b2b_alert_bot.db import canonicalize_url, compute_lead_hash
from b2b_alert_bot.ingestion.base import (
    BaseConnector,
    clean_html,
    parse_timestamp,
    DEFAULT_DESKTOP_UA
)
from b2b_alert_bot.schema import Lead, extract_core_tech_stack


logger = logging.getLogger(__name__)

DEFAULT_SUBREDDITS = ["forhire", "startups", "freelance", "SaaS", "devops", "webdev"]
REDDIT_FORHIRE_RSS_URL = "https://www.reddit.com/r/forhire/.rss"


def parse_target_subreddits(subreddits_env: Optional[str] = None) -> List[str]:
    """Parse comma/space separated subreddits or return default B2B high-ticket list."""
    raw = (subreddits_env or os.getenv("REDDIT_SUBREDDITS", "")).strip()
    if raw:
        subs = [
            re.sub(r"^/?r/", "", s.strip())
            for s in re.split(r"[,\s+]+", raw)
            if s.strip()
        ]
        if subs:
            return subs
    return list(DEFAULT_SUBREDDITS)


def build_multireddit_rss_url(subreddits: List[str]) -> str:
    """Combine multiple subreddits into a single atomic RSS feed to eliminate rate limiting."""
    cleaned = [s.strip().lstrip("r/").strip() for s in subreddits if s and s.strip()]
    if not cleaned:
        cleaned = DEFAULT_SUBREDDITS
    return f"https://www.reddit.com/r/{'+'.join(cleaned)}/.rss"


class RedditConnector(BaseConnector):
    """Production connector for Reddit supporting multi-subreddit RSS and official OAuth JSON."""

    source_name: str = "reddit"

    def __init__(
        self,
        feed_url: Optional[str] = None,
        feed_urls: Optional[List[str]] = None,
        subreddits: Optional[List[str]] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        **kwargs
    ):
        if "user_agent" not in kwargs:
            kwargs["user_agent"] = DEFAULT_DESKTOP_UA
        super().__init__(**kwargs)

        self.subreddits = subreddits or parse_target_subreddits()
        if feed_urls:
            self.feed_urls = list(feed_urls)
            self.feed_url = self.feed_urls[0]
        elif feed_url:
            self.feed_url = feed_url
            self.feed_urls = [feed_url]
        else:
            self.feed_url = build_multireddit_rss_url(self.subreddits)
            self.feed_urls = [self.feed_url]

        # Optional Reddit OAuth credentials
        self.client_id = client_id or os.getenv("REDDIT_CLIENT_ID")
        self.client_secret = client_secret or os.getenv("REDDIT_CLIENT_SECRET")
        self.username = username or os.getenv("REDDIT_USERNAME")
        self.password = password or os.getenv("REDDIT_PASSWORD")
        self._oauth_token: Optional[str] = None
        self._oauth_token_expiry: float = 0.0

    def _get_oauth_token(self) -> Optional[str]:
        """Obtain or refresh Reddit OAuth access token if credentials are provided."""
        if not (self.client_id and self.client_secret):
            return None
        now = time.time()
        if self._oauth_token and now < self._oauth_token_expiry:
            return self._oauth_token

        token_url = "https://www.reddit.com/api/v1/access_token"
        headers = {
            "User-Agent": f"ApexRadarBot/1.0 by {self.username or 'ApexRadar'}"
        }
        if self.username and self.password:
            data = {
                "grant_type": "password",
                "username": self.username,
                "password": self.password,
            }
        else:
            data = {"grant_type": "client_credentials"}

        try:
            resp = self.session.post(
                token_url,
                data=data,
                auth=(self.client_id, self.client_secret),
                headers=headers,
                timeout=(5.0, 10.0),
            )
            if resp.status_code == 200:
                payload = resp.json()
                self._oauth_token = payload.get("access_token")
                expires_in = payload.get("expires_in", 3600)
                self._oauth_token_expiry = now + expires_in - 60
                logger.info("Successfully acquired Reddit OAuth access token.")
                return self._oauth_token
            else:
                logger.warning(f"Reddit OAuth token request failed: {resp.status_code} {resp.text[:100]}")
        except Exception as e:
            logger.warning(f"Reddit OAuth authentication error: {e}")

        return None

    def _fetch_oauth_live(self, token: str) -> List[Lead]:
        """Fetch listings via Reddit official OAuth API."""
        sub_path = "+".join(self.subreddits)
        url = f"https://oauth.reddit.com/r/{sub_path}/new.json?limit=50"
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": f"ApexRadarBot/1.0 by {self.username or 'ApexRadar'}",
        }
        try:
            resp = self.session.get(url, headers=headers, timeout=self.timeout)
            if resp.status_code == 200:
                return self.parse_json(resp.text)
            logger.warning(f"Reddit OAuth fetch failed: {resp.status_code}")
        except Exception as e:
            logger.warning(f"Reddit OAuth fetch error: {e}")
        return []

    def fetch_live(self) -> List[Lead]:
        """Fetch live contract leads from Reddit via OAuth API or resilient RSS multi-feed."""
        oauth_token = self._get_oauth_token()
        if oauth_token:
            leads = self._fetch_oauth_live(oauth_token)
            if leads:
                return leads

        all_leads: List[Lead] = []
        seen_ids = set()

        for i, url in enumerate(self.feed_urls):
            if i > 0:
                time.sleep(2.5)  # Enforce inter-feed spacing to eliminate 429 warnings
            raw_xml = self.fetch_url(url)
            if not raw_xml:
                continue
            parsed = self.parse(raw_xml)
            for lead in parsed:
                if lead.id not in seen_ids:
                    seen_ids.add(lead.id)
                    all_leads.append(lead)

        return all_leads

    def _extract_compensation(self, text: str) -> str:
        """Extract budget or hourly compensation from title or post body."""
        if not text:
            return ""
        patterns = [
            (
                r"([\$€£]\s*\d{1,3}(?:,\d{3})*(?:\.\d+)?"
                r"(?:\s*-\s*[\$€£]?\s*\d{1,3}(?:,\d{3})*(?:\.\d+)?)?"
                r"(?:\s*(?:/|per)\s*(?:hr|hour|h|mo|month|yr|year|day))?)"
            ),
            r"(?i)(?:budget|rate|pay|paying|bounty):\s*([^\n\<\>]+)",
            r"(?i)\b(\d{2,3}k(?:\s*-\s*\d{2,3}k)?(?:\s*(?:/|per)\s*(?:yr|year))?)\b",
            r"(\b\d{1,3}(?:,\d{3})*\s*(?:USD|EUR|GBP)\b)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                snippet = match.group(1 if match.lastindex == 1 else 0).strip()
                clean_snippet = re.sub(r"[ \t]+", " ", snippet).strip(" .,;")
                if len(clean_snippet) <= 100:
                    return clean_snippet
        return ""

    def _filter_and_clean_title(self, raw_title: str, body_text: str = "") -> Tuple[str, bool]:
        """Evaluate hiring intent, rule out job-seekers/announcements, and clean title."""
        raw_title_lower = raw_title.lower()

        # RULE 1: Reject job seekers
        if any(term in raw_title_lower for term in [
            "[for hire]", "[forhire]", "[for-hire]", "hire me",
            "seeking work", "looking for work", "portfolio", "available for hire", "open to work"
        ]):
            return "", False

        # RULE 2: Reject mod stickies & announcements
        if any(term in raw_title_lower for term in [
            "rules reminder", "minimum karma", "announcement", "weekly discussion",
            "monthly discussion", "meta thread", "monthly thread", "feedback thread"
        ]):
            return "", False

        # RULE 3: Match [Hiring] tag OR semantic hiring signal
        has_hiring_tag = "[hiring]" in raw_title_lower
        has_intent_signal = any(sig in raw_title_lower for sig in [
            "looking for", "need a", "need senior", "hiring", "contractor", 
            "freelance", "developer needed", "engineer needed", "paid gig", "bounty",
            "looking to hire", "seeking a", "paying", "budget:", "budget is",
            "build an mvp", "build my", "contract opportunity", "fractional"
        ])

        # Check body preview for intent if title didn't explicitly have it
        has_body_intent = False
        if not has_hiring_tag and not has_intent_signal and body_text:
            body_preview = body_text[:300].lower()
            if any(sig in body_preview for sig in ["looking for", "need a", "hiring", "contractor", "freelance"]):
                has_body_intent = True

        if not (has_hiring_tag or has_intent_signal or has_body_intent):
            return "", False

        # Clean tags from title
        clean_title = re.sub(r"(?i)\[\s*hiring\s*\]", "", raw_title).strip(" :-|")
        if not clean_title:
            clean_title = raw_title

        return clean_title, True

    def parse(self, content: str) -> List[Lead]:
        """Parse Reddit Atom 1.0 XML into standardized Lead objects."""
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

            content_node = _find_elem(entry, "content")
            raw_content = content_node.text if content_node is not None and content_node.text else ""
            plain_description = clean_html(raw_content)

            clean_title, is_valid = self._filter_and_clean_title(raw_title, plain_description)
            if not is_valid:
                continue

            link_node = _find_elem(entry, "link")
            raw_url = ""
            if link_node is not None:
                raw_url = link_node.attrib.get("href", "")
            if not raw_url:
                continue

            canonical_apply_url = canonicalize_url(raw_url)

            # Extract subreddit
            subreddit = "forhire"
            cat_node = _find_elem(entry, "category")
            if cat_node is not None:
                term = cat_node.attrib.get("term") or cat_node.attrib.get("label", "")
                if term:
                    subreddit = term.lstrip("r/").strip()
            if subreddit == "forhire" and raw_url:
                sub_match = re.search(r"/r/([a-zA-Z0-9_]+)/", raw_url)
                if sub_match:
                    subreddit = sub_match.group(1)

            author_node = entry.find(f"{{{atom_ns}}}author/{{{atom_ns}}}name")
            if author_node is None:
                author_node = entry.find("author/name")
            author = author_node.text.strip() if author_node is not None and author_node.text else "Anonymous"

            clean_author = author.replace("/u/", "").replace("u/", "").strip()
            pm_url = f"https://www.reddit.com/message/compose/?to={clean_author}&subject={urllib.parse.quote_plus(clean_title[:50])}"

            lead_id = compute_lead_hash(self.source_name, canonical_apply_url, clean_title, author)

            updated_node = _find_elem(entry, "updated")
            raw_date = updated_node.text.strip() if updated_node is not None and updated_node.text else None
            iso_published_at = parse_timestamp(raw_date)

            raw_compensation = self._extract_compensation(raw_title) or self._extract_compensation(plain_description)
            tech_stack = extract_core_tech_stack(f"{clean_title} {plain_description}")

            id_node = _find_elem(entry, "id")
            raw_metadata = {
                "author": author,
                "clean_author": clean_author,
                "original_title": raw_title,
                "subreddit": subreddit,
                "pm_url": pm_url,
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

    def parse_json(self, content: str) -> List[Lead]:
        """Parse Reddit OAuth API JSON listing into standardized Lead objects."""
        if not content or not content.strip():
            return []
        try:
            data = json.loads(content)
        except Exception as e:
            logger.error(f"Failed to parse Reddit JSON listing: {e}")
            return []

        children = data.get("data", {}).get("children", [])
        leads: List[Lead] = []

        for item in children:
            post = item.get("data", {})
            raw_title = post.get("title", "").strip()
            if not raw_title:
                continue

            selftext = post.get("selftext", "")
            plain_description = clean_html(selftext)

            clean_title, is_valid = self._filter_and_clean_title(raw_title, plain_description)
            if not is_valid:
                continue

            raw_author = post.get("author", "Anonymous")
            author = f"/u/{raw_author}"
            clean_author = raw_author.lstrip("u/").lstrip("/u/").strip()

            permalink = post.get("permalink", "")
            raw_url = f"https://www.reddit.com{permalink}" if permalink.startswith("/") else post.get("url", "")
            if not raw_url:
                continue
            canonical_apply_url = canonicalize_url(raw_url)

            subreddit = post.get("subreddit", "forhire")
            pm_url = f"https://www.reddit.com/message/compose/?to={clean_author}&subject={urllib.parse.quote_plus(clean_title[:50])}"

            created_utc = post.get("created_utc")
            iso_published = datetime.fromtimestamp(created_utc, tz=timezone.utc).isoformat() if created_utc else None

            lead_id = compute_lead_hash(self.source_name, canonical_apply_url, clean_title, author)
            raw_comp = self._extract_compensation(raw_title) or self._extract_compensation(plain_description)
            tech_stack = extract_core_tech_stack(f"{clean_title} {plain_description}")

            lead = Lead(
                id=lead_id,
                title=clean_title,
                source=self.source_name,
                client=author,
                url=canonical_apply_url,
                raw_compensation=raw_comp,
                description=plain_description,
                published_at=iso_published,
                core_tech_stack=tech_stack,
                raw_metadata={
                    "author": author,
                    "clean_author": clean_author,
                    "original_title": raw_title,
                    "subreddit": subreddit,
                    "pm_url": pm_url,
                    "reddit_id": post.get("name") or post.get("id", ""),
                    "score": post.get("score", 0),
                    "num_comments": post.get("num_comments", 0)
                }
            )

            try:
                lead.validate()
                leads.append(lead)
            except ValueError as ve:
                logger.warning(f"Discarding invalid Reddit lead: {ve}")

        return leads
