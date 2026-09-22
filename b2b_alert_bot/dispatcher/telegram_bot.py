"""Telegram Alert Dispatcher.

Features:
- Offline dry-run mode (activated when TELEGRAM_BOT_TOKEN is unset or DRY_RUN=True)
- One-click apply button: InlineKeyboardMarkup with url button pointing to canonical apply link
- Link preview suppression (link_preview_options: {"is_disabled": true})
- Content protection flag (protect_content: true)
- Token bucket rate-limiting (1 msg/s chat, 30 msgs/s global)
- Dispatch queue with exponential backoff on 5xx errors and retry on HTTP 429 retry_after
"""

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
import random
import re
import time
from typing import Any, Dict, List, Optional, Union

import requests

from b2b_alert_bot.dispatcher.formatter import (
    create_apply_markup,
    format_deal_card,
    get_link_preview_options,
)
from b2b_alert_bot.dispatcher.rate_limiter import TelegramRateLimiter
from b2b_alert_bot.schema import EnrichedLead, Lead

logger = logging.getLogger(__name__)


class DispatchResult(dict):
    """Result of a dispatch operation, behaving as a dict and a boolean."""

    def __init__(
        self,
        ok: bool,
        status: str,
        lead_id: str = "",
        telegram_message_id: Optional[int] = None,
        dispatched_at: Optional[str] = None,
        retry_count: int = 0,
        error_message: Optional[str] = None,
        raw_response: Optional[Dict[str, Any]] = None
    ):
        super().__init__(
            ok=ok,
            status=status,
            lead_id=lead_id,
            telegram_message_id=telegram_message_id,
            dispatched_at=dispatched_at or datetime.now(timezone.utc).isoformat(),
            retry_count=retry_count,
            error_message=error_message,
            raw_response=raw_response or {}
        )

    @property
    def ok(self) -> bool:
        return bool(self.get("ok", False))

    @property
    def status(self) -> str:
        return str(self.get("status", "unknown"))

    def __bool__(self) -> bool:
        return self.ok


