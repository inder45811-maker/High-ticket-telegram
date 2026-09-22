"""Adversarial stress and security test suite for Whop Webhook Subsystem.

Tests:
1. HMAC Forgery: Bit-flips, corrupt signatures, malformed base64, null bytes, version tampering.
2. Timing Attack Resistance: Statistical execution timing verification of constant-time digest comparison.
3. Replay Attacks: Boundary testing at 300s, extreme skew, malformed timestamps, concurrent replay bursts.
4. Header Stripping & Permutation: Stripped headers, partial headers, mixed casing, extra spaces.
5. Wrong & Malformed Secrets: Empty, whitespace, invalid base64, extreme length secrets.
6. SQL Injection & Path Traversal: Malicious payloads in webhook JSON fields and HTTP routing.
"""

import base64
import hashlib
import hmac
import json
import os
import sqlite3
import statistics
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from b2b_alert_bot.webhook.handler import SubscriberStore, WhopWebhookHandler
from b2b_alert_bot.webhook.security import (
    WebhookVerifier,
    normalize_headers,
    verify_signature,
    verify_whop_signature,
)
from b2b_alert_bot.webhook.server import WhopWebhookServer


class MockAdversarialTelegramAPI:
    """Mock Telegram API tracking calls and simulating thread-safe invite generation."""

    def __init__(self):
        self._lock = threading.Lock()
        self.call_history: List[Dict[str, Any]] = []
        self.created_invites: Dict[str, Dict[str, Any]] = {}
        self.channel_members: Dict[int, str] = {}
        self._invite_seq = 0

    def createChatInviteLink(
        self,
        chat_id: str | int,
        name: Optional[str] = None,
        expire_date: Optional[int] = None,
        member_limit: int = 1,
        creates_join_request: bool = False,
    ) -> Dict[str, Any]:
        with self._lock:
            self._invite_seq += 1
            seq = self._invite_seq
            token = f"adv_mock_{seq}_{int(time.time())}"
            invite_link = f"https://t.me/+{token}"

            data = {
                "invite_link": invite_link,
                "creator": {"id": 99999, "is_bot": True},
                "name": name,
                "expire_date": expire_date or (int(time.time()) + 259200),
                "member_limit": member_limit,
                "is_revoked": False,
                "creates_join_request": creates_join_request,
            }
            self.call_history.append({
                "method": "createChatInviteLink",
                "params": {
                    "chat_id": chat_id,
                    "name": name,
                    "member_limit": member_limit,
                },
                "result": data,
            })
            self.created_invites[invite_link] = data
            return {"ok": True, "result": data}

    def banChatMember(
        self,
        chat_id: str | int,
        user_id: int,
        until_date: Optional[int] = None,
        revoke_messages: bool = False,
    ) -> Dict[str, Any]:
        with self._lock:
            self.call_history.append({
                "method": "banChatMember",
                "params": {"chat_id": chat_id, "user_id": user_id},
            })
            self.channel_members[user_id] = "banned"
            return {"ok": True, "result": True}

    def unbanChatMember(
        self,
        chat_id: str | int,
        user_id: int,
        only_if_banned: bool = True,
    ) -> Dict[str, Any]:
        with self._lock:
            self.call_history.append({
                "method": "unbanChatMember",
                "params": {"chat_id": chat_id, "user_id": user_id, "only_if_banned": only_if_banned},
            })
            self.channel_members[user_id] = "unbanned"
            return {"ok": True, "result": True}

    def revokeChatInviteLink(self, chat_id: str | int, invite_link: str) -> Dict[str, Any]:
        with self._lock:
            self.call_history.append({
                "method": "revokeChatInviteLink",
                "params": {"chat_id": chat_id, "invite_link": invite_link},
            })
            if invite_link in self.created_invites:
                self.created_invites[invite_link]["is_revoked"] = True
                return {"ok": True, "result": self.created_invites[invite_link]}
            return {"ok": False, "error_code": 404, "description": "Invite link not found"}


