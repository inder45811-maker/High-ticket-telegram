"""High-ticket compensation parser, FX normalizer, and threshold validator for B2B Alert Bot.

Handles fixed budgets ($2,000+, $5k, 5000 USD, €2,500, £2,000),
hourly rates ($50/hr, $60 - $80 / hour, 75 USD/h),
monthly retainers ($10,000/mo), and annual salaries ($100,000/yr).
Converts currencies to USD and evaluates against >= $2,000 fixed or >= $50/hr threshold.
"""

import os
import re
from typing import Optional, Dict, Any, Tuple

from b2b_alert_bot.enrichment.funding_filter import (
    is_funding_false_positive,
    is_outlier_funding_amount,
)

# Currency exchange rates to USD
FX_RATES: Dict[str, float] = {
    "USD": 1.0,
    "EUR": 1.08,
    "GBP": 1.28,
    "CAD": 0.74,
    "AUD": 0.66,
}

CURRENCY_SYMBOL_MAP: Dict[str, str] = {
    "$": "USD",
    "USD": "USD",
    "US$": "USD",
    "€": "EUR",
    "EUR": "EUR",
    "£": "GBP",
    "GBP": "GBP",
    "CAD": "CAD",
    "C$": "CAD",
    "AUD": "AUD",
    "A$": "AUD",
}

# Blacklist of non-monetary / equity-only listing tokens
EQUITY_UNPAID_PATTERNS = [
    r"\bequity\s+only\b",
    r"\bunpaid\b",
    r"\bvolunteer\b",
    r"\brev(?:enue)?\s*share\s+only\b",
    r"\bno\s+cash\b",
    r"\bfor\s+equity\b",
]

# Blacklist of unstated or opaque compensation tokens
UNSTATED_PATTERNS = [
    r"\bcompetitive\s+salary\b",
    r"\bdoe\b",
    r"\bdepends\s+on\s+experience\b",
    r"\bnegotiable\b",
    r"\btbd\b",
    r"\bunspecified\b",
]

# Unified numerical subpattern supporting commas, decimals, k/m suffixes, and trailing '+'
NUM_PATTERN = r"(?:\d+(?:,\d{3})*(?:\.\d+)?\s*[km]\+?|\d+(?:,\d{3})*(?:\.\d+)?\+?)"

# Hourly rate pattern (e.g., $50/hr, $60 - $80 / hour, 75 USD/h, $50 ph)
HOURLY_PATTERN = re.compile(
    r"(?:(?P<curr1>[\$€£]|C\$|A\$)|(?P<curr_code1>USD|EUR|GBP|CAD|AUD)\s*)?"
    r"(?P<min>" + NUM_PATTERN + r")"
    r"(?:\s*(?:-|–|to)\s*(?:(?P<curr2>[\$€£]|C\$|A\$)|(?P<curr_code2>USD|EUR|GBP|CAD|AUD)\s*)?(?P<max>" + NUM_PATTERN + r"))?\s*"
    r"(?:(?P<curr_post>USD|EUR|GBP|CAD|AUD)\s*)?"
    r"(?:/\s*(?:hr|hour|h)\b|per\s+hour|\s*ph\b)",
    re.IGNORECASE,
)

# Annual salary pattern (e.g., $100,000/yr, $120k/year, $100k - $150k / annum)
ANNUAL_PATTERN = re.compile(
    r"(?:(?P<curr1>[\$€£]|C\$|A\$)|(?P<curr_code1>USD|EUR|GBP|CAD|AUD)\s*)?"
    r"(?P<min>" + NUM_PATTERN + r")"
    r"(?:\s*(?:-|–|to)\s*(?:(?P<curr2>[\$€£]|C\$|A\$)|(?P<curr_code2>USD|EUR|GBP|CAD|AUD)\s*)?(?P<max>" + NUM_PATTERN + r"))?\s*"
    r"(?:(?P<curr_post>USD|EUR|GBP|CAD|AUD)\s*)?"
    r"(?:/\s*(?:yr|year|annum)\b|per\s+year|per\s+annum|\s*p\.?a\.?\b)",
    re.IGNORECASE,
)

