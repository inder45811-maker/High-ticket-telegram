"""SQLite WAL persistence and SHA-256 deduplication store for B2B Alert Bot."""

import hashlib
import json
import sqlite3
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Optional, List, Tuple
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

from b2b_alert_bot.schema import Lead


TRACKING_PARAMS = {
    "ref", "source", "fbclid", "gclid", "subid", "mc_cid", "mc_eid",
    "partner", "campaign", "referrer"
}


def canonicalize_url(url: str) -> str:
    """Deterministically normalize and canonicalize URLs for deduplication.

    Rules applied:
    - Lowercase scheme and netloc (domain + port).
    - Upgrade http:// to https://.
    - Strip anchor fragments (#...).
    - Strip trailing slashes from path (except root '/').
    - Strip UTM parameters (utm_*) and common ad/tracking query parameters.
    - Sort remaining query parameters alphabetically.
    """
    if not url or not isinstance(url, str):
        return ""

    url = url.strip()
    url_lower = url.lower()
    if not (url_lower.startswith("http://") or url_lower.startswith("https://")):
        return url

    parsed = urlparse(url)

    scheme = "https"  # Normalize protocol to https
    netloc = parsed.netloc.lower()

    # Clean and normalize path
    path = parsed.path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    elif not path:
        path = ""

    # Strip tracking parameters and sort remaining
    kept_query: List[Tuple[str, str]] = []
    for k, v in parse_qsl(parsed.query, keep_blank_values=True):
        k_lower = k.lower()
        if k_lower.startswith("utm_") or k_lower in TRACKING_PARAMS:
            continue
        kept_query.append((k, v))

    kept_query.sort(key=lambda item: item[0])
    clean_query = urlencode(kept_query)

    # Reassemble without fragment
    return urlunparse((scheme, netloc, path, "", clean_query, ""))


def compute_lead_hash(source: str, url: str, title: str = "", company: Optional[str] = "") -> str:
    """Generate a deterministic 64-character SHA-256 hexadecimal hash for lead deduplication."""
    clean_url = canonicalize_url(url)
    if clean_url and (clean_url.lower().startswith("http://") or clean_url.lower().startswith("https://")):
        return hashlib.sha256(clean_url.encode("utf-8")).hexdigest()

    # Fallback composite hash for items lacking external URLs (e.g. anonymous comments)
    norm_source = (source or "").lower().strip()
    norm_company = (company or "").lower().strip()
    norm_title = "".join(c for c in (title or "").lower() if c.isalnum())
    composite = f"{norm_source}:{norm_company}:{norm_title}"
    return hashlib.sha256(composite.encode("utf-8")).hexdigest()


class LRUCache:
    """Fixed-capacity in-memory set cache for O(1) hash lookups."""
    def __init__(self, capacity: int = 10000):
        self.capacity = capacity
        self.cache: OrderedDict[str, bool] = OrderedDict()

    def contains(self, key: str) -> bool:
        if key in self.cache:
            self.cache.move_to_end(key)
            return True
        return False

    def add(self, key: str) -> None:
        if key in self.cache:
            self.cache.move_to_end(key)
        else:
            if len(self.cache) >= self.capacity:
                self.cache.popitem(last=False)
            self.cache[key] = True

    def __len__(self) -> int:
        return len(self.cache)


