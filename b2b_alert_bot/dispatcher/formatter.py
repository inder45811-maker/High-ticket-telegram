"""Mobile-optimized Telegram HTML Deal Card Formatter.

Conforms to Telegram Bot API HTML parse mode specifications:
- Safe HTML entity escaping with html.escape
- High-visibility header with budget badge (e.g., 💰 $4,500 FIXED or ⏱️ $75/HR)
- 3-bullet deal card summary (Scope, Skills, Winning Angle)
- Link preview suppression configuration (link_preview_options: {"is_disabled": true})
- 1-click apply button markup generation
- Truncation at 4096 characters strictly preserving HTML tag symmetry
"""

import html
import re
from typing import Any, Dict, List, Optional, Union

from b2b_alert_bot.schema import EnrichedLead, Lead

# Telegram hard limit for message text
TELEGRAM_MAX_MESSAGE_LENGTH = 4096

# Telegram supported HTML tags that require closing
SUPPORTED_HTML_TAGS = {
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
    "span", "tg-spoiler", "a", "code", "pre", "blockquote"
}


def format_budget_badge(
    rate_type: str,
    min_amount: float = 0.0,
    max_amount: float = 0.0,
    currency: str = "USD"
) -> str:
    """Format compensation into a high-visibility badge with appropriate emoji.

    Examples:
    - Fixed $4,500 -> 💰 $4,500 FIXED
    - Fixed range $3,000 - $6,000 -> 💰 $3,000 - $6,000 FIXED
    - Hourly $75/hr -> ⏱️ $75/HR
    - Hourly range $60 - $90/hr -> ⏱️ $60 - $90/HR
    - Annual $140,000/yr -> 💼 $140k/YR (~$70/HR)
    - Monthly $4,000/mo -> 💰 $4,000/MO
    """
    clean_type = (rate_type or "unknown").strip().lower()
    min_val = float(min_amount or 0.0)
    max_val = float(max_amount or min_val)

    if clean_type == "hourly":
        if min_val > 0 and min_val != max_val:
            return f"⏱️ ${int(min_val)} - ${int(max_val)}/HR"
        return f"⏱️ ${int(max_val)}/HR"

    elif clean_type == "annual":
        equiv_hourly = int(max_val / 2000.0) if max_val > 0 else 50
        k_val = int(max_val / 1000.0)
        return f"💼 ${k_val}k/YR (~${equiv_hourly}/HR)"

    elif clean_type == "monthly":
        if min_val > 0 and min_val != max_val:
            return f"💰 ${int(min_val):,} - ${int(max_val):,}/MO"
        return f"💰 ${int(max_val):,}/MO"

    elif clean_type == "fixed":
        if min_val > 0 and min_val != max_val:
            return f"💰 ${int(min_val):,} - ${int(max_val):,} FIXED"
        return f"💰 ${int(max_val):,} FIXED"

    # Default fallback for unknown or generic high-ticket opportunities
    if max_val >= 2000.0:
        return f"💰 ${int(max_val):,} HIGH-TICKET"
    return "💰 $2,000+ HIGH-TICKET"


