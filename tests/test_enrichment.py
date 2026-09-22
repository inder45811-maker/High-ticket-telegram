"""Comprehensive unit test suite for R2 AI Enrichment & High-Value Filtering.

Covers:
- All 22 compensation vectors (fixed, hourly, annual, monthly, ranges, foreign currencies)
- FX normalizations (USD, EUR, GBP, CAD, AUD)
- Range evaluation policies (max, min, avg)
- Funding false positive suppression (50-character contextual exclusion window & outlier ceiling)
- Unstated, DOE, and equity-only listing rejections
- Deal card 3-bullet generation across archetypes
- Deterministic NLP fallback engine (deliverables, skills, winning angles)
- EnrichmentEngine Lead -> EnrichedLead conversion & SQLite database status updates
"""

import os
import unittest
from typing import Dict, Any

from b2b_alert_bot.schema import Lead, EnrichedLead
from b2b_alert_bot.db import Database
from b2b_alert_bot.enrichment.compensation import (
    parse_compensation,
    extract_compensation,
    evaluate_high_value_filter,
    format_budget_badge,
    parse_num,
    FX_RATES,
)
from b2b_alert_bot.enrichment.funding_filter import (
    is_funding_false_positive,
    is_outlier_funding_amount,
)
from b2b_alert_bot.enrichment.deal_card import (
    DealCardGenerator,
    generate_deal_card,
    synthesize_winning_angle,
    extract_deliverables_bullet,
    extract_skills_bullet,
)
from b2b_alert_bot.enrichment.engine import EnrichmentEngine


