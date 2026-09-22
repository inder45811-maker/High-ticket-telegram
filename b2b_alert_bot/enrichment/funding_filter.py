"""Contextual funding false-positive filter for B2B contract enrichment.

Prevents company capital raises (e.g. '$2M seed round', 'Series A $10M funding',
'$5M ARR') from matching as project or job compensation.
"""

import re
from typing import List, Optional

# Curated set of keywords indicating investment rounds, corporate valuation,
# venture funding, or company-level revenue rather than contract compensation.
FUNDING_KEYWORDS: List[str] = [
    "raised",
    "raising",
    "seed",
    "series a",
    "series b",
    "series c",
    "series d",
    "series",
    "funding",
    "valuation",
    "arr",
    "mrr",
    "backed by",
    "venture capital",
    "venture fund",
    "capital raise",
    "total funding",
]

# Markers that explicitly confirm a high dollar figure is a bona-fide contract budget
EXPLICIT_CONTRACT_MARKERS: List[str] = [
    "contract",
    "budget",
    "fixed price",
    "fixed-price",
    "project fee",
    "milestone fee",
    "stipend",
    "retainer",
    "bounty",
]


def is_funding_false_positive(
    text: str,
    match_start: int,
    match_end: int,
    window_size: int = 50,
    funding_keywords: Optional[List[str]] = None,
) -> bool:
    """Scan a contextual exclusion window around a matched amount for funding terms.

    Args:
        text: The full text being searched (job title or description).
        match_start: Character index where the numeric match begins.
        match_end: Character index where the numeric match ends.
        window_size: Number of characters before and after the match to examine (default: 50).
        funding_keywords: Optional custom list of funding terms (defaults to FUNDING_KEYWORDS).

    Returns:
        True if any funding term appears in the window, False otherwise.
    """
    if not text:
        return False

    # 1. If immediate context contains an explicit contract marker, it is a valid project budget
    imm_start = max(0, match_start - 30)
    imm_end = min(len(text), match_end + 30)
    imm_window = text[imm_start:imm_end].lower()
    for marker in EXPLICIT_CONTRACT_MARKERS:
        if re.search(rf"\b{re.escape(marker)}\b", imm_window):
            return False

    start = max(0, match_start - window_size)
    end = min(len(text), match_end + window_size)

    # 2. Restrict window to sentence boundaries so prior sentences do not contaminate
    pre_text = text[start:match_start]
    post_text = text[match_end:end]

    for punct in [". ", "!\n", "?\n", ".\n", "! ", "? "]:
        idx = pre_text.rfind(punct)
        if idx != -1:
            pre_text = pre_text[idx + len(punct):]
        idx_post = post_text.find(punct)
        if idx_post != -1:
            post_text = post_text[:idx_post]

    window = (pre_text + text[match_start:match_end] + post_text).lower()

    keywords = funding_keywords if funding_keywords is not None else FUNDING_KEYWORDS

    for kw in keywords:
        pattern = rf"\b{re.escape(kw)}\b"
        if re.search(pattern, window):
            return True

    return False


def is_outlier_funding_amount(
    amount_usd: float,
    text: str,
    ceiling: float = 250000.0,
) -> bool:
    """Detect if an amount exceeds reasonable freelance project ceiling without explicit contract markers.

    Gigs rarely exceed $250k fixed without explicit contract framing; amounts above this
    are overwhelmingly company funding announcements misparsed from unstructured text.

    Args:
        amount_usd: The normalized USD value.
        text: The surrounding context or full post text.
        ceiling: The outlier threshold ceiling in USD (default: $250,000).

    Returns:
        True if the amount exceeds ceiling and lacks contract markers, False otherwise.
    """
    if amount_usd <= ceiling:
        return False

    text_lower = text.lower()
    for marker in EXPLICIT_CONTRACT_MARKERS:
        if marker in text_lower:
            return False

    return True
