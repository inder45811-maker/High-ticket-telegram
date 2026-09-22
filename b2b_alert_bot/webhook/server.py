"""Lightweight HTTP webhook server for Whop and health checks.

Endpoints:
- POST /webhooks/whop: Ingests Whop subscription webhook events, verifies HMAC signatures,
  and provisions/revokes member access. Responds with HTTP 200 on success, HTTP 401 on invalid signature.
- GET /health: Health check probe returning HTTP 200 and operational status.
- GET /ready: Readiness probe checking database connectivity and operational state.
"""

import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple, Type
from urllib.parse import urlparse

from b2b_alert_bot.webhook.handler import WhopWebhookHandler

logger = logging.getLogger(__name__)


class WebhookRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler routing /webhooks/whop, /health, and /ready."""

    # Handlers injected by server instance
    webhook_handler: WhopWebhookHandler

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress default stderr logging for clean test runs."""
        logger.debug("%s - - [%s] %s", self.client_address[0], self.log_date_time_string(), format % args)

    def _send_json_response(self, status_code: int, data: Dict[str, Any]) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed_path = urlparse(self.path).path.rstrip("/")

        if parsed_path in ("/health", ""):
            self._send_json_response(200, {
                "status": "healthy",
                "service": "b2b_alert_bot_webhook",
                "version": "1.0.0",
            })
        elif parsed_path == "/ready":
            # Check database responsiveness
            db_ok = True
            try:
                if hasattr(self.webhook_handler, "store"):
                    with self.webhook_handler.store._get_connection() as conn:
                        conn.execute("SELECT 1;").fetchone()
            except Exception as e:
                logger.error("Readiness probe database check failed: %s", e)
                db_ok = False

            status_code = 200 if db_ok else 503
            self._send_json_response(status_code, {
                "status": "ready" if db_ok else "unhealthy",
                "database": "connected" if db_ok else "error",
            })
        else:
            self._send_json_response(404, {"error": "Not Found", "path": self.path})

    def do_POST(self) -> None:
        parsed_path = urlparse(self.path).path.rstrip("/")

        if parsed_path != "/webhooks/whop":
            self._send_json_response(404, {"error": "Not Found", "path": self.path})
            return

        # Read request body
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except (ValueError, TypeError):
            content_length = 0

        raw_body = self.rfile.read(content_length) if content_length > 0 else b""

        # Extract headers dict
        headers = dict(self.headers)

        # Process through webhook handler
        status_code, response_data = self.webhook_handler.process_webhook(raw_body, headers)
        self._send_json_response(status_code, response_data)


class WhopWebhookServer:
    """Configurable HTTP server instance for running the Whop webhook daemon."""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8080,
        handler: Optional[WhopWebhookHandler] = None,
        db_path: str = "subscribers.db",
        secret: Optional[str] = None,
        bot_token: Optional[str] = None,
        channel_id: Optional[str] = None,
    ):
        self.host = host
        self.port = port
        self.handler = handler or WhopWebhookHandler(
            db_path=db_path,
            secret=secret,
            bot_token=bot_token,
            channel_id=channel_id,
        )

        # Create handler class bound to this server's webhook handler
        handler_instance = self.handler

        class BoundRequestHandler(WebhookRequestHandler):
            webhook_handler = handler_instance

        self._server = ThreadingHTTPServer((self.host, self.port), BoundRequestHandler)
        # In case port 0 was passed, capture actual bound port
        self.actual_port = self._server.server_address[1]
        self._thread: Optional[threading.Thread] = None

    def start(self, background: bool = True) -> None:
        """Start the HTTP server. If background=True, runs in a daemon thread."""
        if background:
            self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
            self._thread.start()
            logger.info("WhopWebhookServer started in background at http://%s:%s", self.host, self.actual_port)
        else:
            logger.info("WhopWebhookServer running on http://%s:%s", self.host, self.actual_port)
            self._server.serve_forever()

    def stop(self) -> None:
        """Shut down the HTTP server cleanly."""
        try:
            self._server.shutdown()
            self._server.server_close()
            if self._thread and self._thread.is_alive():
                self._thread.join(timeout=2.0)
            logger.info("WhopWebhookServer stopped successfully")
        except Exception as e:
            logger.warning("Error during WhopWebhookServer shutdown: %s", e)

    def get_url(self) -> str:
        """Return base URL for client testing."""
        bind_host = "127.0.0.1" if self.host in ("0.0.0.0", "") else self.host
        return f"http://{bind_host}:{self.actual_port}"

    def __enter__(self) -> "WhopWebhookServer":
        self.start(background=True)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()


def create_webhook_app(
    host: str = "0.0.0.0",
    port: int = 8080,
    handler: Optional[WhopWebhookHandler] = None,
    **kwargs: Any,
) -> WhopWebhookServer:
    """Factory helper creating a configured WhopWebhookServer."""
    return WhopWebhookServer(host=host, port=port, handler=handler, **kwargs)


def run_webhook_server(
    host: str = "0.0.0.0",
    port: int = 8080,
    handler: Optional[WhopWebhookHandler] = None,
    **kwargs: Any,
) -> None:
    """CLI runner blocking call for daemon deployment."""
    server = create_webhook_app(host=host, port=port, handler=handler, **kwargs)
    server.start(background=False)
