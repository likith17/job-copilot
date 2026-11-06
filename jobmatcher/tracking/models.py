from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, Any
from datetime import datetime
from enum import Enum
import uuid


class ApplicationStatus(str, Enum):
    """Application tracking statuses as requested."""

    INTERESTED = "Interested"
    APPLIED = "Applied"
    INTERVIEWING = "Interviewing"
    OFFER = "Offer"
    REJECTED = "Rejected"

    # Additional helpful statuses but keep core 5 as primary
    SAVED = "Saved"  # Alias for Interested, for backward compat
    ARCHIVED = "Archived"

    @classmethod
    def values(cls) -> list[str]:
        return [s.value for s in cls]

    @classmethod
    def is_valid(cls, value: str) -> bool:
        try:
            cls(value)
            return True
        except ValueError:
            # Check case-insensitive
            return value.lower().capitalize() in [v.lower().capitalize() for v in cls.values()] or value.lower() in [v.lower() for v in cls.values()]

    @classmethod
    def normalize(cls, value: str) -> "ApplicationStatus":
        """Normalize string to enum, case-insensitive."""
        if not value:
            return cls.INTERESTED
        # Direct match
        try:
            return cls(value)
        except ValueError:
            pass
        # Case-insensitive match
        lower = value.lower()
        mapping = {
            "interested": cls.INTERESTED,
            "saved": cls.INTERESTED,
            "wishlist": cls.INTERESTED,
            "applied": cls.APPLIED,
            "interviewing": cls.INTERVIEWING,
            "interview": cls.INTERVIEWING,
            "offer": cls.OFFER,
            "offered": cls.OFFER,
            "rejected": cls.REJECTED,
            "reject": cls.REJECTED,
            "archived": cls.ARCHIVED,
        }
        return mapping.get(lower, cls.INTERESTED)


@dataclass
class Bookmark:
    """Represents a bookmarked job with tracking info."""

    # Core job identification - persisted
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])  # bookmark id
    job_id: str = ""  # original job id from ATS
    company: str = ""
    title: str = ""
    location: str = ""
    department: str = ""
    url: str = ""
    apply_url: str = ""
    source: str = ""
    description: str = ""  # short snippet or full?

    # Tracking fields
    status: ApplicationStatus = ApplicationStatus.INTERESTED
    notes: str = ""
    match_score: Optional[float] = None
    explanation: str = ""

    # Timestamps
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    # Full job data snapshot (optional)
    job_data: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        # Ensure status is enum
        if isinstance(self.status, str):
            self.status = ApplicationStatus.normalize(self.status)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # Convert enum to value
        d["status"] = self.status.value if isinstance(self.status, ApplicationStatus) else self.status
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Bookmark":
        # Handle status normalization
        status_raw = data.get("status", "Interested")
        try:
            status = ApplicationStatus.normalize(status_raw)
        except Exception:
            status = ApplicationStatus.INTERESTED

        return cls(
            id=data.get("id", str(uuid.uuid4())[:12]),
            job_id=data.get("job_id", data.get("jobId", "")),
            company=data.get("company", ""),
            title=data.get("title", ""),
            location=data.get("location", ""),
            department=data.get("department", ""),
            url=data.get("url", ""),
            apply_url=data.get("apply_url", data.get("applyUrl", "")),
            source=data.get("source", ""),
            description=data.get("description", "")[:500] if data.get("description") else "",
            status=status,
            notes=data.get("notes", ""),
            match_score=data.get("match_score", data.get("matchScore")),
            explanation=data.get("explanation", ""),
            created_at=data.get("created_at", datetime.utcnow().isoformat()),
            updated_at=data.get("updated_at", datetime.utcnow().isoformat()),
            job_data=data.get("job_data", data.get("jobData", {})),
        )

    def update_timestamp(self):
        self.updated_at = datetime.utcnow().isoformat()

    @classmethod
    def from_job(cls, job, status: str = "Interested", notes: str = "", match_score: Optional[float] = None, explanation: str = "") -> "Bookmark":
        """Create bookmark from Job or RankedJob or dict."""
        from jobmatcher.models.job import Job, RankedJob

        if isinstance(job, RankedJob):
            j = job.job
            ms = job.score
            exp = job.explanation
            job_data = j.to_dict()
            # Add ranked extras
            job_data.update({"match_score": ms, "explanation": exp})
            return cls(
                job_id=j.id,
                company=j.company,
                title=j.title,
                location=j.location,
                department=j.department,
                url=j.url,
                apply_url=j.apply_url,
                source=j.source,
                description=(j.description_plain or j.description)[:500],
                status=ApplicationStatus.normalize(status),
                notes=notes,
                match_score=match_score if match_score is not None else ms,
                explanation=explanation or exp,
                job_data=job_data,
            )
        elif isinstance(job, Job):
            j = job
            return cls(
                job_id=j.id,
                company=j.company,
                title=j.title,
                location=j.location,
                department=j.department,
                url=j.url,
                apply_url=j.apply_url,
                source=j.source,
                description=(j.description_plain or j.description)[:500],
                status=ApplicationStatus.normalize(status),
                notes=notes,
                match_score=match_score,
                explanation=explanation,
                job_data=j.to_dict(),
            )
        elif isinstance(job, dict):
            # Dict could be from API
            return cls(
                job_id=job.get("id", job.get("job_id", "")),
                company=job.get("company", ""),
                title=job.get("title", ""),
                location=job.get("location", ""),
                department=job.get("department", ""),
                url=job.get("url", ""),
                apply_url=job.get("apply_url", job.get("applyUrl", "")),
                source=job.get("source", ""),
                description=job.get("description", "")[:500],
                status=ApplicationStatus.normalize(status or job.get("status", "Interested")),
                notes=notes or job.get("notes", ""),
                match_score=match_score if match_score is not None else job.get("match_score"),
                explanation=explanation or job.get("explanation", ""),
                job_data=job,
            )
        else:
            raise ValueError(f"Unsupported job type for bookmark: {type(job)}")
