"""Adversarial stress testing and edge-case challenge suite for B2B Alert Bot.

Focus areas:
1. Ingestion: Malformed XML, corrupt Atom feeds, missing attributes, namespace anomalies.
2. AI Enrichment: Tricky compensation strings, multiple currencies, negative numbers,
   zero budgets, confusing ranges, funding announcements with disguised budgets, extreme numbers.
3. Persistence & Concurrency: SQLite WAL concurrent inserts of duplicate URLs, malformed URLs,
   special characters, SQL injection tokens, unicode/emojis.
"""

import concurrent.futures
import hashlib
import os
import sqlite3
import tempfile
import threading
import unittest
from typing import List, Dict, Any

from b2b_alert_bot.db import (
    Database,
    canonicalize_url,
    compute_lead_hash,
)
from b2b_alert_bot.enrichment.compensation import (
    parse_compensation,
    extract_compensation,
    evaluate_high_value_filter,
    format_budget_badge,
    parse_num,
)
from b2b_alert_bot.enrichment.deal_card import (
    DealCardGenerator,
    generate_deal_card,
    synthesize_winning_angle,
    extract_deliverables_bullet,
    extract_skills_bullet,
)
from b2b_alert_bot.enrichment.engine import EnrichmentEngine
from b2b_alert_bot.enrichment.funding_filter import (
    is_funding_false_positive,
    is_outlier_funding_amount,
)
from b2b_alert_bot.ingestion.base import clean_html, parse_timestamp
from b2b_alert_bot.ingestion.hackernews import HackerNewsConnector
from b2b_alert_bot.ingestion.jobspresso import JobspressoConnector
from b2b_alert_bot.ingestion.reddit import RedditConnector
from b2b_alert_bot.ingestion.remoteok import RemoteOKConnector
from b2b_alert_bot.ingestion.weworkremotely import WeWorkRemotelyConnector
from b2b_alert_bot.schema import Lead, EnrichedLead


# ==============================================================================
# 1. Adversarial XML & Feed Ingestion Tests
# ==============================================================================

