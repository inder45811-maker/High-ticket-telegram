"""Comprehensive unit and integration test suite for Whop Webhook Access Manager (Milestone 4 / R4)."""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
import time
import unittest
from typing import Any, Dict, Optional, Tuple
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from b2b_alert_bot.webhook.handler import SubscriberStore, WhopWebhookHandler
from b2b_alert_bot.webhook.security import (
    WebhookVerifier,
    normalize_headers,
    verify_signature,
    verify_whop_signature,
)
from b2b_alert_bot.webhook.server import WhopWebhookServer, create_webhook_app


class MockTelegramBotAPI:
    """Mock Telegram Bot API simulating channel invite generation and member ejection."""

    def __init__(self):
        self.call_history = []
        self.created_invites: Dict[str, Dict[str, Any]] = {}
        self.channel_members: Dict[int, str] = {}
        self._invite_seq = 1

    def createChatInviteLink(
        self,
        chat_id: str | int,
        name: Optional[str] = None,
        expire_date: Optional[int] = None,
        member_limit: int = 1,
        creates_join_request: bool = False,
    ) -> Dict[str, Any]:
        self.call_history.append({
            "method": "createChatInviteLink",
            "params": {
                "chat_id": chat_id,
                "name": name,
                "expire_date": expire_date,
                "member_limit": member_limit,
                "creates_join_request": creates_join_request,
            },
        })
        if member_limit != 1:
            return {"ok": False, "error_code": 400, "description": "member_limit must be 1"}

        token = f"mock_{self._invite_seq}_{int(time.time())}"
        self._invite_seq += 1
        invite_link = f"https://t.me/+{token}"

        data = {
            "invite_link": invite_link,
            "creator": {"id": 12345, "is_bot": True},
            "name": name,
            "expire_date": expire_date or (int(time.time()) + 259200),
            "member_limit": member_limit,
            "is_revoked": False,
            "creates_join_request": creates_join_request,
        }
        self.created_invites[invite_link] = data
        return {"ok": True, "result": data}

    def banChatMember(
        self,
        chat_id: str | int,
        user_id: int,
        until_date: Optional[int] = None,
        revoke_messages: bool = False,
    ) -> Dict[str, Any]:
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
        self.call_history.append({
            "method": "unbanChatMember",
            "params": {"chat_id": chat_id, "user_id": user_id, "only_if_banned": only_if_banned},
        })
        if only_if_banned and self.channel_members.get(user_id) != "banned":
            return {"ok": True, "result": False}
        self.channel_members[user_id] = "kicked_neutral"
        return {"ok": True, "result": True}

    def revokeChatInviteLink(self, chat_id: str | int, invite_link: str) -> Dict[str, Any]:
        self.call_history.append({
            "method": "revokeChatInviteLink",
            "params": {"chat_id": chat_id, "invite_link": invite_link},
        })
        if invite_link in self.created_invites:
            self.created_invites[invite_link]["is_revoked"] = True
            return {"ok": True, "result": self.created_invites[invite_link]}
        return {"ok": False, "error_code": 404, "description": "Invite link not found"}


def generate_standard_webhook_headers(
    payload_bytes: bytes,
    secret: str,
    msg_id: Optional[str] = None,
    timestamp: Optional[int] = None,
) -> Dict[str, str]:
    """Helper to generate Standard Webhook headers for testing."""
    msg_id = msg_id or f"msg_{hashlib.sha256(payload_bytes).hexdigest()[:16]}"
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


def generate_direct_headers(payload_bytes: bytes, secret: str) -> Dict[str, str]:
    """Helper to generate direct x-whop-signature headers for testing."""
    key_bytes = secret.encode("utf-8")
    sig_hex = hmac.new(key_bytes, payload_bytes, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Whop-Signature": sig_hex,
    }


# ==============================================================================
# Security & HMAC Signature Verification Unit Tests
# ==============================================================================


