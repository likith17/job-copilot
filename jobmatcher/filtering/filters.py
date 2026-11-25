from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional
import re

from jobmatcher.models.job import Job, RankedJob


@dataclass
class FilterCriteria:
    min_score: Optional[float] = None
    max_score: Optional[float] = None
    companies: List[str] = field(default_factory=list)
    locations: List[str] = field(default_factory=list)  # substring match
    keywords: List[str] = field(default_factory=list)  # must contain one of
    exclude_keywords: List[str] = field(default_factory=list)
    title_contains: List[str] = field(default_factory=list)
    remote_only: bool = False
    employment_types: List[str] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)  # greenhouse, lever, ashby


class JobFilter:
    """Filtering engine for ranked and unranked jobs."""

    def __init__(self, criteria: FilterCriteria):
        self.criteria = criteria

    def matches(self, ranked: RankedJob) -> bool:
        job = ranked.job
        c = self.criteria

        # Score filters
        if c.min_score is not None and ranked.score < c.min_score:
            return False
        if c.max_score is not None and ranked.score > c.max_score:
            return False

        # Company filter
        if c.companies:
            if not any(comp.lower() in job.company.lower() for comp in c.companies):
                return False

        # Location filter - OR logic
        if c.locations:
            if not any(loc.lower() in job.location.lower() for loc in c.locations):
                return False

        if c.remote_only:
            if not self._is_remote(job):
                return False

        # Keywords: job searchable text must contain at least one
        searchable = job.searchable_text.lower()
        if c.keywords:
            if not any(kw.lower() in searchable for kw in c.keywords):
                return False

        if c.exclude_keywords:
            if any(kw.lower() in searchable for kw in c.exclude_keywords):
                return False

        if c.title_contains:
            title_lower = job.title.lower()
            if not any(kw.lower() in title_lower for kw in c.title_contains):
                return False

        if c.employment_types:
            emp = (job.employment_type or "").lower()
            dept = (job.department or "").lower()
            combined = f"{emp} {dept}"
            if not any(et.lower() in combined for et in c.employment_types):
                return False

        if c.sources:
            if job.source.lower() not in [s.lower() for s in c.sources]:
                return False

        return True

    def filter(self, jobs: List[RankedJob]) -> List[RankedJob]:
        return [j for j in jobs if self.matches(j)]

    @staticmethod
    def _is_remote(job: Job) -> bool:
        text = f"{job.location} {job.title} {job.description_plain}".lower()
        remote_indicators = ["remote", "work from home", "wfh", "distributed", "anywhere"]
        return any(ind in text for ind in remote_indicators)


def filter_jobs(jobs: List[RankedJob], criteria: FilterCriteria) -> List[RankedJob]:
    f = JobFilter(criteria)
    return f.filter(jobs)
