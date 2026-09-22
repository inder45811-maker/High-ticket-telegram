"""Comprehensive unit and fixture tests for Milestone 1: Lead Ingestion & Reliable Source Connectors."""

import hashlib
import json
import os
from unittest.mock import MagicMock, patch

import pytest

from b2b_alert_bot.db import (
    Database,
    canonicalize_url,
    compute_lead_hash,
    LRUCache
)
from b2b_alert_bot.ingestion.base import (
    clean_html,
    parse_timestamp,
    DEFAULT_DESKTOP_UA
)
from b2b_alert_bot.ingestion.weworkremotely import WeWorkRemotelyConnector
from b2b_alert_bot.ingestion.remoteok import RemoteOKConnector
from b2b_alert_bot.ingestion.jobspresso import JobspressoConnector
from b2b_alert_bot.ingestion.hackernews import HackerNewsConnector
from b2b_alert_bot.ingestion.reddit import RedditConnector
from b2b_alert_bot.schema import (
    Lead,
    EnrichedLead,
    extract_core_tech_stack
)


FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


# ==============================================================================
# 1. Schema & Validation Tests
# ==============================================================================

class TestSchema:
    def test_lead_valid_creation(self):
        lead_id = hashlib.sha256(b"https://example.com/job/123").hexdigest()
        lead = Lead(
            id=lead_id,
            title="Senior Backend Engineer",
            source="weworkremotely",
            client="Acme Corp",
            url="https://example.com/job/123",
            raw_compensation="$120k - $150k",
            description="Looking for senior engineer",
            published_at="2026-09-20T10:00:00+00:00",
            core_tech_stack=["Python", "FastAPI"],
            raw_metadata={"region": "Worldwide"}
        )
        assert lead.validate() is True
        assert lead.client == "Acme Corp"
        assert "Python" in lead.core_tech_stack

    def test_lead_validation_errors(self):
        valid_id = "a" * 64
        # Invalid hash length
        with pytest.raises(ValueError, match="Invalid lead id"):
            Lead(id="too_short", title="Dev", source="reddit", client="A", url="https://example.com").validate()

        # Non-hex characters
        with pytest.raises(ValueError, match="non-hex characters"):
            Lead(id="z" * 64, title="Dev", source="reddit", client="A", url="https://example.com").validate()

        # Empty title
        with pytest.raises(ValueError, match="title cannot be empty"):
            Lead(id=valid_id, title="  ", source="reddit", client="A", url="https://example.com").validate()

        # Empty source
        with pytest.raises(ValueError, match="source cannot be empty"):
            Lead(id=valid_id, title="Dev", source="", client="A", url="https://example.com").validate()

        # Invalid URL
        with pytest.raises(ValueError, match="Invalid lead URL"):
            Lead(id=valid_id, title="Dev", source="reddit", client="A", url="ftp://example.com").validate()

    def test_lead_to_and_from_dict(self):
        lead_id = "1" * 64
        lead = Lead(
            id=lead_id,
            title="Fullstack Developer",
            source="remoteok",
            client="Tech Corp",
            url="https://example.com/apply",
            raw_compensation="$90/hr",
            description="Exciting project",
            published_at="2026-09-20T12:00:00Z",
            core_tech_stack=["React", "Node.js"],
            raw_metadata={"epoch": 12345678}
        )
        data = lead.to_dict()
        assert isinstance(data, dict)
        assert data["id"] == lead_id
        assert data["core_tech_stack"] == ["React", "Node.js"]

        reconstructed = Lead.from_dict(data)
        assert reconstructed.id == lead.id
        assert reconstructed.title == lead.title
        assert reconstructed.core_tech_stack == lead.core_tech_stack
        assert reconstructed.raw_metadata == lead.raw_metadata

    def test_enriched_lead_serialization(self):
        lead = Lead(
            id="2" * 64,
            title="Lead Engineer",
            source="jobspresso",
            client="Global Inc",
            url="https://example.com/job"
        )
        enriched = EnrichedLead(
            lead=lead,
            is_high_ticket=True,
            rate_type="hourly",
            min_amount=75.0,
            max_amount=100.0,
            currency="USD",
            budget_badge="⏱️ $75/HR",
            scope_bullet="Build distributed architecture",
            skills_bullet="Python, Docker, Kubernetes",
            winning_angle="Highlight past migration experience"
        )
        d = enriched.to_dict()
        assert d["is_high_ticket"] is True
        assert d["lead"]["title"] == "Lead Engineer"
        assert d["budget_badge"] == "⏱️ $75/HR"

    def test_extract_core_tech_stack(self):
        sample_text = (
            "We are seeking a senior Python and FastAPI developer with strong PostgreSQL "
            "and Docker expertise. Familiarity with React and AWS is a huge plus. Go or Golang preferred."
        )
        techs = extract_core_tech_stack(sample_text)
        assert "Python" in techs
        assert "FastAPI" in techs
        assert "PostgreSQL" in techs
        assert "Docker" in techs
        assert "React" in techs
        assert "AWS" in techs
        assert "Go" in techs