class TelegramDispatcher:
    """Dispatches high-ticket deal alerts to Telegram channels."""

    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[Union[str, int]] = None,
        dry_run: Optional[bool] = None,
        rate_limiter: Optional[TelegramRateLimiter] = None,
        max_retries: int = 5,
        timeout: float = 10.0,
        protect_content: Optional[bool] = None,
        api_base_url: str = "https://api.telegram.org",
        session: Optional[requests.Session] = None
    ):
        """Initialize TelegramDispatcher.

        Args:
            bot_token: Telegram Bot API token. If omitted, reads TELEGRAM_BOT_TOKEN env var.
            chat_id: Target Telegram channel/group chat ID. If omitted, reads TELEGRAM_CHAT_ID.
            dry_run: If True, bypasses network calls. Defaults to True if token is missing
                     or DRY_RUN=true env var is set.
            rate_limiter: TelegramRateLimiter instance (creates default if None).
            max_retries: Maximum attempts on 429 or 5xx failures.
            timeout: HTTP timeout in seconds.
            protect_content: Whether to restrict message forwarding.
            api_base_url: Base Telegram Bot API URL.
            session: Optional requests.Session instance for connection pooling / testing.
        """
        self.bot_token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "").strip()

        # Resolve dry-run flag
        env_dry_run = os.environ.get("DRY_RUN", "").strip().lower() in ("true", "1", "yes")
        if dry_run is not None:
            self.dry_run = bool(dry_run)
        elif env_dry_run or not self.bot_token:
            self.dry_run = True
        else:
            self.dry_run = False

        env_protect = os.environ.get("PROTECT_CONTENT", "").strip().lower() in ("true", "1", "yes")
        self.protect_content = env_protect if protect_content is None else bool(protect_content)

        self.rate_limiter = rate_limiter or TelegramRateLimiter()
        self.max_retries = max_retries
        self.timeout = timeout
        self.api_base_url = api_base_url.rstrip("/")
        self.session = session or requests.Session()

        # Telemetry & Mock State
        self.sent_messages: List[Dict[str, Any]] = []
        self.call_history: List[Dict[str, Any]] = []
        self._message_counter = 1000

    def format_deal_card(
        self,
        lead: Union[EnrichedLead, Dict[str, Any]],
        relative_time: Optional[str] = None
    ) -> str:
        """Format an enriched lead into mobile-optimized HTML deal card."""
        return format_deal_card(lead, relative_time=relative_time)

    def create_apply_markup(
        self,
        url: str,
        text: str = "🚀 Apply on Source"
    ) -> Optional[Dict[str, Any]]:
        """Generate InlineKeyboardMarkup pointing directly to apply link."""
        return create_apply_markup(url, text=text)

    def _validate_mock_payload(
        self,
        chat_id: Union[str, int],
        text: str,
        parse_mode: str = "HTML",
        reply_markup: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        """Perform offline validation matching Telegram API constraints."""
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

        # Tag balance check
        if parse_mode == "HTML":
            tags_to_check = ["b", "strong", "i", "em", "code", "pre", "a", "u", "s"]
            for tag in tags_to_check:
                opens = len(re.findall(rf"<{tag}(?:\s+[^>]*)*>", text, flags=re.IGNORECASE))
                closes = len(re.findall(rf"</{tag}>", text, flags=re.IGNORECASE))
                if opens != closes:
                    return {
                        "ok": False,
                        "error_code": 400,
                        "description": f"Can't parse entities: unclosed or mismatched HTML tag <{tag}>"
                    }

        # Inline keyboard URL check
        if reply_markup and "inline_keyboard" in reply_markup:
            for row in reply_markup.get("inline_keyboard", []):
                for btn in row:
                    btn_url = btn.get("url", "")
                    if btn_url and not (btn_url.startswith("http://") or btn_url.startswith("https://")):
                        return {
                            "ok": False,
                            "error_code": 400,
                            "description": f"BUTTON_URL_INVALID: '{btn_url}' must be http or https"
                        }

        return None

    def send_message_sync(
        self,
        chat_id: Union[str, int],
        text: str,
        parse_mode: str = "HTML",
        reply_markup: Optional[Dict[str, Any]] = None,
        link_preview_options: Optional[Dict[str, Any]] = None,
        protect_content: Optional[bool] = None
    ) -> Dict[str, Any]:
        """Send message synchronously with rate limiting and retry backoff."""
        target_chat = chat_id or self.chat_id
        if not target_chat:
            raise ValueError("chat_id must be provided or configured via TELEGRAM_CHAT_ID")

        options = link_preview_options if link_preview_options is not None else get_link_preview_options()
        protect = self.protect_content if protect_content is None else protect_content

        # Record call history
        call_entry = {
            "method": "sendMessage",
            "params": {
                "chat_id": target_chat,
                "text": text,
                "parse_mode": parse_mode,
                "link_preview_options": options,
                "reply_markup": reply_markup,
                "protect_content": protect
            }
        }
        self.call_history.append(call_entry)

        # Rate Limiting
        if self.rate_limiter:
            self.rate_limiter.acquire(target_chat)

        # --- Dry Run Mode ---
        if self.dry_run:
            validation_error = self._validate_mock_payload(target_chat, text, parse_mode, reply_markup)
            if validation_error:
                return validation_error

            self._message_counter += 1
            msg_payload = {
                "message_id": self._message_counter,
                "chat": {"id": target_chat, "type": "channel", "title": "B2B High-Ticket Deals Hub"},
                "date": int(time.time()),
                "text": text,
                "reply_markup": reply_markup,
                "entities": []
            }
            self.sent_messages.append(msg_payload)
            return {"ok": True, "result": msg_payload}

        # --- Real Network Mode with Retries ---
        url = f"{self.api_base_url}/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": target_chat,
            "text": text,
            "parse_mode": parse_mode,
            "link_preview_options": options
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        if protect:
            payload["protect_content"] = True

        last_error = None
        for attempt in range(self.max_retries):
            try:
                resp = self.session.post(url, json=payload, timeout=self.timeout)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("ok"):
                        self.sent_messages.append(data.get("result", {}))
                        return data
                    return data

                elif resp.status_code == 429:
                    try:
                        err_data = resp.json()
                    except Exception:
                        err_data = {}
                    retry_after = err_data.get("parameters", {}).get("retry_after", 1)
                    logger.warning(
                        "Telegram rate limit hit (429). Retrying in %s seconds (attempt %s/%s)",
                        retry_after, attempt + 1, self.max_retries
                    )
                    time.sleep(retry_after + 1.0)
                    continue

                elif 500 <= resp.status_code < 600:
                    backoff = min(32.0, (2 ** attempt) + random.uniform(0.1, 0.5))
                    logger.warning(
                        "Telegram server error (%s). Backing off %0.2fs (attempt %s/%s)",
                        resp.status_code, backoff, attempt + 1, self.max_retries
                    )
                    time.sleep(backoff)
                    continue

                else:
                    # Client errors (400, 403, etc.)
                    try:
                        return resp.json()
                    except Exception:
                        return {"ok": False, "error_code": resp.status_code, "description": resp.text}

            except (requests.exceptions.RequestException, RuntimeError) as e:
                last_error = str(e)
                backoff = min(32.0, (2 ** attempt) + random.uniform(0.1, 0.5))
                logger.warning(
                    "Network error during Telegram dispatch: %s. Backing off %0.2fs (attempt %s/%s)",
                    e, backoff, attempt + 1, self.max_retries
                )
                time.sleep(backoff)

        return {
            "ok": False,
            "error_code": 500,
            "description": f"Failed after {self.max_retries} attempts: {last_error}"
        }

    async def send_message(
        self,
        chat_id: Union[str, int],
        text: str,
        parse_mode: str = "HTML",
        reply_markup: Optional[Dict[str, Any]] = None,
        link_preview_options: Optional[Dict[str, Any]] = None,
        protect_content: Optional[bool] = None
    ) -> Dict[str, Any]:
        """Send message asynchronously with rate limiting and retry backoff."""
        return await asyncio.to_thread(
            self.send_message_sync,
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            link_preview_options=link_preview_options,
            protect_content=protect_content
        )

    def dispatch_sync(
        self,
        lead: Union[EnrichedLead, Dict[str, Any]],
        chat_id: Optional[Union[str, int]] = None
    ) -> DispatchResult:
        """Format and dispatch an enriched deal card synchronously.

        Returns:
            DispatchResult containing dispatch metadata, status, and boolean truth value.
        """
        target_chat = chat_id or self.chat_id
        if not target_chat:
            raise ValueError("Target chat_id must be provided or configured via TELEGRAM_CHAT_ID")

        lead_id = ""
        url = ""
        source = ""
        if isinstance(lead, EnrichedLead):
            lead_id = lead.lead.id
            url = lead.lead.url
            source = lead.lead.source
        elif isinstance(lead, dict):
            lead_inner = lead.get("lead", lead)
            if isinstance(lead_inner, Lead):
                lead_id = lead_inner.id
                url = lead_inner.url
                source = lead_inner.source
            elif isinstance(lead_inner, dict):
                lead_id = lead_inner.get("id", "")
                url = lead_inner.get("url", "")
                source = lead_inner.get("source", "")

        text = self.format_deal_card(lead)
        markup = create_apply_markup(url, source=source) if url else None

        res = self.send_message_sync(
            chat_id=target_chat,
            text=text,
            parse_mode="HTML",
            reply_markup=markup,
            link_preview_options=get_link_preview_options()
        )

        ok = bool(res.get("ok", False))
        status = "dry_run" if (ok and self.dry_run) else ("delivered" if ok else "failed")
        msg_id = res.get("result", {}).get("message_id") if ok else None
        err_msg = res.get("description") if not ok else None

        return DispatchResult(
            ok=ok,
            status=status,
            lead_id=lead_id,
            telegram_message_id=msg_id,
            error_message=err_msg,
            raw_response=res
        )

    async def dispatch(
        self,
        lead: Union[EnrichedLead, Dict[str, Any]],
        chat_id: Optional[Union[str, int]] = None
    ) -> DispatchResult:
        """Format and dispatch an enriched deal card asynchronously.

        Returns:
            DispatchResult containing dispatch metadata, status, and boolean truth value.
        """
        return await asyncio.to_thread(self.dispatch_sync, lead, chat_id=chat_id)


class DispatchQueue:
    """In-memory dispatch queue with retry processing and dead-letter queue (DLQ)."""

    def __init__(self, dispatcher: TelegramDispatcher):
        self.dispatcher = dispatcher
        self.queue: List[Dict[str, Any]] = []
        self.dead_letter_queue: List[Dict[str, Any]] = []
        self.history: List[DispatchResult] = []

    def enqueue(
        self,
        lead: Union[EnrichedLead, Dict[str, Any]],
        chat_id: Optional[Union[str, int]] = None
    ) -> int:
        """Add an enriched lead to the dispatch queue."""
        self.queue.append({
            "lead": lead,
            "chat_id": chat_id,
            "enqueued_at": time.time()
        })
        return len(self.queue)

    def process_all_sync(self) -> List[DispatchResult]:
        """Flush the dispatch queue synchronously."""
        results = []
        while self.queue:
            item = self.queue.pop(0)
            res = self.dispatcher.dispatch_sync(item["lead"], chat_id=item["chat_id"])
            results.append(res)
            self.history.append(res)
            if not res.ok:
                self.dead_letter_queue.append({
                    "item": item,
                    "result": res,
                    "failed_at": time.time()
                })
        return results

    async def process_all(self) -> List[DispatchResult]:
        """Flush the dispatch queue asynchronously."""
        return await asyncio.to_thread(self.process_all_sync)
