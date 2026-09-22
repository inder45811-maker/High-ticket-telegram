"""Ingestion package containing connectors for reliable open-access contract and lead feeds."""

from b2b_alert_bot.ingestion.base import (
    BaseConnector,
    clean_html,
    parse_timestamp,
    DEFAULT_DESKTOP_UA,
)
from b2b_alert_bot.ingestion.weworkremotely import WeWorkRemotelyConnector
from b2b_alert_bot.ingestion.remoteok import RemoteOKConnector
from b2b_alert_bot.ingestion.jobspresso import JobspressoConnector
from b2b_alert_bot.ingestion.hackernews import HackerNewsConnector
from b2b_alert_bot.ingestion.reddit import RedditConnector

__all__ = [
    "BaseConnector",
    "clean_html",
    "parse_timestamp",
    "DEFAULT_DESKTOP_UA",
    "WeWorkRemotelyConnector",
    "RemoteOKConnector",
    "JobspressoConnector",
    "HackerNewsConnector",
    "RedditConnector",
]