# ==============================================================================
# 2. Database, URL Normalization & Deduplication Tests
# ==============================================================================

class TestDatabaseAndDeduplication:
    def test_canonicalize_url(self):
        # 1. Lowercasing domain and protocol upgrade
        url1 = "HTTP://WeWorkRemotely.com/job/123/"
        assert canonicalize_url(url1) == "https://weworkremotely.com/job/123"

        # 2. Stripping UTM query parameters
        url2 = "https://example.com/job?utm_source=twitter&utm_medium=feed&utm_campaign=launch&id=99"
        assert canonicalize_url(url2) == "https://example.com/job?id=99"

        # 3. Stripping ad and tracking tags
        url3 = "https://example.com/job?ref=jobboard&fbclid=abcdef&gclid=12345&keep=me"
        assert canonicalize_url(url3) == "https://example.com/job?keep=me"

        # 4. Sorting query parameters deterministically
        url4 = "https://example.com/job?z=3&a=1&m=2"
        assert canonicalize_url(url4) == "https://example.com/job?a=1&m=2&z=3"

        # 5. Stripping anchor fragments
        url5 = "https://example.com/job/dev#apply-now"
        assert canonicalize_url(url5) == "https://example.com/job/dev"

        # 6. Trailing slash on root path
        url6 = "https://example.com/"
        assert canonicalize_url(url6) == "https://example.com/"

    def test_compute_lead_hash_deduplication(self):
        # URLs with different tracking parameters produce identical SHA-256 hashes
        url_a = "https://weworkremotely.com/remote-jobs/acme-eng?utm_source=google"
        url_b = "https://weworkremotely.com/remote-jobs/acme-eng?ref=newsletter#top"
        url_c = "https://weworkremotely.com/remote-jobs/acme-eng/"

        hash_a = compute_lead_hash("weworkremotely", url_a, "Engineer", "Acme")
        hash_b = compute_lead_hash("weworkremotely", url_b, "Engineer", "Acme")
        hash_c = compute_lead_hash("weworkremotely", url_c, "Engineer", "Acme")

        assert len(hash_a) == 64
        assert hash_a == hash_b == hash_c

    def test_composite_hash_fallback(self):
        # Non-URL item generates deterministic composite hash
        h1 = compute_lead_hash("hackernews", "", "Senior Developer", "Startup Co")
        h2 = compute_lead_hash("hackernews", "", "Senior Developer", "Startup Co")
        assert len(h1) == 64
        assert h1 == h2

    def test_lru_cache(self):
        cache = LRUCache(capacity=3)
        cache.add("k1")
        cache.add("k2")
        cache.add("k3")
        assert cache.contains("k1") is True
        assert cache.contains("k2") is True
        # Access k1 to make it recently used
        assert cache.contains("k1") is True
        # Add k4, k3 should be evicted or k2 depending on order
        cache.add("k4")
        assert len(cache) == 3

    def test_database_wal_and_operations(self, tmp_path):
        db_file = str(tmp_path / "test_wal.db")
        db = Database(db_path=db_file)

        # Check WAL mode is enabled
        conn = db._get_connection()
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode;")
        row = cursor.fetchone()
        assert row[0].lower() == "wal"

        lead_id = hashlib.sha256(b"https://example.com/unique-job").hexdigest()
        lead = Lead(
            id=lead_id,
            title="Senior Architect",
            source="weworkremotely",
            client="Acme",
            url="https://example.com/unique-job",
            raw_compensation="$150,000",
            description="Architecture work",
            published_at="2026-09-20T00:00:00Z",
            core_tech_stack=["Python", "AWS"]
        )

        # First insert succeeds
        assert db.insert_lead(lead) is True
        assert db.count_leads() == 1

        # Duplicate check returns True
        assert db.is_duplicate(lead_id) is True

        # Second insert returns False (ignored atomically)
        assert db.insert_lead(lead) is False
        assert db.count_leads() == 1

        # Retrieve lead
        fetched = db.get_lead(lead_id)
        assert fetched is not None
        assert fetched.id == lead_id
        assert fetched.title == "Senior Architect"
        assert fetched.core_tech_stack == ["Python", "AWS"]

        # Status tracking & updates
        leads_ingested = db.get_leads_by_status("ingested")
        assert len(leads_ingested) == 1
        assert leads_ingested[0].id == lead_id

        assert db.update_status(lead_id, "enriched") is True
        assert len(db.get_leads_by_status("ingested")) == 0
        assert len(db.get_leads_by_status("enriched")) == 1

        db.close()


