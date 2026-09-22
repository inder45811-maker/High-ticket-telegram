"""3-bullet Deal Card generator with deterministic NLP fallback engine.

Produces:
1. Scope & Deliverables: Concise technical deliverables & business objective.
2. Required Skills: Primary technologies, frameworks, and architecture.
3. Winning Angle: Exactly one tailored, actionable pitch tip differentiating the applicant.
"""

import os
import re
from typing import Dict, Any, Optional, List, Tuple

from b2b_alert_bot.schema import Lead, EnrichedLead, extract_core_tech_stack

# Archetype matrix mapping keyword triggers to high-leverage winning angles
ARCHETYPES: List[Tuple[List[str], str]] = [
    (
        ["mvp", "from scratch", "prototype", "greenfield", "v1", "founding"],
        "Offer a clickable Figma or 7-day working prototype demo to immediately derisk their launch timeline.",
    ),
    (
        ["migrate", "migration", "refactor", "legacy", "rebuild", "port"],
        "Highlight a zero-downtime database or codebase migration case study and propose a phased rollback strategy.",
    ),
    (
        ["ai", "llm", "agent", "gpt", "fine-tune", "rag", "embeddings", "openai", "anthropic", "machine learning"],
        "Lead with real-world token cost optimization and latency reduction metrics rather than basic prompt wrapper concepts.",
    ),
    (
        ["ios", "android", "react native", "flutter", "swift", "mobile app", "mobile"],
        "Include an immediate TestFlight build or Loom audit of their onboarding flow in your opening outreach.",
    ),
    (
        ["devops", "kubernetes", "terraform", "ci/cd", "pipeline", "aws", "docker", "gcp", "azure"],
        "Propose a paid initial architecture and cloud security audit to identify immediate cost and latency savings.",
    ),
    (
        ["etl", "snowflake", "dbt", "performance", "slow", "postgres", "latency", "sql", "database optimization"],
        "Propose starting with a 48-hour diagnostic benchmark query run to isolate exact database bottlenecks.",
    ),
]

DEFAULT_WINNING_ANGLE = (
    "Lead with a 3-milestone delivery roadmap and 2 direct case study links instead of a traditional resume."
)

ACTION_VERB_PATTERN = re.compile(
    r"\b(build|develop|create|design|migrate|refactor|deploy|integrate|optimize|audit|overhaul|implement|architect|rebuild|scale)\b",
    re.IGNORECASE,
)


def clean_html(text: str) -> str:
    """Remove HTML tags, entities, and excessive whitespace from text."""
    if not text:
        return ""
    cleaned = re.sub(r"<[^>]+>", " ", text)
    cleaned = re.sub(r"&[a-zA-Z]+;", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def extract_deliverables_bullet(lead: Lead) -> str:
    """Extract or generate 25-45 word Scope & Deliverables bullet via deterministic NLP."""
    text = clean_html(lead.description or "")
    if text:
        # Split into sentences
        sentences = re.split(r"(?<=[.!?])\s+", text)
        action_sentences = []
        for s in sentences:
            s_clean = s.strip()
            if ACTION_VERB_PATTERN.search(s_clean) and len(s_clean.split()) >= 4:
                action_sentences.append(s_clean)
                if len(action_sentences) == 2:
                    break

        if action_sentences:
            combined = " ".join(action_sentences)
            words = combined.split()
            if len(words) > 40:
                combined = " ".join(words[:40]).rstrip(".,;:") + "."
            if not combined.endswith("."):
                combined += "."
            return combined

    # Clean fallback based on title and project scope
    title = lead.title.strip()
    return f"Deliver production scope for {title}, ensuring milestones, robust architecture, and requirements are fully met."


def extract_skills_bullet(lead: Lead) -> str:
    """Format required tech skills from lead stack or lexical matching."""
    stack = list(lead.core_tech_stack or [])
    if not stack:
        full_text = f"{lead.title} {lead.description}"
        stack = extract_core_tech_stack(full_text)

    if stack:
        # Deduplicate preserving order
        unique_stack = []
        for tech in stack:
            if tech not in unique_stack:
                unique_stack.append(tech)
        return ", ".join(unique_stack[:6])

    return "Senior Engineering & System Architecture"


def synthesize_winning_angle(lead: Lead) -> str:
    """Categorize job context into archetype and return actionable 1-sentence tip."""
    full_text = f"{lead.title} {lead.description} {' '.join(lead.core_tech_stack)}".lower()

    best_angle = DEFAULT_WINNING_ANGLE
    best_score = 0

    for keywords, angle in ARCHETYPES:
        score = sum(1 for kw in keywords if re.search(rf"\b{re.escape(kw)}\b", full_text))
        if score > best_score:
            best_score = score
            best_angle = angle

    return best_angle


class DealCardGenerator:
    """3-bullet executive deal card generation engine with deterministic NLP fallback."""

    @classmethod
    def generate(
        cls,
        lead: Lead,
        comp_data: Optional[Dict[str, Any]] = None,
    ) -> EnrichedLead:
        """Construct EnrichedLead instance with 3-bullet deal card and budget badge.

        Args:
            lead: Raw or ingested Lead instance.
            comp_data: Extracted compensation metadata dictionary.

        Returns:
            Fully enriched EnrichedLead ready for Telegram dispatch.
        """
        comp = comp_data or {}
        is_high_ticket = bool(comp.get("is_high_ticket", False))
        rate_type = comp.get("rate_type", "unknown")
        min_amount = float(comp.get("min_amount", 0.0))
        max_amount = float(comp.get("max_amount", 0.0))
        currency = comp.get("currency", "USD")
        budget_badge = comp.get("budget_badge", "💰 $2,000+ HIGH-TICKET")

        # 1. Scope & Deliverables
        scope_bullet = extract_deliverables_bullet(lead)

        # 2. Required Skills
        skills_bullet = extract_skills_bullet(lead)

        # 3. Winning Angle
        winning_angle = synthesize_winning_angle(lead)

        return EnrichedLead(
            lead=lead,
            is_high_ticket=is_high_ticket,
            rate_type=rate_type,
            min_amount=min_amount,
            max_amount=max_amount,
            currency=currency,
            budget_badge=budget_badge,
            scope_bullet=scope_bullet,
            skills_bullet=skills_bullet,
            winning_angle=winning_angle,
        )


def generate_deal_card(
    lead: Lead,
    comp_data: Optional[Dict[str, Any]] = None,
) -> EnrichedLead:
    """Convenience functional wrapper for DealCardGenerator.generate."""
    return DealCardGenerator.generate(lead, comp_data)
