"""Unified CLI and daemon service for ApexRadar — Real-Time B2B Contract Intelligence.

Commands:
- poll: Ingests from 5 connectors (WWR, RemoteOK, Jobspresso, HN, Reddit),
        filters with EnrichmentEngine (>= $2,000 fixed/mo or >= $50/hr),
        deduplicates in Database, formats mobile deal cards, and dispatches
        via TelegramDispatcher. Supports --dry-run.
- daemon: Runs the poll cycle periodically (default 30m, configurable) with
          signal handling for graceful shutdown.
- webhook: Runs the Whop webhook server on --host and --port.
- all / serve: Concurrently runs both the webhook server and scheduled polling loop.
- status: Prints database statistics (total ingested, high-ticket count, dispatched count, active subscribers).
"""

import argparse
from collections import OrderedDict
from datetime import datetime, timezone
import json
import logging
import os
import signal
import sqlite3
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Ensure package root is in sys.path if run directly as script
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# Automatically load environment variables from .env if present
def _load_dotenv() -> None:
    for env_path in [
        os.path.join(os.getcwd(), ".env"),
        os.path.join(project_root, ".env"),
        os.path.expanduser("~/.env"),
        "/root/.env",
    ]:
        if os.path.isfile(env_path):
            try:
                with open(env_path, "r", encoding="utf-8") as f:
                    for line in f:
                        clean_line = line.strip()
                        if clean_line and not clean_line.startswith("#") and "=" in clean_line:
                            k, v = clean_line.split("=", 1)
                            k = k.strip()
                            v = v.strip().strip('"').strip("'")
                            if k and k not in os.environ:
                                os.environ[k] = v
            except Exception:
                pass
            break

_load_dotenv()

from b2b_alert_bot.db import Database
from b2b_alert_bot.dispatcher.telegram_bot import TelegramDispatcher
from b2b_alert_bot.dispatcher.linkedin_autopublisher import LinkedInAutoPublisher
from b2b_alert_bot.dispatcher.x_publisher import XAutoPublisher
from b2b_alert_bot.enrichment.engine import EnrichmentEngine
from b2b_alert_bot.ingestion.base import BaseConnector
from b2b_alert_bot.ingestion.hackernews import HackerNewsConnector
from b2b_alert_bot.ingestion.jobspresso import JobspressoConnector
from b2b_alert_bot.ingestion.reddit import RedditConnector
from b2b_alert_bot.ingestion.remoteok import RemoteOKConnector
from b2b_alert_bot.ingestion.weworkremotely import WeWorkRemotelyConnector
from b2b_alert_bot.schema import EnrichedLead, Lead
from b2b_alert_bot.webhook.handler import SubscriberStore
from b2b_alert_bot.webhook.server import WhopWebhookServer, run_webhook_server

logger = logging.getLogger("b2b_alert_bot")

DEFAULT_CONNECTORS: List[Tuple[str, BaseConnector]] = [
    ("weworkremotely", WeWorkRemotelyConnector()),
    ("remoteok", RemoteOKConnector()),
    ("jobspresso", JobspressoConnector()),
    ("hackernews", HackerNewsConnector()),
    ("reddit", RedditConnector()),
]

FIXTURE_CANDIDATES: Dict[str, List[str]] = {
    "weworkremotely": ["sample_wwr.rss", "wwr_sample.xml"],
    "remoteok": ["sample_remoteok.json", "remoteok_sample.json"],
    "jobspresso": ["sample_jobspresso.rss", "jobspresso_sample.xml"],
    "hackernews": ["sample_hn.json", "hn_algolia_sample.json"],
    "reddit": ["sample_reddit.atom", "reddit_atom_sample.xml"],
}


def _resolve_default_db_path(filename: str = "leads.db") -> str:
    """Resolve default SQLite database path considering DATA_DIR env var."""
    data_dir = os.environ.get("DATA_DIR", "").strip()
    if data_dir:
        os.makedirs(data_dir, exist_ok=True)
        return os.path.join(data_dir, filename)
    return filename


def _resolve_fixture_path(fixtures_dir: Optional[str], source_name: str) -> Optional[str]:
    """Find the first matching offline fixture file for a given source."""
    if not fixtures_dir or not os.path.isdir(fixtures_dir):
        return None
    candidates = FIXTURE_CANDIDATES.get(source_name, [])
    for candidate in candidates:
        full_path = os.path.join(fixtures_dir, candidate)
        if os.path.isfile(full_path):
            return full_path
    return None


# ==============================================================================
# 1. POLL IMPLEMENTATION
# ==============================================================================

