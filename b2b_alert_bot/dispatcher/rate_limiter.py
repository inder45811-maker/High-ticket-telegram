"""Token bucket rate limiter enforcing Telegram Bot API limits.

Telegram limits:
- Per chat/channel limit: max 1 message per second
- Global bot limit: max 30 messages per second across all chats
"""

import asyncio
import threading
import time
from typing import Dict, Optional, Union


class TokenBucket:
    """Thread-safe token bucket algorithm implementation.

    Tokens refill continuously at `rate` tokens per second up to `capacity`.
    """

    def __init__(
        self,
        rate: float = 1.0,
        capacity: float = 1.0,
        initial_tokens: Optional[float] = None
    ):
        """Initialize token bucket.

        Args:
            rate: Token refill rate in tokens per second.
            capacity: Maximum token capacity.
            initial_tokens: Starting tokens (defaults to capacity).
        """
        self.rate = float(rate)
        self.capacity = float(capacity)
        self.tokens = float(initial_tokens if initial_tokens is not None else capacity)
        self.last_update = time.time()
        self._lock = threading.Lock()

    def _refill(self, now: Optional[float] = None) -> None:
        """Internal token replenishment based on elapsed time."""
        if now is None:
            now = time.time()
        elapsed = now - self.last_update
        if elapsed > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            self.last_update = now

    def consume(self, tokens: float = 1.0) -> float:
        """Attempt to consume tokens.

        Returns:
            0.0 if tokens were available and consumed immediately.
            Float > 0.0 representing the wait time in seconds needed before tokens can be consumed.
        """
        with self._lock:
            now = time.time()
            self._refill(now)
            if self.tokens >= tokens:
                self.tokens -= tokens
                return 0.0
            return (tokens - self.tokens) / self.rate

    def acquire(self, tokens: float = 1.0) -> float:
        """Synchronously block until tokens are available, then consume.

        Returns:
            Total seconds waited.
        """
        total_waited = 0.0
        while True:
            wait_time = self.consume(tokens)
            if wait_time <= 0.0:
                return total_waited
            time.sleep(wait_time)
            total_waited += wait_time

    async def acquire_async(self, tokens: float = 1.0) -> float:
        """Asynchronously wait until tokens are available, then consume.

        Returns:
            Total seconds waited.
        """
        total_waited = 0.0
        while True:
            wait_time = self.consume(tokens)
            if wait_time <= 0.0:
                return total_waited
            await asyncio.sleep(wait_time)
            total_waited += wait_time

    def reset(self) -> None:
        """Reset tokens to capacity."""
        with self._lock:
            self.tokens = self.capacity
            self.last_update = time.time()


class TelegramRateLimiter:
    """Composite rate limiter enforcing both per-chat and global Telegram limits."""

    def __init__(
        self,
        chat_rate: float = 1.0,
        chat_capacity: float = 1.0,
        global_rate: float = 30.0,
        global_capacity: float = 30.0
    ):
        """Initialize composite rate limiter.

        Args:
            chat_rate: Per-chat allowed rate (default 1.0 msg/s).
            chat_capacity: Per-chat bucket burst capacity (default 1.0).
            global_rate: Global allowed rate (default 30.0 msgs/s).
            global_capacity: Global bucket burst capacity (default 30.0).
        """
        self.chat_rate = float(chat_rate)
        self.chat_capacity = float(chat_capacity)
        self.global_rate = float(global_rate)
        self.global_capacity = float(global_capacity)

        self.global_bucket = TokenBucket(rate=self.global_rate, capacity=self.global_capacity)
        self._chat_buckets: Dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def get_chat_bucket(self, chat_id: Union[str, int]) -> TokenBucket:
        """Retrieve or create a token bucket for a specific chat ID."""
        key = str(chat_id)
        with self._lock:
            if key not in self._chat_buckets:
                self._chat_buckets[key] = TokenBucket(
                    rate=self.chat_rate,
                    capacity=self.chat_capacity
                )
            return self._chat_buckets[key]

    def consume(self, chat_id: Union[str, int]) -> float:
        """Check wait time for both global and per-chat limits.

        If both buckets have at least 1.0 token, consumes 1.0 token from each and returns 0.0.
        Otherwise, returns the maximum wait time required without consuming tokens.
        """
        chat_bucket = self.get_chat_bucket(chat_id)
        with self._lock:
            now = time.time()
            chat_bucket._refill(now)
            self.global_bucket._refill(now)

            chat_wait = 0.0 if chat_bucket.tokens >= 1.0 else (1.0 - chat_bucket.tokens) / chat_bucket.rate
            global_wait = 0.0 if self.global_bucket.tokens >= 1.0 else (1.0 - self.global_bucket.tokens) / self.global_bucket.rate
            max_wait = max(chat_wait, global_wait)

            if max_wait <= 0.0:
                chat_bucket.tokens -= 1.0
                self.global_bucket.tokens -= 1.0
                return 0.0
            return max_wait

    def acquire(self, chat_id: Union[str, int]) -> float:
        """Synchronously wait until both per-chat and global limits allow dispatch."""
        total_waited = 0.0
        while True:
            wait_time = self.consume(chat_id)
            if wait_time <= 0.0:
                return total_waited
            time.sleep(wait_time)
            total_waited += wait_time

    async def acquire_async(self, chat_id: Union[str, int]) -> float:
        """Asynchronously wait until both per-chat and global limits allow dispatch."""
        total_waited = 0.0
        while True:
            wait_time = self.consume(chat_id)
            if wait_time <= 0.0:
                return total_waited
            await asyncio.sleep(wait_time)
            total_waited += wait_time

    def reset(self) -> None:
        """Reset all rate limiter states."""
        with self._lock:
            self.global_bucket.reset()
            self._chat_buckets.clear()