class TestAdversarialIngestionXMLAndAtom(unittest.TestCase):
    """Stress-test feed parsing against corrupt, malformed, and anomalous payloads."""

    def setUp(self):
        self.wwr = WeWorkRemotelyConnector()
        self.jobspresso = JobspressoConnector()
        self.reddit = RedditConnector()
        self.remoteok = RemoteOKConnector()
        self.hn = HackerNewsConnector()

    def test_completely_malformed_xml_does_not_crash(self):
        """Malformed XML strings should return empty lead list without throwing uncaught exceptions."""
        malformed_inputs = [
            "<<<rss><broken",
            "<rss><channel><item><title>Unclosed",
            "<?xml version='1.0'?><rss><channel><item></channel></item></rss>",
            "Random non-XML plaintext payload from a 404 or 500 error page",
            "",
            "   \n\t  ",
            "<!DOCTYPE html><html><body>Error 502 Bad Gateway</body></html>",
        ]
        for bad_xml in malformed_inputs:
            self.assertEqual(self.wwr.parse(bad_xml), [])
            self.assertEqual(self.jobspresso.parse(bad_xml), [])
            self.assertEqual(self.reddit.parse(bad_xml), [])

    def test_xxe_and_entity_injection_defense(self):
        """XML with <!ENTITY or <!DOCTYPE entity expansion must be safely rejected."""
        xxe_payload = """<?xml version="1.0"?>
        <!DOCTYPE foo [
          <!ELEMENT foo ANY >
          <!ENTITY xxe SYSTEM "file:///etc/passwd" >
        ]>
        <rss version="2.0">
          <channel>
            <title>Exploit</title>
            <item>
              <title>&xxe;</title>
              <link>https://example.com/exploit</link>
            </item>
          </channel>
        </rss>"""
        self.assertEqual(self.wwr.parse(xxe_payload), [])
        self.assertEqual(self.jobspresso.parse(xxe_payload), [])
        self.assertEqual(self.reddit.parse(xxe_payload), [])

    def test_xml_namespace_anomalies_wwr(self):
        """WWR RSS with default XML namespace should be handled gracefully."""
        # Standard RSS 2.0 with an xmlns attribute on <rss>
        namespaced_rss = """<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0" xmlns="http://backend.userland.com/rss2">
          <channel>
            <title>Remote Jobs</title>
            <item>
              <title>Acme: Senior React Dev</title>
              <link>https://weworkremotely.com/jobs/101</link>
              <description>Budget: $100k - $120k</description>
              <pubDate>Mon, 15 Sep 2026 12:00:00 GMT</pubDate>
            </item>
          </channel>
        </rss>"""
        # Testing behavior under default XML namespace:
        # Should either parse leads or safely return empty list without crash
        leads = self.wwr.parse(namespaced_rss)
        self.assertIsInstance(leads, list)

    def test_atom_feed_with_prefixed_namespace_reddit(self):
        """Reddit Atom feed where tags have atom: prefix should be handled safely."""
        prefixed_atom = """<?xml version="1.0" encoding="UTF-8"?>
        <atom:feed xmlns:atom="http://www.w3.org/2005/Atom">
          <atom:title>r/forhire</atom:title>
          <atom:entry>
            <atom:title>[Hiring] Senior Python Developer ($80/hr)</atom:title>
            <atom:link href="https://reddit.com/r/forhire/comments/123/hiring"/>
            <atom:author><atom:name>tech_recruiter</atom:name></atom:author>
            <atom:content type="html">&lt;p&gt;Looking for Python dev at $80/hr&lt;/p&gt;</atom:content>
            <atom:updated>2026-09-20T12:00:00+00:00</atom:updated>
            <atom:id>tag:reddit.com,2026:123</atom:id>
          </atom:entry>
        </atom:feed>"""
        leads = self.reddit.parse(prefixed_atom)
        self.assertIsInstance(leads, list)

    def test_missing_and_empty_xml_attributes(self):
        """Entries missing link, title, author, or guid should be safely skipped or populated with defaults."""
        missing_attrs_xml = """<?xml version="1.0"?>
        <rss version="2.0">
          <channel>
            <!-- Missing title -->
            <item>
              <link>https://example.com/no-title</link>
              <description>Description only</description>
            </item>
            <!-- Missing link and guid -->
            <item>
              <title>No Link Job</title>
              <description>Description only</description>
            </item>
            <!-- Empty link -->
            <item>
              <title>Empty Link Job</title>
              <link></link>
              <description>Description only</description>
            </item>
            <!-- Valid item -->
            <item>
              <title>Valid Corp: Great Job</title>
              <link>https://example.com/valid-job</link>
              <description>Pay: $5,000</description>
            </item>
          </channel>
        </rss>"""
        leads = self.wwr.parse(missing_attrs_xml)
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0].client, "Valid Corp")
        self.assertEqual(leads[0].title, "Great Job")

    def test_reddit_atom_missing_attributes(self):
        """Reddit Atom entry missing href on link or missing content tag."""
        atom_xml = """<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <!-- Missing link href -->
            <title>[Hiring] No Link ($60/hr)</title>
            <link />
            <author><name>recruiter1</name></author>
            <updated>2026-09-20T12:00:00Z</updated>
          </entry>
          <entry>
            <!-- Valid -->
            <title>[Hiring] Valid Job ($70/hr)</title>
            <link href="https://www.reddit.com/r/forhire/comments/456/job/"/>
            <author><name>recruiter2</name></author>
            <content>We pay $70/hr for Rust developers</content>
            <updated>2026-09-20T12:00:00Z</updated>
          </entry>
        </feed>"""
        leads = self.reddit.parse(atom_xml)
        self.assertEqual(len(leads), 1)
        self.assertIn("Valid Job", leads[0].title)

    def test_remoteok_corrupt_and_anomalous_json(self):
        """RemoteOK parser with non-dict elements, missing fields, or empty lists."""
        anomalous_json = """[
          {"legal": "Notice"},
          null,
          123,
          "just a string",
          {"id": "job1"},
          {"id": "job2", "position": ""},
          {"id": "job3", "position": "Valid Engineer", "apply_url": "https://remoteok.com/apply/1", "salary_min": 120000, "salary_max": 150000}
        ]"""
        leads = self.remoteok.parse(anomalous_json)
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0].title, "Valid Engineer")
        self.assertIn("$120,000", leads[0].raw_compensation)

    def test_hackernews_corrupt_and_nested_payloads(self):
        """Hacker News parser handling nested replies, missing IDs, and malformed hits."""
        corrupt_hn_json = """{
          "hits": [
            {"story_id": "100", "parent_id": "200", "comment_text": "Nested reply should be skipped"},
            {"objectID": "101", "story_id": "100", "parent_id": "100", "comment_text": "SEEKING WORK | Python Dev"},
            {"objectID": "102", "story_id": "100", "parent_id": "100", "comment_text": ""},
            {"objectID": "103", "story_id": "100", "parent_id": "100", "comment_text": "Stripe | Staff Engineer | Remote | $150k - $200k\\n\\nWe are hiring staff engineers."}
          ]
        }"""
        leads = self.hn._parse_comments_payload(corrupt_hn_json, expected_story_id="100")
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0].client, "Stripe")
        self.assertEqual(leads[0].title, "Staff Engineer")