def run_poll(
    db_path: Optional[str] = None,
    dry_run: bool = False,
    chat_id: Optional[str] = None,
    bot_token: Optional[str] = None,
    min_fixed_usd: float = 2000.0,
    min_hourly_usd: float = 50.0,
    fixtures_dir: Optional[str] = None,
    sources: Optional[List[str]] = None,
    connectors: Optional[List[Tuple[str, BaseConnector]]] = None,
) -> Dict[str, Any]:
    """Execute a single end-to-end ingestion, enrichment, and dispatch poll cycle.

    Args:
        db_path: Path to SQLite leads database.
        dry_run: If True, executes Telegram dispatch in dry-run simulation mode.
        chat_id: Target Telegram channel/group chat ID.
        bot_token: Telegram bot API token.
        min_fixed_usd: Minimum threshold for fixed/monthly contract budget (USD).
        min_hourly_usd: Minimum threshold for hourly contract rate (USD).
        fixtures_dir: Optional directory with offline sample feed files.
        sources: Optional whitelist filter of sources to query.
        connectors: Optional pre-configured list of connector instances.

    Returns:
        Dictionary of cycle execution metrics and counts.
    """
    resolved_db_path = db_path or os.environ.get("DATABASE_PATH") or _resolve_default_db_path("leads.db")
    db = Database(db_path=resolved_db_path)

    enricher = EnrichmentEngine(
        min_fixed_usd=min_fixed_usd,
        min_hourly_usd=min_hourly_usd,
        range_strategy="max",
    )

    resolved_chat_id = (
        chat_id
        or os.environ.get("TELEGRAM_CHAT_ID")
        or os.environ.get("TELEGRAM_CHANNEL_ID")
        or ("-1001234567890" if dry_run else None)
    )

    dispatcher = TelegramDispatcher(
        bot_token=bot_token,
        chat_id=resolved_chat_id,
        dry_run=dry_run,
    )

    linkedin_publisher = LinkedInAutoPublisher(dry_run=dry_run)
    x_publisher = XAutoPublisher(dry_run=dry_run)

    active_connectors = connectors or DEFAULT_CONNECTORS
    if sources:
        normalized_sources = {s.strip().lower() for s in sources if s.strip()}
        active_connectors = [c for c in active_connectors if c[0].lower() in normalized_sources]

    total_queried_sources = len(active_connectors)
    total_ingested = 0
    total_duplicates = 0
    newly_ingested_leads: List[Lead] = []

    logger.info(
        "Starting ingestion poll cycle across %d sources (db: %s, dry_run: %s)",
        total_queried_sources, resolved_db_path, dispatcher.dry_run
    )

    # 1. Ingest from connectors & deduplicate into SQLite
    for source_name, connector in active_connectors:
        fixture_file = _resolve_fixture_path(fixtures_dir, source_name)
        try:
            if fixture_file:
                logger.debug("Ingesting source '%s' from fixture: %s", source_name, fixture_file)
                leads = connector.fetch(fixture_file)
            else:
                logger.debug("Ingesting source '%s' live from endpoints", source_name)
                leads = connector.fetch()
        except Exception as e:
            logger.warning("Failed to fetch leads for source '%s': %s", source_name, e)
            leads = []

        source_new = 0
        source_dups = 0
        for lead in leads:
            if not lead or not getattr(lead, "id", None):
                continue

            # Fast deduplication pre-check
            if db.is_duplicate(lead.id):
                source_dups += 1
                total_duplicates += 1
                continue

            try:
                inserted = db.insert_lead(lead, status="ingested")
                if inserted:
                    source_new += 1
                    total_ingested += 1
                    newly_ingested_leads.append(lead)
                else:
                    source_dups += 1
                    total_duplicates += 1
            except Exception as e:
                logger.error("Error inserting lead %s: %s", getattr(lead, "id", "unknown"), e)

        logger.info("Source '%s': %d new leads, %d duplicates", source_name, source_new, source_dups)

    # 2. Gather leads that require enrichment & dispatch
    # Include both newly ingested leads and any pending 'ingested' leads in DB
    all_to_process: OrderedDict[str, Lead] = OrderedDict()
    for l in newly_ingested_leads:
        all_to_process[l.id] = l

    try:
        pending_in_db = db.get_leads_by_status("ingested")
        for l in pending_in_db:
            if l.id not in all_to_process:
                all_to_process[l.id] = l
    except Exception as e:
        logger.warning("Could not fetch pending 'ingested' leads: %s", e)

    total_high_ticket = 0
    total_below_threshold = 0
    total_dispatched = 0
    failed_dispatches = 0

    # 3. Enrich, filter (>= $2k or >= $50/hr), and dispatch via Telegram
    for lead_id, lead in all_to_process.items():
        try:
            enriched: EnrichedLead = enricher.enrich_lead(lead)
            if enriched.is_high_ticket:
                total_high_ticket += 1
                db.update_status(lead.id, "enriched")

                # Dispatch formatted deal card
                dispatch_res = dispatcher.dispatch_sync(enriched)
                if dispatch_res.ok:
                    db.update_status(lead.id, "dispatched")
                    total_dispatched += 1
                    logger.info(
                        "DISPATCHED: [%s] %s - %s",
                        enriched.budget_badge, lead.client or "Unknown", lead.title
                    )

                    # Autonomously publish teaser to LinkedIn (subject to algorithmic cooldown)
                    if linkedin_publisher and linkedin_publisher.enabled:
                        try:
                            li_res = linkedin_publisher.maybe_publish_lead(enriched, db)
                            if li_res and li_res.get("ok"):
                                logger.info(
                                    "AUTONOMOUS LINKEDIN POST PUBLISHED: %s",
                                    li_res.get("post_urn")
                                )
                        except Exception as e:
                            logger.warning("LinkedIn autopublish error for lead %s: %s", lead.id, e)

                    # Autonomously publish teaser to X / Twitter
                    if x_publisher and x_publisher.enabled:
                        try:
                            x_res = x_publisher.maybe_publish_lead(enriched, db)
                            if x_res and x_res.get("ok"):
                                logger.info(
                                    "AUTONOMOUS X TWEET PUBLISHED: %s",
                                    x_res.get("tweet_id")
                                )
                        except Exception as e:
                            logger.warning("X autopublish error for lead %s: %s", lead.id, e)
                else:
                    failed_dispatches += 1
                    logger.warning(
                        "FAILED DISPATCH for lead %s: %s",
                        lead.id, dispatch_res.get("error_message")
                    )
            else:
                total_below_threshold += 1
                db.update_status(lead.id, "below_threshold")
        except Exception as e:
            logger.error("Error processing lead %s: %s", lead_id, e, exc_info=True)

    # Also check if any previously 'enriched' leads were left un-dispatched
    try:
        pending_enriched = db.get_leads_by_status("enriched")
        for lead in pending_enriched:
            if lead.id not in all_to_process:
                enriched = enricher.enrich_lead(lead)
                if enriched.is_high_ticket:
                    dispatch_res = dispatcher.dispatch_sync(enriched)
                    if dispatch_res.ok:
                        db.update_status(lead.id, "dispatched")
                        total_dispatched += 1
    except Exception as e:
        logger.warning("Could not process pending enriched leads: %s", e)

    db.close()

    metrics = {
        "sources_queried": total_queried_sources,
        "total_ingested": total_ingested,
        "total_duplicates": total_duplicates,
        "total_high_ticket": total_high_ticket,
        "total_below_threshold": total_below_threshold,
        "total_dispatched": total_dispatched,
        "failed_dispatches": failed_dispatches,
        "dry_run": dispatcher.dry_run,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    print("\n" + "=" * 60)
    print("           APEX RADAR — POLL CYCLE COMPLETE")
    print("=" * 60)
    print(f"  Sources Queried:       {metrics['sources_queried']}")
    print(f"  Total Ingested (New):  {metrics['total_ingested']}")
    print(f"  Duplicates Skipped:    {metrics['total_duplicates']}")
    print(f"  High-Ticket Filtered:  {metrics['total_high_ticket']} (>= ${min_fixed_usd:,.0f} fixed/mo or >= ${min_hourly_usd:,.0f}/hr)")
    print(f"  Below Threshold:       {metrics['total_below_threshold']}")
    mode_str = "DRY RUN" if metrics["dry_run"] else "LIVE"
    print(f"  Alerts Dispatched:     {metrics['total_dispatched']} (Mode: {mode_str})")
    if metrics["failed_dispatches"] > 0:
        print(f"  Failed Dispatches:     {metrics['failed_dispatches']}")
    print("=" * 60 + "\n")

    return metrics


# ==============================================================================
# 2. DAEMON IMPLEMENTATION
# ==============================================================================

def run_daemon(
    db_path: Optional[str] = None,
    interval_minutes: float = 30.0,
    interval_seconds: Optional[float] = None,
    dry_run: bool = False,
    chat_id: Optional[str] = None,
    bot_token: Optional[str] = None,
    min_fixed_usd: float = 2000.0,
    min_hourly_usd: float = 50.0,
    fixtures_dir: Optional[str] = None,
    sources: Optional[List[str]] = None,
    max_cycles: Optional[int] = None,
    shutdown_event: Optional[threading.Event] = None,
) -> int:
    """Run scheduled polling loop periodically with signal handling for graceful shutdown.

    Args:
        db_path: Path to leads database.
        interval_minutes: Interval between poll cycles in minutes (default 30).
        interval_seconds: Optional explicit interval in seconds.
        dry_run: Dry-run simulation mode.
        chat_id: Telegram channel/group chat ID.
        bot_token: Telegram bot token.
        min_fixed_usd: Minimum fixed contract budget (USD).
        min_hourly_usd: Minimum hourly contract rate (USD).
        fixtures_dir: Optional offline sample fixtures directory.
        sources: Optional source whitelist filter.
        max_cycles: Optional maximum number of cycles to execute (useful for testing).
        shutdown_event: Optional external shutdown event.

    Returns:
        Total number of cycles completed.
    """
    sleep_seconds = interval_seconds if interval_seconds is not None else (interval_minutes * 60.0)

    if shutdown_event is None:
        shutdown_event = threading.Event()

        def _signal_handler(signum: int, frame: Any) -> None:
            logger.info("Received signal %s. Initiating graceful shutdown...", signum)
            shutdown_event.set()

        try:
            signal.signal(signal.SIGINT, _signal_handler)
            signal.signal(signal.SIGTERM, _signal_handler)
        except (ValueError, AttributeError):
            # Not in main thread or signal registration not supported
            pass

    cycle_count = 0
    logger.info(
        "Starting B2B Alert Bot Daemon (interval: %0.1fm / %0.1fs, dry_run: %s)",
        interval_minutes, sleep_seconds, dry_run
    )

    while not shutdown_event.is_set():
        cycle_count += 1
        logger.info("--- Starting Poll Cycle #%d ---", cycle_count)
        try:
            run_poll(
                db_path=db_path,
                dry_run=dry_run,
                chat_id=chat_id,
                bot_token=bot_token,
                min_fixed_usd=min_fixed_usd,
                min_hourly_usd=min_hourly_usd,
                fixtures_dir=fixtures_dir,
                sources=sources,
            )
        except Exception as e:
            logger.error("Error during poll cycle #%d: %s", cycle_count, e, exc_info=True)

        if max_cycles is not None and cycle_count >= max_cycles:
            logger.info("Reached maximum cycles limit (%d). Terminating daemon.", max_cycles)
            break

        logger.info("Sleeping %0.1f seconds until next cycle...", sleep_seconds)
        if shutdown_event.wait(timeout=sleep_seconds):
            logger.info("Shutdown signal received during sleep. Exiting daemon loop.")
            break

    logger.info("Daemon stopped cleanly after %d cycle(s).", cycle_count)
    return cycle_count


# ==============================================================================
# 3. WEBHOOK IMPLEMENTATION
# ==============================================================================

def run_webhook(
    host: str = "0.0.0.0",
    port: int = 8080,
    secret: Optional[str] = None,
    subscribers_db: Optional[str] = None,
    bot_token: Optional[str] = None,
    channel_id: Optional[str] = None,
    blocking: bool = True,
    shutdown_event: Optional[threading.Event] = None,
) -> WhopWebhookServer:
    """Run the Whop webhook server.

    Args:
        host: Bind host address (default 0.0.0.0).
        port: Bind port (default 8080).
        secret: Whop webhook secret for HMAC-SHA256 signature verification.
        subscribers_db: Path to subscribers SQLite database.
        bot_token: Telegram bot token for invite generation and kicks.
        channel_id: Telegram VIP channel ID.
        blocking: If True, blocks until shutdown signal received.
        shutdown_event: Optional external shutdown event.

    Returns:
        Running WhopWebhookServer instance.
    """
    resolved_sub_db = (
        subscribers_db
        or os.environ.get("SUBSCRIBERS_DB_PATH")
        or _resolve_default_db_path("subscribers.db")
    )
    resolved_secret = secret or os.environ.get("WHOP_WEBHOOK_SECRET", "")
    resolved_bot_token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    resolved_channel_id = (
        channel_id
        or os.environ.get("TELEGRAM_CHANNEL_ID")
        or os.environ.get("TELEGRAM_CHAT_ID", "-1001234567890")
    )

    server = WhopWebhookServer(
        host=host,
        port=port,
        db_path=resolved_sub_db,
        secret=resolved_secret,
        bot_token=resolved_bot_token,
        channel_id=resolved_channel_id,
    )
    server.start(background=True)
    logger.info("Whop Webhook Server listening at %s", server.get_url())
    print(f"Whop Webhook Server running at {server.get_url()} (subscribers db: {resolved_sub_db})")

    if blocking:
        if shutdown_event is None:
            shutdown_event = threading.Event()

            def _signal_handler(signum: int, frame: Any) -> None:
                logger.info("Received signal %s. Stopping webhook server...", signum)
                shutdown_event.set()

            try:
                signal.signal(signal.SIGINT, _signal_handler)
                signal.signal(signal.SIGTERM, _signal_handler)
            except (ValueError, AttributeError):
                pass

        try:
            shutdown_event.wait()
        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt received.")
        finally:
            server.stop()
            print("Whop Webhook Server stopped successfully.")

    return server


# ==============================================================================
# 4. ALL / SERVE IMPLEMENTATION
# ==============================================================================

def run_serve(
    host: str = "0.0.0.0",
    port: int = 8080,
    secret: Optional[str] = None,
    subscribers_db: Optional[str] = None,
    db_path: Optional[str] = None,
    interval_minutes: float = 30.0,
    interval_seconds: Optional[float] = None,
    dry_run: bool = False,
    chat_id: Optional[str] = None,
    bot_token: Optional[str] = None,
    min_fixed_usd: float = 2000.0,
    min_hourly_usd: float = 50.0,
    fixtures_dir: Optional[str] = None,
    sources: Optional[List[str]] = None,
    max_cycles: Optional[int] = None,
    shutdown_event: Optional[threading.Event] = None,
) -> None:
    """Concurrently run both the Whop webhook server and scheduled polling loop."""
    if shutdown_event is None:
        shutdown_event = threading.Event()

        def _signal_handler(signum: int, frame: Any) -> None:
            logger.info("Received signal %s. Initiating shutdown for all services...", signum)
            shutdown_event.set()

        try:
            signal.signal(signal.SIGINT, _signal_handler)
            signal.signal(signal.SIGTERM, _signal_handler)
        except (ValueError, AttributeError):
            pass

    # 1. Start webhook server in background thread
    server = run_webhook(
        host=host,
        port=port,
        secret=secret,
        subscribers_db=subscribers_db,
        bot_token=bot_token,
        channel_id=chat_id,
        blocking=False,
    )

    # 2. Run scheduled daemon in main thread
    try:
        run_daemon(
            db_path=db_path,
            interval_minutes=interval_minutes,
            interval_seconds=interval_seconds,
            dry_run=dry_run,
            chat_id=chat_id,
            bot_token=bot_token,
            min_fixed_usd=min_fixed_usd,
            min_hourly_usd=min_hourly_usd,
            fixtures_dir=fixtures_dir,
            sources=sources,
            max_cycles=max_cycles,
            shutdown_event=shutdown_event,
        )
    finally:
        logger.info("Stopping webhook server...")
        server.stop()
        logger.info("All services stopped.")


# ==============================================================================
# 5. STATUS IMPLEMENTATION
# ==============================================================================

def get_status_stats(
    db_path: Optional[str] = None,
    subscribers_db: Optional[str] = None,
) -> Dict[str, Any]:
    """Query and return statistical summary across leads and subscribers stores."""
    resolved_db_path = db_path or os.environ.get("DATABASE_PATH") or _resolve_default_db_path("leads.db")
    resolved_sub_db = (
        subscribers_db
        or os.environ.get("SUBSCRIBERS_DB_PATH")
        or _resolve_default_db_path("subscribers.db")
    )

    stats: Dict[str, Any] = {
        "leads_db": resolved_db_path,
        "subscribers_db": resolved_sub_db,
        "total_ingested": 0,
        "high_ticket_count": 0,
        "dispatched_count": 0,
        "below_threshold_count": 0,
        "status_breakdown": {},
        "active_subscribers": 0,
        "total_subscribers": 0,
        "revoked_subscribers": 0,
        "webhook_events_count": 0,
    }

    # Query leads database
    if os.path.exists(resolved_db_path):
        db = Database(db_path=resolved_db_path)
        try:
            conn = db._get_connection()
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM ingested_leads;")
            row = cur.fetchone()
            stats["total_ingested"] = row[0] if row else 0

            cur.execute("SELECT status, COUNT(*) FROM ingested_leads GROUP BY status;")
            for st, cnt in cur.fetchall():
                stats["status_breakdown"][st] = cnt

            stats["dispatched_count"] = stats["status_breakdown"].get("dispatched", 0)
            stats["below_threshold_count"] = stats["status_breakdown"].get("below_threshold", 0)
            stats["high_ticket_count"] = (
                stats["status_breakdown"].get("enriched", 0)
                + stats["status_breakdown"].get("dispatched", 0)
            )
        except Exception as e:
            logger.warning("Error inspecting leads database: %s", e)
        finally:
            db.close()

    # Query subscribers database
    if os.path.exists(resolved_sub_db):
        try:
            conn = sqlite3.connect(resolved_sub_db)
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='subscribers';")
            if cur.fetchone():
                cur.execute("SELECT COUNT(*) FROM subscribers WHERE status = 'active';")
                row = cur.fetchone()
                stats["active_subscribers"] = row[0] if row else 0

                cur.execute("SELECT COUNT(*) FROM subscribers WHERE status = 'invalid';")
                row = cur.fetchone()
                stats["revoked_subscribers"] = row[0] if row else 0

                cur.execute("SELECT COUNT(*) FROM subscribers;")
                row = cur.fetchone()
                stats["total_subscribers"] = row[0] if row else 0

            cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='webhook_events';")
            if cur.fetchone():
                cur.execute("SELECT COUNT(*) FROM webhook_events;")
                row = cur.fetchone()
                stats["webhook_events_count"] = row[0] if row else 0

            conn.close()
        except Exception as e:
            logger.warning("Error inspecting subscribers database: %s", e)

    return stats


def print_status(
    db_path: Optional[str] = None,
    subscribers_db: Optional[str] = None,
    as_json: bool = False,
) -> Dict[str, Any]:
    """Print system database status report to stdout."""
    stats = get_status_stats(db_path=db_path, subscribers_db=subscribers_db)

    if as_json:
        print(json.dumps(stats, indent=2))
        return stats

    print("\n" + "=" * 60)
    print("              B2B ALERT BOT — SYSTEM STATUS")
    print("=" * 60)
    print(f"  Leads Database:         {stats['leads_db']}")
    print(f"  Subscribers Database:   {stats['subscribers_db']}")
    print("-" * 60)
    print(f"  Total Leads Ingested:   {stats['total_ingested']}")
    print(f"  High-Ticket Leads:      {stats['high_ticket_count']} (>= $2,000 fixed/mo or >= $50/hr)")
    print(f"  Alerts Dispatched:      {stats['dispatched_count']}")
    print(f"  Below Threshold:        {stats['below_threshold_count']}")
    print(f"  Status Breakdown:       {stats['status_breakdown']}")
    print("-" * 60)
    print(f"  Active Subscribers:     {stats['active_subscribers']}")
    print(f"  Total Subscribers:      {stats['total_subscribers']}")
    print(f"  Revoked Subscribers:    {stats['revoked_subscribers']}")
    print(f"  Webhook Events Logged:  {stats['webhook_events_count']}")
    print("=" * 60 + "\n")

    return stats


# ==============================================================================
# 6. CLI ARGUMENT PARSER & ENTRYPOINT
# ==============================================================================

def build_parser() -> argparse.ArgumentParser:
    """Construct argument parser supporting both subcommands and top-level flags."""
    parser = argparse.ArgumentParser(
        prog="b2b_alert_bot",
        description="B2B High-Ticket Contract & Lead Alert System — Automated Ingestion, Enrichment, Dispatch & Access Management.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # Top-level global flags
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default=os.environ.get("LOG_LEVEL", "INFO").upper(),
        help="Logging verbosity level.",
    )
    parser.add_argument(
        "--db", "--database",
        dest="global_db",
        default=None,
        help="Path to SQLite leads database file.",
    )
    parser.add_argument(
        "--subscribers-db",
        dest="global_subscribers_db",
        default=None,
        help="Path to SQLite subscribers database file.",
    )
    parser.add_argument(
        "--dry-run",
        dest="global_dry_run",
        action="store_true",
        default=os.environ.get("DRY_RUN", "").lower() in ("true", "1", "yes"),
        help="Run Telegram dispatcher in offline dry-run simulation mode.",
    )
    parser.add_argument(
        "--fixtures-dir", "--fixture-dir",
        dest="global_fixtures_dir",
        default=None,
        help="Path to directory containing offline mock XML/JSON/Atom feeds.",
    )

    # Alternate flag-based triggers for environment compatibility
    group = parser.add_argument_group("Shortcut Triggers")
    group.add_argument("--cron", "--poll-once", dest="flag_poll", action="store_true", help="Execute single poll cycle.")
    group.add_argument("--server", dest="flag_server", action="store_true", help="Start Whop webhook server.")
    group.add_argument("--daemon-mode", dest="flag_daemon", action="store_true", help="Start scheduled poll daemon.")
    group.add_argument("--serve", "--all-services", dest="flag_serve", action="store_true", help="Run both server and daemon.")
    group.add_argument("--status-check", dest="flag_status", action="store_true", help="Print system statistics.")

    subparsers = parser.add_subparsers(dest="command", title="Available Commands", help="Action to execute")

    # --- Command: poll ---
    poll_p = subparsers.add_parser(
        "poll",
        aliases=["cron", "ingest"],
        help="Execute single lead ingestion, enrichment, and dispatch cycle.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    poll_p.add_argument("--dry-run", action="store_true", default=None, help="Force offline Telegram dry-run.")
    poll_p.add_argument("--db", default=None, help="Path to leads database.")
    poll_p.add_argument("--chat-id", default=os.environ.get("TELEGRAM_CHAT_ID"), help="Telegram target chat/channel ID.")
    poll_p.add_argument("--bot-token", default=os.environ.get("TELEGRAM_BOT_TOKEN"), help="Telegram bot API token.")
    poll_p.add_argument(
        "--min-fixed",
        type=float,
        default=float(os.environ.get("MIN_BUDGET_USD") or os.environ.get("MIN_FIXED_USD") or 2000.0),
        help="Minimum fixed compensation threshold (USD).",
    )
    poll_p.add_argument(
        "--min-hourly",
        type=float,
        default=float(os.environ.get("MIN_HOURLY_USD") or 50.0),
        help="Minimum hourly compensation threshold (USD).",
    )
    poll_p.add_argument("--fixtures-dir", default=None, help="Directory containing offline sample fixtures.")
    poll_p.add_argument("--sources", nargs="+", default=None, help="Whitelist specific sources to ingest.")

    # --- Command: daemon ---
    daemon_p = subparsers.add_parser(
        "daemon",
        aliases=["schedule"],
        help="Run scheduled polling cycle periodically.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    daemon_p.add_argument(
        "--interval",
        type=float,
        default=float(os.environ.get("POLL_INTERVAL_MINUTES") or 30.0),
        help="Polling interval in minutes.",
    )
    daemon_p.add_argument("--interval-seconds", type=float, default=None, help="Polling interval in seconds.")
    daemon_p.add_argument("--dry-run", action="store_true", default=None, help="Telegram dry-run mode.")
    daemon_p.add_argument("--db", default=None, help="Path to leads database.")
    daemon_p.add_argument("--chat-id", default=os.environ.get("TELEGRAM_CHAT_ID"), help="Telegram chat ID.")
    daemon_p.add_argument("--bot-token", default=os.environ.get("TELEGRAM_BOT_TOKEN"), help="Telegram bot token.")
    daemon_p.add_argument(
        "--min-fixed",
        type=float,
        default=float(os.environ.get("MIN_BUDGET_USD") or 2000.0),
        help="Min fixed USD threshold.",
    )
    daemon_p.add_argument(
        "--min-hourly",
        type=float,
        default=float(os.environ.get("MIN_HOURLY_USD") or 50.0),
        help="Min hourly USD threshold.",
    )
    daemon_p.add_argument("--fixtures-dir", default=None, help="Directory with offline sample fixtures.")
    daemon_p.add_argument("--sources", nargs="+", default=None, help="Filter specific sources.")
    daemon_p.add_argument("--max-cycles", type=int, default=None, help="Max cycles to run before exiting.")

    # --- Command: webhook ---
    webhook_p = subparsers.add_parser(
        "webhook",
        aliases=["server"],
        help="Run Whop webhook receiver HTTP server.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    webhook_p.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"), help="Host to bind server.")
    webhook_p.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", "8080")),
        help="Port to bind server.",
    )
    webhook_p.add_argument("--secret", default=os.environ.get("WHOP_WEBHOOK_SECRET"), help="Whop webhook secret key.")
    webhook_p.add_argument("--subscribers-db", default=None, help="Path to subscribers database.")
    webhook_p.add_argument("--bot-token", default=os.environ.get("TELEGRAM_BOT_TOKEN"), help="Telegram bot token.")
    webhook_p.add_argument(
        "--channel-id",
        default=os.environ.get("TELEGRAM_CHANNEL_ID") or os.environ.get("TELEGRAM_CHAT_ID"),
        help="Telegram channel ID for invite links.",
    )

    # --- Command: all / serve ---
    serve_p = subparsers.add_parser(
        "serve",
        aliases=["all", "both"],
        help="Concurrently run both the Whop webhook server and scheduled polling loop.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    serve_p.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"), help="Host to bind webhook server.")
    serve_p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")), help="Port to bind server.")
    serve_p.add_argument("--secret", default=os.environ.get("WHOP_WEBHOOK_SECRET"), help="Whop webhook secret.")
    serve_p.add_argument("--subscribers-db", default=None, help="Path to subscribers database.")
    serve_p.add_argument("--db", default=None, help="Path to leads database.")
    serve_p.add_argument(
        "--interval",
        type=float,
        default=float(os.environ.get("POLL_INTERVAL_MINUTES") or 30.0),
        help="Polling interval in minutes.",
    )
    serve_p.add_argument("--interval-seconds", type=float, default=None, help="Polling interval in seconds.")
    serve_p.add_argument("--dry-run", action="store_true", default=None, help="Telegram dry-run mode.")
    serve_p.add_argument("--chat-id", default=os.environ.get("TELEGRAM_CHAT_ID"), help="Telegram chat ID.")
    serve_p.add_argument("--bot-token", default=os.environ.get("TELEGRAM_BOT_TOKEN"), help="Telegram bot token.")
    serve_p.add_argument("--min-fixed", type=float, default=2000.0, help="Min fixed USD threshold.")
    serve_p.add_argument("--min-hourly", type=float, default=50.0, help="Min hourly USD threshold.")
    serve_p.add_argument("--fixtures-dir", default=None, help="Directory with offline sample fixtures.")
    serve_p.add_argument("--sources", nargs="+", default=None, help="Filter specific sources.")
    serve_p.add_argument("--max-cycles", type=int, default=None, help="Max cycles to run before exiting.")

    # --- Command: status ---
    status_p = subparsers.add_parser(
        "status",
        aliases=["stats", "info"],
        help="Print database statistics (total ingested, high-ticket, dispatched, subscribers).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    status_p.add_argument("--db", default=None, help="Path to leads database.")
    status_p.add_argument("--subscribers-db", default=None, help="Path to subscribers database.")
    status_p.add_argument("--json", action="store_true", help="Output stats in raw JSON format.")

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI application entrypoint."""
    parser = build_parser()
    args = parser.parse_args(argv)

    # Configure root logging
    log_level = getattr(logging, args.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Determine command from subcommand or flags
    cmd = (args.command or "").lower()
    if not cmd:
        if getattr(args, "flag_poll", False):
            cmd = "poll"
        elif getattr(args, "flag_server", False):
            cmd = "webhook"
        elif getattr(args, "flag_daemon", False):
            cmd = "daemon"
        elif getattr(args, "flag_serve", False):
            cmd = "serve"
        elif getattr(args, "flag_status", False):
            cmd = "status"

    if not cmd:
        parser.print_help()
        return 0

    # Resolve shared parameter overrides
    db_path = getattr(args, "db", None) or args.global_db
    subscribers_db = getattr(args, "subscribers_db", None) or args.global_subscribers_db
    fixtures_dir = getattr(args, "fixtures_dir", None) or args.global_fixtures_dir

    dry_run = getattr(args, "dry_run", None)
    if dry_run is None:
        dry_run = args.global_dry_run

    try:
        if cmd in ("poll", "cron", "ingest"):
            run_poll(
                db_path=db_path,
                dry_run=dry_run,
                chat_id=getattr(args, "chat_id", None),
                bot_token=getattr(args, "bot_token", None),
                min_fixed_usd=getattr(args, "min_fixed", 2000.0),
                min_hourly_usd=getattr(args, "min_hourly", 50.0),
                fixtures_dir=fixtures_dir,
                sources=getattr(args, "sources", None),
            )
            return 0

        elif cmd in ("daemon", "schedule"):
            run_daemon(
                db_path=db_path,
                interval_minutes=getattr(args, "interval", 30.0),
                interval_seconds=getattr(args, "interval_seconds", None),
                dry_run=dry_run,
                chat_id=getattr(args, "chat_id", None),
                bot_token=getattr(args, "bot_token", None),
                min_fixed_usd=getattr(args, "min_fixed", 2000.0),
                min_hourly_usd=getattr(args, "min_hourly", 50.0),
                fixtures_dir=fixtures_dir,
                sources=getattr(args, "sources", None),
                max_cycles=getattr(args, "max_cycles", None),
            )
            return 0

        elif cmd in ("webhook", "server"):
            run_webhook(
                host=getattr(args, "host", "0.0.0.0"),
                port=getattr(args, "port", 8080),
                secret=getattr(args, "secret", None),
                subscribers_db=subscribers_db,
                bot_token=getattr(args, "bot_token", None),
                channel_id=getattr(args, "channel_id", None),
                blocking=True,
            )
            return 0

        elif cmd in ("serve", "all", "both"):
            run_serve(
                host=getattr(args, "host", "0.0.0.0"),
                port=getattr(args, "port", 8080),
                secret=getattr(args, "secret", None),
                subscribers_db=subscribers_db,
                db_path=db_path,
                interval_minutes=getattr(args, "interval", 30.0),
                interval_seconds=getattr(args, "interval_seconds", None),
                dry_run=dry_run,
                chat_id=getattr(args, "chat_id", None),
                bot_token=getattr(args, "bot_token", None),
                min_fixed_usd=getattr(args, "min_fixed", 2000.0),
                min_hourly_usd=getattr(args, "min_hourly", 50.0),
                fixtures_dir=fixtures_dir,
                sources=getattr(args, "sources", None),
                max_cycles=getattr(args, "max_cycles", None),
            )
            return 0

        elif cmd in ("status", "stats", "info"):
            print_status(
                db_path=db_path,
                subscribers_db=subscribers_db,
                as_json=getattr(args, "json", False),
            )
            return 0

        else:
            logger.error("Unknown command: %s", cmd)
            parser.print_help()
            return 2

    except Exception as e:
        logger.error("Fatal error executing command '%s': %s", cmd, e, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
