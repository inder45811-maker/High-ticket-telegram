"""Unit and integration test suite for Milestone 3: Telegram Alert Dispatcher."""

import asyncio
import html
import os
import re
import time
import unittest
from unittest.mock import MagicMock, patch

import pytest

from b2b_alert_bot.dispatcher.formatter import (
    TELEGRAM_MAX_MESSAGE_LENGTH,
    TelegramFormatter,
    create_apply_markup,
    format_budget_badge,
    format_deal_card,
    get_link_preview_options,
    truncate_html,
)
from b2b_alert_bot.dispatcher.rate_limiter import (
    TelegramRateLimiter,
    TokenBucket,
)
from b2b_alert_bot.dispatcher.telegram_bot import (
    DispatchQueue,
    DispatchResult,
    TelegramDispatcher,
)
from b2b_alert_bot.schema import EnrichedLead, Lead


def count_tag_pairs(text: str, tag: str) -> tuple[int, int]:
    """Helper to count opening and closing occurrences of an HTML tag."""
    opens = len(re.findall(rf"<{tag}(?:\s+[^>]*)*>", text, flags=re.IGNORECASE))
    closes = len(re.findall(rf"</{tag}>", text, flags=re.IGNORECASE))
    return opens, closes


class TestFormatter(unittest.TestCase):
    """Test suite for deal card HTML formatting, badges, escaping, and truncation."""

    def setUp(self):
        self.sample_lead = Lead(
            id="0123456789abcdef" * 4,
            title="Senior Distributed Systems Architect",
            source="weworkremotely",
            client="Global Data Corp",
            url="https://weworkremotely.com/jobs/senior-architect",
            core_tech_stack=["Python", "Go", "Kafka", "PostgreSQL", "Docker"]
        )

        self.sample_enriched = EnrichedLead(
            lead=self.sample_lead,
            is_high_ticket=True,
            rate_type="fixed",
            min_amount=7500.0,
            max_amount=7500.0,
            currency="USD",
            budget_badge="💰 $7,500 FIXED",
            scope_bullet="Design and implement a high-throughput event processing pipeline.",
            skills_bullet="Python, Go, Kafka, PostgreSQL, Docker",
            winning_angle="Provide a benchmark latency study from a previous high-scale stream processing deployment."
        )

    def test_budget_badge_styling(self):
        """Test formatting budget badges across all compensation modalities."""
        # Fixed single
        self.assertEqual(format_budget_badge("fixed", 4500.0, 4500.0), "💰 $4,500 FIXED")
        # Fixed range
        self.assertEqual(format_budget_badge("fixed", 3000.0, 6000.0), "💰 $3,000 - $6,000 FIXED")
        # Hourly single
        self.assertEqual(format_budget_badge("hourly", 75.0, 75.0), "⏱️ $75/HR")
        # Hourly range
        self.assertEqual(format_budget_badge("hourly", 60.0, 90.0), "⏱️ $60 - $90/HR")
        # Annual salary equivalent
        self.assertEqual(format_budget_badge("annual", 140000.0, 140000.0), "💼 $140k/YR (~$70/HR)")
        # Monthly single
        self.assertEqual(format_budget_badge("monthly", 4000.0, 4000.0), "💰 $4,000/MO")
        # Monthly range
        self.assertEqual(format_budget_badge("monthly", 3000.0, 5000.0), "💰 $3,000 - $5,000/MO")
        # Fallback high-ticket
        self.assertEqual(format_budget_badge("unknown", 2500.0, 2500.0), "💰 $2,500 HIGH-TICKET")
        self.assertEqual(format_budget_badge("unknown", 0.0, 0.0), "💰 $2,000+ HIGH-TICKET")

    def test_deal_card_formatting_happy_path(self):
        """Test complete 3-bullet deal card layout in HTML parse mode."""
        msg = format_deal_card(self.sample_enriched)

        # Budget badge header
        self.assertIn("<b>💰 $7,500 FIXED</b>", msg)
        # Title and client/source
        self.assertIn("🎯 <b>Senior Distributed Systems Architect</b>", msg)
        self.assertIn("🏢 <i>Global Data Corp</i> • <i>via Weworkremotely</i>", msg)
        # 3-bullet executive summary
        self.assertIn("📋 <b>Executive Summary:</b>", msg)
        self.assertIn("• <b>Scope:</b> Design and implement a high-throughput", msg)
        self.assertIn("• <b>Skills:</b> Python, Go, Kafka", msg)
        self.assertIn("• <b>Winning Angle:</b> Provide a benchmark latency", msg)
        # Footer with short ID
        self.assertIn("🆔 <code>#01234567</code>", msg)
        self.assertIn("🕒 <i>Posted recently</i>", msg)

        # Tag symmetry
        for tag in ["b", "i", "code"]:
            opens, closes = count_tag_pairs(msg, tag)
            self.assertEqual(opens, closes, f"Mismatched <{tag}> tags in deal card")

    def test_deal_card_formatting_dict_input(self):
        """Test deal card formatter accepting raw enriched dictionary."""
        dict_payload = {
            "lead": {
                "id": "abcdef0123456789" * 4,
                "title": "Cloud Infrastructure Lead",
                "client": "Apex Labs",
                "source": "remoteok",
                "url": "https://remoteok.com/jobs/apex"
            },
            "budget_badge": "⏱️ $95/HR",
            "scope_bullet": "Automate Kubernetes multi-region deployments with Terraform.",
            "skills_bullet": "Terraform, AWS, Kubernetes, Helm",
            "winning_angle": "Include a verified Infrastructure as Code security audit checklist."
        }

        msg = format_deal_card(dict_payload)
        self.assertIn("⏱️ $95/HR", msg)
        self.assertIn("🎯 <b>Cloud Infrastructure Lead</b>", msg)
        self.assertIn("🏢 <i>Apex Labs</i> • <i>via Remoteok</i>", msg)
        self.assertIn("• <b>Scope:</b> Automate Kubernetes", msg)
        self.assertIn("• <b>Skills:</b> Terraform, AWS", msg)
        self.assertIn("🆔 <code>#abcdef01</code>", msg)

    def test_html_entity_escaping_integrity(self):
        """Test that special characters (<, >, &, \") are safely escaped for Telegram HTML."""
        toxic_lead = Lead(
            id="deadbeef" * 8,
            title="Senior Fullstack Dev <React & TypeScript> & \"Lead\"",
            source="reddit",
            client="Acme & Sons <Inc.>",
            url="https://reddit.com/r/forhire"
        )
        toxic_enriched = EnrichedLead(
            lead=toxic_lead,
            is_high_ticket=True,
            rate_type="fixed",
            min_amount=5000.0,
            max_amount=5000.0,
            currency="USD",
            budget_badge="💰 $5,000 FIXED",
            scope_bullet="Build <script>alert('xss')</script> & frontend components <v2.0>.",
            skills_bullet="React & TypeScript & Node.js",
            winning_angle="Demonstrate <clean> architectural principles & 100% test coverage."
        )

        msg = format_deal_card(toxic_enriched)

        # Verify literal '<' and '>' inside text are escaped
        self.assertNotIn("<React", msg)
        self.assertIn("&lt;React &amp; TypeScript&gt;", msg)
        self.assertNotIn("<Inc.>", msg)
        self.assertIn("&lt;Inc.&gt;", msg)
        self.assertNotIn("<script>", msg)
        self.assertIn("&lt;script&gt;", msg)

        # All HTML tags in the formatted message should be strictly valid formatting tags
        for tag in ["b", "i", "code"]:
            opens, closes = count_tag_pairs(msg, tag)
            self.assertEqual(opens, closes, f"Unbalanced tags for <{tag}>")

    def test_message_truncation_at_4096_preserving_tags(self):
        """Test that oversized messages are truncated strictly <= 4096 chars with balanced tags."""
        massive_text = "Highly critical deliverables. " * 300  # ~9,000 characters
        lead = Lead(
            id="f" * 64,
            title="Principal AI Research Engineer",
            source="hackernews",
            client="DeepTech AI",
            url="https://news.ycombinator.com/item?id=123"
        )
        enriched = EnrichedLead(
            lead=lead,
            is_high_ticket=True,
            rate_type="fixed",
            min_amount=15000.0,
            max_amount=15000.0,
            currency="USD",
            budget_badge="💰 $15,000 FIXED",
            scope_bullet=massive_text,
            skills_bullet="PyTorch, Transformers, CUDA, Python",
            winning_angle="Share a private link to a fine-tuned model checkpoint achieving SOTA latency."
        )

        msg = format_deal_card(enriched)
        self.assertLessEqual(len(msg), TELEGRAM_MAX_MESSAGE_LENGTH)

        # Ensure all tags are balanced
        for tag in ["b", "i", "code", "pre", "a"]:
            opens, closes = count_tag_pairs(msg, tag)
            self.assertEqual(opens, closes, f"Mismatched <{tag}> tags after truncation")

    def test_truncate_html_nested_tags(self):
        """Test low-level truncate_html function on complex nested markup."""
        sample_html = "<b>Header</b> <i>Outer <code>Nested code with <b>bold</b></code> still in italic</i> end."
        truncated = truncate_html(sample_html, max_length=50)

        self.assertLessEqual(len(truncated), 50)
        self.assertTrue(truncated.endswith("</i>") or truncated.endswith("</code></i>") or truncated.endswith("..."))

        # Verify tag balance on the truncated slice
        for tag in ["b", "i", "code"]:
            opens, closes = count_tag_pairs(truncated, tag)
            self.assertEqual(opens, closes, f"Tags for <{tag}> unbalanced in: {truncated}")

    def test_create_apply_markup_valid_and_invalid(self):
        """Test inline keyboard markup generation with URL validation."""
        markup1 = create_apply_markup("https://weworkremotely.com/jobs/123")
        self.assertIsNotNone(markup1)
        self.assertIn("inline_keyboard", markup1)
        self.assertEqual(markup1["inline_keyboard"][0][0]["text"], "🚀 Apply on Source")
        self.assertEqual(markup1["inline_keyboard"][0][0]["url"], "https://weworkremotely.com/jobs/123")

        markup2 = create_apply_markup("http://example.com/apply", text="🔥 Fast Apply")
        self.assertEqual(markup2["inline_keyboard"][0][0]["text"], "🔥 Fast Apply")

        self.assertIsNone(create_apply_markup(""))
        self.assertIsNone(create_apply_markup("ftp://invalid.com"))
        self.assertIsNone(create_apply_markup("mailto:recruiter@example.com"))
        self.assertIsNone(create_apply_markup("javascript:alert(1)"))
        self.assertIsNone(create_apply_markup(None))

    def test_link_preview_options_disabled(self):
        """Test link preview options suppresses previews on mobile."""
        options = get_link_preview_options()
        self.assertEqual(options, {"is_disabled": True})


