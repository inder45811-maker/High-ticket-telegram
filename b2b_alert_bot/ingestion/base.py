"""Base connector with resilient HTTP client, backoff retries, and offline fixture support."""

import email.utils
import html
import logging
import os
import random
import re
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple

import requests

from b2b_alert_bot.schema import Lead


logger = logging.getLogger(__name__)

DEFAULT_DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


def clean_html(raw_html: str) -> str:
    """Sanitize HTML into clean plaintext while preserving paragraph breaks."""
    if not raw_html:
        return ""
    # Unescape HTML entities first (e.g. &amp;, &lt;, &#x27;)
    text = html.unescape(raw_html)
    # Remove script and style tags completely
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # Replace block break tags with newlines
    text = re.sub(r"</?(?:br|p|div|li|h[1-6]|tr|td)[^>]*>", "\n", text, flags=re.IGNORECASE)
    # Strip all remaining inline tags
    text = re.sub(r"<[^>]+>", "", text)
    # Normalize whitespaces and blank lines
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    clean_lines: List[str] = []
    for line in lines:
        if line:
            clean_lines.append(line)
        elif clean_lines and clean_lines[-1] != "":
            clean_lines.append("")
    return "\n".join(clean_lines).strip()


def parse_timestamp(date_str: Optional[str]) -> Optional[str]:
    """Parse either RFC 2822 or ISO 8601 timestamps into standardized ISO 8601 UTC."""
    if not date_str or not isinstance(date_str, str):
        return None

    date_str = date_str.strip()
    # Attempt RFC 2822 parse (standard for RSS 2.0 pubDate)
    try:
        dt = email.utils.parsedate_to_datetime(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        pass

    # Attempt ISO 8601 parse (standard for Atom / JSON)
    try:
        normalized = date_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        pass

    return None


class BaseConnector(ABC):
    """Abstract base connector establishing resilient network patterns and offline testing."""

    source_name: str = "base"

    def __init__(
        self,
        user_agent: str = DEFAULT_DESKTOP_UA,
        timeout: Tuple[float, float] = (5.0, 15.0),
        max_retries: int = 3,
        base_delay: float = 2.0,
        backoff_multiplier: float = 2.0,
        session: Optional[requests.Session] = None
    ):
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.backoff_multiplier = backoff_multiplier
        self.session = session or requests.Session()

        # Caching state for conditional GET
        self.cached_etags: Dict[str, str] = {}
        self.cached_last_modified: Dict[str, str] = {}

    def get_default_headers(self, url: str) -> Dict[str, str]:
        """Construct request headers including desktop UA and conditional GET tags."""
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/rss+xml, application/atom+xml, application/json, text/xml, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate"
        }
        if url in self.cached_etags:
            headers["If-None-Match"] = self.cached_etags[url]
        if url in self.cached_last_modified:
            headers["If-Modified-Since"] = self.cached_last_modified[url]
        return headers

    def fetch_url(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None
    ) -> Optional[str]:
        """Fetch URL content with retry, exponential backoff, and full jitter."""
        req_headers = self.get_default_headers(url)
        if headers:
            req_headers.update(headers)

        attempt = 0
        while attempt <= self.max_retries:
            try:
                response = self.session.get(
                    url,
                    headers=req_headers,
                    params=params,
                    timeout=self.timeout
                )

                # HTTP 304 Not Modified
                if response.status_code == 304:
                    logger.info(f"Source {url} returned 304 Not Modified; feed unchanged.")
                    return None

                # HTTP 200 OK
                if response.status_code == 200:
                    if "ETag" in response.headers:
                        self.cached_etags[url] = response.headers["ETag"]
                    if "Last-Modified" in response.headers:
                        self.cached_last_modified[url] = response.headers["Last-Modified"]
                    return response.text

                # HTTP 429 Rate Limiting
                if response.status_code == 429:
                    # Check both standard Retry-After and x-ratelimit-reset (used by Reddit, GitHub, etc.)
                    retry_header = response.headers.get("Retry-After") or response.headers.get("x-ratelimit-reset")
                    if retry_header:
                        try:
                            sleep_time = max(1.0, float(retry_header) + 1.0)
                        except (ValueError, TypeError):
                            sleep_time = random.uniform(
                                self.base_delay,
                                self.base_delay * (self.backoff_multiplier ** attempt) + 1.0
                            )
                    else:
                        sleep_time = random.uniform(
                            self.base_delay,
                            self.base_delay * (self.backoff_multiplier ** attempt) + 1.0
                        )
                    logger.warning(f"Rate limited (429) on {url}. Retrying in {sleep_time:.2f}s...")
                    time.sleep(sleep_time)
                    attempt += 1
                    continue

                # 5xx Server Errors
                if 500 <= response.status_code < 600:
                    sleep_time = random.uniform(
                        0,
                        self.base_delay * (self.backoff_multiplier ** attempt)
                    )
                    logger.warning(
                        f"Server error {response.status_code} on {url}. "
                        f"Attempt {attempt + 1}/{self.max_retries + 1}. Retrying in {sleep_time:.2f}s..."
                    )
                    time.sleep(sleep_time)
                    attempt += 1
                    continue

                # 4xx Client Errors (do not retry)
                logger.error(f"Client error {response.status_code} on {url}. Skipping.")
                return None

            except (requests.exceptions.RequestException, requests.exceptions.Timeout) as e:
                attempt += 1
                if attempt > self.max_retries:
                    logger.error(f"Exhausted {self.max_retries} retries for {url}: {e}")
                    return None
                sleep_time = random.uniform(
                    0,
                    self.base_delay * (self.backoff_multiplier ** (attempt - 1))
                )
                logger.warning(f"Network error on {url}: {e}. Retrying in {sleep_time:.2f}s...")
                time.sleep(sleep_time)

        return None

    def fetch(
        self,
        source_input: Optional[str] = None
    ) -> List[Lead]:
        """Unified fetch interface supporting live network, file path, or raw payload string."""
        if source_input is not None:
            # Check if source_input is a path to an existing local fixture file
            if os.path.isfile(source_input):
                with open(source_input, "r", encoding="utf-8") as f:
                    content = f.read()
                return self.parse(content)
            # Otherwise treat as raw content string
            return self.parse(source_input)

        # Fallback to network fetch implementation in subclass
        return self.fetch_live()

    @abstractmethod
    def fetch_live(self) -> List[Lead]:
        """Perform live network retrieval across the connector's target endpoints."""
        pass

    @abstractmethod
    def parse(self, content: str) -> List[Lead]:
        """Parse raw content (XML, JSON, HTML) into standardized Lead objects."""
        pass