class Database:
    """SQLite persistent storage with WAL mode, LRU caching, and atomic deduplication."""

    def __init__(self, db_path: str = "leads.db", cache_capacity: int = 10000):
        self.db_path = db_path
        self._lru_cache = LRUCache(capacity=cache_capacity)
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30.0)
            conn.row_factory = sqlite3.Row
            # Configure WAL mode & performance pragmas
            conn.execute("PRAGMA busy_timeout = 30000;")
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            conn.execute("PRAGMA temp_store = MEMORY;")
            conn.execute("PRAGMA cache_size = -64000;")  # 64MB cache
            self._conn = conn
        return self._conn

    def _init_db(self) -> None:
        """Create tables and indices if they do not exist."""
        for attempt in range(10):
            try:
                conn = self._get_connection()
                with conn:
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS ingested_leads (
                            id TEXT PRIMARY KEY,
                            source TEXT NOT NULL,
                            title TEXT NOT NULL,
                            client TEXT,
                            url TEXT NOT NULL,
                            raw_compensation TEXT,
                            description TEXT,
                            published_at TEXT,
                            core_tech_stack TEXT,
                            raw_metadata TEXT,
                            status TEXT NOT NULL DEFAULT 'ingested',
                            created_at TEXT NOT NULL
                        );
                    """)
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_source ON ingested_leads(source);")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_status ON ingested_leads(status);")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_created_at ON ingested_leads(created_at);")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_published_at ON ingested_leads(published_at);")

                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS linkedin_posts (
                            id TEXT PRIMARY KEY,
                            lead_id TEXT NOT NULL,
                            post_urn TEXT,
                            post_text TEXT,
                            published_at TEXT NOT NULL,
                            FOREIGN KEY(lead_id) REFERENCES ingested_leads(id)
                        );
                    """)
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_li_lead_id ON linkedin_posts(lead_id);")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_li_published_at ON linkedin_posts(published_at);")

                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS x_posts (
                            id TEXT PRIMARY KEY,
                            lead_id TEXT NOT NULL,
                            tweet_id TEXT,
                            tweet_text TEXT,
                            published_at TEXT NOT NULL,
                            FOREIGN KEY(lead_id) REFERENCES ingested_leads(id)
                        );
                    """)
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_x_lead_id ON x_posts(lead_id);")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_x_published_at ON x_posts(published_at);")
                break
            except sqlite3.OperationalError:
                if attempt == 9:
                    raise
                time.sleep(0.05 * (attempt + 1))

    def is_duplicate(self, unique_hash: str) -> bool:
        """Check if a lead has already been ingested. O(1) in-memory or indexed query."""
        if not unique_hash:
            return False

        # 1. Check in-memory LRU cache
        if self._lru_cache.contains(unique_hash):
            return True

        # 2. Check SQLite primary key index
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM ingested_leads WHERE id = ? LIMIT 1;", (unique_hash,))
        exists = cursor.fetchone() is not None

        if exists:
            self._lru_cache.add(unique_hash)

        return exists

    def insert_lead(self, lead: Lead, status: str = "ingested") -> bool:
        """Atomically insert lead if not exists.

        Returns:
            True if newly inserted, False if duplicate.
        """
        lead.validate()

        # In-memory fast pre-check
        if self._lru_cache.contains(lead.id):
            return False

        conn = self._get_connection()
        created_at = datetime.now(timezone.utc).isoformat()

        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT OR IGNORE INTO ingested_leads (
                        id, source, title, client, url, raw_compensation,
                        description, published_at, core_tech_stack, raw_metadata,
                        status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """, (
                    lead.id,
                    lead.source,
                    lead.title,
                    lead.client,
                    lead.url,
                    lead.raw_compensation,
                    lead.description,
                    lead.published_at,
                    json.dumps(lead.core_tech_stack),
                    json.dumps(lead.raw_metadata),
                    status,
                    created_at
                ))
                inserted = cursor.rowcount > 0

            # Add to cache regardless (if inserted or already existed)
            self._lru_cache.add(lead.id)
            return inserted
        except sqlite3.Error as e:
            raise RuntimeError(f"Database error during lead insertion: {e}") from e

    def get_lead(self, lead_id: str) -> Optional[Lead]:
        """Retrieve a stored lead by unique ID."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM ingested_leads WHERE id = ? LIMIT 1;", (lead_id,))
        row = cursor.fetchone()
        if not row:
            return None

        return Lead(
            id=row["id"],
            source=row["source"],
            title=row["title"],
            client=row["client"] or "",
            url=row["url"],
            raw_compensation=row["raw_compensation"] or "",
            description=row["description"] or "",
            published_at=row["published_at"],
            core_tech_stack=json.loads(row["core_tech_stack"] or "[]"),
            raw_metadata=json.loads(row["raw_metadata"] or "{}")
        )

    def get_leads_by_status(self, status: str, limit: int = 100) -> List[Lead]:
        """Fetch leads matching a specific status (e.g. 'ingested', 'enriched')."""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM ingested_leads WHERE status = ? ORDER BY created_at ASC LIMIT ?;",
            (status, limit)
        )
        rows = cursor.fetchall()

        results = []
        for row in rows:
            results.append(Lead(
                id=row["id"],
                source=row["source"],
                title=row["title"],
                client=row["client"] or "",
                url=row["url"],
                raw_compensation=row["raw_compensation"] or "",
                description=row["description"] or "",
                published_at=row["published_at"],
                core_tech_stack=json.loads(row["core_tech_stack"] or "[]"),
                raw_metadata=json.loads(row["raw_metadata"] or "{}")
            ))
        return results

    def update_status(self, lead_id: str, new_status: str) -> bool:
        """Update processing status of a lead."""
        conn = self._get_connection()
        with conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE ingested_leads SET status = ? WHERE id = ?;", (new_status, lead_id))
            return cursor.rowcount > 0

    def count_leads(self, status: Optional[str] = None) -> int:
        """Count total leads or leads matching status."""
        conn = self._get_connection()
        cursor = conn.cursor()
        if status:
            cursor.execute("SELECT COUNT(*) FROM ingested_leads WHERE status = ?;", (status,))
        else:
            cursor.execute("SELECT COUNT(*) FROM ingested_leads;")
        row = cursor.fetchone()
        return row[0] if row else 0

    def prune_leads(self, days: int = 90) -> int:
        """Delete leads older than specified days."""
        conn = self._get_connection()
        with conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM ingested_leads WHERE created_at < datetime('now', ?);", (f"-{days} days",))
            return cursor.rowcount

    def record_linkedin_post(self, post_id: str, lead_id: str, post_urn: str, post_text: str) -> None:
        """Record an autonomously published LinkedIn post."""
        conn = self._get_connection()
        now = datetime.now(timezone.utc).isoformat()
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO linkedin_posts (id, lead_id, post_urn, post_text, published_at) VALUES (?, ?, ?, ?, ?);",
                (post_id, lead_id, post_urn, post_text, now)
            )

    def is_lead_posted_to_linkedin(self, lead_id: str) -> bool:
        """Check if a lead has already been published to LinkedIn."""
        if not lead_id:
            return False
        conn = self._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM linkedin_posts WHERE lead_id = ? LIMIT 1;", (lead_id,))
        return cur.fetchone() is not None

    def get_last_linkedin_post_time(self) -> Optional[datetime]:
        """Get timestamp of the most recent LinkedIn post, or None if none exist."""
        conn = self._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT published_at FROM linkedin_posts ORDER BY published_at DESC LIMIT 1;")
        row = cur.fetchone()
        if row and row[0]:
            try:
                return datetime.fromisoformat(row[0])
            except Exception:
                return None
        return None

    def record_x_post(self, post_id: str, lead_id: str, tweet_id: str, tweet_text: str) -> None:
        """Record an autonomously published X/Twitter tweet."""
        conn = self._get_connection()
        now = datetime.now(timezone.utc).isoformat()
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO x_posts (id, lead_id, tweet_id, tweet_text, published_at) VALUES (?, ?, ?, ?, ?);",
                (post_id, lead_id, tweet_id, tweet_text, now)
            )

    def is_lead_posted_to_x(self, lead_id: str) -> bool:
        """Check if a lead has already been published to X/Twitter."""
        if not lead_id:
            return False
        conn = self._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM x_posts WHERE lead_id = ? LIMIT 1;", (lead_id,))
        return cur.fetchone() is not None

    def get_last_x_post_time(self) -> Optional[datetime]:
        """Get timestamp of the most recent X/Twitter post, or None if none exist."""
        conn = self._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT published_at FROM x_posts ORDER BY published_at DESC LIMIT 1;")
        row = cur.fetchone()
        if row and row[0]:
            try:
                return datetime.fromisoformat(row[0])
            except Exception:
                return None
        return None

    def close(self) -> None:
        """Close SQLite database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
