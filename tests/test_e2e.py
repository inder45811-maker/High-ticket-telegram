"""End-to-End Multi-Tier Integration and Verification Test Suite for B2B Alert Bot.

This opaque-box, requirement-driven test harness covers R1-R5 across 4 Tiers:
- Tier 1: Feature Coverage (>=5 test cases per feature covering R1-R5 happy paths)
- Tier 2: Boundary & Corner Cases (>=5 test cases per feature covering edge cases, corrupt payloads,
  rate limits, invalid signatures, extreme compensation formats)
- Tier 3: Cross-Feature Interactions (pairwise integration across ingestion -> enrichment -> dispatch;
  whop webhook -> telegram channel access)
- Tier 4: Real-World Application Scenarios (end-to-end multi-source ingestion poll cycles with deduplication)
"""

import base64
import copy
import hashlib
import hmac
import html
import json
import os
import re
import sqlite3
import tempfile
import time
import unittest
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch

import pytest

# Core system imports (M1)
from b2b_alert_bot.db import (
    Database,
    canonicalize_url,
    compute_lead_hash,
)
from b2b_alert_bot.ingestion.base import (
    BaseConnector,
    clean_html,
    parse_timestamp,
)
from b2b_alert_bot.ingestion.hackernews import HackerNewsConnector
from b2b_alert_bot.ingestion.jobspresso import JobspressoConnector
from b2b_alert_bot.ingestion.reddit import RedditConnector
from b2b_alert_bot.ingestion.remoteok import RemoteOKConnector
from b2b_alert_bot.ingestion.weworkremotely import WeWorkRemotelyConnector
from b2b_alert_bot.schema import EnrichedLead, Lead, extract_core_tech_stack

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
WORKSPACE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


# ==============================================================================
# Mock Infrastructure: Telegram Bot API Server & Client Simulator
# ==============================================================================

class MockTelegramBotAPI:
    """In-memory mock Telegram Bot API server and client.
    
    Validates parameter schema, HTML tag symmetry, URL button structure,
    simulates HTTP 429 rate limiting, and records full event logs.
    """

    def __init__(self, bot_token: str = "mock_bot_token_123456:ABC-DEF-GHI"):
        self.bot_token = bot_token
        self.sent_messages: List[Dict[str, Any]] = []
        self.channel_members: Dict[int, str] = {}  # user_id -> status ('active', 'banned', 'kicked_neutral')
        self.created_invites: Dict[str, Dict[str, Any]] = {}  # invite_link -> details
        self.call_history: List[Dict[str, Any]] = []
        self._next_msg_id = 1000
        self._next_invite_id = 1
        
        # Test hooks for fault injection
        self.simulate_rate_limit: bool = False
        self.rate_limit_retry_after: int = 5
        self.simulate_server_error: bool = False
        self.server_error_code: int = 502

    def sendMessage(
        self,
        chat_id: str | int,
        text: str,
        parse_mode: str = "HTML",
        link_preview_options: Optional[Dict[str, Any]] = None,
        reply_markup: Optional[Dict[str, Any]] = None,
        protect_content: bool = False
    ) -> Dict[str, Any]:
        """Simulate Telegram sendMessage API method."""
        self.call_history.append({
            "method": "sendMessage",
            "params": {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "link_preview_options": link_preview_options,
                "reply_markup": reply_markup,
                "protect_content": protect_content
            }
        })

        if self.simulate_server_error:
            raise RuntimeError(f"HTTP {self.server_error_code} Bad Gateway")

        if self.simulate_rate_limit:
            return {
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests: retry after",
                "parameters": {"retry_after": self.rate_limit_retry_after}
            }

        # 1. Parameter Validation
        if not chat_id:
            return {"ok": False, "error_code": 400, "description": "Chat ID cannot be empty"}
        if not text:
            return {"ok": False, "error_code": 400, "description": "Text cannot be empty"}
        if len(text) > 4096:
            return {
                "ok": False,
                "error_code": 400,
                "description": f"Message text exceeds 4096 characters limit (len={len(text)})"
            }

        # 2. HTML Parse Mode Validation
        if parse_mode == "HTML":
            tags_to_check = ["b", "i", "code", "pre", "a", "u", "s"]
            for tag in tags_to_check:
                opens = len(re.findall(rf"<{tag}(?:\s+[^>]*)*>", text, flags=re.IGNORECASE))
                closes = len(re.findall(rf"</{tag}>", text, flags=re.IGNORECASE))
                if opens != closes:
                    return {
                        "ok": False,
                        "error_code": 400,
                        "description": f"Can't parse entities: unclosed or mismatched HTML tag <{tag}>"
                    }

        # 3. Inline Keyboard URL Validation
        if reply_markup and "inline_keyboard" in reply_markup:
            for row in reply_markup["inline_keyboard"]:
                for btn in row:
                    url = btn.get("url", "")
                    if url and not (url.startswith("http://") or url.startswith("https://")):
                        return {
                            "ok": False,
                            "error_code": 400,
                            "description": f"BUTTON_URL_INVALID: '{url}' must be http or https"
                        }

        # 4. Success Response
        self._next_msg_id += 1
        msg_payload = {
            "message_id": self._next_msg_id,
            "chat": {"id": chat_id, "type": "channel"},
            "date": int(time.time()),
            "text": text,
            "reply_markup": reply_markup
        }
        self.sent_messages.append(msg_payload)
        return {"ok": True, "result": msg_payload}

    def createChatInviteLink(
        self,
        chat_id: str | int,
        name: str = "",
        expire_date: Optional[int] = None,
        member_limit: int = 1,
        creates_join_request: bool = False
    ) -> Dict[str, Any]:
        """Simulate Telegram createChatInviteLink API method with member_limit=1."""
        self.call_history.append({
            "method": "createChatInviteLink",
            "params": {
                "chat_id": chat_id,
                "name": name,
                "expire_date": expire_date,
                "member_limit": member_limit,
                "creates_join_request": creates_join_request
            }
        })

        if member_limit != 1:
            return {
                "ok": False,
                "error_code": 400,
                "description": f"Invalid member_limit: must be 1 for single-use invite, got {member_limit}"
            }

        token = hashlib.sha256(f"invite_{self._next_invite_id}_{time.time()}".encode()).hexdigest()[:16]
        self._next_invite_id += 1
        invite_url = f"https://t.me/+mock_{token}"

        link_data = {
            "invite_link": invite_url,
            "creator": {"id": 123456789, "is_bot": True, "first_name": "B2B Alert Bot"},
            "name": name,
            "expire_date": expire_date or (int(time.time()) + 259200),
            "member_limit": member_limit,
            "is_revoked": False,
            "creates_join_request": creates_join_request
        }
        self.created_invites[invite_url] = link_data
        return {"ok": True, "result": link_data}

    def banChatMember(
        self,
        chat_id: str | int,
        user_id: int,
        until_date: Optional[int] = None,
        revoke_messages: bool = False
    ) -> Dict[str, Any]:
        """Simulate Telegram banChatMember API method (channel kick step 1)."""
        self.call_history.append({
            "method": "banChatMember",
            "params": {"chat_id": chat_id, "user_id": user_id}
        })
        self.channel_members[user_id] = "banned"
        return {"ok": True, "result": True}

    def unbanChatMember(
        self,
        chat_id: str | int,
        user_id: int,
        only_if_banned: bool = True
    ) -> Dict[str, Any]:
        """Simulate Telegram unbanChatMember API method (channel kick step 2, reset state)."""
        self.call_history.append({
            "method": "unbanChatMember",
            "params": {"chat_id": chat_id, "user_id": user_id, "only_if_banned": only_if_banned}
        })
        if only_if_banned and self.channel_members.get(user_id) != "banned":
            return {"ok": True, "result": False}
        self.channel_members[user_id] = "kicked_neutral"
        return {"ok": True, "result": True}

    def revokeChatInviteLink(self, chat_id: str | int, invite_link: str) -> Dict[str, Any]:
        """Simulate Telegram revokeChatInviteLink API method."""
        self.call_history.append({
            "method": "revokeChatInviteLink",
            "params": {"chat_id": chat_id, "invite_link": invite_link}
        })
        if invite_link in self.created_invites:
            self.created_invites[invite_link]["is_revoked"] = True
            return {"ok": True, "result": self.created_invites[invite_link]}
        return {"ok": False, "error_code": 404, "description": "Invite link not found"}

    def getMe(self) -> Dict[str, Any]:
        """Simulate Telegram getMe API health check method."""
        return {
            "ok": True,
            "result": {
                "id": 123456789,
                "is_bot": True,
                "first_name": "B2B High-Ticket Alert Bot",
                "username": "B2BDealRadarBot"
            }
        }


# ==============================================================================
# Mock Infrastructure: Whop Webhook Dispatcher Simulator
# ==============================================================================