class TestRateLimiter(unittest.TestCase):
    """Test suite for token bucket rate limiter and Telegram limits."""

    def test_token_bucket_consume_and_replenish(self):
        """Test single token bucket consumption and timing calculations."""
        tb = TokenBucket(rate=2.0, capacity=2.0)

        self.assertEqual(tb.consume(1.0), 0.0)
        self.assertEqual(tb.consume(1.0), 0.0)

        wait = tb.consume(1.0)
        self.assertGreater(wait, 0.0)
        self.assertLessEqual(wait, 0.6)

        time.sleep(0.55)
        self.assertEqual(tb.consume(1.0), 0.0)

    def test_token_bucket_acquire_sync(self):
        """Test synchronous blocking acquire."""
        tb = TokenBucket(rate=10.0, capacity=1.0)
        self.assertEqual(tb.acquire(1.0), 0.0)
        t0 = time.time()
        waited = tb.acquire(1.0)
        t1 = time.time()
        self.assertGreater(waited, 0.0)
        self.assertGreaterEqual(t1 - t0, 0.08)

    def test_composite_telegram_rate_limiter_per_chat_and_global(self):
        """Test per-chat limit (1/s) and global limit (30/s)."""
        limiter = TelegramRateLimiter(chat_rate=1.0, chat_capacity=1.0, global_rate=30.0, global_capacity=30.0)

        self.assertEqual(limiter.consume("-1001"), 0.0)
        wait_chat = limiter.consume("-1001")
        self.assertGreater(wait_chat, 0.0)

        self.assertEqual(limiter.consume("-1002"), 0.0)

    def test_composite_rate_limiter_reset(self):
        """Test resetting rate limiter clears chat state."""
        limiter = TelegramRateLimiter(chat_rate=1.0, chat_capacity=1.0)
        self.assertEqual(limiter.consume("-1001"), 0.0)
        self.assertGreater(limiter.consume("-1001"), 0.0)

        limiter.reset()
        self.assertEqual(limiter.consume("-1001"), 0.0)


