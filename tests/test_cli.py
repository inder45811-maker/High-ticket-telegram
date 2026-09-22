"""Unit and integration test suite for B2B Alert Bot CLI and daemon service."""

import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import requests

from b2b_alert_bot.db import Database
from b2b_alert_bot.main import (
    build_parser,
    get_status_stats,
    main,
    print_status,
    run_daemon,
    run_poll,
    run_serve,
    run_webhook,
)
from b2b_alert_bot.schema import Lead
from b2b_alert_bot.webhook.handler import SubscriberStore

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


class TestCLIArgumentParsing(unittest.TestCase):
    """Test CLI argument parser configurations, commands, shortcuts, and validation."""

    def setUp(self):
        self.parser = build_parser()

    def test_parser_has_all_required_subcommands(self):
        subparsers_actions = [
            action for action in self.parser._actions
            if isinstance(action, unittest.mock.MagicMock) or action.dest == "command"
        ]
        self.assertTrue(len(subparsers_actions) > 0)
        choices = subparsers_actions[0].choices
        for expected in ("poll", "daemon", "webhook", "serve", "status"):
            self.assertIn(expected, choices)

    def test_poll_argument_defaults(self):
        args = self.parser.parse_args(["poll"])
        self.assertEqual(args.command, "poll")
        self.assertEqual(args.min_fixed, 2000.0)
        self.assertEqual(args.min_hourly, 50.0)
        self.assertIsNone(args.fixtures_dir)
        self.assertIsNone(args.sources)

    def test_poll_custom_arguments(self):
        args = self.parser.parse_args([
            "poll",
            "--dry-run",
            "--db", "custom_leads.db",
            "--min-fixed", "3500",
            "--min-hourly", "65",
            "--fixtures-dir", "tests/fixtures",
            "--sources", "weworkremotely", "remoteok"
        ])
        self.assertEqual(args.command, "poll")
        self.assertTrue(args.dry_run)
        self.assertEqual(args.db, "custom_leads.db")
        self.assertEqual(args.min_fixed, 3500.0)
        self.assertEqual(args.min_hourly, 65.0)
        self.assertEqual(args.fixtures_dir, "tests/fixtures")
        self.assertEqual(args.sources, ["weworkremotely", "remoteok"])

    def test_daemon_arguments(self):
        args = self.parser.parse_args([
            "daemon",
            "--interval", "15",
            "--interval-seconds", "900",
            "--max-cycles", "5"
        ])
        self.assertEqual(args.command, "daemon")
        self.assertEqual(args.interval, 15.0)
        self.assertEqual(args.interval_seconds, 900.0)
        self.assertEqual(args.max_cycles, 5)

    def test_webhook_arguments(self):
        args = self.parser.parse_args([
            "webhook",
            "--host", "127.0.0.1",
            "--port", "9090",
            "--secret", "test_whsec_123",
            "--channel-id", "-100999999"
        ])
        self.assertEqual(args.command, "webhook")
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 9090)
        self.assertEqual(args.secret, "test_whsec_123")
        self.assertEqual(args.channel_id, "-100999999")

    def test_serve_arguments(self):
        args = self.parser.parse_args([
            "serve",
            "--host", "0.0.0.0",
            "--port", "8888",
            "--interval", "20",
            "--dry-run"
        ])
        self.assertEqual(args.command, "serve")
        self.assertEqual(args.port, 8888)
        self.assertEqual(args.interval, 20.0)
        self.assertTrue(args.dry_run)

    def test_status_arguments(self):
        args = self.parser.parse_args([
            "status",
            "--db", "my_leads.db",
            "--subscribers-db", "my_subs.db",
            "--json"
        ])
        self.assertEqual(args.command, "status")
        self.assertEqual(args.db, "my_leads.db")
        self.assertEqual(args.subscribers_db, "my_subs.db")
        self.assertTrue(args.json)

    def test_shortcut_flags(self):
        args = self.parser.parse_args(["--cron"])
        self.assertTrue(args.flag_poll)

        args = self.parser.parse_args(["--server"])
        self.assertTrue(args.flag_server)

        args = self.parser.parse_args(["--serve"])
        self.assertTrue(args.flag_serve)

        args = self.parser.parse_args(["--status-check"])
        self.assertTrue(args.flag_status)


