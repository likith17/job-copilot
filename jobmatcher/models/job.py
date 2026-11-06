from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any
from datetime import datetime
import hashlib


@dataclass
class Job:
    """Normalized job posting model, ATS-agnostic."""

    id: str
    title: str
    company: str
    location: str = ""
    department: str = ""
    team: str = ""
    employment_type: str = ""
    description: str = ""
    description_plain: str = ""
    url: str = ""
    apply_url: str = ""
    source: str = ""  # greenhouse | lever | ashby | custom
    raw_data: Dict[str, Any] = field(default_factory=dict)
    posted_at: Optional[str] = None
    fetched_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    def __post_init__(self):
        # Ensure id is stable; if not provided generate hash
        if not self.id:
            hash_input = f"{self.company}-{self.title}-{self.url}".encode()
            self.id = hashlib.md5(hash_input).hexdigest()[:12]

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("raw_data", None)
        return d

    @property
    def searchable_text(self) -> str:
        """Combined text for ranking/filtering."""
        parts = [
            self.title,
            self.company,
            self.location,
            self.department,
            self.team,
            self.employment_type,
            self.description_plain or self.description,
        ]
        return "\n".join([p for p in parts if p])


@dataclass
class RankedJob:
    """Job with ranking score and explanation."""

    job: Job
    score: float  # 0-100
    explanation: str = ""
    matched_skills: List[str] = field(default_factory=list)
    missing_skills: List[str] = field(default_factory=list)
    title_score: float = 0.0
    skills_score: float = 0.0
    experience_score: float = 0.0
    keyword_score: float = 0.0
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        base = self.job.to_dict()
        base.update(
            {
                "match_score": round(self.score, 2),
                "explanation": self.explanation,
                "matched_skills": ", ".join(self.matched_skills),
                "missing_skills": ", ".join(self.missing_skills),
                "title_score": round(self.title_score, 2),
                "skills_score": round(self.skills_score, 2),
                "experience_score": round(self.experience_score, 2),
                "keyword_score": round(self.keyword_score, 2),
            }
        )
        return base

    def to_csv_row(self) -> Dict[str, Any]:
        return self.to_dict()
