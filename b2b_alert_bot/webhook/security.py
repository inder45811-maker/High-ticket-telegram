"""Whop Webhook HMAC-SHA256 signature verification and security utilities.

Supports:
1. Standard Webhooks (Svix) specification:
   - `webhook-id`: Unique message ID
   - `webhook-timestamp`: Epoch timestamp in seconds
   - `webhook-signature`: Format 'v1,<base64_sig>' with 300s replay drift check
2. Direct/legacy `x-whop-signature`:
   - HMAC-SHA256 hex or base64 digest of raw payload
3. Constant-time comparison using hmac.compare_digest to prevent timing attacks.
"""

import base64
import hashlib
import hmac
import time
from typing import Any, Dict, Optional, Tuple, Union


def normalize_headers(headers: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """Convert header dictionary keys to lowercase for case-insensitive lookup."""
    if not headers:
        return {}
    return {str(k).lower(): str(v) for k, v in headers.items()}


def verify_whop_signature(
    raw_body: Union[bytes, str],
    headers: Dict[str, Any],
    secret: str,
    max_age_seconds: int = 300,
    current_time: Optional[int] = None,
) -> Tuple[bool, str]:
    """Verify Whop webhook signature against raw payload bytes.

    Args:
        raw_body: Raw request body as bytes or string.
        headers: Request HTTP headers dictionary.
        secret: Whop webhook signing secret (with or without 'whsec_' prefix).
        max_age_seconds: Maximum allowed timestamp drift in seconds (default: 300).
        current_time: Optional explicit timestamp override for deterministic testing.

    Returns:
        Tuple of (is_valid: bool, reason_message: str).
    """
    if not secret:
        return False, "Missing secret"

    if isinstance(raw_body, str):
        raw_body = raw_body.encode("utf-8")
    elif not isinstance(raw_body, (bytes, bytearray)):
        return False, "Invalid payload type"

    norm_headers = normalize_headers(headers)
    now = current_time if current_time is not None else int(time.time())

    # --------------------------------------------------------------------------
    # Case 1: Standard Webhooks specification (Svix standard used by Whop)
    # --------------------------------------------------------------------------
    if "webhook-id" in norm_headers or "webhook-timestamp" in norm_headers or "webhook-signature" in norm_headers:
        msg_id = norm_headers.get("webhook-id")
        ts_str = norm_headers.get("webhook-timestamp")
        sig_header = norm_headers.get("webhook-signature")

        if not (msg_id and sig_header):
            return False, "Missing signature headers"

        for bad_char in ["\x00", "\r", "\n", "\t", "\x1b"]:
            if bad_char in sig_header:
                return False, "Malformed signature header"

        if ts_str is None or not str(ts_str).strip():
            return False, "Invalid timestamp header"

        try:
            ts = int(str(ts_str).strip())
            if abs(now - ts) > max_age_seconds:
                return False, "Timestamp drift exceeds limit (replay attack)"
        except (ValueError, TypeError):
            return False, "Invalid timestamp header"

        signed_content = f"{msg_id}.{ts_str}.".encode("utf-8") + raw_body
        raw_key = secret[6:] if secret.startswith("whsec_") else secret
        try:
            key_bytes = base64.b64decode(raw_key)
        except Exception:
            key_bytes = raw_key.encode("utf-8")

        expected_sig = base64.b64encode(
            hmac.new(key_bytes, signed_content, hashlib.sha256).digest()
        ).decode("utf-8")

        # Header can contain multiple space-separated signatures (e.g. 'v1,abc v1,def')
        signatures = sig_header.split(" ")
        for sig_part in signatures:
            sig_part = sig_part.strip()
            if sig_part.startswith("v1,"):
                candidate_sig = sig_part[3:]
                if hmac.compare_digest(candidate_sig, expected_sig):
                    return True, "Valid Standard Webhook signature"

        return False, "Signature mismatch"

    # --------------------------------------------------------------------------
    # Case 2: Direct / Legacy x-whop-signature header
    # --------------------------------------------------------------------------
    x_sig = norm_headers.get("x-whop-signature")
    if x_sig:
        key_bytes = secret.encode("utf-8")
        expected_hex = hmac.new(key_bytes, raw_body, hashlib.sha256).hexdigest()
        expected_b64 = base64.b64encode(
            hmac.new(key_bytes, raw_body, hashlib.sha256).digest()
        ).decode("utf-8")

        if hmac.compare_digest(x_sig, expected_hex) or hmac.compare_digest(x_sig, expected_b64):
            return True, "Valid x-whop-signature"
        return False, "Signature mismatch"

    return False, "Missing signature headers"


def verify_signature(
    raw_body: Union[bytes, str],
    headers: Dict[str, Any],
    secret: str,
    max_age_seconds: int = 300,
    current_time: Optional[int] = None,
) -> bool:
    """Convenience boolean wrapper around verify_whop_signature."""
    ok, _ = verify_whop_signature(
        raw_body=raw_body,
        headers=headers,
        secret=secret,
        max_age_seconds=max_age_seconds,
        current_time=current_time,
    )
    return ok


class WebhookVerifier:
    """Object-oriented verifier instance with pre-configured secret and drift tolerance."""

    def __init__(self, secret: str, max_age_seconds: int = 300):
        self.secret = secret
        self.max_age_seconds = max_age_seconds

    def verify(
        self,
        raw_body: Union[bytes, str],
        headers: Dict[str, Any],
        current_time: Optional[int] = None,
    ) -> Tuple[bool, str]:
        """Verify signature using configured secret."""
        return verify_whop_signature(
            raw_body=raw_body,
            headers=headers,
            secret=self.secret,
            max_age_seconds=self.max_age_seconds,
            current_time=current_time,
        )

    def is_valid(
        self,
        raw_body: Union[bytes, str],
        headers: Dict[str, Any],
        current_time: Optional[int] = None,
    ) -> bool:
        """Return True if signature is valid, False otherwise."""
        ok, _ = self.verify(raw_body, headers, current_time=current_time)
        return ok
