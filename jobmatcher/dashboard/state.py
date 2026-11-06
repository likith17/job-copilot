from __future__ import annotations

import threading
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field
import time

from jobmatcher.models.resume import ResumeProfile
from jobmatcher.models.company import CompanyConfig
from jobmatcher.models.job import RankedJob


@dataclass
class DashboardState:
    resume_profile: Optional[ResumeProfile] = None
    resume_filename: str = ""
    companies: List[CompanyConfig] = field(default_factory=list)
    companies_source: str = ""  # filename or "manual"
    ranked_jobs: List[RankedJob] = field(default_factory=list)
    status: str = "idle"  # idle, uploading, crawling, done, error
    progress_message: str = ""
    last_error: str = ""
    crawl_stats: Dict[str, Any] = field(default_factory=dict)
    last_crawl_time: Optional[float] = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def to_dict(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "has_resume": self.resume_profile is not None,
                "resume_name": self.resume_filename,
                "resume_skills_count": len(self.resume_profile.skills) if self.resume_profile else 0,
                "resume_skills": sorted(self.resume_profile.skills)[:20] if self.resume_profile else [],
                "resume_titles": self.resume_profile.titles if self.resume_profile else [],
                "resume_experience": self.resume_profile.experience_years if self.resume_profile else None,
                "companies_count": len(self.companies),
                "companies": [{"name": c.name, "ats": c.ats, "identifier": c.identifier} for c in self.companies[:20]],
                "ranked_jobs_count": len(self.ranked_jobs),
                "status": self.status,
                "progress_message": self.progress_message,
                "last_error": self.last_error,
                "crawl_stats": self.crawl_stats,
                "last_crawl_time": self.last_crawl_time,
            }

    def set_status(self, status: str, message: str = "", error: str = ""):
        with self.lock:
            self.status = status
            self.progress_message = message
            if error:
                self.last_error = error
            if status == "error" and not error:
                self.last_error = message

    def set_resume(self, profile: ResumeProfile, filename: str):
        with self.lock:
            self.resume_profile = profile
            self.resume_filename = filename

    def set_companies(self, companies: List[CompanyConfig], source: str = ""):
        with self.lock:
            self.companies = companies
            self.companies_source = source

    def set_ranked_jobs(self, ranked: List[RankedJob], stats: Dict[str, Any] = None):
        with self.lock:
            self.ranked_jobs = ranked
            self.crawl_stats = stats or {}
            self.last_crawl_time = time.time()
            self.status = "done"
            self.progress_message = f"Crawled {stats.get('total_fetched', len(ranked))} jobs, {len(ranked)} after dedup & ranking" if stats else f"{len(ranked)} jobs ranked"

    def clear(self):
        with self.lock:
            self.resume_profile = None
            self.resume_filename = ""
            self.companies = []
            self.companies_source = ""
            self.ranked_jobs = []
            self.status = "idle"
            self.progress_message = ""
            self.last_error = ""
            self.crawl_stats = {}


# Singleton state
_global_state = DashboardState()


def get_state() -> DashboardState:
    return _global_state