class TestCompensationParsingVectors(unittest.TestCase):
    """Verify parsing and threshold decision across all 22 benchmark vectors."""

    def test_vector_01_fixed_standard_pass(self):
        # 1. $2,000 fixed
        res = parse_compensation("$2,000")
        self.assertTrue(res["is_high_ticket"])
        self.assertEqual(res["rate_type"], "fixed")
        self.assertEqual(res["max_amount"], 2000.0)
        self.assertIn("💰 $2,000 FIXED", res["budget_badge"])

    def test_vector_02_fixed_k_suffix_pass(self):
        # 2. $5k fixed
        res = parse_compensation("$5k")
        self.assertTrue(res["is_high_ticket"])
        self.assertEqual(res["rate_type"], "fixed")
        self.assertEqual(res["max_amount"], 5000.0)
        self.assertIn("💰 $5,000 FIXED", res["budget_badge"])

    def test_vector_03_fixed_low_500_reject(self):
        # 3. $500 fixed
        res = parse_compensation("$500")
        self.assertFalse(res["is_high_ticket"])
        self.assertEqual(res["rate_type"], "fixed")
        self.assertEqual(res["max_amount"], 500.0)
        self.assertIn("REJECT_LOW_FIXED", res["status"])

    def test_vector_04_fixed_low_1500_reject(self):
        # 4. $1,500 fixed
        res = parse_compensation("$1,500")
        self.assertFalse(res["is_high_ticket"])
        self.assertEqual(res["rate_type"], "fixed")
        self.assertEqual(res["max_amount"], 1500.0)
        self.assertIn("REJECT_LOW_FIXED", res["status"])

    def test_vector_05_fixed_suffix_currency_pass(self):
        # 5. 5000 USD
        res = parse_compensation("5000 USD")
        self.assertTrue(res["is_high_ticket"])
        self.assertEqual(res["rate_type"], "fixed")
        self.assertEqual(res["max_amount"], 5000.0)
        self.assertIn("💰 $5,000 FIXED", res["budget_badge"])

    def test_vector_06_fixed_prefix_currency_code_pass(self):
        # 6. USD 5,000
        res = parse_compensation("USD 5,000")
        self.assertTrue(res["is_high_ticket"])
        self.assertEqual(res["rate_type"], "fixed")
        self.assertEqual(res["max_amount"], 5000.0)
        self.assertIn("💰 $5,000 FIXED", res["budget_badge"])

    def test_vector_07_fixed_eur_pass(self):
        # 7. €2,500 (EUR 2,500 * 1.08 = $2,700 USD)
        res = parse_compensation("€2,500")
        self.assertTrue(res["is_high_ticket"])
        self.assertEqual(res["rate_type"], "fixed")
        self.assertAlmostEqual(res["max_amount"], 2700.0, places=1)
        self.assertEqual(res["currency"], "EUR")

    def test_vector_08_fixed_gbp_pass(self):
        # 8. £2,000 (GBP 2,000 * 1.28 = $2,560 USD)
        res = parse_compensation("£2,000")
        self.assertTrue(res["is_high_ticket"])
        self.assertEqual(res["rate_type"], "fixed")
        self.assertAlmostEqual(res["max_amount"], 2560.0, places=1)
        self.assertEqual(res["currency"], "GBP")

    def test_vector_09_fixed_range_k_suffix_pass(self):
        # 9. $2.5k - $4k
        res = parse_compensation("$2.5k - $4k")
        self.assertTrue(res["is_high_ticket"])
        self.assertEqual(res["rate_type"], "fixed")
        self.assertEqual(res["min_amount"], 2500.0)
        self.assertEqual(res["max_amount"], 4000.0)
        self.assertIn("💰 $2,500 - $4,000 FIXED", res["budget_badge"])

    def test_vector_10_hourly_exact_threshold_pass(self):
        # 10. $50/hr
        res = parse_compensation("$50/hr")
        self.assertTrue(res["is_high_ticket"])
        self.assertEqual(res["rate_type"], "hourly")
        self.assertEqual(res["max_amount"], 50.0)
        self.assertIn("⏱️ $50/HR", res["budget_badge"])

    def test_vector_11_hourly_range_with_spaces_pass(self):
        # 11. $60 - $80 / hour
        res = parse_compensation("$60 - $80 / hour")
        self.assertTrue(res["is_high_ticket"])
        self.assertEqual(res["rate_type"], "hourly")
        self.assertEqual(res["min_amount"], 60.0)
        self.assertEqual(res["max_amount"], 80.0)
        self.assertIn("⏱️ $60 - $80/HR", res["budget_badge"])

    def test_vector_12_hourly_post_code_pass(self):
        # 12. 75 USD/h
        res = parse_compensation("75 USD/h")
        self.assertTrue(res["is_high_ticket"])
        self.assertEqual(res["rate_type"], "hourly")
        self.assertEqual(res["max_amount"], 75.0)
        self.assertIn("⏱️ $75/HR", res["budget_badge"])

    def test_vector_13_hourly_low_reject(self):
        # 13. $25/hr
        res = parse_compensation("$25/hr")
        self.assertFalse(res["is_high_ticket"])
        self.assertEqual(res["rate_type"], "hourly")
        self.assertEqual(res["max_amount"], 25.0)
        self.assertIn("REJECT_LOW_HOURLY", res["status"])

    def test_vector_14_hourly_split_range_max_pass(self):
        # 14. $40 - $60 / hr (min < 50, max >= 50)
        res = parse_compensation("$40 - $60 / hr")
        self.assertTrue(res["is_high_ticket"])
        self.assertEqual(res["rate_type"], "hourly")
        self.assertEqual(res["min_amount"], 40.0)
        self.assertEqual(res["max_amount"], 60.0)
        self.assertIn("⏱️ $40 - $60/HR", res["budget_badge"])

    def test_vector_15_annual_salary_pass(self):
        # 15. $100,000/yr -> $50.00/hr
        res = parse_compensation("$100,000/yr")
        self.assertTrue(res["is_high_ticket"])
        self.assertEqual(res["rate_type"], "annual")
        self.assertEqual(res["max_amount"], 100000.0)
        self.assertAlmostEqual(res["equiv_hourly"], 50.0, places=1)
        self.assertIn("💼 $100k/YR (~$50/HR)", res["budget_badge"])

    def test_vector_16_annual_salary_reject(self):
        # 16. $80,000/yr -> $40.00/hr (< 50)
        res = parse_compensation("$80,000/yr")
        self.assertFalse(res["is_high_ticket"])
        self.assertEqual(res["rate_type"], "annual")
        self.assertEqual(res["max_amount"], 80000.0)
        self.assertAlmostEqual(res["equiv_hourly"], 40.0, places=1)
        self.assertIn("REJECT_LOW_ANNUAL", res["status"])

    def test_vector_17_monthly_retainer_pass(self):
        # 17. $10,000/mo -> $62.50/hr AND >= $2,000
        res = parse_compensation("$10,000/mo")
        self.assertTrue(res["is_high_ticket"])
        self.assertEqual(res["rate_type"], "monthly")
        self.assertEqual(res["max_amount"], 10000.0)
        self.assertIn("💰 $10,000/MO", res["budget_badge"])

    def test_vector_18_monthly_retainer_reject(self):
        # 18. $1,500/mo (< $2,000 and < $50/hr)
        res = parse_compensation("$1,500/mo")
        self.assertFalse(res["is_high_ticket"])
        self.assertEqual(res["rate_type"], "monthly")
        self.assertEqual(res["max_amount"], 1500.0)
        self.assertIn("REJECT_LOW_MONTHLY", res["status"])

    def test_vector_19_budget_prefixed_pass(self):
        # 19. Budget: $3,500 fixed
        res = parse_compensation("Budget: $3,500 fixed")
        self.assertTrue(res["is_high_ticket"])
        self.assertEqual(res["rate_type"], "fixed")
        self.assertEqual(res["max_amount"], 3500.0)
        self.assertIn("💰 $3,500 FIXED", res["budget_badge"])

    def test_vector_20_paying_prefixed_pass(self):
        # 20. Looking for dev, paying $6k for 2 weeks
        res = parse_compensation("Looking for dev, paying $6k for 2 weeks")
        self.assertTrue(res["is_high_ticket"])
        self.assertEqual(res["rate_type"], "fixed")
        self.assertEqual(res["max_amount"], 6000.0)
        self.assertIn("💰 $6,000 FIXED", res["budget_badge"])

    def test_vector_21_negotiable_unstated_reject(self):
        # 21. Negotiable rate based on experience
        res = parse_compensation("Negotiable rate based on experience")
        self.assertFalse(res["is_high_ticket"])
        self.assertEqual(res["status"], "REJECT_UNSTATED_COMPENSATION")

    def test_vector_22_equity_only_reject(self):
        # 22. Equity only early stage startup
        res = parse_compensation("Equity only early stage startup")
        self.assertFalse(res["is_high_ticket"])
        self.assertEqual(res["rate_type"], "unpaid")
        self.assertEqual(res["status"], "REJECT_UNPAID_OR_EQUITY")