# Monthly retainer pattern (e.g., $10,000/mo, $10k/month, $4,000/month)
MONTHLY_PATTERN = re.compile(
    r"(?:(?P<curr1>[\$€£]|C\$|A\$)|(?P<curr_code1>USD|EUR|GBP|CAD|AUD)\s*)?"
    r"(?P<min>" + NUM_PATTERN + r")"
    r"(?:\s*(?:-|–|to)\s*(?:(?P<curr2>[\$€£]|C\$|A\$)|(?P<curr_code2>USD|EUR|GBP|CAD|AUD)\s*)?(?P<max>" + NUM_PATTERN + r"))?\s*"
    r"(?:(?P<curr_post>USD|EUR|GBP|CAD|AUD)\s*)?"
    r"(?:/\s*(?:mo|month)\b|per\s+month|\s*pm\b)",
    re.IGNORECASE,
)

# Fixed budget prefix pattern (e.g., $2,000, $5k, Budget: $3,500, Paying $6k, €2,500, £2,000, USD 5,000)
FIXED_PREFIX_PATTERN = re.compile(
    r"(?:(?:budget|fixed|fixed-price|flat rate|rate|paying|pay|stipend|fee|bounty):\s*)?"
    r"(?:(?P<curr1>[\$€£]|C\$|A\$)|(?P<curr_code1>USD|EUR|GBP|CAD|AUD)\s*)"
    r"(?P<min>" + NUM_PATTERN + r")"
    r"(?:\s*(?:-|–|to)\s*(?:(?P<curr2>[\$€£]|C\$|A\$)|(?P<curr_code2>USD|EUR|GBP|CAD|AUD)\s*)?(?P<max>" + NUM_PATTERN + r"))?\s*"
    r"(?P<curr_post>USD|EUR|GBP|CAD|AUD)?\b",
    re.IGNORECASE,
)

# Fixed budget suffix pattern (e.g., 5000 USD, 2500 EUR)
FIXED_SUFFIX_PATTERN = re.compile(
    r"(?P<min>" + NUM_PATTERN + r")"
    r"(?:\s*(?:-|–|to)\s*(?P<max>" + NUM_PATTERN + r"))?\s*"
    r"(?P<curr_post>USD|EUR|GBP|CAD|AUD)\b",
    re.IGNORECASE,
)


def parse_num(val_str: str) -> float:
    """Parse numeric string with optional commas, decimals, k/m suffixes, and trailing +."""
    clean = val_str.replace(",", "").strip().lower()
    clean = clean.rstrip("+").strip()
    if clean.endswith("k"):
        return float(clean[:-1].strip()) * 1000.0
    if clean.endswith("m"):
        return float(clean[:-1].strip()) * 1000000.0
    return float(clean)


def _detect_currency(m: re.Match) -> Tuple[str, float]:
    """Extract and normalize currency symbol/code to standard 3-letter currency and multiplier."""
    group_dict = m.groupdict()
    candidates = [
        group_dict.get("curr_code1"),
        group_dict.get("curr_code2"),
        group_dict.get("curr_post"),
        group_dict.get("curr1"),
        group_dict.get("curr2"),
    ]

    detected_code = "USD"
    for cand in candidates:
        if cand:
            clean = cand.strip().upper()
            mapped = CURRENCY_SYMBOL_MAP.get(clean, clean)
            if mapped in FX_RATES:
                detected_code = mapped
                break

    fx = FX_RATES.get(detected_code, 1.0)
    return detected_code, fx


def format_budget_badge(
    rate_type: str,
    min_amount: float,
    max_amount: float,
    currency: str = "USD",
    equiv_hourly: Optional[float] = None,
) -> str:
    """Format mobile-optimized high-visibility Telegram deal card badge."""
    if rate_type == "hourly":
        if round(min_amount) == round(max_amount):
            return f"⏱️ ${int(round(max_amount))}/HR"
        return f"⏱️ ${int(round(min_amount))} - ${int(round(max_amount))}/HR"

    elif rate_type == "annual":
        badge_k = int(round(max_amount / 1000.0))
        h = int(round(equiv_hourly)) if equiv_hourly is not None else int(round(max_amount / 2000.0))
        return f"💼 ${badge_k}k/YR (~${h}/HR)"

    elif rate_type == "monthly":
        if round(min_amount) == round(max_amount):
            return f"💰 ${int(round(max_amount)):,}/MO"
        return f"💰 ${int(round(min_amount)):,} - ${int(round(max_amount)):,}/MO"

    elif rate_type == "fixed":
        if round(min_amount) == round(max_amount):
            return f"💰 ${int(round(max_amount)):,} FIXED"
        return f"💰 ${int(round(min_amount)):,} - ${int(round(max_amount)):,} FIXED"

    return "💰 $2,000+ HIGH-TICKET"