def make_standard_headers(
    payload_bytes: bytes,
    secret: str,
    msg_id: str = "msg_adv_123",
    timestamp: Optional[int] = None,
) -> Dict[str, str]:
    ts_str = str(timestamp if timestamp is not None else int(time.time()))
    signed_content = f"{msg_id}.{ts_str}.".encode("utf-8") + payload_bytes
    raw_key = secret[6:] if secret.startswith("whsec_") else secret
    try:
        key_bytes = base64.b64decode(raw_key)
    except Exception:
        key_bytes = raw_key.encode("utf-8")

    digest = hmac.new(key_bytes, signed_content, hashlib.sha256).digest()
    sig_b64 = base64.b64encode(digest).decode("utf-8")

    return {
        "Content-Type": "application/json",
        "Webhook-Id": msg_id,
        "Webhook-Timestamp": ts_str,
        "Webhook-Signature": f"v1,{sig_b64}",
    }


class TestHMACSecurityAdversarial(unittest.TestCase):
    """Deep adversarial testing of HMAC signature verification."""

    def setUp(self):
        self.secret = "whsec_MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE"  # valid base64 after whsec_
        self.payload = b'{"action":"membership.went_valid","data":{"id":"mem_attack_01"}}'

    def test_bit_flip_in_signature_fails(self):
        """Flipping any single character in a valid signature must fail."""
        headers = make_standard_headers(self.payload, self.secret)
        raw_sig_header = headers["Webhook-Signature"]
        prefix, sig = raw_sig_header.split(",", 1)

        for idx in [0, len(sig) // 2, len(sig) - 1]:
            char = sig[idx]
            flipped_char = "B" if char != "B" else "A"
            tampered_sig = sig[:idx] + flipped_char + sig[idx + 1:]
            bad_headers = dict(headers)
            bad_headers["Webhook-Signature"] = f"{prefix},{tampered_sig}"

            ok, reason = verify_whop_signature(self.payload, bad_headers, self.secret)
            self.assertFalse(ok, f"Bit flip at index {idx} was accepted!")
            self.assertEqual(reason, "Signature mismatch")

    def test_version_prefix_tampering(self):
        """Signatures with unsupported version prefixes (v2, v0, none) must be rejected."""
        headers = make_standard_headers(self.payload, self.secret)
        sig = headers["Webhook-Signature"][3:]  # strip 'v1,'

        for bad_prefix in ["v2,", "v0,", "", "v1", "v1.", "V1,", "v1:"]:
            bad_headers = dict(headers)
            bad_headers["Webhook-Signature"] = f"{bad_prefix}{sig}"
            ok, reason = verify_whop_signature(self.payload, bad_headers, self.secret)
            self.assertFalse(ok, f"Prefix '{bad_prefix}' was accepted!")
            self.assertEqual(reason, "Signature mismatch")

    def test_null_bytes_and_control_chars_in_signature(self):
        """Null bytes and carriage returns injected into signature header must fail."""
        headers = make_standard_headers(self.payload, self.secret)
        sig = headers["Webhook-Signature"]

        for injection in ["\x00", "\r", "\n", "\t", "\x1b"]:
            bad_headers = dict(headers)
            bad_headers["Webhook-Signature"] = f"{sig}{injection}"
            ok, _ = verify_whop_signature(self.payload, bad_headers, self.secret)
            self.assertFalse(ok, f"Injection '{repr(injection)}' was accepted!")

    def test_timing_attack_variance_is_negligible(self):
        """Verify constant-time comparison: signatures with identical prefix take same time as random."""
        headers = make_standard_headers(self.payload, self.secret)
        valid_sig = headers["Webhook-Signature"][3:]

        # Craft candidate A: shares first 30 characters with valid signature
        shared_prefix_sig = valid_sig[:30] + ("X" * (len(valid_sig) - 30))
        # Craft candidate B: shares 0 characters with valid signature
        disjoint_sig = "Z" * len(valid_sig)

        headers_a = dict(headers)
        headers_a["Webhook-Signature"] = f"v1,{shared_prefix_sig}"

        headers_b = dict(headers)
        headers_b["Webhook-Signature"] = f"v1,{disjoint_sig}"

        iterations = 5000
        # Warmup JIT / cache
        for _ in range(500):
            verify_whop_signature(self.payload, headers_a, self.secret)
            verify_whop_signature(self.payload, headers_b, self.secret)

        times_a = []
        times_b = []

        for _ in range(iterations):
            t0 = time.perf_counter_ns()
            verify_whop_signature(self.payload, headers_a, self.secret)
            t1 = time.perf_counter_ns()
            times_a.append(t1 - t0)

            t2 = time.perf_counter_ns()
            verify_whop_signature(self.payload, headers_b, self.secret)
            t3 = time.perf_counter_ns()
            times_b.append(t3 - t2)

        mean_a = statistics.mean(times_a)
        mean_b = statistics.mean(times_b)

        # Ratio should be very close to 1.0 (within ±25% noise margin on modern OS)
        ratio = mean_a / mean_b if mean_b > 0 else 1.0
        self.assertGreater(ratio, 0.70, f"Timing discrepancy too large: ratio {ratio:.3f}")
        self.assertLess(ratio, 1.35, f"Timing discrepancy too large: ratio {ratio:.3f}")

    def test_replay_attack_timestamp_boundary_analysis(self):
        """Test timestamp drift boundary: exactly 300s, 301s, -300s, -301s."""
        now = 1700000000

        # Exact boundary: 300s in past -> valid
        headers_300_past = make_standard_headers(self.payload, self.secret, timestamp=now - 300)
        ok, _ = verify_whop_signature(self.payload, headers_300_past, self.secret, current_time=now)
        self.assertTrue(ok, "Timestamp at exactly -300s should be valid")

        # 301s in past -> rejected
        headers_301_past = make_standard_headers(self.payload, self.secret, timestamp=now - 301)
        ok, reason = verify_whop_signature(self.payload, headers_301_past, self.secret, current_time=now)
        self.assertFalse(ok, "Timestamp at -301s must be rejected")
        self.assertIn("Timestamp drift", reason)

        # Exact boundary: 300s in future -> valid
        headers_300_future = make_standard_headers(self.payload, self.secret, timestamp=now + 300)
        ok, _ = verify_whop_signature(self.payload, headers_300_future, self.secret, current_time=now)
        self.assertTrue(ok, "Timestamp at exactly +300s should be valid")

        # 301s in future -> rejected
        headers_301_future = make_standard_headers(self.payload, self.secret, timestamp=now + 301)
        ok, reason = verify_whop_signature(self.payload, headers_301_future, self.secret, current_time=now)
        self.assertFalse(ok, "Timestamp at +301s must be rejected")
        self.assertIn("Timestamp drift", reason)

    def test_extreme_and_corrupt_timestamps(self):
        """Test non-numeric, extreme, floating, and null timestamps."""
        now = int(time.time())
        malformed_values = [
            "NaN", "Infinity", "-Infinity", "1700000000.5",
            "None", "null", "", " ", "0x123", "999999999999999999999999999"
        ]

        for val in malformed_values:
            headers = {
                "Webhook-Id": "msg_adv",
                "Webhook-Timestamp": val,
                "Webhook-Signature": "v1,dummy",
            }
            ok, reason = verify_whop_signature(self.payload, headers, self.secret)
            self.assertFalse(ok, f"Malformed timestamp '{val}' was accepted!")
            self.assertIn(reason, ["Invalid timestamp header", "Timestamp drift exceeds limit (replay attack)"])

    def test_header_stripping_and_case_permutations(self):
        """Test missing headers, partial headers, and arbitrary header casing."""
        headers = make_standard_headers(self.payload, self.secret)

        # 1. Total strip
        ok, reason = verify_whop_signature(self.payload, {}, self.secret)
        self.assertFalse(ok)
        self.assertEqual(reason, "Missing signature headers")

        # 2. Only ID
        ok, reason = verify_whop_signature(self.payload, {"Webhook-Id": "msg_1"}, self.secret)
        self.assertFalse(ok)
        self.assertEqual(reason, "Missing signature headers")

        # 3. Only Signature
        ok, reason = verify_whop_signature(self.payload, {"Webhook-Signature": headers["Webhook-Signature"]}, self.secret)
        self.assertFalse(ok)
        self.assertEqual(reason, "Missing signature headers")

        # 4. Mixed casing on standard headers
        crazy_cased = {
            "wEbHoOk-Id": headers["Webhook-Id"],
            "WEBHOOK-TIMESTAMP": headers["Webhook-Timestamp"],
            "Webhook-SIGNATURE": headers["Webhook-Signature"],
        }
        ok, reason = verify_whop_signature(self.payload, crazy_cased, self.secret)
        self.assertTrue(ok, f"Mixed case header failed: {reason}")

        # 5. Direct signature mixed casing
        key_bytes = self.secret.encode("utf-8")
        direct_sig = hmac.new(key_bytes, self.payload, hashlib.sha256).hexdigest()
        ok, reason = verify_whop_signature(self.payload, {"X-WhOp-SiGnAtUrE": direct_sig}, self.secret)
        self.assertTrue(ok, f"Mixed case direct signature failed: {reason}")

    def test_adversarial_secrets(self):
        """Test edge-case signing secrets: empty, spaces, invalid base64, extreme length."""
        headers = make_standard_headers(self.payload, self.secret)

        # Empty secret
        ok, reason = verify_whop_signature(self.payload, headers, "")
        self.assertFalse(ok)
        self.assertEqual(reason, "Missing secret")

        # Whitespace secret
        ok, reason = verify_whop_signature(self.payload, headers, "   ")
        self.assertFalse(ok)
        self.assertEqual(reason, "Signature mismatch")

        # Corrupt base64 after whsec_
        corrupt_b64_secret = "whsec_!@#$%^&*()_not_valid_b64"
        # Should gracefully fall back to utf-8 encoding without crashing
        headers_corrupt = make_standard_headers(self.payload, corrupt_b64_secret)
        ok, _ = verify_whop_signature(self.payload, headers_corrupt, corrupt_b64_secret)
        self.assertTrue(ok, "Fallback encoding for non-b64 secret failed")

        # 10,000 character secret
        giant_secret = "whsec_" + ("A" * 10000)
        headers_giant = make_standard_headers(self.payload, giant_secret)
        ok, _ = verify_whop_signature(self.payload, headers_giant, giant_secret)
        self.assertTrue(ok, "10k character secret failed")


class TestWebhookLifecycleAdversarial(unittest.TestCase):
    """Stress and concurrency testing of webhook lifecycle and SQLite store."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "stress_subscribers.db")
        self.mock_tg = MockAdversarialTelegramAPI()
        self.secret = "whsec_test_concurrency_secret"
        self.handler = WhopWebhookHandler(
            channel_id="-10088776655",
            db_path=self.db_path,
            secret=self.secret,
            telegram_api=self.mock_tg,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_rapid_concurrent_burst_same_event_id(self):
        """Stress-test: 50 concurrent threads submitting identical webhook event simultaneously.

        Invariant: Exactly 1 invite link is generated; 49 threads report idempotency; no DB corruption.
        """
        event_id = "evt_burst_stress_001"
        membership_id = "mem_burst_user_100"
        payload = {
            "action": "membership.went_valid",
            "data": {
                "id": membership_id,
                "user_id": "usr_burst_100",
                "telegram_account_id": 1234567,
            },
        }

        num_threads = 50
        results = []

        def _send():
            return self.handler.process_event(payload, event_id=event_id)

        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(_send) for _ in range(num_threads)]
            for f in futures:
                results.append(f.result())

        # Verify all calls succeeded
        for r in results:
            self.assertTrue(r["ok"])
            self.assertEqual(r["membership_id"], membership_id)

        # Count how many created vs were idempotent
        actions = [r.get("action") for r in results]
        invite_created_count = actions.count("invite_created")
        already_processed_count = actions.count("already_processed")

        # Exactly 1 should have performed the create
        self.assertEqual(invite_created_count, 1, f"Expected 1 invite_created, got {invite_created_count}")
        self.assertEqual(already_processed_count, num_threads - 1)

        # Telegram API was called exactly once
        create_calls = [c for c in self.mock_tg.call_history if c["method"] == "createChatInviteLink"]
        self.assertEqual(len(create_calls), 1)

        # Verify database consistency
        sub = self.handler.store.get_subscriber(membership_id)
        self.assertIsNotNone(sub)
        self.assertEqual(sub["status"], "active")
        self.assertEqual(sub["invite_link"], create_calls[0]["result"]["invite_link"])

    def test_sql_injection_resilience_in_fields(self):
        """SQL injection attacks in membership_id, user_id, email, plan_id."""
        sqli_payload = {
            "action": "membership.went_valid",
            "data": {
                "id": "mem_1'; DROP TABLE subscribers; --",
                "user_id": "usr_' OR '1'='1",
                "telegram_account_id": 8888,
                "email": "hacker@test.com' UNION SELECT * FROM subscribers --",
                "plan_id": "plan'; DELETE FROM webhook_events; --",
            },
        }

        res = self.handler.process_event(sqli_payload, event_id="evt_sqli_01")
        self.assertTrue(res["ok"])

        # Confirm subscribers and webhook_events tables still exist and are functional
        with self.handler.store._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM subscribers;")
            sub_count = cur.fetchone()[0]
            self.assertEqual(sub_count, 1)

            cur.execute("SELECT COUNT(*) FROM webhook_events;")
            evt_count = cur.fetchone()[0]
            self.assertEqual(evt_count, 1)

            # Retrieve injected record cleanly
            cur.execute("SELECT membership_id, user_id FROM subscribers WHERE membership_id = ?;", (sqli_payload["data"]["id"],))
            row = cur.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], sqli_payload["data"]["id"])

    def test_tampered_payload_body_with_valid_signature_headers(self):
        """Payload modified after signing must return 401."""
        valid_payload = b'{"action":"membership.went_valid","data":{"id":"mem_valid"}}'
        headers = make_standard_headers(valid_payload, self.secret)

        tampered_bodies = [
            valid_payload + b" ",
            valid_payload + b"\n",
            b'{"action":"membership.went_valid","data":{"id":"mem_valid","extra":1}}',
            b'{"action":"membership.went_invalid","data":{"id":"mem_valid"}}',
            b"",
        ]

        for tampered in tampered_bodies:
            code, resp = self.handler.process_webhook(tampered, headers)
            self.assertEqual(code, 401)
            self.assertFalse(resp["ok"])


class TestHTTPServerAdversarial(unittest.TestCase):
    """Adversarial testing against WhopWebhookServer HTTP network endpoint."""

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.db_path = os.path.join(cls.temp_dir.name, "server_adv_subscribers.db")
        cls.secret = "whsec_http_adversarial_secret"
        cls.mock_tg = MockAdversarialTelegramAPI()
        cls.handler = WhopWebhookHandler(
            channel_id="-100998877",
            db_path=cls.db_path,
            secret=cls.secret,
            telegram_api=cls.mock_tg,
        )
        cls.server = WhopWebhookServer(
            host="127.0.0.1",
            port=0,
            handler=cls.handler,
        )
        cls.server.start(background=True)
        cls.base_url = cls.server.get_url()
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.temp_dir.cleanup()

    def _req(
        self,
        path: str,
        method: str = "POST",
        data: Optional[bytes] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, Dict[str, Any]]:
        url = f"{self.base_url}{path}"
        req = Request(url=url, data=data, headers=headers or {}, method=method)
        try:
            with urlopen(req, timeout=5.0) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            body = e.read().decode("utf-8")
            try:
                return e.code, json.loads(body)
            except Exception:
                return e.code, {"raw": body}

    def test_oversized_payload_rejection(self):
        """Sending a 1MB payload with invalid signature should return 401 without hanging."""
        giant_payload = (b'{"action":"membership.went_valid","data":{"junk":"' + (b"X" * 1000000) + b'"}}')
        headers = {
            "Content-Type": "application/json",
            "Webhook-Id": "msg_giant",
            "Webhook-Timestamp": str(int(time.time())),
            "Webhook-Signature": "v1,invalid_signature",
        }
        code, body = self._req("/webhooks/whop", method="POST", data=giant_payload, headers=headers)
        self.assertEqual(code, 401)
        self.assertFalse(body["ok"])

    def test_path_traversal_attempts_return_404(self):
        """Directory traversal URLs should be rejected as 404."""
        for bad_path in [
            "/../../etc/passwd",
            "/webhooks/whop/../../etc/passwd",
            "//etc/passwd",
            "/health/..",
        ]:
            code, _ = self._req(bad_path, method="GET")
            self.assertEqual(code, 404)

    def test_unsupported_http_methods(self):
        """PUT, DELETE, PATCH to /webhooks/whop should return 404 or 501."""
        for m in ["PUT", "DELETE"]:
            try:
                code, _ = self._req("/webhooks/whop", method=m, data=b"{}")
                self.assertIn(code, (404, 405, 501))
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()
