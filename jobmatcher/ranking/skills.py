from __future__ import annotations

from typing import Set, List, Tuple
from jobmatcher.parsers.skills import extract_skills


class SkillMatcher:
    """Matches resume skills against job description."""

    def __init__(self, custom_skills: List[str] | None = None):
        self.custom_skills = custom_skills or []

    def match(self, resume_skills: Set[str], job_text: str) -> Tuple[Set[str], Set[str], float, Set[str]]:
        """
        Match resume skills vs job description.

        Returns:
            matched_skills, missing_skills, score (0-1), job_skills
        """
        if not job_text:
            return set(), set(), 0.0, set()

        job_skills = extract_skills(job_text, custom_skills=self.custom_skills)

        if not job_skills:
            # No skills detected in job -> neutral scoring, return 0.5?
            # Better to return 0 but let caller handle
            return set(), set(), 0.0, set()

        resume_lower = {s.lower(): s for s in resume_skills}
        job_lower = {s.lower(): s for s in job_skills}

        matched_lower = set(resume_lower.keys()) & set(job_lower.keys())

        matched = {resume_lower[low] for low in matched_lower}
        # Actually preserve canonical names from job side may be better, but use resume original case
        missing_lower = set(job_lower.keys()) - set(resume_lower.keys())
        missing = {job_lower[low] for low in missing_lower}

        # Jaccard-like score but weighted by matched / job_skills count
        # If resume has many skills, but job requires few, we care about covering job requirements
        score = len(matched) / len(job_skills) if job_skills else 0.0

        return matched, missing, score, job_skills

    def extract_job_skills(self, job_text: str) -> Set[str]:
        return extract_skills(job_text, custom_skills=self.custom_skills)