class TestForeignCurrencyNormalization(unittest.TestCase):
    """Verify foreign currency conversions for CAD, AUD, EUR, GBP."""

    def test_cad_above_and_below_threshold(self):
        # CAD 3,000 * 0.74 = $2,220 USD -> PASS
        res1 = parse_compensation("CAD 3,000")
        self.assertTrue(res1["is_high_ticket"])
        self.assertAlmostEqual(res1["max_amount"], 2220.0, places=1)
        self.assertEqual(res1["currency"], "CAD")

        # CAD 2,000 * 0.74 = $1,480 USD -> REJECT
        res2 = parse_compensation("CAD 2,000")
        self.assertFalse(res2["is_high_ticket"])
        self.assertAlmostEqual(res2["max_amount"], 1480.0, places=1)

    def test_aud_above_and_below_threshold(self):
        # AUD 4,000 * 0.66 = $2,640 USD -> PASS
        res1 = parse_compensation("AUD 4,000")
        self.assertTrue(res1["is_high_ticket"])
        self.assertAlmostEqual(res1["max_amount"], 2640.0, places=1)
        self.assertEqual(res1["currency"], "AUD")

        # AUD 2,000 * 0.66 = $1,320 USD -> REJECT
        res2 = parse_compensation("AUD 2,000")
        self.assertFalse(res2["is_high_ticket"])
        self.assertAlmostEqual(res2["max_amount"], 1320.0, places=1)

    def test_hourly_foreign_currency(self):
        # €50/hr * 1.08 = $54.00/hr -> PASS
        res_eur = parse_compensation("€50/hr")
        self.assertTrue(res_eur["is_high_ticket"])
        self.assertAlmostEqual(res_eur["max_amount"], 54.0, places=1)

        # £45/hr * 1.28 = $57.60/hr -> PASS
        res_gbp = parse_compensation("£45/hr")
        self.assertTrue(res_gbp["is_high_ticket"])
        self.assertAlmostEqual(res_gbp["max_amount"], 57.6, places=1)

        # £30/hr * 1.28 = $38.40/hr -> REJECT
        res_gbp_low = parse_compensation("£30/hr")
        self.assertFalse(res_gbp_low["is_high_ticket"])


