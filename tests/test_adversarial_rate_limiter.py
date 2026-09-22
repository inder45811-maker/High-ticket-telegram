"""Adversarial stress and concurrency test suite for Rate Limiter and Telegram Dispatch Queue.

Tests:
1. Rapid Concurrent Bursts: 50+ threads contending for single chat and global tokens.
2. Token Starvation & Fair Scheduling: Multi-chat isolation (Chat A cannot starve Chat B).
3. Retry Queue Exponential Backoff: HTTP 429 retry_after handling, 5xx backoff, max retries exhaustion, DLQ behavior.
"""

import asyncio
import os
import random
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import requests

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


class TestRateLimiterConcurrencyAdversarial(unittest.TestCase):
    """Stress testing rate limiter under concurrent burst conditions."""

    def test_rapid_concurrent_burst_50_threads_single_chat(self):
        """50 concurrent threads attempting to consume tokens for the same chat.

        With chat_rate=100.0 msg/s, chat_capacity=50.0, 50 threads must all acquire without deadlock or corruption.
        """
        limiter = TelegramRateLimiter(
            chat_rate=200.0,
            chat_capacity=50.0,
            global_rate=500.0,
            global_capacity=100.0,
        )

        chat_id = "-100998877"
        num_threads = 50
        waited_times = []

        def _worker():
            w = limiter.acquire(chat_id)
            return w

        t0 = time.time()
        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(_worker) for _ in range(num_threads)]
            for f in futures:
                waited_times.append(f.result())
        duration = time.time() - t0

        # All 50 threads completed
        self.assertEqual(len(waited_times), num_threads)

        # Invariant: Token bucket counts must never be negative or exceed capacity
        bucket = limiter.get_chat_bucket(chat_id)
        with limiter._lock:
            self.assertGreaterEqual(bucket.tokens, 0.0)
            self.assertLessEqual(bucket.tokens, bucket.capacity)
            self.assertGreaterEqual(limiter.global_bucket.tokens, 0.0)
            self.assertLessEqual(limiter.global_bucket.tokens, limiter.global_bucket.capacity)

    def test_multi_chat_fairness_no_starvation(self):
        """Chat A flooding requests cannot starve independent Chat B."""
        limiter = TelegramRateLimiter(
            chat_rate=1.0,
            chat_capacity=1.0,
            global_rate=100.0,
            global_capacity=100.0,
        )

        # Chat A consumes its single token
        w_a1 = limiter.consume("chat_A")
        self.assertEqual(w_a1, 0.0)

        # Chat A attempts second token immediately -> throttled (> 0.5s wait)
        w_a2 = limiter.consume("chat_A")
        self.assertGreater(w_a2, 0.8)

        # Chat B now requests token -> MUST consume immediately (wait=0.0) because buckets are isolated
        w_b = limiter.consume("chat_B")
        self.assertEqual(w_b, 0.0, "Chat B was starved by Chat A's exhausted bucket!")

    def test_token_bucket_boundary_conditions(self):
        """Test consuming 0 tokens, more than capacity, or on empty bucket."""
        tb = TokenBucket(rate=10.0, capacity=5.0, initial_tokens=5.0)

        # Consuming 0 tokens
        self.assertEqual(tb.consume(0.0), 0.0)

        # Consuming exactly capacity
        self.assertEqual(tb.consume(5.0), 0.0)

        # Bucket now empty
        wait = tb.consume(1.0)
        self.assertGreater(wait, 0.0)
        self.assertAlmostEqual(wait, 0.1, places=2)

        # Consuming more than total capacity
        wait_excess = tb.consume(10.0)
        self.assertGreater(wait_excess, 0.5)

    def test_async_acquire_stress(self):
        """Async acquire concurrency under asyncio loop."""
        limiter = TelegramRateLimiter(
            chat_rate=200.0,
            chat_capacity=50.0,
            global_rate=500.0,
            global_capacity=100.0,
        )

        async def _run_burst():
            tasks = [limiter.acquire_async("-100_async") for _ in range(50)]
            results = await asyncio.gather(*tasks)
            return results

        results = asyncio.run(_run_burst())
        self.assertEqual(len(results), 50)