# ==============================================================================
# 3. BaseConnector & Network Resilience Tests
# ==============================================================================

class TestBaseConnector:
    def test_clean_html(self):
        raw = (
            "<div><p>Hello &amp; welcome to <b>Acme Corp</b>!</p>"
            "<script>alert('xss');</script>"
            "<p>We offer $50/hr.<br>Apply now.</p></div>"
        )
        cleaned = clean_html(raw)
        assert "Hello & welcome to Acme Corp!" in cleaned
        assert "alert('xss')" not in cleaned
        assert "<script>" not in cleaned
        assert "$50/hr." in cleaned

    def test_parse_timestamp(self):
        # RFC 2822
        rfc_date = "Mon, 07 Sep 2026 07:30:54 +0000"
        iso_res = parse_timestamp(rfc_date)
        assert iso_res is not None
        assert "2026-09-07" in iso_res

        # ISO 8601 with Z
        iso_date = "2026-09-19T08:00:34Z"
        iso_res2 = parse_timestamp(iso_date)
        assert iso_res2 is not None
        assert "2026-09-19" in iso_res2

        # Invalid returns None
        assert parse_timestamp("invalid-date") is None
        assert parse_timestamp("") is None
        assert parse_timestamp(None) is None

    def test_base_connector_headers(self):
        connector = WeWorkRemotelyConnector()
        headers = connector.get_default_headers("https://example.com")
        assert headers["User-Agent"] == DEFAULT_DESKTOP_UA
        assert "application/rss+xml" in headers["Accept"]

        # Conditional GET headers
        connector.cached_etags["https://example.com"] = '"etag123"'
        connector.cached_last_modified["https://example.com"] = "Mon, 07 Sep 2026 00:00:00 GMT"
        headers_cond = connector.get_default_headers("https://example.com")
        assert headers_cond["If-None-Match"] == '"etag123"'
        assert headers_cond["If-Modified-Since"] == "Mon, 07 Sep 2026 00:00:00 GMT"

    @patch("time.sleep", return_value=None)
    def test_fetch_url_retries_and_exponential_backoff(self, mock_sleep):
        session_mock = MagicMock()
        connector = WeWorkRemotelyConnector(session=session_mock, max_retries=2, base_delay=0.1)

        # 1. 429 Too Many Requests -> retry -> 200 OK
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.headers = {"Retry-After": "1"}

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.headers = {"ETag": '"etag99"'}
        resp_200.text = "<rss></rss>"

        session_mock.get.side_effect = [resp_429, resp_200]
        res = connector.fetch_url("https://example.com/feed")
        assert res == "<rss></rss>"
        assert connector.cached_etags["https://example.com/feed"] == '"etag99"'
        assert mock_sleep.called

    @patch("time.sleep", return_value=None)
    def test_fetch_url_304_not_modified(self, mock_sleep):
        session_mock = MagicMock()
        connector = WeWorkRemotelyConnector(session=session_mock)

        resp_304 = MagicMock()
        resp_304.status_code = 304
        session_mock.get.return_value = resp_304

        res = connector.fetch_url("https://example.com/feed")
        assert res is None

    @patch("time.sleep", return_value=None)
    def test_fetch_url_404_no_retry(self, mock_sleep):
        session_mock = MagicMock()
        connector = WeWorkRemotelyConnector(session=session_mock)

        resp_404 = MagicMock()
        resp_404.status_code = 404
        session_mock.get.return_value = resp_404

        res = connector.fetch_url("https://example.com/notfound")
        assert res is None
        assert session_mock.get.call_count == 1
        assert not mock_sleep.called