def truncate_html(
    html_str: str,
    max_length: int = TELEGRAM_MAX_MESSAGE_LENGTH,
    suffix: str = "..."
) -> str:
    """Truncate HTML string to max_length while preserving valid tag balance.

    Ensures all open tags (<b>, <i>, <code>, <a>, etc.) are properly closed
    and total length does not exceed max_length.
    """
    if len(html_str) <= max_length:
        return html_str

    if max_length <= len(suffix):
        return suffix[:max_length]

    # Regex token matching: HTML tags, HTML entities, or plain characters
    # Group 1: opening/closing tag
    # Group 2: tag name
    # Group 3: entity
    # Group 4: plain character
    token_pattern = re.compile(
        r"(<(/)?([a-zA-Z0-9_-]+)(?:\s+[^>]*)?>)|"  # Tag: (1 full, 2 slash, 3 name)
        r"(&[a-zA-Z0-9#]+;)|"                      # Entity (4)
        r"([^<&]+)"                                # Plain text chunk (5)
    )

    open_tags: List[str] = []
    accumulated_tokens: List[str] = []
    current_len = 0

    def get_closing_tags_len(tags: List[str]) -> int:
        return sum(len(f"</{t}>") for t in tags)

    tokens = list(token_pattern.finditer(html_str))

    for m in tokens:
        full_tag = m.group(1)
        is_closing = bool(m.group(2))
        tag_name = (m.group(3) or "").lower()
        entity = m.group(4)
        plain_text = m.group(5)

        if full_tag:
            if is_closing:
                # Closing tag
                if open_tags and open_tags[-1] == tag_name:
                    open_tags.pop()
                elif tag_name in open_tags:
                    # Non-nested closing: pop back to it
                    idx = len(open_tags) - 1 - open_tags[::-1].index(tag_name)
                    open_tags.pop(idx)

                needed_closing_len = get_closing_tags_len(open_tags)
                if current_len + len(full_tag) + needed_closing_len + len(suffix) <= max_length:
                    accumulated_tokens.append(full_tag)
                    current_len += len(full_tag)
                else:
                    break
            else:
                # Opening tag
                new_open_tags = list(open_tags)
                if tag_name in SUPPORTED_HTML_TAGS:
                    new_open_tags.append(tag_name)

                needed_closing_len = get_closing_tags_len(new_open_tags)
                if current_len + len(full_tag) + needed_closing_len + len(suffix) <= max_length:
                    accumulated_tokens.append(full_tag)
                    current_len += len(full_tag)
                    if tag_name in SUPPORTED_HTML_TAGS:
                        open_tags.append(tag_name)
                else:
                    break

        elif entity:
            needed_closing_len = get_closing_tags_len(open_tags)
            if current_len + len(entity) + needed_closing_len + len(suffix) <= max_length:
                accumulated_tokens.append(entity)
                current_len += len(entity)
            else:
                break

        elif plain_text:
            needed_closing_len = get_closing_tags_len(open_tags)
            available = max_length - current_len - needed_closing_len - len(suffix)

            if available <= 0:
                break

            if len(plain_text) <= available:
                accumulated_tokens.append(plain_text)
                current_len += len(plain_text)
            else:
                # Partial slice of plain text
                chunk = plain_text[:available]
                # Avoid cutting off trailing backslash or orphan ampersand
                chunk = chunk.rstrip("&")
                accumulated_tokens.append(chunk)
                current_len += len(chunk)
                break

    # Append suffix if we truncated
    accumulated_tokens.append(suffix)

    # Close all remaining open tags in reverse order
    for tag in reversed(open_tags):
        accumulated_tokens.append(f"</{tag}>")

    result = "".join(accumulated_tokens)
    if len(result) > max_length:
        # Fallback emergency slice
        return result[:max_length]
    return result


def create_apply_markup(
    url: str,
    text: str = "🚀 Apply on Source"
) -> Optional[Dict[str, Any]]:
    """Construct Telegram InlineKeyboardMarkup with direct URL button.

    Validates that the URL scheme is http or https. Returns None if invalid or empty.
    """
    if not url or not isinstance(url, str):
        return None

    clean_url = url.strip()
    if not (clean_url.startswith("http://") or clean_url.startswith("https://")):
        return None

    return {
        "inline_keyboard": [
            [
                {
                    "text": text,
                    "url": clean_url
                }
            ]
        ]
    }


def get_link_preview_options() -> Dict[str, Any]:
    """Return link preview options suppressing bulky webpage scrapers on mobile."""
    return {"is_disabled": True}


