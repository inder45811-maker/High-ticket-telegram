"""Unit tests for autonomous X (formerly Twitter) publisher and formatting engine."""

import os
import pytest
from datetime import datetime, timezone, timedelta

from b2b_alert_bot.schema import Lead, EnrichedLead
from b2b_alert_bot.db import Database
from b2b_alert_bot.dispatcher.x_publisher import (
    XAutoPublisher,
    format_x_deal_teaser,
)


@pytest.fixture
def sample_lead():
    return Lead(
        id="lead_x_12345",
        source="weworkremotely",
        title="Senior Distributed Systems Architect",
        client="Confidential Fintech Inc",
        url="https://weworkremotely.com/jobs/senior-distributed-systems",
        raw_compensation="$140/hr",
        description="Build high-throughput streaming pipelines with Kafka and Go.",
        published_at=datetime.now(timezone.utc).isoformat(),
        core_tech_stack=["Go", "Kafka", "Kubernetes"],
    )


@pytest.fixture
def sample_enriched_lead(sample_lead):
    return EnrichedLead(
        lead=sample_lead,
        is_high_ticket=True,
        rate_type="hourly",
        min_amount=140.0,
        max_amount=140.0,
        currency="USD",
        budget_badge="⏱️ $140/HR",
        scope_bullet="Design and deploy low-latency Kafka message queues.",
        skills_bullet="Go, Kafka, Kubernetes",
        winning_angle="Demonstrate previous experience handling 50k events/sec.",
    )


@pytest.fixture
def temp_db(tmp_path):
    db_file = str(tmp_path / "test_x_leads.db")
    db = Database(db_path=db_file)
    yield db
    db.close()


def test_format_x_deal_teaser_all_archetypes_under_280_chars(sample_enriched_lead):
    from b2b_alert_bot.dispatcher.x_publisher import X_TEMPLATES

    for tmpl in X_TEMPLATES:
        tweet = format_x_deal_teaser(
            sample_enriched_lead,
            "https://whop.com/t-7e74/high-ticket-contract-alerts",
            template=tmpl,
        )
        assert len(tweet) <= 280, f"Template {tmpl} exceeded 280 chars ({len(tweet)} chars)"
        assert "https://whop.com/t-7e74/high-ticket-contract-alerts" in tweet

    # Specific checks
    teaser_pitch = format_x_deal_teaser(sample_enriched_lead, template="STEAL_THE_PITCH")
    assert "Bookmark" in teaser_pitch
    assert "💡 Pitch:" in teaser_pitch

    teaser_tax = format_x_deal_teaser(sample_enriched_lead, template="PLATFORM_TAX_ROAST")
    assert "20% platform tax" in teaser_tax

    teaser_speed = format_x_deal_teaser(sample_enriched_lead, template="SPEED_ASYMMETRY")
    assert "< 15 mins" in teaser_speed


def test_format_x_deal_teaser_truncates_long_titles(sample_enriched_lead):
    # Extremely long title
    sample_enriched_lead.lead.title = (
        "Principal Enterprise Cloud Infrastructure Solutions Architect and Senior VP of "
        "Kubernetes Migrations for Ultra High Scale Fortune 500 Global Logistics Networks"
    )
    for tmpl in ["ARBITRAGE_RADAR_DROP", "STEAL_THE_PITCH", "PLATFORM_TAX_ROAST", "SPEED_ASYMMETRY"]:
        tweet = format_x_deal_teaser(
            sample_enriched_lead,
            "https://whop.com/t-7e74/high-ticket-contract-alerts",
            template=tmpl,
        )
        assert len(tweet) <= 280, f"Template {tmpl} with long title exceeded 280 chars ({len(tweet)} chars)"
        assert "..." in tweet
        assert "https://whop.com/t-7e74/high-ticket-contract-alerts" in tweet


def test_x_publisher_disabled_when_flag_false(sample_enriched_lead, temp_db):
    publisher = XAutoPublisher(
        api_key="mock_key",
        api_secret="mock_secret",
        access_token="mock_token",
        access_token_secret="mock_token_secret",
        enabled=False,
    )
    result = publisher.maybe_publish_lead(sample_enriched_lead, temp_db)
    assert result is None


def test_x_publisher_skips_non_high_ticket(sample_lead, temp_db):
    low_lead = EnrichedLead(
        lead=sample_lead,
        is_high_ticket=False,
        rate_type="fixed",
        min_amount=300.0,
        max_amount=300.0,
        currency="USD",
        budget_badge="💰 $300 FIXED",
        scope_bullet="Quick script fix",
        skills_bullet="Bash",
        winning_angle="Immediate delivery",
    )
    publisher = XAutoPublisher(
        api_key="mock_key",
        api_secret="mock_secret",
        access_token="mock_token",
        access_token_secret="mock_token_secret",
        enabled=True,
        dry_run=True,
    )
    result = publisher.maybe_publish_lead(low_lead, temp_db)
    assert result is None


def test_x_publisher_dry_run_success(sample_enriched_lead, temp_db):
    publisher = XAutoPublisher(
        api_key="mock_key",
        api_secret="mock_secret",
        access_token="mock_token",
        access_token_secret="mock_token_secret",
        window_start_utc=0,
        window_end_utc=24,
        enabled=True,
        dry_run=True,
    )
    result = publisher.maybe_publish_lead(sample_enriched_lead, temp_db)
    assert result is not None
    assert result.get("ok") is True
    assert result.get("dry_run") is True
    assert "tweet_" in result.get("tweet_id")

    # Verify recorded in DB
    assert temp_db.is_lead_posted_to_x(sample_enriched_lead.lead.id) is True


