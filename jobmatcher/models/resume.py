from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Set, Dict, Optional
import re


@dataclass
class ResumeProfile:
    """Structured representation of a resume."""

    raw_text: str
    name: str = ""
    email: str = ""
    phone: str = ""
    summary: str = ""
    skills: Set[str] = field(default_factory=set)
    experience_years: Optional[int] = None
    titles: List[str] = field(default_factory=list)  # past job titles
    education: List[str] = field(default_factory=list)
    keywords: Set[str] = field(default_factory=set)  # important keywords extracted

    def __post_init__(self):
        # Normalize skills to lower? Keep original case mapping via lower lookup
        # but store normalized lower for matching; keep original set lowercased
        normalized = set()
        for s in self.skills:
            if s:
                normalized.add(s.strip())
        self.skills = normalized

    @property
    def skills_lower(self) -> Set[str]:
        return {s.lower() for s in self.skills}

    @property
    def searchable_text(self) -> str:
        return f"{self.summary}\n{self.raw_text}"

    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "email": self.email,
            "phone": self.phone,
            "summary": self.summary[:500],
            "skills": sorted(self.skills),
            "experience_years": self.experience_years,
            "titles": self.titles,
            "education": self.education,
        }

    @staticmethod
    def extract_email(text: str) -> str:
        match = re.search(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", text)
        return match.group(0) if match else ""

    @staticmethod
    def extract_phone(text: str) -> str:
        # simple US/international phone
        match = re.search(r"(\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}", text)
        return match.group(0) if match else ""