class TestWhopWebhookSecurity(unittest.TestCase):
    """Test suite for HMAC-SHA256 signature verification and replay prevention."""

    def setUp(self):
        self.secret = "whsec_test_secret_abc12345"
        self.plain_secret = "my_plain_webhook_secret_key"
        self.payload = b'{"action":"membership.went_valid","data":{"id":"mem_001"}}'

    def test_standard_webhook_valid_signature_with_whsec_prefix(self):
        headers = generate_standard_webhook_headers(self.payload, self.secret)
        ok, msg = verify_whop_signature(self.payload, headers, self.secret)
        self.assertTrue(ok)
        self.assertEqual(msg, "Valid Standard Webhook signature")

    def test_standard_webhook_valid_signature_plain_secret(self):
        headers = generate_standard_webhook_headers(self.payload, self.plain_secret)
        ok, msg = verify_whop_signature(self.payload, headers, self.plain_secret)
        self.assertTrue(ok)
        self.assertEqual(msg, "Valid Standard Webhook signature")

    def test_standard_webhook_multiple_signatures_header(self):
        headers = generate_standard_webhook_headers(self.payload, self.secret)
        # Prepend an older or alternative signature
        headers["Webhook-Signature"] = f"v1,old_invalid_signature_here {headers['Webhook-Signature']}"
        ok, msg = verify_whop_signature(self.payload, headers, self.secret)
        self.assertTrue(ok)
        self.assertEqual(msg, "Valid Standard Webhook signature")

    def test_direct_whop_signature_hex(self):
        headers = generate_direct_headers(self.payload, self.plain_secret)
        ok, msg = verify_whop_signature(self.payload, headers, self.plain_secret)
        self.assertTrue(ok)
        self.assertEqual(msg, "Valid x-whop-signature")

    def test_direct_whop_signature_base64(self):
        key_bytes = self.plain_secret.encode("utf-8")
        sig_b64 = base64.b64encode(hmac.new(key_bytes, self.payload, hashlib.sha256).digest()).decode("utf-8")
        headers = {"x-whop-signature": sig_b64}
        ok, msg = verify_whop_signature(self.payload, headers, self.plain_secret)
        self.assertTrue(ok)
        self.assertEqual(msg, "Valid x-whop-signature")

    def test_tampered_payload_fails_verification(self):
        headers = generate_standard_webhook_headers(self.payload, self.secret)
        tampered_body = b'{"action":"membership.went_valid","data":{"id":"mem_tampered"}}'
        ok, msg = verify_whop_signature(tampered_body, headers, self.secret)
        self.assertFalse(ok)
        self.assertIn("Signature mismatch", msg)

    def test_expired_timestamp_rejected_as_replay_attack(self):
        old_ts = int(time.time()) - 400  # 400 seconds ago (> 300s limit)
        headers = generate_standard_webhook_headers(self.payload, self.secret, timestamp=old_ts)
        ok, msg = verify_whop_signature(self.payload, headers, self.secret)
        self.assertFalse(ok)
        self.assertIn("Timestamp drift", msg)

    def test_future_timestamp_drift_rejected(self):
        future_ts = int(time.time()) + 400  # 400 seconds in future
        headers = generate_standard_webhook_headers(self.payload, self.secret, timestamp=future_ts)
        ok, msg = verify_whop_signature(self.payload, headers, self.secret)
        self.assertFalse(ok)
        self.assertIn("Timestamp drift", msg)

    def test_invalid_timestamp_header_format(self):
        headers = {
            "Webhook-Id": "msg_test",
            "Webhook-Timestamp": "not_an_integer",
            "Webhook-Signature": "v1,abc",
        }
        ok, msg = verify_whop_signature(self.payload, headers, self.secret)
        self.assertFalse(ok)
        self.assertIn("Invalid timestamp", msg)

    def test_missing_signature_headers(self):
        headers = {"Content-Type": "application/json"}
        ok, msg = verify_whop_signature(self.payload, headers, self.secret)
        self.assertFalse(ok)
        self.assertEqual(msg, "Missing signature headers")

    def test_missing_secret(self):
        headers = generate_standard_webhook_headers(self.payload, self.secret)
        ok, msg = verify_whop_signature(self.payload, headers, "")
        self.assertFalse(ok)
        self.assertEqual(msg, "Missing secret")

    def test_wrong_secret_fails_signature(self):
        headers = generate_standard_webhook_headers(self.payload, self.secret)
        ok, msg = verify_whop_signature(self.payload, headers, "wrong_secret_xyz")
        self.assertFalse(ok)
        self.assertIn("Signature mismatch", msg)

    def test_case_insensitive_header_lookup(self):
        headers = {
            "WEBHOOK-ID": "msg_uppercase_123",
            "WEBHOOK-TIMESTAMP": str(int(time.time())),
            "WEBHOOK-SIGNATURE": "",
        }
        # Compute signature with uppercase keys
        signed_content = f"msg_uppercase_123.{headers['WEBHOOK-TIMESTAMP']}.".encode("utf-8") + self.payload
        key_bytes = self.plain_secret.encode("utf-8")
        sig_b64 = base64.b64encode(hmac.new(key_bytes, signed_content, hashlib.sha256).digest()).decode("utf-8")
        headers["WEBHOOK-SIGNATURE"] = f"v1,{sig_b64}"

        ok, msg = verify_whop_signature(self.payload, headers, self.plain_secret)
        self.assertTrue(ok)
        self.assertEqual(msg, "Valid Standard Webhook signature")

    def test_string_payload_auto_encoding(self):
        payload_str = self.payload.decode("utf-8")
        headers = generate_standard_webhook_headers(self.payload, self.secret)
        ok, msg = verify_whop_signature(payload_str, headers, self.secret)
        self.assertTrue(ok)

    def test_webhook_verifier_class_helper(self):
        verifier = WebhookVerifier(secret=self.secret)
        headers = generate_standard_webhook_headers(self.payload, self.secret)
        self.assertTrue(verifier.is_valid(self.payload, headers))

        tampered = b"tampered_body"
        self.assertFalse(verifier.is_valid(tampered, headers))

    def test_verify_signature_boolean_wrapper(self):
        headers = generate_standard_webhook_headers(self.payload, self.secret)
        self.assertTrue(verify_signature(self.payload, headers, self.secret))
        self.assertFalse(verify_signature(self.payload, headers, "bad_secret"))


