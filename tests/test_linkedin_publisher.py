"""Unit tests for autonomous LinkedIn Deal Teaser publisher and cooldown engine."""

import os
import pytest
from datetime import datetime, timezone, timedelta

from b2b_alert_bot.schema import Lead, EnrichedLead
from b2b_alert_bot.db import Database
from b2b_alert_bot.dispatcher.linkedin_autopublisher import (
    LinkedInAutoPublisher,
    format_deal_teaser,
)


@pytest.fixture
def sample_lead():
    return Lead(
        id="lead_li_12345",
        source="weworkremotely",
        title="Full Stack Python & React Contract",
        client="Confidential Agency Inc",
        url="https://weworkremotely.com/jobs/full-stack-python-react",
        raw_compensation="$120/hr",
        description="We need a senior contractor to rebuild our payment flow.",
        published_at=datetime.now(timezone.utc).isoformat(),
        core_tech_stack=["Python", "React", "PostgreSQL"],
    )


@pytest.fixture
def sample_enriched_lead(sample_lead):
    return EnrichedLead(
        lead=sample_lead,
        is_high_ticket=True,
        rate_type="hourly",
        min_amount=120.0,
        max_amount=120.0,
        currency="USD",
        budget_badge="⏱️ $120/HR",
        scope_bullet="Rebuild core payment infrastructure and integrate Stripe webhook.",
        skills_bullet="Python, React, PostgreSQL",
        winning_angle="Highlight experience with idempotent billing queues.",
    )


@pytest.fixture
def temp_db(tmp_path):
    db_file = str(tmp_path / "test_leads.db")
    db = Database(db_path=db_file)
    yield db
    db.close()


def test_format_deal_teaser(sample_enriched_lead):
    teaser = format_deal_teaser(sample_enriched_lead, "https://whop.com/test-store")
    assert "🚨 NEW HIGH-TICKET CONTRACT RADAR ALERT" in teaser
    assert "⏱️ $120/HR" in teaser
    assert "Full Stack Python & React Contract" in teaser
    assert "REDACTED" in teaser
    assert "Confidential Agency Inc" not in teaser  # Must redact client name
    assert "https://whop.com/test-store" in teaser
    assert "Rebuild core payment infrastructure" in teaser
    assert "Python, React, PostgreSQL" in teaser
    assert "Highlight experience with idempotent billing queues" in teaser


def test_publisher_disabled_when_flag_false(sample_enriched_lead, temp_db):
    publisher = LinkedInAutoPublisher(
        token="mock_token",
        author_urn="urn:li:person:mock",
        enabled=False,
    )
    result = publisher.maybe_publish_lead(sample_enriched_lead, temp_db)
    assert result is None


def test_publisher_skips_non_high_ticket(sample_lead, temp_db):
    low_enriched = EnrichedLead(
        lead=sample_lead,
        is_high_ticket=False,
        rate_type="fixed",
        min_amount=500.0,
        max_amount=500.0,
        currency="USD",
        budget_badge="💰 $500 FIXED",
        scope_bullet="Small landing page bugfix",
        skills_bullet="HTML, CSS",
        winning_angle="Fast 24h turnaround",
    )
    publisher = LinkedInAutoPublisher(
        token="mock_token",
        author_urn="urn:li:person:mock",
        enabled=True,
    )
    result = publisher.maybe_publish_lead(low_enriched, temp_db)
    assert result is None


def test_publisher_dry_run_success(sample_enriched_lead, temp_db):
    publisher = LinkedInAutoPublisher(
        token="mock_token",
        author_urn="urn:li:person:mock",
        window_start_utc=0,
        window_end_utc=24,
        enabled=True,
        dry_run=True,
    )
    result = publisher.maybe_publish_lead(sample_enriched_lead, temp_db)
    assert result is not None
    assert result.get("ok") is True
    assert result.get("dry_run") is True
    assert "urn:li:share:" in result.get("post_urn")

    # Verify recorded in DB
    assert temp_db.is_lead_posted_to_linkedin(sample_enriched_lead.lead.id) is True


def test_publisher_deduplication(sample_enriched_lead, temp_db):
    publisher = LinkedInAutoPublisher(
        token="mock_token",
        author_urn="urn:li:person:mock",
        window_start_utc=0,
        window_end_utc=24,
        enabled=True,
        dry_run=True,
    )
    # First publish
    res1 = publisher.maybe_publish_lead(sample_enriched_lead, temp_db)
    assert res1 is not None and res1.get("ok") is True

    # Immediate second publish of same lead should be skipped
    res2 = publisher.maybe_publish_lead(sample_enriched_lead, temp_db)
    assert res2 is None