def format_deal_card(
    enriched: Union[EnrichedLead, Dict[str, Any]],
    relative_time: Optional[str] = None
) -> str:
    """Format an enriched lead into a mobile-optimized HTML deal card.

    Card Structure:
    <b>[BUDGET BADGE]</b>

    🎯 <b>[TITLE]</b>
    🏢 <i>[CLIENT]</i> • <i>via [SOURCE]</i>

    📋 <b>Executive Summary:</b>
    • <b>Scope:</b> [SCOPE_DELIVERABLES]
    • <b>Skills:</b> [CORE_TECH_STACK]
    • <b>Winning Angle:</b> [WINNING_PITCH_ANGLE]

    🕒 <i>[POSTED]</i> | 🆔 <code>#[SHORT_ID]</code>

    Guarantees:
    - HTML entity safety (html.escape)
    - Output length <= 4096 characters with 100% tag symmetry
    """
    # Normalize input whether EnrichedLead dataclass or dict
    if isinstance(enriched, EnrichedLead):
        lead = enriched.lead
        badge = enriched.budget_badge or format_budget_badge(
            enriched.rate_type, enriched.min_amount, enriched.max_amount, enriched.currency
        )
        scope = enriched.scope_bullet or ""
        skills = enriched.skills_bullet or ""
        winning_angle = enriched.winning_angle or ""
        lead_id = lead.id or ""
        lead_title = lead.title or ""
        lead_client = lead.client or ""
        lead_source = lead.source or ""
    elif isinstance(enriched, dict):
        lead_raw = enriched.get("lead", enriched)
        if isinstance(lead_raw, Lead):
            lead_id = lead_raw.id or ""
            lead_title = lead_raw.title or ""
            lead_client = lead_raw.client or ""
            lead_source = lead_raw.source or ""
        else:
            lead_id = lead_raw.get("id", "")
            lead_title = lead_raw.get("title", "")
            lead_client = lead_raw.get("client", "")
            lead_source = lead_raw.get("source", "")

        badge = enriched.get("budget_badge") or format_budget_badge(
            enriched.get("rate_type", "unknown"),
            enriched.get("min_amount", 0.0),
            enriched.get("max_amount", 0.0),
            enriched.get("currency", "USD")
        )
        scope = enriched.get("scope_bullet") or enriched.get("scope", "")
        skills_raw = enriched.get("skills_bullet") or enriched.get("skills", "")
        if isinstance(skills_raw, list):
            skills = ", ".join(skills_raw)
        else:
            skills = str(skills_raw or "")
        winning_angle = enriched.get("winning_angle", "")
    else:
        raise TypeError(f"Expected EnrichedLead or dict, got {type(enriched).__name__}")

    # Escaping
    esc_badge = html.escape(str(badge))
    esc_title = html.escape(str(lead_title or "High-Ticket Opportunity").strip())
    esc_client = html.escape(str(lead_client or "Direct Client").strip())
    esc_source = html.escape(str(lead_source or "Direct").strip().capitalize())
    esc_skills = html.escape(str(skills or "Senior Engineering & Architecture").strip())
    esc_angle = html.escape(str(winning_angle or "Propose a phased milestone delivery with relevant portfolio case studies.").strip())

    short_id = html.escape(lead_id[:8] if lead_id else "deal")
    time_str = html.escape(relative_time or "Posted recently")

    # If scope is massive, pre-truncate it to 3,000 characters before composing
    if len(scope) > 3000:
        scope = scope[:2950] + "..."
    esc_scope = html.escape(scope)

    card = (
        f"<b>{esc_badge}</b>\n\n"
        f"🎯 <b>{esc_title}</b>\n"
        f"🏢 <i>{esc_client}</i> • <i>via {esc_source}</i>\n\n"
        f"📋 <b>Executive Summary:</b>\n"
        f"• <b>Scope:</b> {esc_scope}\n"
        f"• <b>Skills:</b> {esc_skills}\n"
        f"• <b>Winning Angle:</b> {esc_angle}\n\n"
        f"🕒 <i>{time_str}</i> | 🆔 <code>#{short_id}</code>"
    )

    if len(card) > TELEGRAM_MAX_MESSAGE_LENGTH:
        card = truncate_html(card, max_length=TELEGRAM_MAX_MESSAGE_LENGTH)

    return card


class TelegramFormatter:
    """Class wrapper providing static access to formatter functions."""

    format_deal_card = staticmethod(format_deal_card)
    create_apply_markup = staticmethod(create_apply_markup)
    format_budget_badge = staticmethod(format_budget_badge)
    truncate_html = staticmethod(truncate_html)
    get_link_preview_options = staticmethod(get_link_preview_options)