def evaluate_high_value_filter(
    comp: Optional[Dict[str, Any]],
    min_fixed_usd: float = 2000.0,
    min_hourly_usd: float = 50.0,
    range_strategy: str = "max",
) -> Tuple[bool, str, Dict[str, Any]]:
    """Evaluate extracted compensation against high-ticket threshold.

    Threshold criteria:
    - Fixed >= $2,000 USD
    - Hourly >= $50/hr USD
    - Annual: equivalent hourly rate (annual / 2,000) >= $50/hr USD
    - Monthly: monthly >= $2,000 USD or equivalent hourly (monthly / 160) >= $50/hr USD

    Args:
        comp: Extracted compensation metadata dictionary.
        min_fixed_usd: Minimum threshold for fixed projects (default: $2,000).
        min_hourly_usd: Minimum threshold for hourly rate (default: $50/hr).
        range_strategy: Strategy for evaluating ranges: 'max' (default), 'min', or 'avg'.

    Returns:
        Tuple of (is_pass, reason_string, enriched_metadata_dict).
    """
    if comp is None:
        return False, "REJECT_UNSTATED_COMPENSATION", {}

    if comp.get("rate_type") == "unpaid":
        return False, "REJECT_UNPAID_OR_EQUITY", comp

    rate_type = comp.get("rate_type", "unknown")
    if rate_type == "unknown":
        return False, comp.get("status", "REJECT_UNSTATED_COMPENSATION"), comp

    min_usd = float(comp.get("min_amount", 0.0))
    max_usd = float(comp.get("max_amount", 0.0))

    if range_strategy == "min":
        eval_usd = min_usd
    elif range_strategy == "avg":
        eval_usd = (min_usd + max_usd) / 2.0
    else:  # default 'max'
        eval_usd = max_usd

    meta = {**comp, "eval_usd": eval_usd}

    if rate_type == "fixed":
        if eval_usd >= min_fixed_usd:
            return True, f"PASS_FIXED (${eval_usd:,.2f} >= ${min_fixed_usd:,.2f})", meta
        return False, f"REJECT_LOW_FIXED (${eval_usd:,.2f} < ${min_fixed_usd:,.2f})", meta

    elif rate_type == "hourly":
        if eval_usd >= min_hourly_usd:
            return True, f"PASS_HOURLY (${eval_usd:,.2f}/hr >= ${min_hourly_usd:,.2f}/hr)", meta
        return False, f"REJECT_LOW_HOURLY (${eval_usd:,.2f}/hr < ${min_hourly_usd:,.2f}/hr)", meta

    elif rate_type == "annual":
        equiv_hourly = eval_usd / 2000.0
        meta["equiv_hourly"] = equiv_hourly
        if equiv_hourly >= min_hourly_usd:
            return True, f"PASS_ANNUAL (${eval_usd:,.2f}/yr = ${equiv_hourly:,.2f}/hr >= ${min_hourly_usd:,.2f}/hr)", meta
        return False, f"REJECT_LOW_ANNUAL (${eval_usd:,.2f}/yr = ${equiv_hourly:,.2f}/hr < ${min_hourly_usd:,.2f}/hr)", meta

    elif rate_type == "monthly":
        equiv_hourly = eval_usd / 160.0
        meta["equiv_hourly"] = equiv_hourly
        if eval_usd >= min_fixed_usd or equiv_hourly >= min_hourly_usd:
            return True, f"PASS_MONTHLY (${eval_usd:,.2f}/mo = ${equiv_hourly:,.2f}/hr >= ${min_hourly_usd:,.2f}/hr)", meta
        return False, f"REJECT_LOW_MONTHLY (${eval_usd:,.2f}/mo < ${min_fixed_usd:,.2f})", meta

    return False, f"REJECT_UNKNOWN_RATE_TYPE ({rate_type})", meta


