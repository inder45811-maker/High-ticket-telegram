"""Unified enrichment pipeline engine converting Lead into EnrichedLead.

Processes raw ingested leads, extracts compensation, validates against high-ticket
thresholds ($2,000+ fixed or $50+/hr), generates 3-bullet deal cards, and assigns
high-visibility budget badges.
"""

from typing import List, Optional, Dict, Any

from b2b_alert_bot.schema import Lead, EnrichedLead
from b2b_alert_bot.enrichment.compensation import (
    parse_compensation,
)
from b2b_alert_bot.enrichment.deal_card import DealCardGenerator


class EnrichmentEngine:
    """End-to-end enrichment engine connecting compensation parsing and deal card generation."""

    def __init__(
        self,
        min_fixed_usd: float = 2000.0,
        min_hourly_usd: float = 50.0,
        range_strategy: str = "max",
    ) -> None:
        self.min_fixed_usd = min_fixed_usd
        self.min_hourly_usd = min_hourly_usd
        self.range_strategy = range_strategy

    def enrich_lead(self, lead: Lead) -> EnrichedLead:
        """Process a single Lead, parse compensation, and generate an EnrichedLead.

        Searches lead.raw_compensation first; if unstated or empty, scans title and description.
        """
        comp_data: Optional[Dict[str, Any]] = None

        # 1. Try raw_compensation if present
        if lead.raw_compensation and lead.raw_compensation.strip():
            comp_data = parse_compensation(
                lead.raw_compensation,
                range_strategy=self.range_strategy,
                min_fixed_usd=self.min_fixed_usd,
                min_hourly_usd=self.min_hourly_usd,
            )

        # 2. If not found or unstated, scan title followed by description
        if not comp_data or comp_data.get("rate_type") in ("unknown", None):
            title_comp = parse_compensation(
                lead.title,
                range_strategy=self.range_strategy,
                min_fixed_usd=self.min_fixed_usd,
                min_hourly_usd=self.min_hourly_usd,
            )
            if title_comp.get("rate_type") != "unknown":
                comp_data = title_comp
            elif lead.description:
                desc_comp = parse_compensation(
                    lead.description,
                    range_strategy=self.range_strategy,
                    min_fixed_usd=self.min_fixed_usd,
                    min_hourly_usd=self.min_hourly_usd,
                )
                if desc_comp.get("rate_type") != "unknown":
                    comp_data = desc_comp

        if not comp_data:
            comp_data = {
                "rate_type": "unknown",
                "min_amount": 0.0,
                "max_amount": 0.0,
                "currency": "USD",
                "is_high_ticket": False,
                "budget_badge": "❓ UNSTATED",
                "status": "UNSTATED",
            }

        # 3. Generate 3-bullet deal card
        enriched = DealCardGenerator.generate(lead, comp_data)

        # 4. Probabilistic Jev Audit (if candidate is high ticket)
        if enriched.is_high_ticket:
            try:
                from b2b_alert_bot.jev.bridge import evaluate_lead_with_jev
                jev_res = evaluate_lead_with_jev({
                    "title": lead.title,
                    "budget": enriched.budget_badge,
                    "scope": enriched.scope_bullet,
                    "skills": enriched.skills_bullet,
                    "client": lead.client,
                    "source": lead.source,
                })
                if jev_res and jev_res.get("success"):
                    enriched.jev_badge = jev_res.get("badge_summary")
                    enriched.jev_confidence = jev_res.get("high_ticket_probability")
            except Exception:
                pass

        return enriched

    def enrich(self, lead: Lead) -> EnrichedLead:
        """Alias for enrich_lead."""
        return self.enrich_lead(lead)

    def filter_high_ticket(self, leads: List[Lead]) -> List[EnrichedLead]:
        """Enrich a list of leads and return only those meeting the high-ticket threshold."""
        results: List[EnrichedLead] = []
        for lead in leads:
            enriched = self.enrich_lead(lead)
            if enriched.is_high_ticket:
                results.append(enriched)
        return results

    def process_and_save(
        self,
        db: Any,
        leads: Optional[List[Lead]] = None,
    ) -> List[EnrichedLead]:
        """Enrich leads and persist updated status ('enriched' vs 'below_threshold') to SQLite."""
        if leads is None:
            leads = db.get_leads_by_status("ingested")

        high_ticket_leads: List[EnrichedLead] = []
        for lead in leads:
            enriched = self.enrich_lead(lead)
            new_status = "enriched" if enriched.is_high_ticket else "below_threshold"
            db.update_status(lead.id, new_status)
            if enriched.is_high_ticket:
                high_ticket_leads.append(enriched)

        return high_ticket_leads