class TestTelegramDispatcher(unittest.TestCase):
    """Test suite for TelegramDispatcher with offline dry-run and live mock execution."""

    def setUp(self):
        self.dispatcher = TelegramDispatcher(
            bot_token=None,
            chat_id="-1001987654321",
            dry_run=True
        )

        self.lead = Lead(
            id="12345678" * 8,
            title="Senior React Native Engineer",
            source="jobspresso",
            client="Mobile First Studio",
            url="https://jobspresso.co/jobs/react-native"
        )
        self.enriched = EnrichedLead(
            lead=self.lead,
            is_high_ticket=True,
            rate_type="fixed",
            min_amount=8000.0,
            max_amount=8000.0,
            currency="USD",
            budget_badge="💰 $8,000 FIXED",
            scope_bullet="Build offline-first cross-platform mobile app with SQLite.",
            skills_bullet="React Native, TypeScript, SQLite, Redux",
            winning_angle="Offer an immediate TestFlight build of an offline-first demo application."
        )

    def test_dry_run_mode_activation_and_execution(self):
        """Test offline dry-run returns synthetic HTTP 200 payload without network."""
        self.assertTrue(self.dispatcher.dry_run)

        res = self.dispatcher.dispatch_sync(self.enriched)

        self.assertTrue(res.ok)
        self.assertTrue(bool(res))
        self.assertEqual(res.status, "dry_run")
        self.assertEqual(res["lead_id"], self.lead.id)
        self.assertIsNotNone(res["telegram_message_id"])

        self.assertEqual(len(self.dispatcher.sent_messages), 1)
        sent = self.dispatcher.sent_messages[0]
        self.assertEqual(sent["chat"]["id"], "-1001987654321")
        self.assertIn("💰 $8,000 FIXED", sent["text"])
        self.assertIsNotNone(sent["reply_markup"])
        self.assertEqual(
            sent["reply_markup"]["inline_keyboard"][0][0]["url"],
            "https://jobspresso.co/jobs/react-native"
        )

    def test_dry_run_validation_tag_mismatch_failure(self):
        """Test dry-run mode catches unbalanced HTML before dispatch."""
        bad_html = "<b>Unclosed bold text <i>italic</i>"
        res = self.dispatcher.send_message_sync(chat_id="-1001", text=bad_html)

        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], 400)
        self.assertIn("unclosed or mismatched HTML tag", res["description"])

    def test_dry_run_validation_invalid_url_failure(self):
        """Test dry-run mode catches invalid button URLs."""
        bad_markup = {"inline_keyboard": [[{"text": "Bad", "url": "ftp://broken.com"}]]}
        res = self.dispatcher.send_message_sync(
            chat_id="-1001",
            text="<b>Valid text</b>",
            reply_markup=bad_markup
        )

        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], 400)
        self.assertIn("BUTTON_URL_INVALID", res["description"])

    def test_async_dispatch_happy_path(self):
        """Test asynchronous dispatch coroutine."""
        async def run_test():
            res = await self.dispatcher.dispatch(self.enriched)
            self.assertTrue(res.ok)
            self.assertEqual(res.status, "dry_run")
            return res

        result = asyncio.run(run_test())
        self.assertTrue(result.ok)

    def test_dispatch_queue_processing(self):
        """Test DispatchQueue batch processing with history and DLQ tracking."""
        queue = DispatchQueue(self.dispatcher)

        queue.enqueue(self.enriched)
        queue.enqueue(self.enriched)
        queue.enqueue(self.enriched)
        self.assertEqual(len(queue.queue), 3)

        results = queue.process_all_sync()
        self.assertEqual(len(results), 3)
        self.assertEqual(len(queue.queue), 0)
        self.assertEqual(len(queue.dead_letter_queue), 0)
        self.assertEqual(len(queue.history), 3)
        for r in results:
            self.assertTrue(r.ok)

    def test_retry_on_http_429_rate_limit(self):
        """Test dispatcher intercepts HTTP 429 and retries using retry_after."""
        mock_session = MagicMock()

        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.json.return_value = {
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests",
            "parameters": {"retry_after": 1}
        }

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {
            "ok": True,
            "result": {"message_id": 9999, "chat": {"id": "-1001"}}
        }

        mock_session.post.side_effect = [resp_429, resp_200]

        live_dispatcher = TelegramDispatcher(
            bot_token="test_token_123",
            chat_id="-1001",
            dry_run=False,
            session=mock_session
        )

        with patch("time.sleep") as mock_sleep:
            res = live_dispatcher.send_message_sync(chat_id="-1001", text="<b>Rate limit test</b>")

        self.assertTrue(res["ok"])
        self.assertEqual(res["result"]["message_id"], 9999)
        self.assertEqual(mock_session.post.call_count, 2)
        mock_sleep.assert_called()

    def test_retry_on_5xx_server_error(self):
        """Test dispatcher backs off exponentially on transient 502/503 errors."""
        mock_session = MagicMock()

        resp_503 = MagicMock()
        resp_503.status_code = 503
        resp_503.text = "Bad Gateway"

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {
            "ok": True,
            "result": {"message_id": 8888, "chat": {"id": "-1001"}}
        }

        mock_session.post.side_effect = [resp_503, resp_200]

        live_dispatcher = TelegramDispatcher(
            bot_token="test_token_123",
            chat_id="-1001",
            dry_run=False,
            session=mock_session
        )

        with patch("time.sleep") as mock_sleep:
            res = live_dispatcher.send_message_sync(chat_id="-1001", text="<b>Server error test</b>")

        self.assertTrue(res["ok"])
        self.assertEqual(res["result"]["message_id"], 8888)
        self.assertEqual(mock_session.post.call_count, 2)
        mock_sleep.assert_called()

    def test_non_retryable_client_error(self):
        """Test dispatcher does not retry on fatal client errors (e.g. 403 Forbidden)."""
        mock_session = MagicMock()
        resp_403 = MagicMock()
        resp_403.status_code = 403
        resp_403.json.return_value = {
            "ok": False,
            "error_code": 403,
            "description": "Forbidden: bot was kicked from the channel chat"
        }
        mock_session.post.return_value = resp_403

        live_dispatcher = TelegramDispatcher(
            bot_token="test_token_123",
            chat_id="-1001",
            dry_run=False,
            session=mock_session
        )

        res = live_dispatcher.send_message_sync(chat_id="-1001", text="<b>Test</b>")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], 403)
        self.assertEqual(mock_session.post.call_count, 1)


if __name__ == "__main__":
    unittest.main()