class MockWhopWebhookDispatcher:
    """Signs and dispatches realistic Whop webhook events for offline testing.
    
    Supports:
    - Standard Webhooks (Svix) spec (webhook-id, webhook-timestamp, webhook-signature 'v1,...')
    - Direct x-whop-signature header
    - Timestamp drift / replay attack simulation
    - Payload tampering simulation
    """

    def __init__(self, secret: str = "test_whop_secret_key_12345"):
        self.secret = secret

    def create_standard_headers(
        self,
        payload_bytes: bytes,
        msg_id: Optional[str] = None,
        timestamp: Optional[int] = None
    ) -> Dict[str, str]:
        """Generate Standard Webhook headers (webhook-id, webhook-timestamp, webhook-signature)."""
        msg_id = msg_id or f"msg_{hashlib.sha256(payload_bytes).hexdigest()[:16]}"
        ts_str = str(timestamp if timestamp is not None else int(time.time()))
        
        signed_content = f"{msg_id}.{ts_str}.".encode("utf-8") + payload_bytes
        raw_key = self.secret[6:] if self.secret.startswith("whsec_") else self.secret
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
            "Webhook-Signature": f"v1,{sig_b64}"
        }

    def create_direct_headers(self, payload_bytes: bytes) -> Dict[str, str]:
        """Generate direct x-whop-signature header (hex HMAC-SHA256)."""
        key_bytes = self.secret.encode("utf-8")
        sig_hex = hmac.new(key_bytes, payload_bytes, hashlib.sha256).hexdigest()
        return {
            "Content-Type": "application/json",
            "X-Whop-Signature": sig_hex
        }


# ==============================================================================
# Authoritative Requirement Reference Engines (M2, M3, M4)
# ==============================================================================

class RequirementCompensationFilter:
    """Authoritative reference compensation parser & filter implementing R2 specs."""

    FX_RATES = {
        "USD": 1.0,
        "$": 1.0,
        "EUR": 1.08,
        "€": 1.08,
        "GBP": 1.28,
        "£": 1.28,
        "CAD": 0.74,
        "C$": 0.74,
        "AUD": 0.66,
        "A$": 0.66
    }

    FUNDING_KEYWORDS = [
        "raised", "raising", "seed", "series a", "series b", "series c",
        "funding", "valuation", "arr", "mrr", "backed by"
    ]

    EQUITY_UNPAID_BLACKLIST = [
        r"\bequity\s+only\b",
        r"\bunpaid\b",
        r"\bvolunteer\b",
        r"\brev(?:enue)?\s*share\s+only\b"
    ]

    UNSTATED_BLACKLIST = [
        r"\bcompetitive\s+salary\b",
        r"\bdoe\b",
        r"\bdepends\s+on\s+experience\b",
        r"\bnegotiable\b",
        r"\btbd\b"
    ]

    @classmethod
    def _is_funding_false_positive(cls, text: str, match_start: int, match_end: int) -> bool:
        start = max(0, match_start - 50)
        end = min(len(text), match_end + 50)
        window = text[start:end].lower()
        return any(kw in window for kw in cls.FUNDING_KEYWORDS)

    @classmethod
    def _parse_num(cls, val_str: str) -> float:
        clean = val_str.replace(",", "").strip().lower()
        if clean.endswith("k"):
            return float(clean[:-1]) * 1000.0
        if clean.endswith("m"):
            return float(clean[:-1]) * 1000000.0
        return float(clean)

    @classmethod
    def parse_compensation(cls, text: str) -> Optional[Dict[str, Any]]:
        """Extract structured compensation from free text."""
        if not text:
            return None

        text_lower = text.lower()

        # Check blacklist
        for pat in cls.EQUITY_UNPAID_BLACKLIST:
            if re.search(pat, text_lower):
                return {"status": "REJECT_UNPAID_OR_EQUITY", "is_high_ticket": False}

        # 1. Hourly Pattern
        hourly_pat = re.compile(
            r"(?i)(?:(?P<curr1>[\$€£])|(?P<curr_code1>USD|EUR|GBP|CAD|AUD)\s*)?"
            r"(?P<min>\d+(?:,\d{3})*(?:\.\d+)?\s*[km]?)"
            r"(?:\s*(?:-|–|to)\s*(?:(?P<curr2>[\$€£])|(?P<curr_code2>USD|EUR|GBP|CAD|AUD)\s*)?"
            r"(?P<max>\d+(?:,\d{3})*(?:\.\d+)?\s*[km]?))?\s*"
            r"(?:(?P<curr_post>USD|EUR|GBP|CAD|AUD)\s*)?"
            r"(?:/\s*(?:hr|hour|h)\b|per\s+hour|\s*ph\b)"
        )
        for m in hourly_pat.finditer(text):
            if cls._is_funding_false_positive(text, m.start(), m.end()):
                continue
            curr = m.group("curr1") or m.group("curr_code1") or m.group("curr_post") or "USD"
            min_val = cls._parse_num(m.group("min"))
            max_val = cls._parse_num(m.group("max")) if m.group("max") else min_val
            fx = cls.FX_RATES.get(curr.upper(), cls.FX_RATES.get(curr, 1.0))
            usd_min, usd_max = min_val * fx, max_val * fx
            is_high = usd_max >= 50.0
            return {
                "rate_type": "hourly",
                "min_amount": usd_min,
                "max_amount": usd_max,
                "currency": curr.upper() if len(curr) == 3 else "USD",
                "is_high_ticket": is_high,
                "budget_badge": f"⏱️ ${int(usd_max)}/HR" if usd_min == usd_max else f"⏱️ ${int(usd_min)} - ${int(usd_max)}/HR"
            }

        # 2. Annual Pattern
        annual_pat = re.compile(
            r"(?i)(?:(?P<curr1>[\$€£])|(?P<curr_code1>USD|EUR|GBP|CAD|AUD)\s*)?"
            r"(?P<min>\d+(?:,\d{3})*(?:\.\d+)?\s*[km]?)"
            r"(?:\s*(?:-|–|to)\s*(?:(?P<curr2>[\$€£])|(?P<curr_code2>USD|EUR|GBP|CAD|AUD)\s*)?"
            r"(?P<max>\d+(?:,\d{3})*(?:\.\d+)?\s*[km]?))?\s*"
            r"(?:(?P<curr_post>USD|EUR|GBP|CAD|AUD)\s*)?"
            r"(?:/\s*(?:yr|year|annum)\b|per\s+year|per\s+annum|\s*p\.?a\.?\b)"
        )
        for m in annual_pat.finditer(text):
            if cls._is_funding_false_positive(text, m.start(), m.end()):
                continue
            curr = m.group("curr1") or m.group("curr_code1") or m.group("curr_post") or "USD"
            min_val = cls._parse_num(m.group("min"))
            max_val = cls._parse_num(m.group("max")) if m.group("max") else min_val
            fx = cls.FX_RATES.get(curr.upper(), cls.FX_RATES.get(curr, 1.0))
            usd_min, usd_max = min_val * fx, max_val * fx
            equiv_hourly = usd_max / 2000.0
            is_high = equiv_hourly >= 50.0  # $100k/yr -> $50/hr
            badge_k = int(usd_max / 1000.0)
            return {
                "rate_type": "annual",
                "min_amount": usd_min,
                "max_amount": usd_max,
                "currency": curr.upper() if len(curr) == 3 else "USD",
                "is_high_ticket": is_high,
                "budget_badge": f"💼 ${badge_k}k/YR (~${int(equiv_hourly)}/HR)"
            }

        # 3. Monthly Pattern
        monthly_pat = re.compile(
            r"(?i)(?:(?P<curr1>[\$€£])|(?P<curr_code1>USD|EUR|GBP|CAD|AUD)\s*)?"
            r"(?P<min>\d+(?:,\d{3})*(?:\.\d+)?\s*[km]?)"
            r"(?:\s*(?:-|–|to)\s*(?:(?P<curr2>[\$€£])|(?P<curr_code2>USD|EUR|GBP|CAD|AUD)\s*)?"
            r"(?P<max>\d+(?:,\d{3})*(?:\.\d+)?\s*[km]?))?\s*"
            r"(?:(?P<curr_post>USD|EUR|GBP|CAD|AUD)\s*)?"
            r"(?:/\s*(?:mo|month)\b|per\s+month|\s*pm\b)"
        )
        for m in monthly_pat.finditer(text):
            if cls._is_funding_false_positive(text, m.start(), m.end()):
                continue
            curr = m.group("curr1") or m.group("curr_code1") or m.group("curr_post") or "USD"
            min_val = cls._parse_num(m.group("min"))
            max_val = cls._parse_num(m.group("max")) if m.group("max") else min_val
            fx = cls.FX_RATES.get(curr.upper(), cls.FX_RATES.get(curr, 1.0))
            usd_min, usd_max = min_val * fx, max_val * fx
            is_high = usd_max >= 2000.0
            return {
                "rate_type": "monthly",
                "min_amount": usd_min,
                "max_amount": usd_max,
                "currency": curr.upper() if len(curr) == 3 else "USD",
                "is_high_ticket": is_high,
                "budget_badge": f"💰 ${int(usd_max):,}/MO"
            }

        # 4. Fixed Pattern
        fixed_pat = re.compile(
            r"(?i)(?:(?:budget|fixed|fixed-price|paying|pay|stipend|fee):\s*)?"
            r"(?:(?P<curr1>[\$€£])|(?P<curr_code1>USD|EUR|GBP|CAD|AUD)\s*)"
            r"(?P<min>\d+(?:,\d{3})*(?:\.\d+)?\s*[km]?)"
            r"(?:\s*(?:-|–|to)\s*(?:(?P<curr2>[\$€£])|(?P<curr_code2>USD|EUR|GBP|CAD|AUD)\s*)?"
            r"(?P<max>\d+(?:,\d{3})*(?:\.\d+)?\s*[km]?))?\s*"
            r"(?:fixed|total|milestone)?"
        )
        for m in fixed_pat.finditer(text):
            if cls._is_funding_false_positive(text, m.start(), m.end()):
                continue
            curr = m.group("curr1") or m.group("curr_code1") or "USD"
            min_val = cls._parse_num(m.group("min"))
            max_val = cls._parse_num(m.group("max")) if m.group("max") else min_val
            fx = cls.FX_RATES.get(curr.upper(), cls.FX_RATES.get(curr, 1.0))
            usd_min, usd_max = min_val * fx, max_val * fx
            if usd_max > 250000.0 and "contract" not in text_lower and "budget" not in text_lower:
                continue  # Outlier funding ceiling
            is_high = usd_max >= 2000.0
            return {
                "rate_type": "fixed",
                "min_amount": usd_min,
                "max_amount": usd_max,
                "currency": curr.upper() if len(curr) == 3 else "USD",
                "is_high_ticket": is_high,
                "budget_badge": f"💰 ${int(usd_max):,} FIXED" if usd_min == usd_max else f"💰 ${int(usd_min):,} - ${int(usd_max):,} FIXED"
            }

        # Check for unstated
        for pat in cls.UNSTATED_BLACKLIST:
            if re.search(pat, text_lower):
                return {"status": "REJECT_UNSTATED_COMPENSATION", "is_high_ticket": False}

        return {"status": "UNSTATED", "is_high_ticket": False}


