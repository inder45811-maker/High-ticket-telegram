"""Telegram alert dispatcher package for B2B Alert Bot."""

from b2b_alert_bot.dispatcher.formatter import (
    TELEGRAM_MAX_MESSAGE_LENGTH,
    TelegramFormatter,
    create_apply_markup,
    format_budget_badge,
    format_deal_card,
    get_link_preview_options,
    truncate_html,
)
from b2b_alert_bot.dispatcher.rate_limiter import (
    TelegramRateLimiter,
    TokenBucket,
)
from b2b_alert_bot.dispatcher.telegram_bot import (
    DispatchQueue,
    DispatchResult,
    TelegramDispatcher,
)

__all__ = [
    "TELEGRAM_MAX_MESSAGE_LENGTH",
    "TelegramFormatter",
    "format_deal_card",
    "format_budget_badge",
    "create_apply_markup",
    "truncate_html",
    "get_link_preview_options",
    "TokenBucket",
    "TelegramRateLimiter",
    "TelegramDispatcher",
    "DispatchResult",
    "DispatchQueue",
]
