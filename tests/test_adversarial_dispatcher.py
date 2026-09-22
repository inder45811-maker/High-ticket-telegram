"""Adversarial stress and injection test suite for Telegram Dispatcher subsystem.

Tests:
1. HTML Injection & XSS Payloads: Script tags, img onerror, unclosed tags, entity attacks.
2. Extreme Length Truncation & Tag Symmetry: 100k char payloads truncated strictly <= 4096 with balanced tags.
3. Unicode, Compound Emojis & Zalgo Text: ZWJ emoji sequences, extreme combining marks (ReDoS check).
4. Apply Button URL Sanitization: Dangerous URL schemes (javascript, file, data, vbscript) strictly rejected.
5. Telegram Dry-Run Mock Validator Edge Cases: Validation of mock payload constraints.
"""

import html
import re
import time
import unittest
from typing import Any, Dict, List

from b2b_alert_bot.dispatcher.formatter import (
    TELEGRAM_MAX_MESSAGE_LENGTH,
    TelegramFormatter,
    create_apply_markup,
    format_deal_card,
    truncate_html,
)
from b2b_alert_bot.dispatcher.telegram_bot import TelegramDispatcher
from b2b_alert_bot.schema import EnrichedLead, Lead


def get_tag_counts(text: str, tag: str) -> tuple[int, int]:
    opens = len(re.findall(rf"<{tag}(?:\s+[^>]*)*>", text, flags=re.IGNORECASE))
    closes = len(re.findall(rf"</{tag}>", text, flags=re.IGNORECASE))
    return opens, closes


class TestTelegramHTMLInjectionAdversarial(unittest.TestCase):
    """Adversarial testing of Telegram deal card formatting against injection vectors."""

    def setUp(self):
        self.dispatcher = TelegramDispatcher(dry_run=True, chat_id="-100123456789")

    def _create_lead(self, title: str, client: str, scope: str, skills: str, angle: str, url: str = "https://apply.com") -> EnrichedLead:
        lead = Lead(
            id="adv_lead_" + "a" * 55,
            title=title,
            source="adversarial",
            client=client,
            url=url,
            core_tech_stack=[skills]
        )
        return EnrichedLead(
            lead=lead,
            is_high_ticket=True,
            rate_type="fixed",
            min_amount=10000.0,
            max_amount=10000.0,
            currency="USD",
            budget_badge="💰 $10,000 FIXED",
            scope_bullet=scope,
            skills_bullet=skills,
            winning_angle=angle,
        )

    def test_script_tag_injection_sanitization(self):
        """Injecting <script> tags into any field must be safely escaped."""
        xss_payload = "<script>alert('XSS_EXECUTION')</script>"
        enriched = self._create_lead(
            title=f"Lead {xss_payload}",
            client=f"Client {xss_payload}",
            scope=f"Scope {xss_payload}",
            skills=f"Skills {xss_payload}",
            angle=f"Angle {xss_payload}",
        )

        card = format_deal_card(enriched)

        # Must NOT contain literal unescaped <script>
        self.assertNotIn("<script>", card)
        self.assertNotIn("</script>", card)
        self.assertIn("&lt;script&gt;alert('XSS_EXECUTION')&lt;/script&gt;", card)

        # Dispatcher validation must accept the safely escaped output
        val_err = self.dispatcher._validate_mock_payload("-1001", card)
        self.assertIsNone(val_err, f"Validation failed on escaped script payload: {val_err}")

    def test_unclosed_html_tags_in_inputs(self):
        """Unclosed opening tags (<b, <i, <a href=) in inputs must not break tag balance."""
        toxic_inputs = [
            "<b unclosed tag",
            "<i style='color:red'",
            "<a href='http://evil.com'",
            "<code>def foo():",
            "<pre>console.log('test')",
            "<b><i><code nested unclosed",
            "<style>body { display: none; }</style>",
            "<img src=x onerror=alert(1)>",
            "<!-- comment -->",
            "<![CDATA[<script>alert(1)</script>]]>",
        ]

        for payload in toxic_inputs:
            enriched = self._create_lead(
                title=f"Title with {payload}",
                client=f"Client with {payload}",
                scope=f"Scope with {payload}",
                skills="Python",
                angle="Portfolio",
            )
            card = format_deal_card(enriched)

            # Must not have raw '<' from the payload
            self.assertNotIn(payload, card)

            # Check tag balance
            for tag in ["b", "i", "code", "pre", "a", "strong", "em"]:
                opens, closes = get_tag_counts(card, tag)
                self.assertEqual(opens, closes, f"Unbalanced tag <{tag}> with input '{payload}'")

            # Mock validator must pass
            val_err = self.dispatcher._validate_mock_payload("-1001", card)
            self.assertIsNone(val_err, f"Validator error on payload '{payload}': {val_err}")

    def test_dangerous_button_url_schemes(self):
        """create_apply_markup must reject dangerous and non-http schemes."""
        evil_schemes = [
            "javascript:alert(document.cookie)",
            "JAVASCRIPT:alert(1)",
            "javascript://alert(1)",
            "file:///etc/shadow",
            "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
            "vbscript:MsgBox('owned')",
            "about:blank",
            "chrome://settings",
            "blob:https://example.com/uuid",
            "ftp://ftp.example.com",
            "mailto:victim@example.com",
            "tel:+1234567890",
            "//protocol-relative.com/apply",
            "",
            "None",
            "12345",
        ]

        for url in evil_schemes:
            markup = create_apply_markup(url)
            self.assertIsNone(markup, f"Dangerous scheme was not rejected: {url}")

    def test_button_url_control_characters_edge_case(self):
        """Test URL containing embedded control characters (newlines/tabs).

        Note: create_apply_markup only validates startswith('http://') or startswith('https://').
        Embedded newlines currently pass create_apply_markup, presenting an edge case where
        live Telegram Bot API would reject the inline keyboard button with HTTP 400.
        """
        newline_url = "https://evil.com\n/attack"
        markup = create_apply_markup(newline_url)
        # Document current behavior: it accepts the URL because it starts with https://
        self.assertIsNotNone(markup)
        url_in_btn = markup["inline_keyboard"][0][0]["url"]
        self.assertEqual(url_in_btn, newline_url)