def extract_compensation(text: str) -> Optional[Dict[str, Any]]:
    """Extract raw compensation without threshold decision (for pipeline compatibility)."""
    result = parse_compensation(text)
    if result is None or result.get("rate_type") == "unknown":
        return None
    return result


def parse_compensation(
    text: str,
    range_strategy: Optional[str] = None,
    min_fixed_usd: float = 2000.0,
    min_hourly_usd: float = 50.0,
) -> Dict[str, Any]:
    """Parse, normalize, and evaluate compensation from unstructured job text.

    Args:
        text: Free-form text (job title, description, or compensation snippet).
        range_strategy: Range evaluation strategy ('max', 'min', 'avg'). Defaults to 'max'.
        min_fixed_usd: Fixed threshold in USD (default: 2000.0).
        min_hourly_usd: Hourly threshold in USD (default: 50.0).

    Returns:
        Structured dictionary with parsed amounts, USD conversion, high-ticket status,
        and budget badge.
    """
    if not text or not text.strip():
        return {
            "rate_type": "unknown",
            "min_amount": 0.0,
            "max_amount": 0.0,
            "currency": "USD",
            "is_high_ticket": False,
            "budget_badge": "❓ UNSTATED",
            "status": "UNSTATED",
        }

    strategy = range_strategy or os.getenv("FILTER_RANGE_STRATEGY", "max")
    text_lower = text.lower()

    # 1. Check unpaid / equity blacklist
    for pattern in EQUITY_UNPAID_PATTERNS:
        if re.search(pattern, text_lower):
            return {
                "rate_type": "unpaid",
                "min_amount": 0.0,
                "max_amount": 0.0,
                "raw_min_amount": 0.0,
                "raw_max_amount": 0.0,
                "currency": "USD",
                "is_high_ticket": False,
                "budget_badge": "❌ UNPAID/EQUITY",
                "status": "REJECT_UNPAID_OR_EQUITY",
                "raw_match": "equity/unpaid",
            }

    # 2. Hourly rate extraction
    for m in HOURLY_PATTERN.finditer(text):
        if is_funding_false_positive(text, m.start(), m.end()):
            continue
        curr, fx = _detect_currency(m)
        raw_min = parse_num(m.group("min"))
        raw_max = parse_num(m.group("max")) if m.group("max") else raw_min
        min_usd = raw_min * fx
        max_usd = raw_max * fx

        comp = {
            "rate_type": "hourly",
            "min_amount": min_usd,
            "max_amount": max_usd,
            "raw_min_amount": raw_min,
            "raw_max_amount": raw_max,
            "currency": curr,
            "raw_match": m.group(0).strip(),
        }
        passed, reason, meta = evaluate_high_value_filter(
            comp, min_fixed_usd=min_fixed_usd, min_hourly_usd=min_hourly_usd, range_strategy=strategy
        )
        badge = format_budget_badge("hourly", min_usd, max_usd, curr)
        return {
            **comp,
            "is_high_ticket": passed,
            "budget_badge": badge,
            "status": reason,
        }

    # 3. Annual salary extraction
    for m in ANNUAL_PATTERN.finditer(text):
        if is_funding_false_positive(text, m.start(), m.end()):
            continue
        curr, fx = _detect_currency(m)
        raw_min = parse_num(m.group("min"))
        raw_max = parse_num(m.group("max")) if m.group("max") else raw_min
        min_usd = raw_min * fx
        max_usd = raw_max * fx

        comp = {
            "rate_type": "annual",
            "min_amount": min_usd,
            "max_amount": max_usd,
            "raw_min_amount": raw_min,
            "raw_max_amount": raw_max,
            "currency": curr,
            "raw_match": m.group(0).strip(),
        }
        passed, reason, meta = evaluate_high_value_filter(
            comp, min_fixed_usd=min_fixed_usd, min_hourly_usd=min_hourly_usd, range_strategy=strategy
        )
        equiv_hourly = meta.get("equiv_hourly", max_usd / 2000.0)
        badge = format_budget_badge("annual", min_usd, max_usd, curr, equiv_hourly=equiv_hourly)
        return {
            **comp,
            "is_high_ticket": passed,
            "equiv_hourly": equiv_hourly,
            "budget_badge": badge,
            "status": reason,
        }

    # 4. Monthly retainer extraction
    for m in MONTHLY_PATTERN.finditer(text):
        if is_funding_false_positive(text, m.start(), m.end()):
            continue
        curr, fx = _detect_currency(m)
        raw_min = parse_num(m.group("min"))
        raw_max = parse_num(m.group("max")) if m.group("max") else raw_min
        min_usd = raw_min * fx
        max_usd = raw_max * fx

        comp = {
            "rate_type": "monthly",
            "min_amount": min_usd,
            "max_amount": max_usd,
            "raw_min_amount": raw_min,
            "raw_max_amount": raw_max,
            "currency": curr,
            "raw_match": m.group(0).strip(),
        }
        passed, reason, meta = evaluate_high_value_filter(
            comp, min_fixed_usd=min_fixed_usd, min_hourly_usd=min_hourly_usd, range_strategy=strategy
        )
        badge = format_budget_badge("monthly", min_usd, max_usd, curr)
        return {
            **comp,
            "is_high_ticket": passed,
            "equiv_hourly": meta.get("equiv_hourly", max_usd / 160.0),
            "budget_badge": badge,
            "status": reason,
        }

    # 5. Fixed budget extraction (prefix currency/keyword)
    for m in FIXED_PREFIX_PATTERN.finditer(text):
        if is_funding_false_positive(text, m.start(), m.end()):
            continue
        curr, fx = _detect_currency(m)
        raw_min = parse_num(m.group("min"))
        raw_max = parse_num(m.group("max")) if m.group("max") else raw_min
        min_usd = raw_min * fx
        max_usd = raw_max * fx

        if is_outlier_funding_amount(max_usd, text):
            continue

        comp = {
            "rate_type": "fixed",
            "min_amount": min_usd,
            "max_amount": max_usd,
            "raw_min_amount": raw_min,
            "raw_max_amount": raw_max,
            "currency": curr,
            "raw_match": m.group(0).strip(),
        }
        passed, reason, meta = evaluate_high_value_filter(
            comp, min_fixed_usd=min_fixed_usd, min_hourly_usd=min_hourly_usd, range_strategy=strategy
        )
        badge = format_budget_badge("fixed", min_usd, max_usd, curr)
        return {
            **comp,
            "is_high_ticket": passed,
            "budget_badge": badge,
            "status": reason,
        }

    # 6. Fixed budget extraction (suffix currency: e.g. "5000 USD")
    for m in FIXED_SUFFIX_PATTERN.finditer(text):
        if is_funding_false_positive(text, m.start(), m.end()):
            continue
        curr, fx = _detect_currency(m)
        raw_min = parse_num(m.group("min"))
        raw_max = parse_num(m.group("max")) if m.group("max") else raw_min
        min_usd = raw_min * fx
        max_usd = raw_max * fx

        if is_outlier_funding_amount(max_usd, text):
            continue

        comp = {
            "rate_type": "fixed",
            "min_amount": min_usd,
            "max_amount": max_usd,
            "raw_min_amount": raw_min,
            "raw_max_amount": raw_max,
            "currency": curr,
            "raw_match": m.group(0).strip(),
        }
        passed, reason, meta = evaluate_high_value_filter(
            comp, min_fixed_usd=min_fixed_usd, min_hourly_usd=min_hourly_usd, range_strategy=strategy
        )
        badge = format_budget_badge("fixed", min_usd, max_usd, curr)
        return {
            **comp,
            "is_high_ticket": passed,
            "budget_badge": badge,
            "status": reason,
        }

    # 7. Check unstated blacklist
    for pattern in UNSTATED_PATTERNS:
        if re.search(pattern, text_lower):
            return {
                "rate_type": "unknown",
                "min_amount": 0.0,
                "max_amount": 0.0,
                "raw_min_amount": 0.0,
                "raw_max_amount": 0.0,
                "currency": "USD",
                "is_high_ticket": False,
                "budget_badge": "❓ UNSTATED",
                "status": "REJECT_UNSTATED_COMPENSATION",
                "raw_match": "",
            }

    # 8. Unstated default
    return {
        "rate_type": "unknown",
        "min_amount": 0.0,
        "max_amount": 0.0,
        "raw_min_amount": 0.0,
        "raw_max_amount": 0.0,
        "currency": "USD",
        "is_high_ticket": False,
        "budget_badge": "❓ UNSTATED",
        "status": "UNSTATED",
        "raw_match": "",
    }