class TestRangeEvaluationStrategies(unittest.TestCase):
    """Test policy differences across 'max', 'min', and 'avg' range strategies."""

    def test_hourly_range_strategy_max(self):
        # $40 - $60 / hr under 'max': 60 >= 50 -> PASS
        res = parse_compensation("$40 - $60 / hr", range_strategy="max")
        self.assertTrue(res["is_high_ticket"])

    def test_hourly_range_strategy_min(self):
        # $40 - $60 / hr under 'min': 40 < 50 -> REJECT
        res = parse_compensation("$40 - $60 / hr", range_strategy="min")
        self.assertFalse(res["is_high_ticket"])

    def test_hourly_range_strategy_avg(self):
        # $40 - $60 / hr under 'avg': (40+60)/2 = 50 >= 50 -> PASS
        res1 = parse_compensation("$40 - $60 / hr", range_strategy="avg")
        self.assertTrue(res1["is_high_ticket"])

        # $30 - $60 / hr under 'avg': (30+60)/2 = 45 < 50 -> REJECT
        res2 = parse_compensation("$30 - $60 / hr", range_strategy="avg")
        self.assertFalse(res2["is_high_ticket"])

    def test_fixed_range_strategy(self):
        # $1,500 - $3,000 under 'min' -> 1500 < 2000 -> REJECT
        res_min = parse_compensation("$1,500 - $3,000", range_strategy="min")
        self.assertFalse(res_min["is_high_ticket"])

        # $1,500 - $3,000 under 'max' -> 3000 >= 2000 -> PASS
        res_max = parse_compensation("$1,500 - $3,000", range_strategy="max")
        self.assertTrue(res_max["is_high_ticket"])


class TestFundingContextExclusion(unittest.TestCase):
    """Verify 50-character contextual exclusion window and outlier suppression."""

    def test_raised_seed_round_suppression(self):
        text = "We just raised $2M seed round from top venture funds, hiring our first frontend engineer."
        res = parse_compensation(text)
        self.assertFalse(res["is_high_ticket"])

    def test_series_funding_suppression(self):
        text = "Series A funded startup ($10M funding) hiring backend dev. Unstated salary."
        res = parse_compensation(text)
        self.assertFalse(res["is_high_ticket"])

    def test_mrr_arr_valuation_suppression(self):
        text = "Bootstrapped SaaS with $1M ARR looking for growth contractor. Compensation DOE."
        res = parse_compensation(text)
        self.assertFalse(res["is_high_ticket"])

    def test_funding_mention_with_distinct_job_budget(self):
        # Funding term precedes the true project budget beyond the exclusion window
        text = "We raised a seed round last month. Looking for freelance developer, budget $5,000 fixed."
        res = parse_compensation(text)
        self.assertTrue(res["is_high_ticket"])
        self.assertEqual(res["max_amount"], 5000.0)

    def test_outlier_ceiling_suppression(self):
        # $500,000 without contract framing is flagged as company funding
        text = "Announcing $500,000 round led by angel investors."
        res = parse_compensation(text)
        self.assertFalse(res["is_high_ticket"])