class TestTelegramMessageLengthAndTruncationAdversarial(unittest.TestCase):
    """Stress-test message truncation engine with massive payloads and deep tag nesting."""

    def test_massive_payload_truncation_preserves_length_and_tags(self):
        """Massive 100,000 character scope must truncate <= 4096 chars with 100% tag symmetry."""
        giant_scope = "Comprehensive microservices refactoring in Rust and Go. " * 1800  # ~100k chars
        lead = Lead(
            id="adv_id_" + "9" * 57,
            title="Principal Infrastructure Architect",
            source="wwr",
            client="Enterprise Scaling Corp",
            url="https://weworkremotely.com/jobs/123"
        )
        enriched = EnrichedLead(
            lead=lead,
            is_high_ticket=True,
            rate_type="hourly",
            min_amount=120.0,
            max_amount=150.0,
            currency="USD",
            budget_badge="⏱️ $120 - $150/HR",
            scope_bullet=giant_scope,
            skills_bullet="Rust, Go, Kubernetes, eBPF",
            winning_angle="Demonstrate benchmark throughput on multi-region low-latency clusters.",
        )

        card = format_deal_card(enriched)

        self.assertLessEqual(len(card), TELEGRAM_MAX_MESSAGE_LENGTH)
        self.assertGreater(len(card), 2000, "Truncation was overly aggressive")

        # Invariant: Tag symmetry on truncated output
        for tag in ["b", "i", "code", "pre", "a"]:
            opens, closes = get_tag_counts(card, tag)
            self.assertEqual(opens, closes, f"Mismatched tag <{tag}> after 100k truncation: {opens} != {closes}")

        # Invariant: Valid mock telegram dispatch
        dispatcher = TelegramDispatcher(dry_run=True, chat_id="-1001")
        res = dispatcher.dispatch_sync(enriched)
        self.assertTrue(res.ok)
        self.assertEqual(res.status, "dry_run")

    def test_truncate_html_boundary_lengths(self):
        """Test truncate_html at low and boundary max_lengths (0, 1, 3, 5, 10, 20, 50)."""
        html_input = "<b>Header</b> <i>Italic text with <code>inline code</code> and <a>link</a>.</i>"

        for max_len in [0, 1, 2, 3, 5, 10, 15, 25, 40, 50, 100]:
            truncated = truncate_html(html_input, max_length=max_len)
            self.assertLessEqual(len(truncated), max_len, f"Truncated length {len(truncated)} exceeds {max_len}")

            # For max_len >= 15, verify tag balance
            if max_len >= 15:
                for tag in ["b", "i", "code", "a"]:
                    opens, closes = get_tag_counts(truncated, tag)
                    self.assertEqual(opens, closes, f"Unbalanced tag <{tag}> at max_len={max_len} in: {truncated}")

    def test_truncate_html_deeply_nested_tags(self):
        """Test truncate_html on deeply nested markup (5 levels deep)."""
        nested_html = "<b>1<i>2<code>3<pre>4<a>5 deeply nested</a></pre></code></i></b>"
        for max_len in [20, 35, 45, 60]:
            truncated = truncate_html(nested_html, max_length=max_len)
            self.assertLessEqual(len(truncated), max_len)
            for tag in ["b", "i", "code", "pre", "a"]:
                opens, closes = get_tag_counts(truncated, tag)
                self.assertEqual(opens, closes, f"Deeply nested tag <{tag}> unbalanced at max_len={max_len}")


