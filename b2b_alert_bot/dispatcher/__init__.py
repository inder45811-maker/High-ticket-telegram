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

from b2b_alert_bot.dispatcher.linkedin_autopublisher import (
    LinkedInAutoPublisher,
    format_deal_teaser,
)

from b2b_alert_bot.dispatcher.x_publisher import (
    XAutoPublisher,
    format_x_deal_teaser,
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
    "LinkedInAutoPublisher",
    "format_deal_teaser",
    "XAutoPublisher",
    "format_x_deal_teaser",
]