class TestRetryQueueAndBackoffAdversarial(unittest.TestCase):
    """Adversarial stress testing of TelegramDispatcher retries, backoffs, and DLQ."""

    def _sample_enriched_lead(self, idx: int = 1) -> EnrichedLead:
        lead = Lead(
            id=f"lead_retry_{idx}_" + "0" * 48,
            title=f"DevOps Lead #{idx}",
            source="wwr",
            client="CloudCorp",
            url="https://weworkremotely.com/jobs/devops",
        )
        return EnrichedLead(
            lead=lead,
            is_high_ticket=True,
            rate_type="fixed",
            min_amount=6000.0,
            max_amount=6000.0,
            currency="USD",
            budget_badge="💰 $6,000 FIXED",
            scope_bullet="Build multi-cloud CI/CD.",
            skills_bullet="Terraform, AWS, GitHub Actions",
            winning_angle="Share pipeline performance metrics.",
        )

    def test_http_429_retry_after_honored(self):
        """When Telegram returns HTTP 429 with parameters.retry_after, sleep is called with retry_after + 1.0."""
        mock_session = MagicMock()

        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.json.return_value = {
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests: retry after 3",
            "parameters": {"retry_after": 3}
        }

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {
            "ok": True,
            "result": {"message_id": 12345, "chat": {"id": "-1001"}}
        }

        mock_session.post.side_effect = [resp_429, resp_200]

        dispatcher = TelegramDispatcher(
            bot_token="test_token",
            chat_id="-1001",
            dry_run=False,
            session=mock_session,
            max_retries=3,
        )

        sleep_calls = []
        with patch("time.sleep", side_effect=lambda s: sleep_calls.append(s)):
            res = dispatcher.send_message_sync(chat_id="-1001", text="Test 429 backoff")

        self.assertTrue(res["ok"])
        self.assertEqual(mock_session.post.call_count, 2)
        # Should have slept 3 + 1.0 = 4.0 seconds
        self.assertIn(4.0, sleep_calls)

    def test_http_429_exhaustion_returns_failure(self):
        """Persistent HTTP 429 through all max_retries attempts fails gracefully."""
        mock_session = MagicMock()

        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.json.return_value = {
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests",
            "parameters": {"retry_after": 1}
        }

        mock_session.post.return_value = resp_429

        dispatcher = TelegramDispatcher(
            bot_token="test_token",
            chat_id="-1001",
            dry_run=False,
            session=mock_session,
            max_retries=3,
        )

        with patch("time.sleep"):
            res = dispatcher.send_message_sync(chat_id="-1001", text="Persistent 429")

        self.assertFalse(res["ok"])
        self.assertEqual(mock_session.post.call_count, 3)

    def test_5xx_server_error_exponential_backoff(self):
        """500/502/503 errors trigger exponential backoff before retrying."""
        mock_session = MagicMock()

        resp_502 = MagicMock()
        resp_502.status_code = 502
        resp_502.text = "Bad Gateway"

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {"ok": True, "result": {"message_id": 777}}

        mock_session.post.side_effect = [resp_502, resp_502, resp_200]

        dispatcher = TelegramDispatcher(
            bot_token="test_token",
            chat_id="-1001",
            dry_run=False,
            session=mock_session,
            max_retries=5,
        )

        sleep_calls = []
        with patch("time.sleep", side_effect=lambda s: sleep_calls.append(s)):
            res = dispatcher.send_message_sync(chat_id="-1001", text="5xx backoff test")

        self.assertTrue(res["ok"])
        self.assertEqual(mock_session.post.call_count, 3)
        self.assertEqual(len(sleep_calls), 2)
        # First backoff attempt 0: ~2^0 + jitter = ~1.1 - 1.5
        self.assertGreaterEqual(sleep_calls[0], 1.0)
        # Second backoff attempt 1: ~2^1 + jitter = ~2.1 - 2.5
        self.assertGreaterEqual(sleep_calls[1], 2.0)

    def test_dispatch_queue_dead_letter_queue_tracking(self):
        """Failed messages are systematically routed to dead_letter_queue."""
        mock_session = MagicMock()

        # Succeed for item 1, fail fatally (400 bad request) for item 2, succeed for item 3
        resp_ok = MagicMock()
        resp_ok.status_code = 200
        resp_ok.json.return_value = {"ok": True, "result": {"message_id": 111}}

        resp_fail = MagicMock()
        resp_fail.status_code = 400
        resp_fail.json.return_value = {"ok": False, "error_code": 400, "description": "Chat not found"}

        mock_session.post.side_effect = [resp_ok, resp_fail, resp_ok]

        dispatcher = TelegramDispatcher(
            bot_token="test_token",
            chat_id="-1001",
            dry_run=False,
            session=mock_session,
        )

        queue = DispatchQueue(dispatcher)
        lead1 = self._sample_enriched_lead(1)
        lead2 = self._sample_enriched_lead(2)
        lead3 = self._sample_enriched_lead(3)

        queue.enqueue(lead1)
        queue.enqueue(lead2)
        queue.enqueue(lead3)

        results = queue.process_all_sync()

        self.assertEqual(len(results), 3)
        self.assertTrue(results[0].ok)
        self.assertFalse(results[1].ok)
        self.assertTrue(results[2].ok)

        # Invariant: Dead letter queue contains exactly the 1 failed message
        self.assertEqual(len(queue.dead_letter_queue), 1)
        self.assertEqual(queue.dead_letter_queue[0]["item"]["lead"], lead2)
        self.assertIn("failed_at", queue.dead_letter_queue[0])
        self.assertEqual(len(queue.history), 3)


if __name__ == "__main__":
    unittest.main()