class TestUnicodeEmojiZalgoAdversarial(unittest.TestCase):
    """Stress-test Unicode, compound emojis, and Zalgo diacritic strings."""

    def setUp(self):
        self.dispatcher = TelegramDispatcher(dry_run=True, chat_id="-1001")

    def test_compound_emojis_and_zwj_sequences(self):
        """Compound emojis (ZWJ, skin tones, flags) must format cleanly without corruption."""
        compound_emojis = (
            "👨‍👩‍👧‍👦 "  # Family ZWJ
            "👩🏾‍💻 "  # Woman technologist (dark skin tone)
            "🏳️‍⚧️ "    # Transgender flag ZWJ
            "🇺🇸 🇬🇧 🇪🇺 🇯🇵 "  # Regional indicator flags
            "🔥 💰 🚀 ⚡ 💎 🎯 📋 🕒 🆔"
        )
        lead = Lead(
            id="adv_emoji_" + "1" * 54,
            title=f"Staff Web3 Engineer {compound_emojis}",
            source="remoteok",
            client="CryptoGlobal 🚀",
            url="https://remoteok.com/jobs/crypto",
            core_tech_stack=["Solidity", "Rust", compound_emojis]
        )
        enriched = EnrichedLead(
            lead=lead,
            is_high_ticket=True,
            rate_type="fixed",
            min_amount=15000.0,
            max_amount=15000.0,
            currency="USD",
            budget_badge="💰 $15,000 FIXED",
            scope_bullet=f"Smart contract auditing {compound_emojis}",
            skills_bullet=f"Solidity, Foundry {compound_emojis}",
            winning_angle=f"Deliver proof of concept exploit fix {compound_emojis}",
        )

        card = format_deal_card(enriched)
        self.assertIn("👨‍👩‍👧‍👦", card)
        self.assertIn("👩🏾‍💻", card)
        self.assertIn("🏳️‍⚧️", card)

        val_err = self.dispatcher._validate_mock_payload("-1001", card)
        self.assertIsNone(val_err)

    def test_zalgo_combining_diacritics_redos_stress(self):
        """Zalgo text with 2,000 combining marks must process rapidly without ReDoS or crash."""
        # Craft zalgo text: base letters followed by excessive combining diacritical marks (\u0300-\u036F)
        zalgo_chars = []
        base = "URGENT OPPORTUNITY CONTRACT "
        for c in base:
            zalgo_chars.append(c)
            for mark in range(0x0300, 0x0345):  # ~70 combining marks per base letter
                zalgo_chars.append(chr(mark))
        zalgo_str = "".join(zalgo_chars)

        lead = Lead(
            id="adv_zalgo_" + "2" * 54,
            title=zalgo_str[:500],
            source="hackernews",
            client="Zalgo Corp",
            url="https://news.ycombinator.com/item?id=99",
            core_tech_stack=["Python"]
        )
        enriched = EnrichedLead(
            lead=lead,
            is_high_ticket=True,
            rate_type="hourly",
            min_amount=95.0,
            max_amount=95.0,
            currency="USD",
            budget_badge="⏱️ $95/HR",
            scope_bullet=zalgo_str,
            skills_bullet="NLP, Unicode Parsing",
            winning_angle="Provide benchmark parsing speed under Unicode stress.",
        )

        t0 = time.perf_counter()
        card = format_deal_card(enriched)
        t1 = time.perf_counter()

        # Invariant: Execution time < 500ms (no catastrophic backtracking)
        duration = t1 - t0
        self.assertLess(duration, 0.50, f"Zalgo formatting took too long: {duration:.3f}s (possible ReDoS)")

        # Invariant: Length limit holds
        self.assertLessEqual(len(card), TELEGRAM_MAX_MESSAGE_LENGTH)

        # Invariant: Tags balanced
        for tag in ["b", "i", "code"]:
            opens, closes = get_tag_counts(card, tag)
            self.assertEqual(opens, closes)

    def test_multilingual_and_rtl_scripts(self):
        """Arabic, Hebrew (RTL), Chinese, Japanese, Cyrillic, Hindi text."""
        lead = Lead(
            id="adv_multi_" + "3" * 54,
            title="مهندس برمجيات أول / מתכנת בכיר / 高级分布式架构师",
            source="wwr",
            client="Global Multilingual Corp",
            url="https://weworkremotely.com/jobs/multi"
        )
        enriched = EnrichedLead(
            lead=lead,
            is_high_ticket=True,
            rate_type="monthly",
            min_amount=8000.0,
            max_amount=8000.0,
            currency="USD",
            budget_badge="💰 $8,000/MO",
            scope_bullet="تطوير الأنظمة الموزعة مع التوافق العالي. פיתוח מערכות מבוזרות. 分布式系统架构设计与高可用保障。",
            skills_bullet="Python, Go, Кафка, डॉकर",
            winning_angle="הצג מקרי מבחן קודמים להפחתת זמני השהייה. قدم دراسות حالة مفصلة.",
        )

        card = format_deal_card(enriched)
        self.assertIn("مهندس برمجيات أول", card)
        self.assertIn("מתכנת בכיר", card)
        self.assertIn("高级分布式架构师", card)

        val_err = self.dispatcher._validate_mock_payload("-1001", card)
        self.assertIsNone(val_err)


if __name__ == "__main__":
    unittest.main()