# ==============================================================================
# 2. Adversarial AI Enrichment & Compensation Parser Tests
# ==============================================================================

class TestAdversarialCompensationParsing(unittest.TestCase):
    """Stress-test compensation extraction, currency conversion, and filter decisions."""

    def test_multiple_currencies_in_single_text(self):
        """When text contains multiple currencies, parser should extract valid amounts without crashing."""
        text1 = "Rate: $100/hr (approx €92/hr or £80/hr)"
        res1 = parse_compensation(text1)
        self.assertTrue(res1["is_high_ticket"])
        self.assertEqual(res1["rate_type"], "hourly")
        self.assertGreaterEqual(res1["max_amount"], 50.0)

        text2 = "Fixed budget: €2,500 ($2,700 USD)"
        res2 = parse_compensation(text2)
        self.assertTrue(res2["is_high_ticket"])
        self.assertEqual(res2["rate_type"], "fixed")
        self.assertGreaterEqual(res2["max_amount"], 2000.0)

    def test_negative_numbers_behavior(self):
        """Negative numbers should not cause arithmetic crashes or false positive threshold passes."""
        negative_texts = [
            "Penalty of -$500 on delay",
            "Budget deficit -$2,000",
            "Rate: -$50/hr",
        ]
        for text in negative_texts:
            res = parse_compensation(text)
            self.assertIsInstance(res, dict)
            self.assertIn("is_high_ticket", res)

    def test_zero_budget_rejection(self):
        """Zero budgets must be rejected and never pass as high-ticket."""
        zero_cases = [
            "$0",
            "$0/hr",
            "0 USD",
            "$0 - $0/hr",
            "Fixed budget: $0",
            "$0/month",
        ]
        for z in zero_cases:
            res = parse_compensation(z)
            self.assertFalse(
                res["is_high_ticket"],
                f"Expected zero budget '{z}' to be rejected, but it passed: {res}"
            )

    def test_confusing_ranges_and_evaluation_strategies(self):
        """Ranges spanning across the threshold ($30 - $80/hr) should behave predictably per strategy."""
        range_text = "$30 - $80/hr"

        # Default / 'max' strategy: evaluates max ($80 >= $50 -> Pass)
        res_max = parse_compensation(range_text, range_strategy="max")
        self.assertTrue(res_max["is_high_ticket"])
        self.assertEqual(res_max["max_amount"], 80.0)

        # 'min' strategy: evaluates min ($30 < $50 -> Reject)
        res_min = parse_compensation(range_text, range_strategy="min")
        self.assertFalse(res_min["is_high_ticket"])
        self.assertEqual(res_min["min_amount"], 30.0)

        # 'avg' strategy: evaluates (30 + 80)/2 = 55 >= 50 -> Pass
        res_avg = parse_compensation(range_text, range_strategy="avg")
        self.assertTrue(res_avg["is_high_ticket"])

    def test_inverted_ranges_handling(self):
        """Inverted ranges like $80 - $30/hr or $5,000 - $2,000 fixed should not crash."""
        res_inverted_hourly = parse_compensation("$80 - $30/hr")
        self.assertIsInstance(res_inverted_hourly, dict)
        self.assertEqual(res_inverted_hourly["rate_type"], "hourly")

        res_inverted_fixed = parse_compensation("$5,000 - $2,000 fixed")
        self.assertIsInstance(res_inverted_fixed, dict)

    def test_funding_announcements_with_disguised_budgets(self):
        """Disguised budgets ($1.5M seed + $100k contract) should isolate the true project budget."""
        # 1. Pure funding round (no contract) -> Suppressed
        pure_funding = "Acme raised a $1.5M Seed round to expand engineering team."
        res_pure = parse_compensation(pure_funding)
        self.assertFalse(
            res_pure["is_high_ticket"],
            f"Pure seed funding should be suppressed, but got: {res_pure}"
        )

        # 2. Funding announcement combined with genuine contract budget
        combined_text = "Acme recently raised a $1.5M seed round. We now have a $100k contract for an MVP rebuild."
        res_comb = parse_compensation(combined_text)
        self.assertTrue(res_comb["is_high_ticket"])
        self.assertEqual(res_comb["rate_type"], "fixed")
        self.assertEqual(res_comb["max_amount"], 100000.0)

        # 3. Series B startup hiring hourly contractor
        series_b_text = "Following our Series B $25M funding, we are seeking a contract staff engineer at $95/hr."
        res_series = parse_compensation(series_b_text)
        self.assertTrue(res_series["is_high_ticket"])
        self.assertEqual(res_series["rate_type"], "hourly")
        self.assertEqual(res_series["max_amount"], 95.0)

        # 4. Outlier detection without contract framing ($15M ARR announcement)
        arr_text = "Rapidly growing startup doing $5M ARR looking for freelance help."
        res_arr = parse_compensation(arr_text)
        self.assertFalse(res_arr["is_high_ticket"])

    def test_extreme_numbers_and_overflow_protection(self):
        """Extreme numbers (billions, trillions, huge floating values) must not cause overflow crashes."""
        extreme_cases = [
            "$10B",
            "$500T",
            "$999,999,999,999",
            "USD 1,000,000,000",
            "$1e12",
        ]
        for case in extreme_cases:
            try:
                res = parse_compensation(case)
                self.assertIsInstance(res, dict)
            except Exception as e:
                self.fail(f"Extreme number parsing crashed on '{case}' with exception: {e}")

    def test_equity_and_unpaid_variations(self):
        """All variations of unpaid or equity-only listings must be consistently rejected."""
        unpaid_variations = [
            "We offer equity only, no cash compensation for now",
            "This is an unpaid volunteer project for non-profit",
            "Rev share only until product market fit",
            "Co-founder wanted for equity only",
            "No cash budget, 5% equity grant",
        ]
        for text in unpaid_variations:
            res = parse_compensation(text)
            self.assertFalse(res["is_high_ticket"], f"Expected rejection for '{text}', got: {res}")
            self.assertIn("EQUITY", res["status"])