def test_x_publisher_deduplication(sample_enriched_lead, temp_db):
    publisher = XAutoPublisher(
        api_key="mock_key",
        api_secret="mock_secret",
        access_token="mock_token",
        access_token_secret="mock_token_secret",
        window_start_utc=0,
        window_end_utc=24,
        enabled=True,
        dry_run=True,
    )
    # First publish
    res1 = publisher.maybe_publish_lead(sample_enriched_lead, temp_db)
    assert res1 is not None and res1.get("ok") is True

    # Immediate second publish must be skipped
    res2 = publisher.maybe_publish_lead(sample_enriched_lead, temp_db)
    assert res2 is None


def test_x_publisher_cooldown(sample_enriched_lead, temp_db):
    publisher = XAutoPublisher(
        api_key="mock_key",
        api_secret="mock_secret",
        access_token="mock_token",
        access_token_secret="mock_token_secret",
        cooldown_hours=2.0,
        window_start_utc=0,
        window_end_utc=24,
        enabled=True,
        dry_run=True,
    )
    # Publish lead 1
    res1 = publisher.maybe_publish_lead(sample_enriched_lead, temp_db)
    assert res1 is not None and res1.get("ok") is True

    # Lead 2 right away should be blocked by cooldown
    lead2 = Lead(
        id="lead_x_67890",
        source="remoteok",
        title="AI Engineer",
        client="Startup AI",
        url="https://remoteok.com/jobs/ai-lead",
    )
    enriched2 = EnrichedLead(
        lead=lead2,
        is_high_ticket=True,
        rate_type="fixed",
        min_amount=8000.0,
        max_amount=8000.0,
        currency="USD",
        budget_badge="💰 $8,000 FIXED",
        scope_bullet="Fine-tune LLM for legal documents.",
        skills_bullet="PyTorch, Transformers",
        winning_angle="Showcase previous legal fine-tuning eval metrics.",
    )
    res2 = publisher.maybe_publish_lead(enriched2, temp_db)
    assert res2 is None

    # Simulate 3 hours passing in SQLite
    conn = temp_db._get_connection()
    old_time = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    with conn:
        conn.execute("UPDATE x_posts SET published_at = ?;", (old_time,))

    # Now lead 2 should successfully publish
    res3 = publisher.maybe_publish_lead(enriched2, temp_db)
    assert res3 is not None and res3.get("ok") is True
    assert temp_db.is_lead_posted_to_x(lead2.id) is True


def test_x_publisher_window_gating():
    publisher = XAutoPublisher(
        api_key="mock_key",
        api_secret="mock_secret",
        access_token="mock_token",
        access_token_secret="mock_token_secret",
        window_start_utc=7,
        window_end_utc=21,
    )
    # Active window: 13:00 UTC
    dt_active = datetime(2026, 9, 22, 13, 0, tzinfo=timezone.utc)
    in_win, _ = publisher.is_within_publish_window(dt_active)
    assert in_win is True

    # Dead zone: 03:00 UTC
    dt_dead = datetime(2026, 9, 22, 3, 0, tzinfo=timezone.utc)
    in_win, reason = publisher.is_within_publish_window(dt_dead)
    assert in_win is False
    assert "Overnight Dead Zone" in reason


def test_x_publisher_webhook_bridge(monkeypatch):
    monkeypatch.delenv("BUFFER_ACCESS_TOKEN", raising=False)
    class MockResponse:
        status_code = 200
        text = "ok"

    def mock_post(url, json=None, headers=None, timeout=None):
        assert "make.com" in url
        assert "text" in json
        return MockResponse()

    import requests
    monkeypatch.setattr(requests, "post", mock_post)

    publisher = XAutoPublisher(
        webhook_url="https://hook.eu1.make.com/test_webhook",
        enabled=True,
    )
    publisher.buffer_token = ""
    res = publisher.publish_tweet("Testing webhook bridge tweet")
    assert res.get("ok") is True
    assert res.get("method") == "webhook"
    assert "hook_" in res.get("tweet_id")


def test_x_publisher_buffer_integration(monkeypatch):
    class MockBufferResponse:
        status_code = 200
        text = '{"data":{"createPost":{"post":{"id":"buf_test_123","status":"sent"}}}}'
        def json(self):
            return {"data": {"createPost": {"post": {"id": "buf_test_123", "status": "sent"}}}}

    def mock_buffer_post(url, json=None, headers=None, timeout=None):
        assert "api.buffer.com" in url
        return MockBufferResponse()

    import requests
    monkeypatch.setattr(requests, "post", mock_buffer_post)

    publisher = XAutoPublisher(
        enabled=True,
    )
    publisher.buffer_token = "mock_buffer_token"
    publisher._cached_buffer_channel_id = "mock_channel_123"
    res = publisher.publish_tweet("Testing Buffer live deal drop")
    assert res.get("ok") is True
    assert res.get("method") == "buffer"
    assert res.get("tweet_id") == "buf_test_123"
