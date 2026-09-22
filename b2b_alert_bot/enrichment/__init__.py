"""B2B Alert Bot AI Enrichment and High-Value Filtering Package."""

from b2b_alert_bot.enrichment.compensation import (
    parse_compensation,
    evaluate_high_value_filter,
    format_budget_badge,
    FX_RATES,
)
from b2b_alert_bot.enrichment.funding_filter import is_funding_false_positive
from b2b_alert_bot.enrichment.deal_card import generate_deal_card, DealCardGenerator
from b2b_alert_bot.enrichment.engine import EnrichmentEngine

__all__ = [
    "parse_compensation",
    "evaluate_high_value_filter",
    "format_budget_badge",
    "FX_RATES",
    "is_funding_false_positive",
    "generate_deal_card",
    "DealCardGenerator",
    "EnrichmentEngine",
]