class TestDealCardGeneration(unittest.TestCase):
    """Verify 3-bullet Deal Card extraction across archetypes and fallback modes."""

    def test_mvp_archetype_winning_angle(self):
        lead = Lead(
            id="1" * 64,
            title="Founding Engineer for MVP",
            source="weworkremotely",
            client="LaunchPad",
            url="https://example.com/apply",
            core_tech_stack=["React", "Node.js", "Supabase"],
            description="Build our greenfield MVP prototype from scratch in 4 weeks."
        )
        card = generate_deal_card(lead, {"is_high_ticket": True, "budget_badge": "💰 $8,000 FIXED"})
        self.assertTrue(card.is_high_ticket)
        self.assertIn("Figma", card.winning_angle)
        self.assertIn("prototype", card.winning_angle)
        self.assertTrue(card.winning_angle.endswith("."))

    def test_migration_archetype_winning_angle(self):
        lead = Lead(
            id="2" * 64,
            title="Senior Backend Migration Lead",
            source="remoteok",
            client="LegacySys",
            url="https://example.com/apply",
            core_tech_stack=["Python", "PostgreSQL", "Django"],
            description="Migrate our legacy database and rebuild monolithic endpoints."
        )
        card = generate_deal_card(lead, {"is_high_ticket": True, "budget_badge": "💰 $12,000 FIXED"})
        self.assertIn("zero-downtime", card.winning_angle)
        self.assertIn("migration", card.winning_angle)

    def test_ai_archetype_winning_angle(self):
        lead = Lead(
            id="3" * 64,
            title="AI Application Engineer",
            source="jobspresso",
            client="GenAI Labs",
            url="https://example.com/apply",
            core_tech_stack=["OpenAI", "Python", "FastAPI", "RAG"],
            description="Develop RAG pipelines and integrate LLM agents with OpenAI."
        )
        card = generate_deal_card(lead, {"is_high_ticket": True, "budget_badge": "⏱️ $90/HR"})
        self.assertIn("token cost optimization", card.winning_angle)

    def test_mobile_archetype_winning_angle(self):
        lead = Lead(
            id="4" * 64,
            title="React Native Developer",
            source="reddit",
            client="MobileFirst",
            url="https://example.com/apply",
            core_tech_stack=["React Native", "TypeScript", "iOS"],
            description="Build iOS and Android mobile app."
        )
        card = generate_deal_card(lead, {"is_high_ticket": True, "budget_badge": "⏱️ $65/HR"})
        self.assertIn("TestFlight", card.winning_angle)

    def test_devops_archetype_winning_angle(self):
        lead = Lead(
            id="5" * 64,
            title="Cloud & DevOps Architect",
            source="hackernews",
            client="CloudCorp",
            url="https://example.com/apply",
            core_tech_stack=["AWS", "Kubernetes", "Terraform", "Docker"],
            description="Optimize our CI/CD pipeline and cloud infrastructure on AWS."
        )
        card = generate_deal_card(lead, {"is_high_ticket": True, "budget_badge": "💰 $15,000 FIXED"})
        self.assertIn("cloud security audit", card.winning_angle)

    def test_data_archetype_winning_angle(self):
        lead = Lead(
            id="6" * 64,
            title="Data Performance Engineer",
            source="weworkremotely",
            client="DataFlow",
            url="https://example.com/apply",
            core_tech_stack=["Snowflake", "dbt", "PostgreSQL", "SQL"],
            description="Rebuild slow ETL pipelines and optimize SQL query latency."
        )
        card = generate_deal_card(lead, {"is_high_ticket": True, "budget_badge": "⏱️ $85/HR"})
        self.assertIn("48-hour diagnostic benchmark", card.winning_angle)

    def test_general_fallback_archetype_winning_angle(self):
        lead = Lead(
            id="7" * 64,
            title="Software Consultant",
            source="remoteok",
            client="ConsultingCo",
            url="https://example.com/apply",
            core_tech_stack=[],
            description="Help company improve internal operational tools."
        )
        card = generate_deal_card(lead, {"is_high_ticket": True, "budget_badge": "💰 $5,000 FIXED"})
        self.assertIn("3-milestone delivery roadmap", card.winning_angle)

    def test_deliverables_and_skills_bullets(self):
        lead = Lead(
            id="8" * 64,
            title="Senior Full-Stack Engineer",
            source="remoteok",
            client="NextGen",
            url="https://example.com/apply",
            core_tech_stack=["React", "TypeScript", "FastAPI", "PostgreSQL"],
            description="<p>Build and deploy a customer dashboard with React and FastAPI.</p>"
        )
        card = generate_deal_card(lead, {"is_high_ticket": True, "budget_badge": "💰 $7,000 FIXED"})
        # Scope bullet contains action deliverables
        self.assertGreater(len(card.scope_bullet), 15)
        self.assertTrue(card.scope_bullet.endswith("."))
        # Skills bullet lists core stack
        self.assertIn("React", card.skills_bullet)
        self.assertIn("FastAPI", card.skills_bullet)


