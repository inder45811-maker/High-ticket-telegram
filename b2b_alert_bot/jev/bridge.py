"""Python Bridge to Jev Probabilistic AI Service.

Bridges the Python B2B Deal Alert Bot into the Vercel AI Gateway's `typesafe-ai/jev` model.
Provides calibrated probability evaluations for:
1. Deal Card Quality & High-Ticket Certification (Telegram broadcast)
2. Social Media Viral Copy & Conversion Auditing (LinkedIn & X)
3. Multi-Candidate Head-to-Head Campaign Optimization
"""

import os
import sys
import json
import logging
import subprocess
from typing import Dict, Any, Optional

logger = logging.getLogger("b2b_alert_bot.jev_bridge")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEAL_EVALUATOR_SCRIPT = os.path.join(BASE_DIR, "jev_deal_evaluator.js")
SOCIAL_EVALUATOR_SCRIPT = os.path.join(BASE_DIR, "jev_social_evaluator.js")
CAMPAIGN_OPTIMIZER_SCRIPT = os.path.join(BASE_DIR, "jev_campaign_optimizer.js")


def evaluate_lead_with_jev(lead_data: Dict[str, Any], timeout: int = 15) -> Optional[Dict[str, Any]]:
    """Evaluate a deal with Jev and return calibrated probabilities and verification badge."""
    try:
        if not os.path.exists(DEAL_EVALUATOR_SCRIPT):
            logger.warning(f"Jev evaluator script not found: {DEAL_EVALUATOR_SCRIPT}")
            return None

        # Build payload
        payload = {
            "title": lead_data.get("title", ""),
            "budget": lead_data.get("budget", lead_data.get("budget_badge", "")),
            "scope": lead_data.get("scope", lead_data.get("scope_bullet", "")),
            "skills": lead_data.get("skills", lead_data.get("skills_bullet", "")),
            "client": lead_data.get("client", "Direct Client"),
            "source": lead_data.get("source", "Direct Feed"),
        }

        # Run Node.js script
        env = os.environ.copy()
        proc = subprocess.run(
            ["node", DEAL_EVALUATOR_SCRIPT, json.dumps(payload)],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env
        )

        if proc.returncode != 0:
            logger.warning(f"Jev deal evaluation failed (exit {proc.returncode}): {proc.stderr}")
            return None

        result = json.loads(proc.stdout)
        return result

    except Exception as e:
        logger.warning(f"Failed to run Jev lead evaluation: {e}")
        return None


def evaluate_post_with_jev(post_text: str, platform: str = "linkedin", timeout: int = 15) -> Optional[Dict[str, Any]]:
    """Evaluate social marketing copy before publishing to verify hook and conversion strength."""
    try:
        if not os.path.exists(SOCIAL_EVALUATOR_SCRIPT):
            logger.warning(f"Jev social evaluator script not found: {SOCIAL_EVALUATOR_SCRIPT}")
            return None

        payload = {
            "platform": platform,
            "text": post_text
        }

        env = os.environ.copy()
        proc = subprocess.run(
            ["node", SOCIAL_EVALUATOR_SCRIPT, json.dumps(payload)],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env
        )

        if proc.returncode != 0:
            logger.warning(f"Jev social evaluation failed (exit {proc.returncode}): {proc.stderr}")
            return None

        result = json.loads(proc.stdout)
        return result

    except Exception as e:
        logger.warning(f"Failed to run Jev social evaluation: {e}")
        return None


def optimize_campaign_with_jev(
    candidates: Dict[str, str],
    platform: str = "linkedin",
    deal_info: Optional[Dict[str, Any]] = None,
    timeout: int = 20
) -> Optional[Dict[str, Any]]:
    """Compare multiple copy variations head-to-head with Jev and pick the mathematical winner."""
    try:
        if not os.path.exists(CAMPAIGN_OPTIMIZER_SCRIPT):
            logger.warning(f"Jev campaign optimizer script not found: {CAMPAIGN_OPTIMIZER_SCRIPT}")
            return None

        payload = {
            "candidates": candidates,
            "platform": platform,
            "deal_info": deal_info or {}
        }

        env = os.environ.copy()
        proc = subprocess.run(
            ["node", CAMPAIGN_OPTIMIZER_SCRIPT, json.dumps(payload)],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env
        )

        if proc.returncode != 0:
            logger.warning(f"Jev campaign optimization failed (exit {proc.returncode}): {proc.stderr}")
            return None

        result = json.loads(proc.stdout)
        return result

    except Exception as e:
        logger.warning(f"Failed to run Jev campaign optimization: {e}")
        return None
