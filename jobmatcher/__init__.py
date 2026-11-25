"""
JobMatcher - Modular resume-based job crawler and ranker.

Crawls public career pages and ATS platforms (Greenhouse, Lever, Ashby),
ranks jobs against a resume profile, supports filtering and CSV export.
"""

__version__ = "0.1.0"

from jobmatcher.models.job import Job
from jobmatcher.models.resume import ResumeProfile
from jobmatcher.models.company import CompanyConfig

__all__ = ["Job", "ResumeProfile", "CompanyConfig"]