class TestEnrichmentEngine(unittest.TestCase):
    """Verify end-to-end EnrichmentEngine workflows and database synchronization."""

    def test_enrich_lead_from_raw_compensation(self):
        engine = EnrichmentEngine()
        lead = Lead(
            id="a" * 64,
            title="Senior Backend Engineer",
            source="weworkremotely",
            client="Vercel",
            url="https://example.com/apply1",
            raw_compensation="$8,500 fixed milestone",
            description="Build scalable serverless functions."
        )
        enriched = engine.enrich_lead(lead)
        self.assertTrue(enriched.is_high_ticket)
        self.assertEqual(enriched.rate_type, "fixed")
        self.assertEqual(enriched.max_amount, 8500.0)
        self.assertIn("💰 $8,500 FIXED", enriched.budget_badge)

    def test_enrich_lead_fallback_to_description(self):
        engine = EnrichmentEngine()
        lead = Lead(
            id="b" * 64,
            title="TypeScript Developer",
            source="remoteok",
            client="Acme",
            url="https://example.com/apply2",
            raw_compensation="",
            description="Looking for an engineer. Rate is $75/hr. Full remote."
        )
        enriched = engine.enrich_lead(lead)
        self.assertTrue(enriched.is_high_ticket)
        self.assertEqual(enriched.rate_type, "hourly")
        self.assertEqual(enriched.max_amount, 75.0)
        self.assertIn("⏱️ $75/HR", enriched.budget_badge)

    def test_filter_high_ticket(self):
        engine = EnrichmentEngine()
        leads = [
            Lead(
                id="c" * 64,
                title="Lead 1",
                source="test",
                client="C1",
                url="https://example.com/1",
                raw_compensation="$60/hr"
            ),
            Lead(
                id="d" * 64,
                title="Lead 2",
                source="test",
                client="C2",
                url="https://example.com/2",
                raw_compensation="$20/hr"
            ),
        ]
        high_ticket = engine.filter_high_ticket(leads)
        self.assertEqual(len(high_ticket), 1)
        self.assertEqual(high_ticket[0].lead.id, "c" * 64)

    def test_process_and_save_to_database(self, tmp_path=None):
        import tempfile
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "test_leads.db")
            db = Database(db_path=db_path)

            lead1 = Lead(
                id="1" * 64,
                title="High Ticket Lead",
                source="weworkremotely",
                client="BigCo",
                url="https://example.com/lead1",
                raw_compensation="$4,500 fixed"
            )
            lead2 = Lead(
                id="2" * 64,
                title="Low Ticket Lead",
                source="remoteok",
                client="SmallCo",
                url="https://example.com/lead2",
                raw_compensation="$300 fixed"
            )
            db.insert_lead(lead1, status="ingested")
            db.insert_lead(lead2, status="ingested")

            engine = EnrichmentEngine()
            high_ticket = engine.process_and_save(db)

            self.assertEqual(len(high_ticket), 1)
            self.assertEqual(high_ticket[0].lead.id, "1" * 64)

            # Verify persisted statuses in SQLite
            db_lead1 = db.get_lead("1" * 64)
            db_lead2 = db.get_lead("2" * 64)
            
            # Check statuses via query
            conn = db._get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT status FROM ingested_leads WHERE id = ?", ("1" * 64,))
            self.assertEqual(cursor.fetchone()[0], "enriched")
            cursor.execute("SELECT status FROM ingested_leads WHERE id = ?", ("2" * 64,))
            self.assertEqual(cursor.fetchone()[0], "below_threshold")

            db.close()


if __name__ == "__main__":
    unittest.main()
