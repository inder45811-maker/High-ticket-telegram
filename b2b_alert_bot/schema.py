"""Data schemas and normalization models for B2B Alert Bot."""

from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any
import re


# Standard taxonomy of core tech stack keywords
TECH_TAXONOMY = [
    # Languages
    "Python", "TypeScript", "JavaScript", "Golang", "Go", "Rust", "Java",
    "C++", "C#", "Ruby", "PHP", "Swift", "Kotlin", "Solidity", "SQL",
    # Frameworks & Libraries
    "React", "Next.js", "Vue", "Angular", "Node.js", "Express", "FastAPI",
    "Django", "Flask", "Ruby on Rails", "Spring Boot", "Svelte", "Tailwind",
    # Databases & Queues
    "PostgreSQL", "MySQL", "MongoDB", "Redis", "Elasticsearch", "Supabase",
    "Firebase", "Kafka", "RabbitMQ", "SQLite",
    # Cloud & DevOps
    "AWS", "GCP", "Azure", "Docker", "Kubernetes", "Terraform", "Linux", "Cloudflare",
    # AI & Automation
    "OpenAI", "LLM", "LangChain", "LlamaIndex", "PyTorch", "TensorFlow", "RAG",
    # SaaS & Platforms
    "Shopify", "WordPress", "Stripe", "Webflow", "HubSpot", "Salesforce"
]


def extract_core_tech_stack(text: str) -> List[str]:
    """Extract recognized technology keywords from text using boundary-safe regex."""
    if not text:
        return []

    found = []
    text_lower = text.lower()

    for tech in TECH_TAXONOMY:
        if tech.lower() == "go":
            if (re.search(r"\bgolang\b", text_lower) or re.search(r"\bGo\b", text)) and "Go" not in found:
                found.append("Go")
        else:
            pattern = rf"\b{re.escape(tech.lower())}\b"
            if re.search(pattern, text_lower) and tech not in found:
                found.append(tech)

    return found


@dataclass
class Lead:
    """Standardized representation of a high-value contract or job opportunity."""
    id: str                         # SHA-256 hash of canonical URL or composite identifier
    title: str                      # Raw or cleaned job title
    source: str                     # "weworkremotely", "remoteok", "jobspresso", "hackernews", "reddit"
    client: str                     # Company or hiring entity
    url: str                        # Canonical apply URL
    raw_compensation: str = ""      # Raw compensation string or snippet
    description: str = ""           # Full text or HTML snippet
    published_at: Optional[str] = None  # ISO timestamp
    core_tech_stack: List[str] = field(default_factory=list)
    raw_metadata: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> bool:
        """Validate lead integrity and field constraints."""
        if not self.id or len(self.id) != 64:
            raise ValueError(f"Invalid lead id: must be 64-char SHA-256 hex string, got '{self.id}'")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", self.id):
            raise ValueError(f"Lead id contains non-hex characters: '{self.id}'")
        if not self.title or not self.title.strip():
            raise ValueError("Lead title cannot be empty")
        if not self.source or not self.source.strip():
            raise ValueError("Lead source cannot be empty")
        if not self.url or not (self.url.startswith("http://") or self.url.startswith("https://")):
            raise ValueError(f"Invalid lead URL: '{self.url}'")
        return True

    def to_dict(self) -> Dict[str, Any]:
        """Serialize Lead instance to dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Lead":
        """Construct Lead instance from dictionary."""
        return cls(
            id=data["id"],
            title=data.get("title", ""),
            source=data.get("source", ""),
            client=data.get("client", ""),
            url=data.get("url", ""),
            raw_compensation=data.get("raw_compensation", ""),
            description=data.get("description", ""),
            published_at=data.get("published_at"),
            core_tech_stack=list(data.get("core_tech_stack", [])),
            raw_metadata=dict(data.get("raw_metadata", {}))
        )


@dataclass
class EnrichedLead:
    """Enriched lead structure produced by R2 filter for R3 dispatcher."""
    lead: Lead
    is_high_ticket: bool            # True if comp >= $2,000 fixed/mo or >= $50/hr
    rate_type: str                  # "fixed", "hourly", "monthly", "annual", "unknown"
    min_amount: float               # Normalized USD amount
    max_amount: float               # Normalized USD amount
    currency: str                   # "USD", "EUR", "GBP", etc.
    budget_badge: str               # e.g., "💰 $4,500 FIXED", "⏱️ $75/HR"
    scope_bullet: str               # Deliverables & scope summary
    skills_bullet: str              # Key required tech & skills
    winning_angle: str              # Actionable 1-sentence application tip

    def to_dict(self) -> Dict[str, Any]:
        """Serialize EnrichedLead instance to dictionary."""
        return {
            "lead": self.lead.to_dict(),
            "is_high_ticket": self.is_high_ticket,
            "rate_type": self.rate_type,
            "min_amount": self.min_amount,
            "max_amount": self.max_amount,
            "currency": self.currency,
            "budget_badge": self.budget_badge,
            "scope_bullet": self.scope_bullet,
            "skills_bullet": self.skills_bullet,
            "winning_angle": self.winning_angle
        }
