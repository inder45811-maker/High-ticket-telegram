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


def sanitize_title(title: str) -> str:
    """Clean job/contract titles from scraper tags and informal markers."""
    if not title:
        return "Senior Contract Specialist"
    cleaned = title.strip()
    # Remove leading [Hiring], [HIRING], Hiring:, etc.
    cleaned = re.sub(r"(?i)^\[\s*hiring\s*\]\s*[:-]?\s*", "", cleaned)
    cleaned = re.sub(r"(?i)^hiring\s*[:-]\s*", "", cleaned)
    # Remove [Remote], [Paid], [Contract], [Freelance] tags
    cleaned = re.sub(r"(?i)\[\s*(?:remote|paid|contract|freelance|b2b|fixed|hourly)\s*\]", "", cleaned)
    # Remove trailing price markers like - $4,000 or ($3k-$5k)
    cleaned = re.sub(r"[\(\[\-]\s*[\$€£]\s*\d+.*$", "", cleaned)
    # Remove trailing location markers like | Remote or (Remote)
    cleaned = re.sub(r"(?i)[\|–-]\s*remote\s*$", "", cleaned)
    cleaned = re.sub(r"(?i)\(\s*remote\s*\)$", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" :-|;,")
    if not cleaned:
        cleaned = title.strip()
    return cleaned


def sanitize_client_name(client: str, title: str = "", description: str = "", source: str = "") -> str:
    """Sanitize raw author handles (/u/...) into executive B2B client profiles."""
    raw = (client or "").strip()
    text = f"{title} {description}".lower()

    # Detect Reddit username handles or anonymous / unstated
    is_handle = (
        raw.startswith("/u/") or 
        raw.startswith("u/") or 
        raw.lower() in ("anonymous", "direct client", "direct", "unknown", "none", "") or
        bool(re.match(r"^user_\w+", raw, re.IGNORECASE)) or
        ("_" in raw and len(raw) < 22)
    )

    if is_handle:
        if any(k in text for k in ["ai", "llm", "rag", "agent", "machine learning", "deep learning"]):
            return "Venture-Backed AI Lab (Direct)"
        elif any(k in text for k in ["crypto", "web3", "solidity", "defi", "blockchain"]):
            return "Web3 Protocol Client (Direct)"
        elif any(k in text for k in ["ecommerce", "shopify", "dtc", "woocommerce"]):
            return "E-Commerce Growth Brand (Direct)"
        elif any(k in text for k in ["fintech", "banking", "payments", "trading"]):
            return "Fintech Scaleup (Direct)"
        elif any(k in text for k in ["saas", "b2b", "cloud", "devops", "kubernetes"]):
            return "B2B SaaS Scaleup (Direct)"
        elif any(k in text for k in ["ios", "android", "mobile", "flutter", "react native"]):
            return "Consumer Mobile Scaleup (Direct)"
        elif (source or "").lower() == "hackernews":
            return "Y Combinator / HN Founder (Direct)"
        return "Direct Venture Client"

    # Clean valid corporate names: remove raw punctuation
    clean_corp = re.sub(r"\s+", " ", raw).strip()
    return clean_corp if clean_corp else "Direct Enterprise Client"


def synthesize_business_requirements(lead: Lead, clean_title_str: str, deliverables_str: str) -> Dict[str, Any]:
    """Generate structured B2B Business Requirements from lead metadata."""
    full_text = f"{lead.title} {lead.description}".lower()

    if "retainer" in full_text or "monthly" in full_text or "/mo" in full_text:
        engagement_type = "High-Ticket Advisory Retainer"
    elif "audit" in full_text or "assessment" in full_text:
        engagement_type = "Diagnostic Architecture SOW"
    elif "hourly" in full_text or "/hr" in full_text:
        engagement_type = "Senior Contracting SOW (Hourly)"
    else:
        engagement_type = "Milestone Escrow Contract"

    obj = f"Lead executive technical delivery and milestone execution for {clean_title_str}."

    reqs = []
    if deliverables_str:
        reqs.append(f"Deliverable: {deliverables_str}")
    else:
        reqs.append(f"Deliver production architecture and core milestones for {clean_title_str}.")

    tech_stack = extract_skills_bullet(lead)

    return {
        "commercial_objective": obj,
        "business_requirements": reqs,
        "engagement_type": engagement_type,
        "required_capabilities": tech_stack
    }


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

        # 4. Executive Sanitization & Structured Business Requirements
        clean_title_str = sanitize_title(lead.title)
        client_display_str = sanitize_client_name(
            lead.client, title=lead.title, description=lead.description, source=lead.source
        )
        b2b_reqs = synthesize_business_requirements(lead, clean_title_str, scope_bullet)

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
            clean_title=clean_title_str,
            client_display=client_display_str,
            engagement_type=b2b_reqs["engagement_type"],
            commercial_objective=b2b_reqs["commercial_objective"],
            business_requirements=b2b_reqs["business_requirements"],
            required_capabilities=b2b_reqs["required_capabilities"],
        )


def generate_deal_card(
    lead: Lead,
    comp_data: Optional[Dict[str, Any]] = None,
) -> EnrichedLead:
    """Convenience functional wrapper for DealCardGenerator.generate."""
    return DealCardGenerator.generate(lead, comp_data)