# ==============================================================================
# 4. WeWorkRemotelyConnector Tests
# ==============================================================================

class TestWeWorkRemotelyConnector:
    def test_parse_wwr_fixture(self):
        fixture_path = os.path.join(FIXTURES_DIR, "wwr_sample.xml")
        connector = WeWorkRemotelyConnector()
        leads = connector.fetch(fixture_path)

        assert len(leads) == 3

        # Lead 1: Legion
        lead1 = leads[0]
        assert lead1.client == "Legion"
        assert lead1.title == "Director of Production Engineering"
        assert lead1.source == "weworkremotely"
        assert "https://weworkremotely.com/remote-jobs/legion-director-of-production-engineering" in lead1.url
        assert "utm_source" not in lead1.url  # Canonicalized
        assert "$150,000 - $180,000" in lead1.raw_compensation
        assert lead1.raw_metadata["region"] == "Anywhere in the World"
        assert lead1.raw_metadata["category"] == "Full-Stack Programming"
        assert "Kubernetes" in lead1.core_tech_stack
        assert "Python" in lead1.core_tech_stack

        # Lead 2: Acme Corp
        lead2 = leads[1]
        assert lead2.client == "Acme Corp"
        assert lead2.title == "Senior Python / FastAPI Architect"
        assert "$90/hr" in lead2.raw_compensation
        assert "FastAPI" in lead2.core_tech_stack
        assert "Redis" in lead2.core_tech_stack

        # Lead 3: Without colon
        lead3 = leads[2]
        assert lead3.client == "Unknown"
        assert lead3.title == "DevOps Consultant"
        assert "$5,000" in lead3.raw_compensation

        for lead in leads:
            assert lead.validate() is True


# ==============================================================================
# 5. RemoteOKConnector Tests
# ==============================================================================

