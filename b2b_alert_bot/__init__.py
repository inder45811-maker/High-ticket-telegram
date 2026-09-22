"""B2B High-Ticket Contract & Lead Alert System."""

from b2b_alert_bot.schema import Lead, EnrichedLead, extract_core_tech_stack
from b2b_alert_bot.db import Database, canonicalize_url, compute_lead_hash

__version__ = "0.1.0"

__all__ = [
    "Lead",
    "EnrichedLead",
    "extract_core_tech_stack",
    "Database",
    "canonicalize_url",
    "compute_lead_hash",
]