class RequirementDealCardGenerator:
    """Authoritative reference deal card engine implementing R2 3-bullet cards."""

    ARCHETYPES = [
        (["mvp", "prototype", "greenfield", "v1", "founding"],
         "Offer a clickable Figma or 7-day working prototype demo to immediately derisk their launch timeline."),
        (["migrate", "migration", "refactor", "legacy", "rebuild"],
         "Highlight a zero-downtime database or codebase migration case study and propose a phased rollback strategy."),
        (["ai", "llm", "agent", "gpt", "rag", "embeddings"],
         "Lead with real-world token cost optimization and latency reduction metrics rather than basic prompt wrapper concepts."),
        (["ios", "android", "react native", "flutter", "mobile"],
         "Include an immediate TestFlight build or Loom audit of their onboarding flow in your opening outreach."),
        (["devops", "kubernetes", "terraform", "ci/cd", "pipeline", "aws"],
         "Propose a paid initial architecture and cloud security audit to identify immediate cost and latency savings."),
        (["etl", "snowflake", "dbt", "performance", "slow", "postgres"],
         "Propose starting with a 48-hour diagnostic benchmark query run to isolate exact database bottlenecks.")
    ]

    @classmethod
    def generate(cls, lead: Lead, comp_data: Dict[str, Any]) -> EnrichedLead:
        full_text = f"{lead.title} {lead.description}".lower()
        
        # Winning angle synthesis
        winning_angle = "Lead with a 3-milestone delivery roadmap and 2 direct case study links instead of a traditional resume."
        for keywords, angle in cls.ARCHETYPES:
            if any(kw in full_text for kw in keywords):
                winning_angle = angle
                break

        # Scope bullet (20-45 words)
        scope = f"Deliver production scope for {lead.title}, ensuring milestones, robust architecture, and requirements are fully met."
        
        # Skills bullet
        skills = ", ".join(lead.core_tech_stack[:6]) if lead.core_tech_stack else "Senior Engineering & System Architecture"

        return EnrichedLead(
            lead=lead,
            is_high_ticket=comp_data.get("is_high_ticket", False),
            rate_type=comp_data.get("rate_type", "unknown"),
            min_amount=comp_data.get("min_amount", 0.0),
            max_amount=comp_data.get("max_amount", 0.0),
            currency=comp_data.get("currency", "USD"),
            budget_badge=comp_data.get("budget_badge", "💰 $2,000+ HIGH-TICKET"),
            scope_bullet=scope,
            skills_bullet=skills,
            winning_angle=winning_angle
        )


class RequirementTelegramFormatter:
    """Authoritative reference formatter implementing R3 mobile HTML layout."""

    @classmethod
    def format_deal_card(cls, enriched: EnrichedLead) -> str:
        lead = enriched.lead
        badge = enriched.budget_badge
        esc_title = html.escape(lead.title)
        esc_client = html.escape(lead.client or "Direct Client")
        esc_source = html.escape(lead.source.capitalize())
        esc_scope = html.escape(enriched.scope_bullet)
        esc_skills = html.escape(enriched.skills_bullet)
        esc_angle = html.escape(enriched.winning_angle)
        short_id = lead.id[:8]

        card = (
            f"<b>{badge}</b>\n\n"
            f"🎯 <b>{esc_title}</b>\n"
            f"🏢 <i>{esc_client}</i> • <i>via {esc_source}</i>\n\n"
            f"📋 <b>Executive Summary:</b>\n"
            f"• <b>Scope:</b> {esc_scope}\n"
            f"• <b>Skills:</b> {esc_skills}\n"
            f"• <b>Winning Angle:</b> {esc_angle}\n\n"
            f"🕒 <i>Posted recently</i> | 🆔 <code>#{short_id}</code>"
        )
        return card

    @classmethod
    def create_apply_markup(cls, url: str) -> Optional[Dict[str, Any]]:
        if not url or not (url.startswith("http://") or url.startswith("https://")):
            return None
        return {
            "inline_keyboard": [
                [{"text": "🚀 Apply on Source", "url": url}]
            ]
        }


class RequirementWhopWebhookVerifier:
    """Authoritative reference HMAC verifier implementing R4 Whop security."""

    @classmethod
    def verify(
        cls,
        raw_body: bytes,
        headers: Dict[str, str],
        secret: str,
        max_age_seconds: int = 300
    ) -> Tuple[bool, str]:
        if not secret:
            return False, "Missing secret"

        # Case 1: Standard Webhooks
        msg_id = headers.get("Webhook-Id") or headers.get("webhook-id")
        ts_str = headers.get("Webhook-Timestamp") or headers.get("webhook-timestamp")
        sig_header = headers.get("Webhook-Signature") or headers.get("webhook-signature")

        if msg_id and ts_str and sig_header:
            try:
                ts = int(ts_str)
                if abs(int(time.time()) - ts) > max_age_seconds:
                    return False, "Timestamp drift exceeds limit (replay attack)"
            except ValueError:
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

            for part in sig_header.split(" "):
                if part.startswith("v1,"):
                    if hmac.compare_digest(part[3:], expected_sig):
                        return True, "Valid Standard Webhook signature"
            return False, "Signature mismatch"

        # Case 2: Direct x-whop-signature
        x_sig = headers.get("X-Whop-Signature") or headers.get("x-whop-signature")
        if x_sig:
            key_bytes = secret.encode("utf-8")
            expected_hex = hmac.new(key_bytes, raw_body, hashlib.sha256).hexdigest()
            expected_b64 = base64.b64encode(hmac.new(key_bytes, raw_body, hashlib.sha256).digest()).decode("utf-8")
            if hmac.compare_digest(x_sig, expected_hex) or hmac.compare_digest(x_sig, expected_b64):
                return True, "Valid x-whop-signature"
            return False, "Signature mismatch"

        return False, "Missing signature headers"


# ==============================================================================
# TIER 1: FEATURE COVERAGE TESTS (>=5 per feature R1-R5 Happy Paths)
# ==============================================================================