class TestRemoteOKConnector:
    def test_parse_remoteok_fixture(self):
        fixture_path = os.path.join(FIXTURES_DIR, "remoteok_sample.json")
        connector = RemoteOKConnector()
        leads = connector.fetch(fixture_path)

        # Index 0 is legal notice and MUST be skipped -> exactly 2 leads parsed
        assert len(leads) == 2

        # Lead 1: KPI Solutions
        lead1 = leads[0]
        assert lead1.client == "KPI Solutions"
        assert lead1.title == "Software Deployment Engineer"
        assert lead1.source == "remoteok"
        assert "$80,000 - $140,000/yr" in lead1.raw_compensation
        assert lead1.raw_metadata["salary_min"] == 80000
        assert lead1.raw_metadata["salary_max"] == 140000
        assert "golang" in lead1.core_tech_stack or "Go" in lead1.core_tech_stack
        assert "Docker" in lead1.core_tech_stack

        # Lead 2: TechFlow Systems (salary unstated: 0)
        lead2 = leads[1]
        assert lead2.client == "TechFlow Systems"
        assert lead2.title == "Lead React / Next.js Engineer"
        assert lead2.raw_compensation == ""  # Zero salary unstated
        assert "react" in lead2.core_tech_stack or "React" in lead2.core_tech_stack

        for lead in leads:
            assert lead.validate() is True


# ==============================================================================
# 6. JobspressoConnector Tests
# ==============================================================================

class TestJobspressoConnector:
    def test_parse_jobspresso_fixture(self):
        fixture_path = os.path.join(FIXTURES_DIR, "jobspresso_sample.xml")
        connector = JobspressoConnector()
        leads = connector.fetch(fixture_path)

        assert len(leads) == 2

        # Lead 1: Hopper (parsed from dc:creator 'Hopper<br>⚲&nbsp;Canada')
        lead1 = leads[0]
        assert lead1.client == "Hopper"
        assert lead1.title == "Senior Full Stack Engineer, Realtime & Voice"
        assert lead1.source == "jobspresso"
        assert lead1.raw_metadata["location"] == "Canada"
        assert "$120,000 - $160,000" in lead1.raw_compensation
        assert "Python" in lead1.core_tech_stack
        assert "React" in lead1.core_tech_stack
        assert "utm_campaign" not in lead1.url

        # Lead 2: Stripe
        lead2 = leads[1]
        assert lead2.client == "Stripe"
        assert lead2.title == "Cloud Infrastructure Consultant"
        assert "$95/hr" in lead2.raw_compensation
        assert "Terraform" in lead2.core_tech_stack
        assert "Kubernetes" in lead2.core_tech_stack

        for lead in leads:
            assert lead.validate() is True


# ==============================================================================
# 7. HackerNewsConnector Tests
# ==============================================================================

class TestHackerNewsConnector:
    def test_parse_hn_fixture(self):
        fixture_path = os.path.join(FIXTURES_DIR, "hn_algolia_sample.json")
        connector = HackerNewsConnector()
        leads = connector.fetch(fixture_path)

        # Fixture contains 4 hits:
        # 1. Top-level hiring (parent_id == story_id) -> KEEP
        # 2. Nested reply (parent_id != story_id) -> DROP
        # 3. Seeking work (SEEKING WORK) -> DROP
        # 4. Freelancer posting (SEEKING FREELANCER) -> KEEP
        assert len(leads) == 2

        # Lead 1: Lumen Labs
        lead1 = leads[0]
        assert lead1.client == "Lumen Labs"
        assert "Robotics / Hardware Engineer" in lead1.title
        assert lead1.source == "hackernews"
        assert "$80-$120/hr" in lead1.raw_compensation or "$80" in lead1.raw_compensation
        assert "Python" in lead1.core_tech_stack
        assert "https://news.ycombinator.com/item?id=49522910" == lead1.url

        # Lead 2: CloudScale Solutions
        lead2 = leads[1]
        assert lead2.client == "CloudScale Solutions"
        assert "Senior DevOps / Terraform Architect" in lead2.title
        assert "$90 - $110/hr" in lead2.raw_compensation or "$90" in lead2.raw_compensation
        assert "Terraform" in lead2.core_tech_stack
        assert "Kubernetes" in lead2.core_tech_stack

        for lead in leads:
            assert lead.validate() is True


# ==============================================================================
# 8. RedditConnector Tests
# ==============================================================================

