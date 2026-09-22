#!/usr/bin/env python3
"""Jev-Optimized Social Campaign Generator for B2B Deal Alerts.

Pulls real high-ticket leads from leads.db, drafts psychological conversion copy,
and uses typesafe-ai/jev to mathematically evaluate and score the viral hook
and conversion probability before publishing to LinkedIn, X, or direct outreach.
"""

import os
import sys
import json
import sqlite3
import argparse
from typing import List, Dict, Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from b2b_alert_bot.jev.bridge import evaluate_post_with_jev
from b2b_alert_bot.dispatcher.linkedin_autopublisher import LinkedInAutoPublisher
from b2b_alert_bot.dispatcher.x_publisher import XAutoPublisher

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "leads.db")
WHOP_URL = "https://whop.com/t-7e74/high-ticket-contract-alerts"


def get_latest_high_ticket_deals(limit: int = 3) -> List[Dict[str, Any]]:
    """Retrieve top verified high-ticket deals from SQLite."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        """SELECT id, title, raw_compensation, client, source, description 
           FROM ingested_leads 
           WHERE status IN ('dispatched', 'enriched') 
           ORDER BY published_at DESC LIMIT ?""",
        (limit,)
    )
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def create_post_candidates(deal: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Generate 3 distinct psychological conversion archetypes for Jev scoring."""
    title = deal.get("title", "Lead Technical Consultant")
    comp = deal.get("raw_compensation", "High-Ticket Enterprise Contract")
    client = deal.get("client", "Confidential Tech Scaleup")
    
    candidates = [
        {
            "name": "Actionable Teardown (High Value)",
            "platform": "linkedin",
            "text": (
                f"A client just posted a {comp} contract for: \"{title}\".\n\n"
                f"Most applicants will submit a 4-page CV and get ignored.\n"
                f"Here is the winning strategy for landing a deal of this size:\n\n"
                f"1. Target the core architectural bottleneck directly in your opening line.\n"
                f"2. Present 2 relevant milestone case studies instead of a chronological work history.\n"
                f"3. Propose an initial paid 3-day technical audit to derisk the delivery for their leadership.\n\n"
                f"We stream pre-vetted £5k-£30k+ contracts and winning pitch teardowns 24/7 in our private channel.\n\n"
                f"Explore verified contract opportunities on our radar:\n"
                f"{WHOP_URL}"
            )
        },
        {
            "name": "Speed Asymmetry (Scarcity)",
            "platform": "linkedin",
            "text": (
                f"Speed is the only unfair advantage in high-ticket tech contracting.\n\n"
                f"Right now on our private radar:\n"
                f"🎯 Role: {title}\n"
                f"💰 Budget: {comp}\n"
                f"🏢 Hiring Entity: [Direct Tech Feed]\n\n"
                f"The first 3 qualified applicants who pitch the client's problem directly win 65% of these contracts. "
                f"Wait 12 hours, and you will be buried beneath 100+ generic proposals.\n\n"
                f"Get automated, real-time contract alerts sent directly to your Telegram with direct client apply links:\n"
                f"{WHOP_URL}"
            )
        },
        {
            "name": "Short-Form High Impact (X / Twitter)",
            "platform": "x",
            "text": (
                f"🚨 High-Ticket Contract Alert\n\n"
                f"🎯 {title[:40]}\n"
                f"💰 {comp[:35]}\n"
                f"⚡ Client: Direct (0% platform tax)\n\n"
                f"Direct client links + winning pitch angles stream 24/7 on our private radar:\n"
                f"{WHOP_URL}\n"
                f"#freelance #techjobs"
            )
        }
    ]
    return candidates


def run_campaign_evaluation(publish_winner: bool = False):
    """Evaluate candidates using Jev and rank by probability scores."""
    deals = get_latest_high_ticket_deals(limit=1)
    if not deals:
        print("No high-ticket deals found in database.")
        return

    deal = deals[0]
    print(f"\n=======================================================")
    print(f"🎯 CANDIDATE DEAL: {deal['title']}")
    print(f"💰 BUDGET: {deal['raw_compensation']}")
    print(f"=======================================================\n")

    candidates = create_post_candidates(deal)
    evaluated = []

    for cand in candidates:
        print(f"Evaluating Archetype: [{cand['name']}] via Jev...")
        jev_res = evaluate_post_with_jev(cand["text"], platform=cand["platform"])
        if jev_res and jev_res.get("success"):
            cand["jev"] = jev_res
            cand["score"] = (
                (jev_res.get("hook_probability", 0) * 0.4) +
                (jev_res.get("conversion_score", 0) / 5.0 * 0.4) +
                ((1.0 - jev_res.get("spam_risk_probability", 1.0)) * 0.2)
            )
            evaluated.append(cand)
            print(f"  ✓ Hook Prob: {(jev_res.get('hook_probability', 0)*100):.1f}%")
            print(f"  ✓ Conversion Score: {jev_res.get('conversion_score', 0):.2f}/5.0")
            print(f"  ✓ Spam Risk: {(jev_res.get('spam_risk_probability', 0)*100):.1f}%")
            print(f"  ✓ Composite Viral Score: {(cand['score']*100):.1f}/100\n")
        else:
            print("  ✗ Jev evaluation skipped or unavailable.\n")

    if not evaluated:
        print("No evaluations completed.")
        return

    # Sort candidates by highest composite score
    evaluated.sort(key=lambda x: x["score"], reverse=True)
    winner = evaluated[0]

    print("=======================================================")
    print(f"🏆 WINNING SOCIAL POST: {winner['name']} (Score: {(winner['score']*100):.1f}/100)")
    print("=======================================================\n")
    print(winner["text"])
    print("\n-------------------------------------------------------")

    if publish_winner:
        print(f"🚀 Publishing winner to {winner['platform'].upper()}...")
        if winner["platform"] == "linkedin":
            pub = LinkedInAutoPublisher()
            res = pub.publish_text(winner["text"])
            print("LinkedIn Publish Result:", res)
        elif winner["platform"] == "x":
            pub = XAutoPublisher()
            res = pub.publish_tweet(winner["text"])
            print("X Publish Result:", res)
    else:
        print("💡 To auto-publish this winning post, run with --publish")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Jev Social Campaign Optimizer")
    parser.add_argument("--publish", action="store_true", help="Publish the highest-scoring post")
    args = parser.parse_args()

    run_campaign_evaluation(publish_winner=args.publish)