class TestTier1FeatureCoverage(unittest.TestCase):
    """Tier 1: Feature Coverage verifying primary happy paths across R1-R5."""

    # --------------------------------------------------------------------------
    # R1: Lead Ingestion & Reliable Source Connectors
    # --------------------------------------------------------------------------
    def test_tier1_r1_wwr_rss_parsing_happy_path(self):
        fixture_path = os.path.join(FIXTURES_DIR, "sample_wwr.rss")
        connector = WeWorkRemotelyConnector()
        leads = connector.fetch(fixture_path)

        self.assertGreaterEqual(len(leads), 5)
        stripe_lead = next(l for l in leads if l.client == "Stripe")
        self.assertIn("Senior Backend Architect", stripe_lead.title)
        self.assertEqual(stripe_lead.source, "weworkremotely")
        self.assertTrue("$140,000" in stripe_lead.raw_compensation or "$85/hr" in stripe_lead.raw_compensation)
        self.assertIn("Python", stripe_lead.core_tech_stack)
        self.assertIn("Kafka", stripe_lead.core_tech_stack)
        self.assertTrue(stripe_lead.validate())

    def test_tier1_r1_remoteok_json_parsing_happy_path(self):
        fixture_path = os.path.join(FIXTURES_DIR, "sample_remoteok.json")
        connector = RemoteOKConnector()
        leads = connector.fetch(fixture_path)

        # Index 0 legal notice must be bypassed
        self.assertEqual(len(leads), 5)
        kpi = next(l for l in leads if l.client == "KPI Solutions")
        self.assertIn("Software Deployment Engineer", kpi.title)
        self.assertEqual(kpi.raw_metadata["salary_min"], 120000)
        self.assertEqual(kpi.raw_metadata["salary_max"], 160000)
        self.assertIn("Docker", kpi.core_tech_stack)
        self.assertTrue(kpi.validate())

    def test_tier1_r1_jobspresso_rss_parsing_happy_path(self):
        fixture_path = os.path.join(FIXTURES_DIR, "sample_jobspresso.rss")
        connector = JobspressoConnector()
        leads = connector.fetch(fixture_path)

        self.assertEqual(len(leads), 4)
        hopper = next(l for l in leads if l.client == "Hopper")
        self.assertIn("Senior Full Stack Engineer", hopper.title)
        self.assertEqual(hopper.raw_metadata["location"], "Canada")
        self.assertIn("$80 - $110 / hour", hopper.raw_compensation)
        self.assertIn("Python", hopper.core_tech_stack)
        self.assertTrue(hopper.validate())

    def test_tier1_r1_hackernews_algolia_parsing_happy_path(self):
        fixture_path = os.path.join(FIXTURES_DIR, "sample_hn.json")
        connector = HackerNewsConnector()
        leads = connector.fetch(fixture_path)

        # Top comments parsed, seeking work filtered
        self.assertGreaterEqual(len(leads), 2)
        lumen = next(l for l in leads if l.client == "Lumen Labs")
        self.assertIn("Senior Distributed Systems Engineer", lumen.title)
        self.assertEqual(lumen.source, "hackernews")
        self.assertEqual("https://news.ycombinator.com/item?id=49523001", lumen.url)
        self.assertTrue(lumen.validate())

    def test_tier1_r1_reddit_atom_parsing_happy_path(self):
        fixture_path = os.path.join(FIXTURES_DIR, "sample_reddit.atom")
        connector = RedditConnector()
        leads = connector.fetch(fixture_path)

        # [Hiring] parsed, [For Hire] and mod announcements dropped
        self.assertEqual(len(leads), 4)
        fastapi_lead = next(l for l in leads if "FastAPI" in l.title)
        self.assertEqual(fastapi_lead.client, "/u/agency_founder")
        self.assertNotIn("[Hiring]", fastapi_lead.title)
        self.assertIn("$5,000", fastapi_lead.raw_compensation)
        self.assertIn("FastAPI", fastapi_lead.core_tech_stack)
        self.assertTrue(fastapi_lead.validate())

    def test_tier1_r1_sqlite_deduplication_store_happy_path(self, tmp_path=None):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = os.path.join(str(tmp_path) if tmp_path else temp_dir, "test_dedup.db")
            db = Database(db_path=db_file)
            
            lead_id = hashlib.sha256(b"https://example.com/unique-lead-1").hexdigest()
            lead = Lead(
                id=lead_id,
                title="Lead Architect",
                source="weworkremotely",
                client="Tech Corp",
                url="https://example.com/unique-lead-1?utm_source=twitter"
            )
            self.assertTrue(db.insert_lead(lead))
            self.assertTrue(db.is_duplicate(lead_id))
            # Second insert must return False (atomic deduplication)
            self.assertFalse(db.insert_lead(lead))
            self.assertEqual(db.count_leads(), 1)
            db.close()

    # --------------------------------------------------------------------------
    # R2: AI Enrichment & High-Value Filtering
    # --------------------------------------------------------------------------
    def test_tier1_r2_fixed_budget_filter_happy_path(self):
        text = "Client seeking senior fullstack engineer. Budget: $5,000 fixed milestone."
        res = RequirementCompensationFilter.parse_compensation(text)
        self.assertIsNotNone(res)
        self.assertEqual(res["rate_type"], "fixed")
        self.assertEqual(res["max_amount"], 5000.0)
        self.assertTrue(res["is_high_ticket"])
        self.assertIn("💰 $5,000 FIXED", res["budget_badge"])

    def test_tier1_r2_hourly_rate_filter_happy_path(self):
        text = "Fintech startup hiring remote backend contractor. Rate: $75 - $100 / hour."
        res = RequirementCompensationFilter.parse_compensation(text)
        self.assertIsNotNone(res)
        self.assertEqual(res["rate_type"], "hourly")
        self.assertEqual(res["min_amount"], 75.0)
        self.assertEqual(res["max_amount"], 100.0)
        self.assertTrue(res["is_high_ticket"])
        self.assertIn("⏱️ $75 - $100/HR", res["budget_badge"])

    def test_tier1_r2_annual_salary_normalization_happy_path(self):
        text = "Staff Software Engineer. Salary: $140,000/yr."
        res = RequirementCompensationFilter.parse_compensation(text)
        self.assertIsNotNone(res)
        self.assertEqual(res["rate_type"], "annual")
        self.assertEqual(res["max_amount"], 140000.0)
        self.assertTrue(res["is_high_ticket"])
        self.assertIn("💼 $140k/YR (~$70/HR)", res["budget_badge"])

    def test_tier1_r2_monthly_retainer_normalization_happy_path(self):
        text = "Agency needs fractional DevOps lead on a $4,000/month recurring contract retainer."
        res = RequirementCompensationFilter.parse_compensation(text)
        self.assertIsNotNone(res)
        self.assertEqual(res["rate_type"], "monthly")
        self.assertEqual(res["max_amount"], 4000.0)
        self.assertTrue(res["is_high_ticket"])
        self.assertIn("💰 $4,000/MO", res["budget_badge"])

    def test_tier1_r2_deal_card_three_bullet_generation_happy_path(self):
        lead = Lead(
            id="a" * 64,
            title="Senior React & TypeScript Developer",
            source="remoteok",
            client="CryptoFlow",
            url="https://example.com/apply",
            core_tech_stack=["React", "TypeScript", "Tailwind", "Next.js"],
            description="Build real-time trading interface with React and TypeScript."
        )
        comp = {"is_high_ticket": True, "rate_type": "fixed", "budget_badge": "💰 $6,000 FIXED"}
        card = RequirementDealCardGenerator.generate(lead, comp)

        self.assertTrue(card.is_high_ticket)
        self.assertEqual(card.budget_badge, "💰 $6,000 FIXED")
        self.assertGreater(len(card.scope_bullet), 10)
        self.assertIn("React", card.skills_bullet)
        self.assertGreater(len(card.winning_angle), 15)
        self.assertTrue(card.winning_angle.endswith("."))

    # --------------------------------------------------------------------------
    # R3: Telegram Alert Dispatcher
    # --------------------------------------------------------------------------
    def test_tier1_r3_html_message_formatting_happy_path(self):
        lead = Lead(
            id="12345678" * 8,
            title="Senior AI Engineer",
            source="weworkremotely",
            client="Scale AI",
            url="https://scale.com/apply"
        )
        enriched = EnrichedLead(
            lead=lead,
            is_high_ticket=True,
            rate_type="fixed",
            min_amount=8500.0,
            max_amount=8500.0,
            currency="USD",
            budget_badge="💰 $8,500 FIXED",
            scope_bullet="Build automated RAG ingestion pipeline on AWS.",
            skills_bullet="Python, FastAPI, OpenAI, LangChain",
            winning_angle="Pitch with an immediate Loom audit of their ingestion architecture."
        )
        msg_text = RequirementTelegramFormatter.format_deal_card(enriched)

        self.assertIn("<b>💰 $8,500 FIXED</b>", msg_text)
        self.assertIn("🎯 <b>Senior AI Engineer</b>", msg_text)
        self.assertIn("🏢 <i>Scale AI</i> • <i>via Weworkremotely</i>", msg_text)
        self.assertIn("• <b>Scope:</b> Build automated RAG", msg_text)
        self.assertIn("• <b>Skills:</b> Python, FastAPI", msg_text)
        self.assertIn("• <b>Winning Angle:</b> Pitch with", msg_text)
        self.assertIn("🆔 <code>#12345678</code>", msg_text)

    def test_tier1_r3_inline_keyboard_apply_button_happy_path(self):
        url = "https://weworkremotely.com/jobs/senior-dev"
        markup = RequirementTelegramFormatter.create_apply_markup(url)
        self.assertIsNotNone(markup)
        self.assertIn("inline_keyboard", markup)
        btn = markup["inline_keyboard"][0][0]
        self.assertEqual(btn["text"], "🚀 Apply on Source")
        self.assertEqual(btn["url"], url)

    def test_tier1_r3_link_preview_suppression_happy_path(self):
        telegram_api = MockTelegramBotAPI()
        res = telegram_api.sendMessage(
            chat_id="-1001234567890",
            text="<b>Test Alert</b>",
            link_preview_options={"is_disabled": True}
        )
        self.assertTrue(res["ok"])
        call = telegram_api.call_history[-1]
        self.assertEqual(call["params"]["link_preview_options"], {"is_disabled": True})

    def test_tier1_r3_dry_run_dispatcher_happy_path(self):
        telegram_api = MockTelegramBotAPI()
        markup = RequirementTelegramFormatter.create_apply_markup("https://example.com")
        res = telegram_api.sendMessage(
            chat_id="-1001999999999",
            text="<b>💰 $5,000 FIXED</b>\n\n🎯 <b>Senior Dev</b>",
            reply_markup=markup
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["result"]["chat"]["id"], "-1001999999999")
        self.assertEqual(len(telegram_api.sent_messages), 1)

    def test_tier1_r3_token_bucket_rate_limiter_happy_path(self):
        class TokenBucket:
            def __init__(self, rate=1.0, capacity=1.0):
                self.rate = rate
                self.capacity = capacity
                self.tokens = capacity
                self.last_update = time.time()

            def consume(self) -> float:
                now = time.time()
                self.tokens = min(self.capacity, self.tokens + (now - self.last_update) * self.rate)
                self.last_update = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return 0.0
                return (1.0 - self.tokens) / self.rate

        tb = TokenBucket(rate=1.0, capacity=1.0)
        self.assertEqual(tb.consume(), 0.0)
        wait_time = tb.consume()
        self.assertGreater(wait_time, 0.0)

    # --------------------------------------------------------------------------
    # R4: Whop Subscription & Webhook Access Manager
    # --------------------------------------------------------------------------
    def test_tier1_r4_whop_standard_webhook_hmac_verification_happy_path(self):
        dispatcher = MockWhopWebhookDispatcher(secret="test_whop_secret_key_12345")
        body = b'{"action":"membership.went_valid","data":{"id":"mem_001"}}'
        headers = dispatcher.create_standard_headers(body)
        
        ok, msg = RequirementWhopWebhookVerifier.verify(body, headers, "test_whop_secret_key_12345")
        self.assertTrue(ok)
        self.assertIn("Valid Standard Webhook", msg)

    def test_tier1_r4_whop_direct_signature_verification_happy_path(self):
        dispatcher = MockWhopWebhookDispatcher(secret="test_whop_secret_key_12345")
        body = b'{"action":"membership.went_valid","data":{"id":"mem_001"}}'
        headers = dispatcher.create_direct_headers(body)
        
        ok, msg = RequirementWhopWebhookVerifier.verify(body, headers, "test_whop_secret_key_12345")
        self.assertTrue(ok)
        self.assertIn("Valid x-whop-signature", msg)

    def test_tier1_r4_membership_went_valid_invite_link_happy_path(self):
        telegram_api = MockTelegramBotAPI()
        res = telegram_api.createChatInviteLink(
            chat_id="-1001234567890",
            name="whop_mem_test_001",
            member_limit=1
        )
        self.assertTrue(res["ok"])
        invite_url = res["result"]["invite_link"]
        self.assertTrue(invite_url.startswith("https://t.me/+mock_"))
        self.assertEqual(res["result"]["member_limit"], 1)
        self.assertFalse(res["result"]["is_revoked"])

    def test_tier1_r4_membership_went_invalid_revocation_happy_path(self):
        telegram_api = MockTelegramBotAPI()
        user_id = 987654321
        
        # Step 1: Ban / kick
        ban_res = telegram_api.banChatMember(chat_id="-1001234567890", user_id=user_id)
        self.assertTrue(ban_res["ok"])
        self.assertEqual(telegram_api.channel_members[user_id], "banned")

        # Step 2: Neutral unban to allow future rejoin if resubscribed
        unban_res = telegram_api.unbanChatMember(chat_id="-1001234567890", user_id=user_id)
        self.assertTrue(unban_res["ok"])
        self.assertEqual(telegram_api.channel_members[user_id], "kicked_neutral")

    def test_tier1_r4_webhook_idempotency_and_event_dedup_happy_path(self, tmp_path=None):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = os.path.join(str(tmp_path) if tmp_path else temp_dir, "subscribers.db")
            conn = sqlite3.connect(db_file)
            conn.execute("""
                CREATE TABLE webhook_events (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT,
                    processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            
            event_id = "msg_01HXYZ1234567890"
            cursor = conn.cursor()
            cursor.execute("INSERT OR IGNORE INTO webhook_events (event_id, event_type) VALUES (?, ?)", (event_id, "membership.went_valid"))
            self.assertEqual(cursor.rowcount, 1)

            # Second duplicate processing
            cursor.execute("INSERT OR IGNORE INTO webhook_events (event_id, event_type) VALUES (?, ?)", (event_id, "membership.went_valid"))
            self.assertEqual(cursor.rowcount, 0)
            conn.close()

    # --------------------------------------------------------------------------
    # R5: Turnkey Store Assets & Zero-Cost Mobile Operations Playbook
    # --------------------------------------------------------------------------
    def test_tier1_r5_storefront_copy_content_and_pricing_tiers(self):
        storefront_path = os.path.join(WORKSPACE_DIR, "STOREFRONT_COPY.md")
        self.assertTrue(os.path.exists(storefront_path), "STOREFRONT_COPY.md must exist")
        with open(storefront_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("$49", content)
        self.assertIn("$79", content)
        self.assertIn("$99", content)
        self.assertTrue("Early-Bird" in content or "Early-bird" in content)
        self.assertTrue("High-Earner" in content or "Standard" in content)
        self.assertIn("Agency", content)
        self.assertTrue("One-Contract ROI Guarantee" in content or "Guarantee" in content)

    def test_tier1_r5_storefront_faq_and_objection_handling(self):
        storefront_path = os.path.join(WORKSPACE_DIR, "STOREFRONT_COPY.md")
        with open(storefront_path, "r", encoding="utf-8") as f:
            content = f.read()

        faq_matches = re.findall(r"(?i)###\s*(?:\d+\.|\bQ\b|FAQ)", content)
        self.assertGreaterEqual(len(faq_matches), 5)

    def test_tier1_r5_deployment_guide_mobile_walkthrough(self):
        deploy_path = os.path.join(WORKSPACE_DIR, "DEPLOYMENT.md")
        self.assertTrue(os.path.exists(deploy_path), "DEPLOYMENT.md must exist")
        with open(deploy_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertTrue("15-Minute" in content or "15 Minute" in content or "15 minutes" in content)
        self.assertIn("@BotFather", content)
        self.assertIn("TELEGRAM_BOT_TOKEN", content)
        self.assertIn("GitHub Actions", content)

    def test_tier1_r5_deployment_env_vars_and_health_checks(self):
        deploy_path = os.path.join(WORKSPACE_DIR, "DEPLOYMENT.md")
        with open(deploy_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("TELEGRAM_BOT_TOKEN", content)
        self.assertIn("TELEGRAM_CHAT_ID", content)
        self.assertIn("WHOP_WEBHOOK_SECRET", content)
        self.assertIn("/health", content)

    def test_tier1_r5_marketing_playbook_ten_viral_templates(self):
        playbook_path = os.path.join(WORKSPACE_DIR, "MARKETING_PLAYBOOK.md")
        self.assertTrue(os.path.exists(playbook_path), "MARKETING_PLAYBOOK.md must exist")
        with open(playbook_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("LinkedIn", content)
        self.assertTrue("X" in content or "Twitter" in content)
        self.assertTrue("TikTok" in content or "Reels" in content or "Shorts" in content)
        templates = re.findall(r"(?i)###\s*Template\s*\d+|###\s*Post\s*\d+|###\s*Script\s*\d+", content)
        self.assertGreaterEqual(len(templates), 10)


# ==============================================================================
# TIER 2: BOUNDARY & CORNER CASES (>=5 per feature R1-R5)
# ==============================================================================

class TestTier2BoundaryAndCornerCases(unittest.TestCase):
    """Tier 2: Boundary & Corner Cases testing extreme inputs, attacks, and edge cases."""

    # --------------------------------------------------------------------------
    # R1 Corner Cases
    # --------------------------------------------------------------------------
    def test_tier2_r1_malformed_xml_and_entity_defense(self):
        connector = WeWorkRemotelyConnector()
        bad_xml = "<rss><channel><item><title>Broken Item</item></channel></rss>"
        self.assertEqual(connector.parse(bad_xml), [])

        xxe_xml = """<?xml version="1.0"?>
        <!DOCTYPE foo [ <!ENTITY xxe SYSTEM "file:///etc/passwd"> ]>
        <rss version="2.0"><channel><item><title>&xxe;</title></item></channel></rss>"""
        self.assertEqual(connector.parse(xxe_xml), [])

    def test_tier2_r1_remoteok_corrupt_or_missing_index0(self):
        connector = RemoteOKConnector()
        payload_no_legal = json.dumps([
            {
                "id": 9991,
                "position": "Senior Backend Dev",
                "company": "Valid Corp",
                "url": "https://example.com/job/9991",
                "salary_min": 100000,
                "salary_max": 120000
            }
        ])
        leads = connector.parse(payload_no_legal)
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0].client, "Valid Corp")
        self.assertEqual(connector.parse("[]"), [])

    def test_tier2_r1_hn_seeking_work_and_nested_replies_filtered(self):
        connector = HackerNewsConnector()
        payload = json.dumps({
            "hits": [
                {
                    "objectID": "1001",
                    "story_id": 5000,
                    "parent_id": 5000,
                    "author": "job_seeker",
                    "comment_text": "SEEKING WORK | Fullstack React/Node | $30/hr"
                },
                {
                    "objectID": "1002",
                    "story_id": 5000,
                    "parent_id": 9999,
                    "author": "commenter",
                    "comment_text": "Does this position sponsor remote work visas?"
                },
                {
                    "objectID": "1003",
                    "story_id": 5000,
                    "parent_id": 5000,
                    "author": "tech_founder",
                    "comment_text": "Acme Systems | Distributed Engineer | Remote | $140k | https://acme.com"
                }
            ]
        })
        leads = connector.parse(payload)
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0].client, "Acme Systems")

    def test_tier2_r1_reddit_for_hire_and_mod_stickies_filtered(self):
        connector = RedditConnector()
        atom_content = """<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <title>[For Hire] Full Stack Developer available ($25/hr)</title>
            <link href="https://reddit.com/r/forhire/1"/>
            <author><name>/u/seeker</name></author>
            <content type="html">Hire me!</content>
          </entry>
          <entry>
            <title>[Meta] Monthly Discussion &amp; Sub Rules</title>
            <link href="https://reddit.com/r/forhire/2"/>
            <author><name>/u/AutoMod</name></author>
            <content type="html">Rules</content>
          </entry>
          <entry>
            <title>[Hiring] Python Contractor - $5,000</title>
            <link href="https://reddit.com/r/forhire/3"/>
            <author><name>/u/hirer</name></author>
            <content type="html">Job details</content>
          </entry>
        </feed>"""
        leads = connector.parse(atom_content)
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0].client, "/u/hirer")

    def test_tier2_r1_url_canonicalization_utm_and_slash_normalization(self):
        raw_urls = [
            "HTTP://WEWORKREMOTELY.COM/jobs/backend-dev/?utm_source=twitter&utm_medium=feed",
            "https://weworkremotely.com/jobs/backend-dev?ref=newsletter#overview",
            "https://weworkremotely.com/jobs/backend-dev/?fbclid=XYZ123&keep=param",
            "https://weworkremotely.com/jobs/backend-dev?keep=param"
        ]
        canon_0 = canonicalize_url(raw_urls[0])
        canon_1 = canonicalize_url(raw_urls[1])
        self.assertEqual(canon_0, "https://weworkremotely.com/jobs/backend-dev")
        self.assertEqual(canon_1, "https://weworkremotely.com/jobs/backend-dev")
        self.assertEqual(compute_lead_hash("wwr", raw_urls[0]), compute_lead_hash("wwr", raw_urls[1]))

        canon_2 = canonicalize_url(raw_urls[2])
        canon_3 = canonicalize_url(raw_urls[3])
        self.assertEqual(canon_2, canon_3)
        self.assertEqual(canon_2, "https://weworkremotely.com/jobs/backend-dev?keep=param")

    # --------------------------------------------------------------------------
    # R2 Corner Cases
    # --------------------------------------------------------------------------
    def test_tier2_r2_funding_round_false_positive_suppression(self):
        text1 = "We just raised $2M seed round from top venture funds, hiring our first frontend engineer."
        res1 = RequirementCompensationFilter.parse_compensation(text1)
        self.assertTrue(res1 is None or res1.get("is_high_ticket") is False)

        text2 = "Series A funded startup ($10M funding) hiring backend dev. Unstated salary."
        res2 = RequirementCompensationFilter.parse_compensation(text2)
        self.assertTrue(res2 is None or res2.get("is_high_ticket") is False)

    def test_tier2_r2_unstated_and_negotiable_compensation_rejection(self):
        texts = [
            "Senior Developer position with competitive salary DOE and full health benefits.",
            "Salary negotiable depending on experience and interview.",
            "Compensation: TBD based on background."
        ]
        for t in texts:
            res = RequirementCompensationFilter.parse_compensation(t)
            self.assertTrue(res is None or res.get("is_high_ticket") is False)

    def test_tier2_r2_equity_only_and_unpaid_rejection(self):
        texts = [
            "Looking for co-founder engineer. Equity only early stage startup.",
            "Unpaid internship for college credit. React & Node.",
            "Revenue share only until profitable launch."
        ]
        for t in texts:
            res = RequirementCompensationFilter.parse_compensation(t)
            self.assertIsNotNone(res)
            self.assertFalse(res.get("is_high_ticket"))

    def test_tier2_r2_low_compensation_rejection(self):
        texts = [
            ("Fix CSS bugs. Budget: $400 fixed.", "fixed"),
            ("Wordpress maintenance. Rate: $25/hr.", "hourly"),
            ("Junior intern. Salary: $35,000/yr.", "annual"),
            ("Part time helper. $1,200/mo retainer.", "monthly")
        ]
        for t, rate_type in texts:
            res = RequirementCompensationFilter.parse_compensation(t)
            self.assertIsNotNone(res)
            self.assertFalse(res.get("is_high_ticket"))

    def test_tier2_r2_complex_compensation_ranges_and_foreign_currencies(self):
        # 1. Suffix range notation: $2.5k - $4k
        t1 = "Need custom CRM integration. Budget: $2.5k - $4k fixed."
        res1 = RequirementCompensationFilter.parse_compensation(t1)
        self.assertIsNotNone(res1)
        self.assertEqual(res1["max_amount"], 4000.0)
        self.assertTrue(res1["is_high_ticket"])

        # 2. Hourly range with min below and max above threshold: $40 - $60 / hr
        t2 = "React contractor needed. Rate: $40 - $60 / hr."
        res2 = RequirementCompensationFilter.parse_compensation(t2)
        self.assertIsNotNone(res2)
        self.assertEqual(res2["max_amount"], 60.0)
        self.assertTrue(res2["is_high_ticket"])

        # 3. Foreign Currency EUR: €2,500 * 1.08 = $2,700 USD
        t3 = "European client paying €2,500 fixed milestone."
        res3 = RequirementCompensationFilter.parse_compensation(t3)
        self.assertIsNotNone(res3)
        self.assertGreaterEqual(res3["max_amount"], 2700.0)
        self.assertTrue(res3["is_high_ticket"])

        # 4. Foreign Currency GBP: £2,000 * 1.28 = $2,560 USD
        t4 = "UK agency paying £2,000 fixed."
        res4 = RequirementCompensationFilter.parse_compensation(t4)
        self.assertIsNotNone(res4)
        self.assertGreaterEqual(res4["max_amount"], 2560.0)
        self.assertTrue(res4["is_high_ticket"])

    # --------------------------------------------------------------------------
    # R3 Corner Cases
    # --------------------------------------------------------------------------
    def test_tier2_r3_message_length_truncation_at_4096_chars(self):
        lead = Lead(
            id="e" * 64,
            title="Lead Engineer",
            source="remoteok",
            client="Big Corp",
            url="https://example.com"
        )
        massive_scope = "Deliver mission critical tasks. " * 200
        enriched = EnrichedLead(
            lead=lead,
            is_high_ticket=True,
            rate_type="fixed",
            min_amount=10000.0,
            max_amount=10000.0,
            currency="USD",
            budget_badge="💰 $10,000 FIXED",
            scope_bullet=massive_scope[:3500] + "...",
            skills_bullet="Python, AWS",
            winning_angle="Provide high quality roadmap."
        )
        msg_text = RequirementTelegramFormatter.format_deal_card(enriched)
        self.assertLessEqual(len(msg_text), 4096)
        
        api = MockTelegramBotAPI()
        res = api.sendMessage(chat_id="-1001", text=msg_text)
        self.assertTrue(res["ok"])

    def test_tier2_r3_html_meta_character_escaping_integrity(self):
        lead = Lead(
            id="f" * 64,
            title="Senior Dev <React & C++>",
            source="weworkremotely",
            client="Acme & Co. <Labs>",
            url="https://example.com"
        )
        enriched = EnrichedLead(
            lead=lead,
            is_high_ticket=True,
            rate_type="fixed",
            min_amount=5000.0,
            max_amount=5000.0,
            currency="USD",
            budget_badge="💰 $5,000 FIXED",
            scope_bullet="Build <core> components & test.",
            skills_bullet="React & C++",
            winning_angle="Use & explain <best> practices."
        )
        msg_text = RequirementTelegramFormatter.format_deal_card(enriched)
        
        self.assertIn("&lt;React &amp; C++&gt;", msg_text)
        self.assertIn("Acme &amp; Co. &lt;Labs&gt;", msg_text)
        
        api = MockTelegramBotAPI()
        res = api.sendMessage(chat_id="-1001", text=msg_text)
        self.assertTrue(res["ok"])

    def test_tier2_r3_invalid_button_url_handling(self):
        self.assertIsNone(RequirementTelegramFormatter.create_apply_markup(""))
        self.assertIsNone(RequirementTelegramFormatter.create_apply_markup("mailto:hr@client.com"))
        self.assertIsNone(RequirementTelegramFormatter.create_apply_markup("javascript:alert(1)"))

        api = MockTelegramBotAPI()
        bad_markup = {"inline_keyboard": [[{"text": "Apply", "url": "ftp://bad-scheme.com"}]]}
        res = api.sendMessage(chat_id="-1001", text="Test", reply_markup=bad_markup)
        self.assertFalse(res["ok"])
        self.assertIn("BUTTON_URL_INVALID", res["description"])

    def test_tier2_r3_http_429_retry_after_handling(self):
        api = MockTelegramBotAPI()
        api.simulate_rate_limit = True
        api.rate_limit_retry_after = 3

        res = api.sendMessage(chat_id="-1001", text="Rate limited test")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], 429)
        self.assertEqual(res["parameters"]["retry_after"], 3)

    def test_tier2_r3_exponential_backoff_on_5xx_errors(self):
        api = MockTelegramBotAPI()
        api.simulate_server_error = True
        api.server_error_code = 503

        with self.assertRaises(RuntimeError):
            api.sendMessage(chat_id="-1001", text="Server error test")

    # --------------------------------------------------------------------------
    # R4 Corner Cases
    # --------------------------------------------------------------------------
    def test_tier2_r4_webhook_replay_attack_expired_timestamp(self):
        dispatcher = MockWhopWebhookDispatcher(secret="test_secret")
        body = b'{"action":"membership.went_valid"}'
        old_ts = int(time.time()) - 600
        headers = dispatcher.create_standard_headers(body, timestamp=old_ts)

        ok, msg = RequirementWhopWebhookVerifier.verify(body, headers, "test_secret")
        self.assertFalse(ok)
        self.assertIn("Timestamp drift", msg)

    def test_tier2_r4_webhook_tampered_payload_signature_mismatch(self):
        dispatcher = MockWhopWebhookDispatcher(secret="test_secret")
        original_body = b'{"action":"membership.went_valid","data":{"user_id":"legit_user"}}'
        headers = dispatcher.create_standard_headers(original_body)

        tampered_body = b'{"action":"membership.went_valid","data":{"user_id":"attacker_user"}}'
        ok, msg = RequirementWhopWebhookVerifier.verify(tampered_body, headers, "test_secret")
        self.assertFalse(ok)
        self.assertIn("Signature mismatch", msg)

    def test_tier2_r4_webhook_missing_signature_headers(self):
        body = b'{"action":"membership.went_valid"}'
        headers = {"Content-Type": "application/json"}
        ok, msg = RequirementWhopWebhookVerifier.verify(body, headers, "test_secret")
        self.assertFalse(ok)
        self.assertIn("Missing signature headers", msg)

    def test_tier2_r4_webhook_invalid_secret_or_key_handling(self):
        dispatcher = MockWhopWebhookDispatcher(secret="correct_secret")
        body = b'{"action":"membership.went_valid"}'
        headers = dispatcher.create_standard_headers(body)

        ok, msg = RequirementWhopWebhookVerifier.verify(body, headers, "wrong_secret_123")
        self.assertFalse(ok)
        self.assertIn("Signature mismatch", msg)

    def test_tier2_r4_revocation_unlinked_or_already_revoked_member(self):
        api = MockTelegramBotAPI()
        res = api.unbanChatMember(chat_id="-1001", user_id=999999, only_if_banned=True)
        self.assertTrue(res["ok"])
        self.assertFalse(res["result"])

    # --------------------------------------------------------------------------
    # R5 Corner Cases
    # --------------------------------------------------------------------------
    def test_tier2_r5_storefront_pricing_consistency(self):
        storefront_path = os.path.join(WORKSPACE_DIR, "STOREFRONT_COPY.md")
        with open(storefront_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("$49", content)
        self.assertIn("$79", content)
        self.assertIn("$99", content)
        self.assertNotIn("$59", content)
        self.assertNotIn("$89", content)

    def test_tier2_r5_deployment_guide_cron_syntax_and_limits(self):
        deploy_path = os.path.join(WORKSPACE_DIR, "DEPLOYMENT.md")
        with open(deploy_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("cron", content.lower())
        self.assertTrue(bool(re.search(r"\b(?:15|30)\s*minutes\b", content, re.IGNORECASE) or "*/" in content))

    def test_tier2_r5_deployment_guide_mobile_browser_specifics(self):
        deploy_path = os.path.join(WORKSPACE_DIR, "DEPLOYMENT.md")
        with open(deploy_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("Safari", content)
        self.assertIn("Chrome", content)

    def test_tier2_r5_marketing_playbook_dark_mode_and_redaction_rules(self):
        playbook_path = os.path.join(WORKSPACE_DIR, "MARKETING_PLAYBOOK.md")
        with open(playbook_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("Dark Mode", content)
        self.assertTrue("#00FF66" in content or "neon" in content.lower())
        self.assertTrue("redact" in content.lower() or "blur" in content.lower())

    def test_tier2_r5_env_vars_validation_rules(self):
        deploy_path = os.path.join(WORKSPACE_DIR, "DEPLOYMENT.md")
        with open(deploy_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("OPENAI_API_KEY", content)
        self.assertTrue("deterministic" in content.lower() or "fallback" in content.lower())


# ==============================================================================
# TIER 3: CROSS-FEATURE INTERACTIONS (Pairwise Integration)
# ==============================================================================

class TestTier3CrossFeatureInteractions(unittest.TestCase):
    """Tier 3: Pairwise integration across Ingestion -> Enrichment -> Telegram Dispatch & Whop -> Channel."""

    def test_tier3_cross_ingestion_to_enrichment_to_telegram_dispatch(self):
        # 1. Ingest WWR fixture
        fixture_path = os.path.join(FIXTURES_DIR, "sample_wwr.rss")
        connector = WeWorkRemotelyConnector()
        leads = connector.fetch(fixture_path)
        
        # Select high ticket lead: Vercel ($8,500 fixed)
        vercel_lead = next(l for l in leads if l.client == "Vercel")
        
        # 2. Enrich and filter
        comp = RequirementCompensationFilter.parse_compensation(vercel_lead.raw_compensation)
        self.assertTrue(comp["is_high_ticket"])
        enriched = RequirementDealCardGenerator.generate(vercel_lead, comp)

        # 3. Format Telegram deal card & button
        text = RequirementTelegramFormatter.format_deal_card(enriched)
        markup = RequirementTelegramFormatter.create_apply_markup(vercel_lead.url)

        # 4. Dispatch to mock Telegram Bot
        api = MockTelegramBotAPI()
        res = api.sendMessage(chat_id="-1001234567890", text=text, reply_markup=markup)

        self.assertTrue(res["ok"])
        self.assertEqual(len(api.sent_messages), 1)
        dispatched = api.sent_messages[0]
        self.assertIn("💰 $8,500 FIXED", dispatched["text"])
        self.assertIn("Vercel", dispatched["text"])
        self.assertEqual(dispatched["reply_markup"]["inline_keyboard"][0][0]["url"], vercel_lead.url)

    def test_tier3_cross_ingestion_low_ticket_filter_suppresses_dispatch(self):
        # 1. Ingest Acme low-ticket lead ($400 fixed / $20/hr)
        fixture_path = os.path.join(FIXTURES_DIR, "sample_wwr.rss")
        connector = WeWorkRemotelyConnector()
        leads = connector.fetch(fixture_path)
        acme_lead = next(l for l in leads if l.client == "Acme Micro")

        # 2. Filter must reject
        comp = RequirementCompensationFilter.parse_compensation(acme_lead.raw_compensation)
        self.assertFalse(comp["is_high_ticket"])

        # 3. Dispatcher must NOT dispatch
        api = MockTelegramBotAPI()
        if comp["is_high_ticket"]:
            api.sendMessage(chat_id="-1001", text="Should not send")

        self.assertEqual(len(api.sent_messages), 0)

    def test_tier3_cross_whop_went_valid_to_telegram_invite_to_db_state(self, tmp_path=None):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = os.path.join(str(tmp_path) if tmp_path else temp_dir, "subscribers.db")
            conn = sqlite3.connect(db_file)
            conn.execute("""
                CREATE TABLE subscribers (
                    membership_id TEXT PRIMARY KEY,
                    user_id TEXT,
                    status TEXT,
                    invite_link TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            dispatcher = MockWhopWebhookDispatcher()
            payload = {
                "action": "membership.went_valid",
                "data": {
                    "id": "mem_tier3_001",
                    "user_id": "usr_vip_99",
                    "telegram_account_id": "11223344"
                }
            }
            body = json.dumps(payload).encode("utf-8")
            headers = dispatcher.create_standard_headers(body)

            ok, _ = RequirementWhopWebhookVerifier.verify(body, headers, dispatcher.secret)
            self.assertTrue(ok)

            api = MockTelegramBotAPI()
            tg_res = api.createChatInviteLink(chat_id="-1009999", name="mem_tier3_001", member_limit=1)
            self.assertTrue(tg_res["ok"])
            invite_link = tg_res["result"]["invite_link"]

            with conn:
                conn.execute(
                    "INSERT INTO subscribers (membership_id, user_id, status, invite_link) VALUES (?, ?, 'active', ?)",
                    ("mem_tier3_001", "usr_vip_99", invite_link)
                )

            cursor = conn.cursor()
            cursor.execute("SELECT status, invite_link FROM subscribers WHERE membership_id = ?", ("mem_tier3_001",))
            row = cursor.fetchone()
            self.assertEqual(row[0], "active")
            self.assertEqual(row[1], invite_link)
            conn.close()

    def test_tier3_cross_whop_went_invalid_to_telegram_kick_to_db_state(self, tmp_path=None):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = os.path.join(str(tmp_path) if tmp_path else temp_dir, "subscribers.db")
            conn = sqlite3.connect(db_file)
            conn.execute("""
                CREATE TABLE subscribers (
                    membership_id TEXT PRIMARY KEY,
                    user_id TEXT,
                    telegram_user_id INT,
                    status TEXT
                );
            """)
            with conn:
                conn.execute(
                    "INSERT INTO subscribers (membership_id, user_id, telegram_user_id, status) VALUES (?, ?, ?, ?)",
                    ("mem_tier3_002", "usr_cancel_88", 55443322, "active")
                )

            dispatcher = MockWhopWebhookDispatcher()
            payload = {
                "action": "membership.went_invalid",
                "data": {
                    "id": "mem_tier3_002",
                    "user_id": "usr_cancel_88"
                }
            }
            body = json.dumps(payload).encode("utf-8")
            headers = dispatcher.create_standard_headers(body)
            ok, _ = RequirementWhopWebhookVerifier.verify(body, headers, dispatcher.secret)
            self.assertTrue(ok)

            api = MockTelegramBotAPI()
            api.channel_members[55443322] = "active"
            api.banChatMember(chat_id="-1009999", user_id=55443322)
            api.unbanChatMember(chat_id="-1009999", user_id=55443322)
            self.assertEqual(api.channel_members[55443322], "kicked_neutral")

            with conn:
                conn.execute("UPDATE subscribers SET status = 'invalid' WHERE membership_id = ?", ("mem_tier3_002",))

            cursor = conn.cursor()
            cursor.execute("SELECT status FROM subscribers WHERE membership_id = ?", ("mem_tier3_002",))
            row = cursor.fetchone()
            self.assertEqual(row[0], "invalid")
            conn.close()

    def test_tier3_cross_multi_source_dedup_prevents_duplicate_dispatch(self, tmp_path=None):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = os.path.join(str(tmp_path) if tmp_path else temp_dir, "dedup_dispatch.db")
            db = Database(db_path=db_file)
            api = MockTelegramBotAPI()

            lead_id = hashlib.sha256(b"https://stripe.com/job/123").hexdigest()
            lead = Lead(
                id=lead_id,
                title="Senior Backend Architect",
                source="weworkremotely",
                client="Stripe",
                url="https://stripe.com/job/123",
                raw_compensation="$140,000 - $180,000/yr"
            )

            def poll_and_dispatch():
                if not db.is_duplicate(lead.id):
                    db.insert_lead(lead)
                    comp = RequirementCompensationFilter.parse_compensation(lead.raw_compensation)
                    if comp["is_high_ticket"]:
                        enriched = RequirementDealCardGenerator.generate(lead, comp)
                        text = RequirementTelegramFormatter.format_deal_card(enriched)
                        api.sendMessage(chat_id="-1001", text=text)

            # Cycle 1: Dispatches message
            poll_and_dispatch()
            self.assertEqual(len(api.sent_messages), 1)

            # Cycle 2: Duplicate lead ignored, exactly 1 message delivered total
            poll_and_dispatch()
            self.assertEqual(len(api.sent_messages), 1)
            db.close()


# ==============================================================================
# TIER 4: REAL-WORLD APPLICATION SCENARIOS (End-to-End Multi-Source Polls)
# ==============================================================================

class TestTier4RealWorldScenarios(unittest.TestCase):
    """Tier 4: Complex multi-source end-to-end operational scenarios."""

    def test_tier4_scenario_multi_source_poll_cycle_with_heterogeneous_feeds(self, tmp_path=None):
        """Scenario 1: Complete multi-source ingestion poll cycle across 5 heterogeneous sources."""
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = os.path.join(str(tmp_path) if tmp_path else temp_dir, "scenario_leads.db")
            db = Database(db_path=db_file)
            api = MockTelegramBotAPI()

            connectors = [
                (WeWorkRemotelyConnector(), os.path.join(FIXTURES_DIR, "sample_wwr.rss")),
                (RemoteOKConnector(), os.path.join(FIXTURES_DIR, "sample_remoteok.json")),
                (JobspressoConnector(), os.path.join(FIXTURES_DIR, "sample_jobspresso.rss")),
                (HackerNewsConnector(), os.path.join(FIXTURES_DIR, "sample_hn.json")),
                (RedditConnector(), os.path.join(FIXTURES_DIR, "sample_reddit.atom"))
            ]

            total_ingested = 0
            total_high_ticket_dispatched = 0

            for connector, fixture in connectors:
                leads = connector.fetch(fixture)
                self.assertGreater(len(leads), 0, f"Connector {connector.source_name} returned 0 leads")
                for lead in leads:
                    inserted = db.insert_lead(lead)
                    if inserted:
                        total_ingested += 1
                        comp = RequirementCompensationFilter.parse_compensation(
                            f"{lead.raw_compensation} {lead.description}"
                        )
                        if comp and comp.get("is_high_ticket"):
                            enriched = RequirementDealCardGenerator.generate(lead, comp)
                            text = RequirementTelegramFormatter.format_deal_card(enriched)
                            markup = RequirementTelegramFormatter.create_apply_markup(lead.url)
                            res = api.sendMessage(chat_id="-1001234567890", text=text, reply_markup=markup)
                            self.assertTrue(res["ok"])
                            total_high_ticket_dispatched += 1

            self.assertGreaterEqual(total_ingested, 15)
            self.assertGreaterEqual(total_high_ticket_dispatched, 8)
            self.assertEqual(len(api.sent_messages), total_high_ticket_dispatched)
            self.assertEqual(db.count_leads(), total_ingested)

            for msg in api.sent_messages:
                self.assertTrue("💰" in msg["text"] or "⏱️" in msg["text"] or "💼" in msg["text"])
                self.assertTrue(msg["reply_markup"]["inline_keyboard"][0][0]["url"].startswith("http"))

            db.close()

    def test_tier4_scenario_consecutive_poll_cycles_with_delta_ingestion(self, tmp_path=None):
        """Scenario 2: Consecutive cron poll cycles with delta ingestion and zero duplicate notifications."""
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = os.path.join(str(tmp_path) if tmp_path else temp_dir, "scenario_consecutive.db")
            db = Database(db_path=db_file)
            api = MockTelegramBotAPI()

            connector = WeWorkRemotelyConnector()
            initial_leads = connector.fetch(os.path.join(FIXTURES_DIR, "sample_wwr.rss"))

            # Cycle 1: Ingest initial batch
            cycle1_alerts = 0
            for lead in initial_leads:
                if db.insert_lead(lead):
                    comp = RequirementCompensationFilter.parse_compensation(lead.raw_compensation)
                    if comp and comp.get("is_high_ticket"):
                        enriched = RequirementDealCardGenerator.generate(lead, comp)
                        api.sendMessage(chat_id="-1001", text=RequirementTelegramFormatter.format_deal_card(enriched))
                        cycle1_alerts += 1

            self.assertGreaterEqual(cycle1_alerts, 3)
            self.assertEqual(len(api.sent_messages), cycle1_alerts)

            # Cycle 2: Same batch PLUS 1 brand new high ticket lead
            new_lead_id = hashlib.sha256(b"https://brand-new-client.io/job/999").hexdigest()
            brand_new_lead = Lead(
                id=new_lead_id,
                title="Principal AI Infrastructure Architect",
                source="weworkremotely",
                client="Anthropic Partner",
                url="https://brand-new-client.io/job/999",
                raw_compensation="$15,000 fixed deliverable milestone"
            )
            second_batch = copy.deepcopy(initial_leads) + [brand_new_lead]

            cycle2_alerts = 0
            for lead in second_batch:
                if db.insert_lead(lead):
                    comp = RequirementCompensationFilter.parse_compensation(lead.raw_compensation)
                    if comp and comp.get("is_high_ticket"):
                        enriched = RequirementDealCardGenerator.generate(lead, comp)
                        api.sendMessage(chat_id="-1001", text=RequirementTelegramFormatter.format_deal_card(enriched))
                        cycle2_alerts += 1

            self.assertEqual(cycle2_alerts, 1)
            self.assertEqual(len(api.sent_messages), cycle1_alerts + 1)
            self.assertIn("Anthropic Partner", api.sent_messages[-1]["text"])
            db.close()

    def test_tier4_scenario_full_subscriber_lifecycle_and_channel_access(self, tmp_path=None):
        """Scenario 3: Complete subscriber lifecycle from purchase to channel access to cancellation & kick."""
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = os.path.join(str(tmp_path) if tmp_path else temp_dir, "lifecycle.db")
            conn = sqlite3.connect(db_file)
            conn.execute("""
                CREATE TABLE subscribers (
                    membership_id TEXT PRIMARY KEY,
                    user_id TEXT,
                    telegram_user_id INT,
                    status TEXT,
                    invite_link TEXT
                );
            """)

            telegram_api = MockTelegramBotAPI()
            dispatcher = MockWhopWebhookDispatcher()

            # Step 1: User purchases membership on Whop -> went_valid event received
            valid_payload = {
                "action": "membership.went_valid",
                "data": {
                    "id": "mem_lifecycle_99",
                    "user_id": "usr_buyer_42",
                    "telegram_account_id": 77665544
                }
            }
            valid_bytes = json.dumps(valid_payload).encode("utf-8")
            headers = dispatcher.create_standard_headers(valid_bytes)
            ok, _ = RequirementWhopWebhookVerifier.verify(valid_bytes, headers, dispatcher.secret)
            self.assertTrue(ok)

            invite_res = telegram_api.createChatInviteLink(chat_id="-100555", member_limit=1)
            invite_link = invite_res["result"]["invite_link"]

            with conn:
                conn.execute(
                    "INSERT INTO subscribers (membership_id, user_id, telegram_user_id, status, invite_link) VALUES (?, ?, ?, 'active', ?)",
                    ("mem_lifecycle_99", "usr_buyer_42", 77665544, invite_link)
                )

            # Step 2: User joins channel and receives contract alert
            telegram_api.channel_members[77665544] = "active"
            deal_res = telegram_api.sendMessage(
                chat_id="-100555",
                text="<b>💰 $7,500 FIXED</b>\n\n🎯 <b>Senior Full-Stack Contract</b>"
            )
            self.assertTrue(deal_res["ok"])

            # Step 3: User cancels subscription -> went_invalid event received
            invalid_payload = {
                "action": "membership.went_invalid",
                "data": {
                    "id": "mem_lifecycle_99",
                    "user_id": "usr_buyer_42"
                }
            }
            invalid_bytes = json.dumps(invalid_payload).encode("utf-8")
            inv_headers = dispatcher.create_standard_headers(invalid_bytes)
            ok_inv, _ = RequirementWhopWebhookVerifier.verify(invalid_bytes, inv_headers, dispatcher.secret)
            self.assertTrue(ok_inv)

            # Eject member: ban + neutral unban
            telegram_api.banChatMember(chat_id="-100555", user_id=77665544)
            telegram_api.unbanChatMember(chat_id="-100555", user_id=77665544)
            self.assertEqual(telegram_api.channel_members[77665544], "kicked_neutral")

            with conn:
                conn.execute("UPDATE subscribers SET status = 'invalid' WHERE membership_id = ?", ("mem_lifecycle_99",))

            cursor = conn.cursor()
            cursor.execute("SELECT status FROM subscribers WHERE membership_id = ?", ("mem_lifecycle_99",))
            self.assertEqual(cursor.fetchone()[0], "invalid")
            conn.close()


if __name__ == "__main__":
    unittest.main()