class TestRedditConnector:
    def test_parse_reddit_fixture(self):
        fixture_path = os.path.join(FIXTURES_DIR, "reddit_atom_sample.xml")
        connector = RedditConnector()
        leads = connector.fetch(fixture_path)

        # Fixture entries:
        # 1. [Hiring] Python & FastAPI Architect ($75/hr) -> KEEP
        # 2. [For Hire] Freelancer candidate -> DROP
        # 3. Rules Reminder mod sticky -> DROP
        # 4. [HIRING] Shopify Theme Overhaul ($3,500 fixed) -> KEEP
        assert len(leads) == 2

        # Lead 1: Python/FastAPI
        lead1 = leads[0]
        assert lead1.client == "/u/agency_lead"
        assert "[Hiring]" not in lead1.title
        assert "Senior Python & FastAPI Backend Architect" in lead1.title
        assert "$75/hr" in lead1.raw_compensation
        assert "FastAPI" in lead1.core_tech_stack
        assert "Python" in lead1.core_tech_stack
        assert "utm_source" not in lead1.url

        # Lead 2: Shopify Developer
        lead2 = leads[1]
        assert lead2.client == "/u/ecom_brand"
        assert "[HIRING]" not in lead2.title
        assert "Shopify & Liquid Developer" in lead2.title
        assert "$3,500 fixed" in lead2.raw_compensation or "$3,500" in lead2.raw_compensation
        assert "Shopify" in lead2.core_tech_stack

        for lead in leads:
            assert lead.validate() is True


# ==============================================================================
# 9. End-to-End Pipeline & WAL Integration Test
# ==============================================================================

class TestEndToEndIntegration:
    def test_multi_source_ingestion_and_deduplication(self, tmp_path):
        db_path = str(tmp_path / "production_leads.db")
        db = Database(db_path=db_path)

        connectors = [
            (WeWorkRemotelyConnector(), os.path.join(FIXTURES_DIR, "wwr_sample.xml")),
            (RemoteOKConnector(), os.path.join(FIXTURES_DIR, "remoteok_sample.json")),
            (JobspressoConnector(), os.path.join(FIXTURES_DIR, "jobspresso_sample.xml")),
            (HackerNewsConnector(), os.path.join(FIXTURES_DIR, "hn_algolia_sample.json")),
            (RedditConnector(), os.path.join(FIXTURES_DIR, "reddit_atom_sample.xml")),
        ]

        total_ingested = 0
        all_leads = []

        for connector, fixture in connectors:
            leads = connector.fetch(fixture)
            assert len(leads) > 0, f"Connector {connector.source_name} yielded 0 leads"
            for lead in leads:
                inserted = db.insert_lead(lead)
                assert inserted is True, f"Lead {lead.id} should be newly inserted"
                total_ingested += 1
                all_leads.append(lead)

        assert total_ingested == 3 + 2 + 2 + 2 + 2  # 11 total leads
        assert db.count_leads() == 11

        # Second ingestion cycle: all 11 must be recognized as duplicates and ignored
        second_cycle_inserted = 0
        for connector, fixture in connectors:
            leads = connector.fetch(fixture)
            for lead in leads:
                if db.insert_lead(lead):
                    second_cycle_inserted += 1

        assert second_cycle_inserted == 0
        assert db.count_leads() == 11

        # Check queries by status
        ingested_leads = db.get_leads_by_status("ingested", limit=50)
        assert len(ingested_leads) == 11

        # Test updating lead status to 'enriched'
        sample_lead_id = all_leads[0].id
        assert db.update_status(sample_lead_id, "enriched") is True
        assert len(db.get_leads_by_status("enriched")) == 1
        assert len(db.get_leads_by_status("ingested")) == 10

        db.close()


# ==============================================================================
# 10. Edge Cases, Malformed Payloads & Security Defenses
# ==============================================================================