def test_publisher_algorithmic_cooldown(sample_enriched_lead, temp_db):
    publisher = LinkedInAutoPublisher(
        token="mock_token",
        author_urn="urn:li:person:mock",
        cooldown_hours=4.0,
        window_start_utc=0,
        window_end_utc=24,
        enabled=True,
        dry_run=True,
    )

    # Publish lead 1
    res1 = publisher.maybe_publish_lead(sample_enriched_lead, temp_db)
    assert res1 is not None and res1.get("ok") is True

    # Create lead 2
    lead2 = Lead(
        id="lead_li_67890",
        source="remoteok",
        title="Senior Go Engineer",
        client="Go Startup Ltd",
        url="https://remoteok.com/jobs/lead2",
    )
    enriched2 = EnrichedLead(
        lead=lead2,
        is_high_ticket=True,
        rate_type="fixed",
        min_amount=5000.0,
        max_amount=5000.0,
        currency="USD",
        budget_badge="💰 $5,000 FIXED",
        scope_bullet="Build distributed cache service in Go.",
        skills_bullet="Go, Redis, Kubernetes",
        winning_angle="Share benchmark test suite from previous microservices.",
    )

    # Lead 2 right away should be blocked by cooldown
    res2 = publisher.maybe_publish_lead(enriched2, temp_db)
    assert res2 is None

    # Simulate 5 hours passing by artificially updating the DB timestamp
    conn = temp_db._get_connection()
    old_time = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    with conn:
        conn.execute("UPDATE linkedin_posts SET published_at = ?;", (old_time,))

    # Now lead 2 should successfully publish
    res3 = publisher.maybe_publish_lead(enriched2, temp_db)
    assert res3 is not None and res3.get("ok") is True
    assert temp_db.is_lead_posted_to_linkedin(lead2.id) is True


def test_publisher_window_gating_blocks_dead_zone(sample_enriched_lead, temp_db):
    publisher = LinkedInAutoPublisher(
        token="mock_token",
        author_urn="urn:li:person:mock",
        window_start_utc=7,
        window_end_utc=21,
        enabled=True,
        dry_run=True,
    )
    # 02:30 UTC is deep in the Transatlantic Dead Zone (London 03:30 BST, NY 22:30 EDT)
    dead_zone_dt = datetime(2026, 9, 22, 2, 30, tzinfo=timezone.utc)
    in_win, reason = publisher.is_within_publish_window(dead_zone_dt)
    assert in_win is False
    assert "Overnight Dead Zone" in reason


def test_publisher_window_gating_allows_active_window():
    publisher = LinkedInAutoPublisher(
        token="mock_token",
        author_urn="urn:li:person:mock",
        window_start_utc=7,
        window_end_utc=21,
    )
    # 13:00 UTC is prime Transatlantic window (London 14:00 BST, NY 09:00 EDT)
    prime_dt = datetime(2026, 9, 22, 13, 0, tzinfo=timezone.utc)
    in_win, reason = publisher.is_within_publish_window(prime_dt)
    assert in_win is True
    assert "Within active transatlantic hours" in reason


def test_publisher_prime_windows():
    publisher = LinkedInAutoPublisher(
        token="mock_token",
        author_urn="urn:li:person:mock",
        only_prime_windows=True,
    )
    # Window 1: 12:30 UTC is prime
    w1_dt = datetime(2026, 9, 22, 12, 30, tzinfo=timezone.utc)
    assert publisher.is_within_publish_window(w1_dt)[0] is True

    # Window 2: 17:00 UTC is prime
    w2_dt = datetime(2026, 9, 22, 17, 0, tzinfo=timezone.utc)
    assert publisher.is_within_publish_window(w2_dt)[0] is True

    # 10:00 UTC is outside prime windows
    off_dt = datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc)
    assert publisher.is_within_publish_window(off_dt)[0] is False


def test_publisher_weekend_skipping():
    publisher = LinkedInAutoPublisher(
        token="mock_token",
        author_urn="urn:li:person:mock",
        skip_weekends=True,
    )
    # 2026-09-26 is a Saturday
    saturday_dt = datetime(2026, 9, 26, 13, 0, tzinfo=timezone.utc)
    assert publisher.is_within_publish_window(saturday_dt)[0] is False

    # 2026-09-23 is a Wednesday
    wednesday_dt = datetime(2026, 9, 23, 13, 0, tzinfo=timezone.utc)
    assert publisher.is_within_publish_window(wednesday_dt)[0] is True