# ==============================================================================
# Webhook Handler Lifecycle & Idempotency Unit Tests
# ==============================================================================


class TestWhopWebhookHandler(unittest.TestCase):
    """Test suite for WhopWebhookHandler business logic and state transitions."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_subscribers.db")
        self.mock_tg = MockTelegramBotAPI()
        self.secret = "test_whop_secret_999"
        self.handler = WhopWebhookHandler(
            channel_id="-100999888777",
            db_path=self.db_path,
            secret=self.secret,
            telegram_api=self.mock_tg,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_membership_went_valid_provisions_invite_link(self):
        payload = {
            "action": "membership.went_valid",
            "data": {
                "id": "mem_cust_101",
                "user_id": "usr_vip_001",
                "telegram_account_id": 987654321,
                "email": "lead@b2balert.com",
                "plan_id": "plan_monthly_79",
            },
        }

        res = self.handler.process_event(payload, event_id="evt_001")
        self.assertTrue(res["ok"])
        self.assertEqual(res["action"], "invite_created")
        self.assertEqual(res["membership_id"], "mem_cust_101")
        self.assertTrue(res["invite_link"].startswith("https://t.me/+mock_"))
        self.assertEqual(res["member_limit"], 1)

        # Verify record in SQLite database
        sub = self.handler.store.get_subscriber("mem_cust_101")
        self.assertIsNotNone(sub)
        self.assertEqual(sub["membership_id"], "mem_cust_101")
        self.assertEqual(sub["telegram_user_id"], 987654321)
        self.assertEqual(sub["status"], "active")
        self.assertEqual(sub["invite_link"], res["invite_link"])

    def test_went_valid_idempotency_same_event_id(self):
        payload = {
            "action": "membership.went_valid",
            "data": {
                "id": "mem_cust_102",
                "user_id": "usr_vip_002",
                "telegram_account_id": 11223344,
            },
        }

        # First call
        res1 = self.handler.process_event(payload, event_id="evt_dedup_01")
        self.assertTrue(res1["ok"])
        link1 = res1["invite_link"]

        # Duplicate call with exact same event_id
        res2 = self.handler.process_event(payload, event_id="evt_dedup_01")
        self.assertTrue(res2["ok"])
        self.assertTrue(res2.get("idempotent"))
        self.assertEqual(res2["invite_link"], link1)

        # Verify only 1 invite link was created in mock Telegram API
        invites = [c for c in self.mock_tg.call_history if c["method"] == "createChatInviteLink"]
        self.assertEqual(len(invites), 1)

    def test_went_valid_idempotency_resends_existing_active_link_on_different_event_id(self):
        payload = {
            "action": "membership.went_valid",
            "data": {
                "id": "mem_cust_103",
                "user_id": "usr_vip_003",
            },
        }

        res1 = self.handler.process_event(payload, event_id="evt_initial")
        self.assertTrue(res1["ok"])

        # Second call with new event_id (e.g. Whop retry with new message id)
        res2 = self.handler.process_event(payload, event_id="evt_retry_whop")
        self.assertTrue(res2["ok"])
        self.assertTrue(res2.get("idempotent"))
        self.assertEqual(res2["invite_link"], res1["invite_link"])

    def test_membership_went_invalid_revocation_kick(self):
        # Setup active subscriber
        setup_payload = {
            "action": "membership.went_valid",
            "data": {
                "id": "mem_to_cancel_201",
                "user_id": "usr_churn_01",
                "telegram_account_id": 555666777,
            },
        }
        res_valid = self.handler.process_event(setup_payload)
        active_link = res_valid["invite_link"]

        # Mark user as joined
        self.mock_tg.channel_members[555666777] = "active"

        # Now trigger membership.went_invalid
        cancel_payload = {
            "action": "membership.went_invalid",
            "data": {
                "id": "mem_to_cancel_201",
            },
        }
        res_cancel = self.handler.process_event(cancel_payload, event_id="evt_cancel_01")
        self.assertTrue(res_cancel["ok"])
        self.assertEqual(res_cancel["action"], "revoked")
        self.assertTrue(res_cancel["kick_executed"])

        # Check Telegram API calls: ban followed by unban
        history = self.mock_tg.call_history
        ban_calls = [c for c in history if c["method"] == "banChatMember"]
        unban_calls = [c for c in history if c["method"] == "unbanChatMember"]
        revoke_link_calls = [c for c in history if c["method"] == "revokeChatInviteLink"]

        self.assertEqual(len(ban_calls), 1)
        self.assertEqual(ban_calls[0]["params"]["user_id"], 555666777)
        self.assertEqual(len(unban_calls), 1)
        self.assertEqual(unban_calls[0]["params"]["user_id"], 555666777)
        self.assertTrue(unban_calls[0]["params"]["only_if_banned"])
        self.assertEqual(len(revoke_link_calls), 1)
        self.assertEqual(revoke_link_calls[0]["params"]["invite_link"], active_link)

        # Check user member state in mock
        self.assertEqual(self.mock_tg.channel_members[555666777], "kicked_neutral")

        # Check DB status
        sub = self.handler.store.get_subscriber("mem_to_cancel_201")
        self.assertEqual(sub["status"], "invalid")
        self.assertIsNotNone(sub["revoked_at"])

    def test_membership_went_invalid_unlinked_user_revokes_invite_gracefully(self):
        # Subscriber never linked their Telegram account
        setup_payload = {
            "action": "membership.went_valid",
            "data": {
                "id": "mem_unlinked_301",
                "user_id": "usr_anon_301",
            },
        }
        res_valid = self.handler.process_event(setup_payload)
        invite_link = res_valid["invite_link"]

        # Cancel event
        cancel_payload = {
            "action": "membership.went_invalid",
            "data": {"id": "mem_unlinked_301"},
        }
        res_cancel = self.handler.process_event(cancel_payload)
        self.assertTrue(res_cancel["ok"])
        self.assertFalse(res_cancel["kick_executed"])  # No Telegram ID to kick

        # Invite link must still be revoked
        revoke_calls = [c for c in self.mock_tg.call_history if c["method"] == "revokeChatInviteLink"]
        self.assertEqual(len(revoke_calls), 1)
        self.assertEqual(revoke_calls[0]["params"]["invite_link"], invite_link)

    def test_unhandled_action_is_safely_ignored(self):
        payload = {
            "action": "payment.succeeded",
            "data": {"id": "pay_999"},
        }
        res = self.handler.process_event(payload)
        self.assertTrue(res["ok"])
        self.assertEqual(res["status"], "ignored")

    def test_async_handle_event_interface_contract(self):
        data = {
            "id": "mem_async_401",
            "user_id": "usr_async_401",
            "telegram_account_id": 99887766,
        }
        res = asyncio.run(self.handler.handle_event("membership.went_valid", data))
        self.assertTrue(res["ok"])
        self.assertEqual(res["membership_id"], "mem_async_401")

    def test_process_webhook_pipeline_with_signature_and_invalid_auth(self):
        payload = b'{"action":"membership.went_valid","data":{"id":"mem_sig_501"}}'
        headers = generate_standard_webhook_headers(payload, self.secret)

        # 1. Valid signature
        code, body = self.handler.process_webhook(payload, headers)
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])

        # 2. Invalid signature
        bad_headers = dict(headers)
        bad_headers["Webhook-Signature"] = "v1,bad_sig"
        code2, body2 = self.handler.process_webhook(payload, bad_headers)
        self.assertEqual(code2, 401)
        self.assertFalse(body2["ok"])

        # 3. Invalid JSON
        code3, body3 = self.handler.process_webhook(b"not valid json", headers)
        # Signature fails because payload changed or json fails
        self.assertIn(code3, (400, 401))


# ==============================================================================
# Webhook HTTP Server Integration Tests
# ==============================================================================


class TestWhopWebhookServer(unittest.TestCase):
    """Test suite for HTTP server endpoints (/webhooks/whop, /health, /ready)."""

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.db_path = os.path.join(cls.temp_dir.name, "server_subscribers.db")
        cls.secret = "whsec_server_test_secret_123"
        cls.mock_tg = MockTelegramBotAPI()
        cls.handler = WhopWebhookHandler(
            channel_id="-10011223344",
            db_path=cls.db_path,
            secret=cls.secret,
            telegram_api=cls.mock_tg,
        )
        # Bind to ephemeral port 0
        cls.server = WhopWebhookServer(
            host="127.0.0.1",
            port=0,
            handler=cls.handler,
        )
        cls.server.start(background=True)
        cls.base_url = cls.server.get_url()
        # Allow thread to bind
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.temp_dir.cleanup()

    def _http_request(
        self,
        path: str,
        method: str = "GET",
        data: Optional[bytes] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, Dict[str, Any]]:
        url = f"{self.base_url}{path}"
        req_headers = headers or {}
        req = Request(url=url, data=data, headers=req_headers, method=method)
        try:
            with urlopen(req, timeout=5.0) as resp:
                body = resp.read().decode("utf-8")
                return resp.status, json.loads(body)
        except HTTPError as e:
            body = e.read().decode("utf-8")
            try:
                return e.code, json.loads(body)
            except Exception:
                return e.code, {"raw": body}

    def test_get_health_endpoint(self):
        code, body = self._http_request("/health")
        self.assertEqual(code, 200)
        self.assertEqual(body.get("status"), "healthy")
        self.assertEqual(body.get("service"), "b2b_alert_bot_webhook")

    def test_get_ready_endpoint(self):
        code, body = self._http_request("/ready")
        self.assertEqual(code, 200)
        self.assertEqual(body.get("status"), "ready")
        self.assertEqual(body.get("database"), "connected")

    def test_post_webhook_valid_went_valid(self):
        payload = json.dumps({
            "action": "membership.went_valid",
            "data": {
                "id": "mem_http_001",
                "user_id": "usr_http_001",
                "telegram_account_id": 990011,
            },
        }).encode("utf-8")

        headers = generate_standard_webhook_headers(payload, self.secret)
        code, body = self._http_request("/webhooks/whop", method="POST", data=payload, headers=headers)

        self.assertEqual(code, 200)
        self.assertTrue(body.get("ok"))
        self.assertEqual(body.get("action"), "invite_created")
        self.assertTrue(body.get("invite_link").startswith("https://t.me/+mock_"))

    def test_post_webhook_invalid_signature_returns_401(self):
        payload = json.dumps({"action": "membership.went_valid", "data": {"id": "mem_hack"}}).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Webhook-Id": "msg_spoofed",
            "Webhook-Timestamp": str(int(time.time())),
            "Webhook-Signature": "v1,forged_signature",
        }
        code, body = self._http_request("/webhooks/whop", method="POST", data=payload, headers=headers)
        self.assertEqual(code, 401)
        self.assertFalse(body.get("ok"))
        self.assertEqual(body.get("error"), "Unauthorized")

    def test_post_webhook_went_invalid_revocation(self):
        payload = json.dumps({
            "action": "membership.went_invalid",
            "data": {
                "id": "mem_http_001",
            },
        }).encode("utf-8")

        headers = generate_standard_webhook_headers(payload, self.secret)
        code, body = self._http_request("/webhooks/whop", method="POST", data=payload, headers=headers)

        self.assertEqual(code, 200)
        self.assertTrue(body.get("ok"))
        self.assertEqual(body.get("action"), "revoked")

    def test_unknown_path_returns_404(self):
        code, body = self._http_request("/nonexistent_path")
        self.assertEqual(code, 404)
        self.assertIn("error", body)


if __name__ == "__main__":
    unittest.main()