class TestEdgeCasesAndDefenses:
    def test_malformed_xml_safety(self):
        malformed = "<rss><channel><item><title>Broken XML"
        assert WeWorkRemotelyConnector().parse(malformed) == []
        assert JobspressoConnector().parse(malformed) == []
        assert RedditConnector().parse(malformed) == []

    def test_xml_entity_expansion_defense(self):
        xml_bomb = (
            '<!DOCTYPE lolz ['
            '<!ENTITY lol "lol">'
            '<!ELEMENT lolz (#PCDATA)>'
            ']><rss><channel><item><title>&lol;</title></item></channel></rss>'
        )
        assert WeWorkRemotelyConnector().parse(xml_bomb) == []
        assert JobspressoConnector().parse(xml_bomb) == []
        assert RedditConnector().parse(xml_bomb) == []

    def test_malformed_json_safety(self):
        assert RemoteOKConnector().parse("{invalid json") == []
        assert RemoteOKConnector().parse('{"not": "a list"}') == []
        assert HackerNewsConnector().parse("{invalid json") == []
        assert HackerNewsConnector().parse('"not an object or list"') == []

    def test_empty_payloads(self):
        for connector in [
            WeWorkRemotelyConnector(),
            RemoteOKConnector(),
            JobspressoConnector(),
            HackerNewsConnector(),
            RedditConnector(),
        ]:
            assert connector.parse("") == []
            assert connector.parse("   ") == []
            assert connector.fetch("") == []

    def test_direct_raw_string_fetch(self):
        # Verify that connector.fetch(raw_string) parses string directly
        raw_remoteok = json.dumps([
            {"legal": "disclaimer"},
            {
                "id": 9999,
                "company": "Direct String Corp",
                "position": "Senior Engineer",
                "apply_url": "https://example.com/apply/9999",
                "tags": ["python", "fastapi"]
            }
        ])
        leads = RemoteOKConnector().fetch(raw_remoteok)
        assert len(leads) == 1
        assert leads[0].client == "Direct String Corp"
        assert leads[0].title == "Senior Engineer"

    def test_database_prune_and_missing_records(self, tmp_path):
        db = Database(db_path=str(tmp_path / "prune.db"))
        # Nonexistent record returns None
        assert db.get_lead("nonexistent_id") is None
        # Nonexistent update returns False
        assert db.update_status("nonexistent_id", "status") is False
        # Prune with 0 leads returns 0
        assert db.prune_leads(days=30) == 0
        db.close()

    def test_database_context_manager(self, tmp_path):
        db_path = str(tmp_path / "ctx.db")
        with Database(db_path=db_path) as db:
            assert db.count_leads() == 0

    @patch.object(RedditConnector, "fetch_url")
    def test_reddit_live_fetch_mock(self, mock_fetch_url):
        mock_fetch_url.return_value = """<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <id>t3_test1</id>
            <title>[Hiring] Contract Cloud Architect ($100/hr)</title>
            <link href="https://reddit.com/r/forhire/comments/test1/hiring/"/>
            <author><name>/u/test_user</name></author>
            <updated>2026-09-20T10:00:00Z</updated>
            <content type="html">&lt;p&gt;Need AWS cloud architect.&lt;/p&gt;</content>
          </entry>
        </feed>"""
        connector = RedditConnector()
        leads = connector.fetch()  # No source_input -> invokes fetch_live() -> calls fetch_url
        assert len(leads) == 1
        assert leads[0].client == "/u/test_user"
        assert "$100/hr" in leads[0].raw_compensation

    @patch.object(RemoteOKConnector, "fetch_url")
    def test_remoteok_live_fetch_mock(self, mock_fetch_url):
        mock_fetch_url.return_value = json.dumps([
            {"legal": "disclaimer"},
            {
                "id": 5555,
                "company": "Live Mock Co",
                "position": "Backend Go Developer",
                "url": "https://remoteok.com/remote-jobs/5555",
                "salary_min": 90000,
                "salary_max": 120000
            }
        ])
        connector = RemoteOKConnector()
        leads = connector.fetch()
        assert len(leads) == 1
        assert leads[0].client == "Live Mock Co"