class TestStatusReporting(unittest.TestCase):
    """Test status query and output formatting."""

    def test_get_status_stats_nonexistent_files(self):
        stats = get_status_stats(
            db_path="/tmp/nonexistent_leads_123.db",
            subscribers_db="/tmp/nonexistent_subs_123.db"
        )
        self.assertEqual(stats["total_ingested"], 0)
        self.assertEqual(stats["high_ticket_count"], 0)
        self.assertEqual(stats["dispatched_count"], 0)
        self.assertEqual(stats["active_subscribers"], 0)

    def test_get_status_stats_populated_db(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            leads_db = os.path.join(tmp_dir, "leads.db")
            subs_db = os.path.join(tmp_dir, "subs.db")

            # Populate leads DB
            db = Database(db_path=leads_db)
            h1 = hashlib.sha256(b"https://techcorp.com/job/1").hexdigest()
            h2 = hashlib.sha256(b"https://smallstartup.com/job/2").hexdigest()
            h3 = hashlib.sha256(b"https://appagency.com/job/3").hexdigest()
            lead1 = Lead(
                id=h1,
                title="Staff Python Architect",
                source="weworkremotely",
                client="TechCorp",
                url="https://techcorp.com/job/1",
                raw_compensation="$150,000/yr"
            )
            lead2 = Lead(
                id=h2,
                title="Entry Level QA",
                source="remoteok",
                client="SmallStartup",
                url="https://smallstartup.com/job/2",
                raw_compensation="$15/hr"
            )
            lead3 = Lead(
                id=h3,
                title="Senior React Developer",
                source="jobspresso",
                client="AppAgency",
                url="https://appagency.com/job/3",
                raw_compensation="$80/hr"
            )
            db.insert_lead(lead1)
            db.update_status(h1, "dispatched")

            db.insert_lead(lead2)
            db.update_status(h2, "below_threshold")

            db.insert_lead(lead3)
            db.update_status(h3, "enriched")
            db.close()

            # Populate subscribers DB
            store = SubscriberStore(db_path=subs_db)
            store.upsert_subscriber("sub_active_1", telegram_user_id=111, status="active")
            store.upsert_subscriber("sub_active_2", telegram_user_id=222, status="active")
            store.upsert_subscriber("sub_invalid_1", telegram_user_id=333, status="invalid")
            store.record_event("evt_001", "membership.went_valid", "sub_active_1")

            stats = get_status_stats(db_path=leads_db, subscribers_db=subs_db)

            self.assertEqual(stats["total_ingested"], 3)
            self.assertEqual(stats["dispatched_count"], 1)
            self.assertEqual(stats["below_threshold_count"], 1)
            self.assertEqual(stats["high_ticket_count"], 2)  # lead1 (dispatched) + lead3 (enriched)
            self.assertEqual(stats["active_subscribers"], 2)
            self.assertEqual(stats["revoked_subscribers"], 1)
            self.assertEqual(stats["total_subscribers"], 3)
            self.assertEqual(stats["webhook_events_count"], 1)

    def test_print_status_text_and_json(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            leads_db = os.path.join(tmp_dir, "leads.db")
            subs_db = os.path.join(tmp_dir, "subs.db")

            # Capture text output
            captured_stdout = io.StringIO()
            with patch("sys.stdout", captured_stdout):
                print_status(db_path=leads_db, subscribers_db=subs_db, as_json=False)
            output = captured_stdout.getvalue()
            self.assertIn("B2B ALERT BOT — SYSTEM STATUS", output)
            self.assertIn("Total Leads Ingested:", output)

            # Capture JSON output
            captured_json = io.StringIO()
            with patch("sys.stdout", captured_json):
                print_status(db_path=leads_db, subscribers_db=subs_db, as_json=True)
            json_str = captured_json.getvalue().strip()
            parsed = json.loads(json_str)
            self.assertEqual(parsed["total_ingested"], 0)
            self.assertIn("status_breakdown", parsed)


class TestPollCycle(unittest.TestCase):
    """Test lead ingestion poll cycle execution, filtering, deduplication, and dispatch."""

    def test_run_poll_dry_run_with_fixtures(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_file = os.path.join(tmp_dir, "poll_test.db")
            metrics = run_poll(
                db_path=db_file,
                dry_run=True,
                fixtures_dir=FIXTURES_DIR,
                min_fixed_usd=2000.0,
                min_hourly_usd=50.0,
            )

            self.assertGreaterEqual(metrics["sources_queried"], 5)
            self.assertGreater(metrics["total_ingested"], 10)
            self.assertGreater(metrics["total_high_ticket"], 5)
            self.assertEqual(metrics["total_dispatched"], metrics["total_high_ticket"])
            self.assertTrue(metrics["dry_run"])

            # Verify persisted DB records
            db = Database(db_path=db_file)
            self.assertEqual(db.count_leads(), metrics["total_ingested"])
            self.assertEqual(db.count_leads("dispatched"), metrics["total_dispatched"])
            self.assertEqual(db.count_leads("below_threshold"), metrics["total_below_threshold"])
            db.close()

    def test_run_poll_deduplication_across_runs(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_file = os.path.join(tmp_dir, "dedup_test.db")

            # Run 1: Ingests all fixture leads
            metrics1 = run_poll(
                db_path=db_file,
                dry_run=True,
                fixtures_dir=FIXTURES_DIR,
            )
            self.assertGreater(metrics1["total_ingested"], 0)
            self.assertEqual(metrics1["total_duplicates"], 0)

            # Run 2: Exact same fixtures -> 0 new leads, all duplicates
            metrics2 = run_poll(
                db_path=db_file,
                dry_run=True,
                fixtures_dir=FIXTURES_DIR,
            )
            self.assertEqual(metrics2["total_ingested"], 0)
            self.assertEqual(metrics2["total_dispatched"], 0)
            self.assertGreaterEqual(metrics2["total_duplicates"], metrics1["total_ingested"])

    def test_run_poll_source_whitelist(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_file = os.path.join(tmp_dir, "whitelist_test.db")
            metrics = run_poll(
                db_path=db_file,
                dry_run=True,
                fixtures_dir=FIXTURES_DIR,
                sources=["weworkremotely"],
            )
            self.assertEqual(metrics["sources_queried"], 1)
            self.assertGreater(metrics["total_ingested"], 0)

    def test_run_poll_connector_resilience(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_file = os.path.join(tmp_dir, "resilience_test.db")

            mock_failing_connector = MagicMock()
            mock_failing_connector.fetch.side_effect = RuntimeError("Network timeout simulation")

            good_hash = hashlib.sha256(b"https://goodco.com/job/1").hexdigest()
            mock_working_connector = MagicMock()
            mock_working_connector.fetch.return_value = [
                Lead(
                    id=good_hash,
                    title="Principal Engineer",
                    source="mock_working",
                    client="GoodCo",
                    url="https://goodco.com/job/1",
                    raw_compensation="$8,000/mo"
                )
            ]

            connectors = [
                ("failing_source", mock_failing_connector),
                ("working_source", mock_working_connector)
            ]

            # Should not raise exception; failing source logged and skipped
            metrics = run_poll(
                db_path=db_file,
                dry_run=True,
                connectors=connectors
            )
            self.assertEqual(metrics["total_ingested"], 1)
            self.assertEqual(metrics["total_dispatched"], 1)


class TestDaemonExecution(unittest.TestCase):
    """Test daemon loop execution, interval sleeping, and graceful signal shutdown."""

    def test_daemon_max_cycles(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_file = os.path.join(tmp_dir, "daemon_test.db")
            cycles = run_daemon(
                db_path=db_file,
                interval_seconds=0.01,
                dry_run=True,
                fixtures_dir=FIXTURES_DIR,
                max_cycles=2
            )
            self.assertEqual(cycles, 2)

    def test_daemon_graceful_shutdown_event(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_file = os.path.join(tmp_dir, "shutdown_test.db")
            shutdown_event = threading.Event()

            def trigger_shutdown_after_first_cycle():
                time.sleep(0.1)
                shutdown_event.set()

            t = threading.Thread(target=trigger_shutdown_after_first_cycle, daemon=True)
            t.start()

            cycles = run_daemon(
                db_path=db_file,
                interval_seconds=5.0,
                dry_run=True,
                fixtures_dir=FIXTURES_DIR,
                shutdown_event=shutdown_event
            )
            self.assertEqual(cycles, 1)


class TestWebhookServerCLI(unittest.TestCase):
    """Test running Whop webhook server via CLI entrypoints."""

    def test_run_webhook_health_and_ready(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            subs_db = os.path.join(tmp_dir, "subs.db")
            server = run_webhook(
                host="127.0.0.1",
                port=0,
                subscribers_db=subs_db,
                blocking=False,
            )
            try:
                base_url = server.get_url()

                # Test /health
                resp_health = requests.get(f"{base_url}/health", timeout=5.0)
                self.assertEqual(resp_health.status_code, 200)
                self.assertEqual(resp_health.json().get("status"), "healthy")

                # Test /ready
                resp_ready = requests.get(f"{base_url}/ready", timeout=5.0)
                self.assertEqual(resp_ready.status_code, 200)
                self.assertEqual(resp_ready.json().get("status"), "ready")

            finally:
                server.stop()


class TestServeConcurrentExecution(unittest.TestCase):
    """Test concurrent execution of webhook server and scheduled polling loop."""

    def test_run_serve_with_max_cycles(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            leads_db = os.path.join(tmp_dir, "serve_leads.db")
            subs_db = os.path.join(tmp_dir, "serve_subs.db")
            shutdown_event = threading.Event()

            # Should start webhook server, execute 1 daemon poll cycle, and terminate cleanly
            run_serve(
                host="127.0.0.1",
                port=0,
                db_path=leads_db,
                subscribers_db=subs_db,
                interval_seconds=0.01,
                dry_run=True,
                fixtures_dir=FIXTURES_DIR,
                max_cycles=1,
                shutdown_event=shutdown_event,
            )

            # Check leads were ingested and recorded
            db = Database(db_path=leads_db)
            self.assertGreater(db.count_leads(), 0)
            db.close()


class TestMainEntrypoint(unittest.TestCase):
    """Test top-level main() entrypoint invocations."""

    def test_main_no_args_prints_help(self):
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            exit_code = main([])
        self.assertEqual(exit_code, 0)
        self.assertIn("usage: b2b_alert_bot", captured.getvalue())

    def test_main_status(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            leads_db = os.path.join(tmp_dir, "main_status.db")
            exit_code = main(["status", "--db", leads_db])
            self.assertEqual(exit_code, 0)

    def test_main_status_json(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            leads_db = os.path.join(tmp_dir, "main_status.db")
            captured = io.StringIO()
            with patch("sys.stdout", captured):
                exit_code = main(["status", "--db", leads_db, "--json"])
            self.assertEqual(exit_code, 0)
            data = json.loads(captured.getvalue())
            self.assertEqual(data["total_ingested"], 0)

    def test_main_poll_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            leads_db = os.path.join(tmp_dir, "main_poll.db")
            exit_code = main([
                "poll",
                "--dry-run",
                "--fixtures-dir", FIXTURES_DIR,
                "--db", leads_db
            ])
            self.assertEqual(exit_code, 0)

    def test_main_shortcut_cron_flag(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            leads_db = os.path.join(tmp_dir, "main_cron.db")
            exit_code = main([
                "--cron",
                "--dry-run",
                "--fixtures-dir", FIXTURES_DIR,
                "--db", leads_db
            ])
            self.assertEqual(exit_code, 0)


if __name__ == "__main__":
    unittest.main()