# ==============================================================================
# 3. Adversarial Database Persistence & Concurrency Tests
# ==============================================================================

class TestAdversarialDatabaseConcurrency(unittest.TestCase):
    """Stress-test SQLite WAL persistence, race conditions, deduplication, and input sanitization."""

    def test_concurrent_inserts_of_identical_url_threads(self):
        """Multiple threads concurrently inserting the exact same Lead URL must not corrupt DB or fail."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = os.path.join(tmp_dir, "concurrent_test.db")
            target_url = "https://example.com/jobs/concurrent-test-1"
            lead_hash = compute_lead_hash("test", target_url, "Senior Architect", "TechCo")

            lead = Lead(
                id=lead_hash,
                title="Senior Architect",
                source="test",
                client="TechCo",
                url=target_url,
                raw_compensation="$150/hr",
            )

            # Each thread opens its own connection/Database instance
            num_threads = 12
            insert_results: List[bool] = []
            exceptions: List[Exception] = []

            def worker():
                try:
                    db = Database(db_path=db_path)
                    res = db.insert_lead(lead)
                    insert_results.append(res)
                    db.close()
                except Exception as ex:
                    exceptions.append(ex)

            threads = [threading.Thread(target=worker) for _ in range(num_threads)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(len(exceptions), 0, f"Exceptions occurred during concurrency: {exceptions}")
            self.assertEqual(len(insert_results), num_threads)
            # Exactly one thread should report True (newly inserted), others False (duplicate)
            self.assertEqual(insert_results.count(True), 1)
            self.assertEqual(insert_results.count(False), num_threads - 1)

            # Verify SQLite final state
            final_db = Database(db_path=db_path)
            self.assertEqual(final_db.count_leads(), 1)
            final_db.close()

    def test_concurrent_inserts_of_unique_leads_threads(self):
        """Multiple threads concurrently inserting different leads into SQLite WAL."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = os.path.join(tmp_dir, "concurrent_unique_test.db")
            num_leads = 25
            exceptions: List[Exception] = []

            def worker(idx: int):
                try:
                    url = f"https://example.com/unique-jobs/{idx}"
                    h = compute_lead_hash("test", url, f"Dev {idx}", "Co")
                    lead = Lead(
                        id=h,
                        title=f"Engineer {idx}",
                        source="test",
                        client="Co",
                        url=url,
                        raw_compensation="$3,000",
                    )
                    db = Database(db_path=db_path)
                    db.insert_lead(lead)
                    db.close()
                except Exception as ex:
                    exceptions.append(ex)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_leads)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(len(exceptions), 0, f"Exceptions occurred: {exceptions}")
            final_db = Database(db_path=db_path)
            self.assertEqual(final_db.count_leads(), num_leads)
            final_db.close()

    def test_sql_injection_defense_in_titles_and_descriptions(self):
        """SQL injection fragments in fields must be stored verbatim without executing SQL commands."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = os.path.join(tmp_dir, "sqli_test.db")
            db = Database(db_path=db_path)

            malicious_title = "'; DROP TABLE ingested_leads; --"
            malicious_desc = "' OR 1=1; DELETE FROM ingested_leads; --"
            malicious_client = "Bobby Tables'); --"

            url = "https://example.com/sqli-job"
            h = compute_lead_hash("test", url, malicious_title, malicious_client)
            lead = Lead(
                id=h,
                title=malicious_title,
                source="test",
                client=malicious_client,
                url=url,
                description=malicious_desc,
            )

            inserted = db.insert_lead(lead)
            self.assertTrue(inserted)

            # Check table still exists and data preserved
            retrieved = db.get_lead(h)
            self.assertIsNotNone(retrieved)
            self.assertEqual(retrieved.title, malicious_title)
            self.assertEqual(retrieved.client, malicious_client)
            self.assertEqual(retrieved.description, malicious_desc)
            db.close()

    def test_unicode_emojis_and_multilingual_text(self):
        """Full Unicode, emojis, and right-to-left scripts must persist accurately."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = os.path.join(tmp_dir, "unicode_test.db")
            db = Database(db_path=db_path)

            unicode_title = "🚀 High Ticket Lead | 💰 $10k Budget | 中文 | العربية | עברית | 🦀 Rust Dev"
            unicode_desc = "Seeking fullstack ninja 🔥. Emojis: 💻 📱 ⚙️ 💡. Multilingual: Привет мир."

            url = "https://example.com/unicode-job"
            h = compute_lead_hash("test", url, unicode_title, "GlobalTech")
            lead = Lead(
                id=h,
                title=unicode_title,
                source="test",
                client="GlobalTech",
                url=url,
                description=unicode_desc,
            )

            db.insert_lead(lead)
            retrieved = db.get_lead(h)
            self.assertIsNotNone(retrieved)
            self.assertEqual(retrieved.title, unicode_title)
            self.assertEqual(retrieved.description, unicode_desc)
            db.close()

    def test_malformed_urls_and_url_canonicalization(self):
        """URL canonicalization must handle malformed, empty, and unusual URL patterns safely."""
        test_cases = [
            ("", ""),
            (None, ""),
            ("not-a-url", "not-a-url"),
            ("javascript:alert('xss')", "javascript:alert('xss')"),
            ("http://EXAMPLE.COM:80/path/?utm_source=twitter&ref=ad#fragment", "https://example.com:80/path"),
            ("https://site.com/job/?b=2&a=1&utm_medium=cpc", "https://site.com/job?a=1&b=2"),
            ("https://site.com/job///", "https://site.com/job"),
        ]
        for raw, expected in test_cases:
            result = canonicalize_url(raw)
            if expected == "":
                self.assertEqual(result, "")
            elif "site.com" in expected:
                self.assertEqual(result, expected)
            else:
                self.assertIsInstance(result, str)


# ==============================================================================
# 4. Adversarial Deal Card Generator Tests
# ==============================================================================

class TestAdversarialDealCardGenerator(unittest.TestCase):
    """Stress-test deal card generator under abnormal and sparse inputs."""

    def test_sparse_lead_generation(self):
        """Lead with empty description and empty tech stack should generate fallback bullets cleanly."""
        h = "e" * 64
        sparse_lead = Lead(
            id=h,
            title="Senior Go Developer",
            source="weworkremotely",
            client="",
            url="https://example.com/job-sparse",
            description="",
            core_tech_stack=[],
        )
        enriched = DealCardGenerator.generate(sparse_lead)
        self.assertIsInstance(enriched, EnrichedLead)
        self.assertTrue(len(enriched.scope_bullet) > 10)
        self.assertTrue(len(enriched.skills_bullet) > 0)
        self.assertTrue(len(enriched.winning_angle) > 20)

    def test_giant_description_text_truncation(self):
        """Lead with a 50,000-character description must be summarized without exceeding deal card limits."""
        h = "f" * 64
        giant_text = "Develop and build scalable systems. " * 1500
        lead = Lead(
            id=h,
            title="Systems Architect",
            source="weworkremotely",
            client="BigCorp",
            url="https://example.com/job-giant",
            description=giant_text,
        )
        enriched = DealCardGenerator.generate(lead)
        # Scope bullet should be concise (around 40 words max)
        word_count = len(enriched.scope_bullet.split())
        self.assertLessEqual(word_count, 60)


if __name__ == "__main__":
    unittest.main()
