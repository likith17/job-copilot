from __future__ import annotations

import re
from typing import List, Tuple
from dataclasses import dataclass

from jobmatcher.models.job import Job, RankedJob
from jobmatcher.models.resume import ResumeProfile
from jobmatcher.ranking.skills import SkillMatcher
from jobmatcher.parsers.skills import extract_skills


@dataclass
class RankingWeights:
    skills: float = 0.5
    title: float = 0.25
    experience: float = 0.1
    keywords: float = 0.15

    def normalize(self):
        total = self.skills + self.title + self.experience + self.keywords
        if total == 0:
            return
        self.skills /= total
        self.title /= total
        self.experience /= total
        self.keywords /= total


class RankingEngine:
    """
    Ranks jobs against a resume profile.

    Scoring breakdown:
    - Skills match (Jaccard coverage of job-required skills): 50%
    - Title relevance (token overlap vs resume past titles + skills): 25%
    - Experience alignment: 10%
    - Keyword / TF overlap: 15%

    Produces 0-100 score + explanation.
    """

    def __init__(self, weights: RankingWeights | None = None, custom_skills: List[str] | None = None):
        self.weights = weights or RankingWeights()
        self.weights.normalize()
        self.skill_matcher = SkillMatcher(custom_skills=custom_skills)
        self.custom_skills = custom_skills or []

    def rank(self, resume: ResumeProfile, job: Job) -> RankedJob:
        job_text = job.searchable_text

        # Skills
        matched, missing, skills_score, job_skills = self.skill_matcher.match(resume.skills, job_text)

        # Title relevance
        title_score = self._score_title(resume, job)

        # Experience
        exp_score = self._score_experience(resume, job)

        # Keyword overlap (simple TF)
        keyword_score = self._score_keywords(resume, job)

        # Weighted final
        final = (
            skills_score * self.weights.skills
            + title_score * self.weights.title
            + exp_score * self.weights.experience
            + keyword_score * self.weights.keywords
        )
        # Scale to 0-100
        score_100 = round(final * 100, 2)

        explanation = self._generate_explanation(
            resume, job, matched, missing, job_skills, skills_score, title_score, exp_score, keyword_score, score_100
        )

        return RankedJob(
            job=job,
            score=score_100,
            explanation=explanation,
            matched_skills=sorted(matched),
            missing_skills=sorted(missing),
            title_score=title_score * 100,
            skills_score=skills_score * 100,
            experience_score=exp_score * 100,
            keyword_score=keyword_score * 100,
            details={
                "job_skills": sorted(job_skills),
                "resume_skills_count": len(resume.skills),
            },
        )

    def rank_all(self, resume: ResumeProfile, jobs: List[Job]) -> List[RankedJob]:
        ranked = [self.rank(resume, job) for job in jobs]
        # Sort descending by score
        ranked.sort(key=lambda r: r.score, reverse=True)
        return ranked

    def _score_title(self, resume: ResumeProfile, job: Job) -> float:
        """
        Title relevance: how well job title matches resume titles/skills.
        Token overlap + exact title match bonus.
        """
        if not job.title:
            return 0.0

        job_title_lower = job.title.lower()
        resume_titles_lower = [t.lower() for t in resume.titles]
        resume_skills_lower = {s.lower() for s in resume.skills}

        # Exact title match
        for rt in resume_titles_lower:
            if rt and rt in job_title_lower:
                return 1.0
            # Also check if significant word overlap
            rt_tokens = set(rt.split())
            jt_tokens = set(job_title_lower.split())
            if rt_tokens & jt_tokens:
                overlap = len(rt_tokens & jt_tokens) / max(len(rt_tokens), 1)
                if overlap >= 0.5:
                    return 0.8

        # Skill mentions in title (e.g., "Python Engineer")
        job_title_tokens = set(re.findall(r"\w+", job_title_lower))
        skill_tokens_in_title = job_title_tokens & resume_skills_lower
        if skill_tokens_in_title:
            # e.g., 1 matching skill token out of maybe 3 tokens = fairly relevant
            return min(0.7 + 0.1 * len(skill_tokens_in_title), 1.0)

        # Generic engineering titles alignment
        if any(k in job_title_lower for k in ["engineer", "developer", "scientist", "manager"]):
            if any(k in " ".join(resume_titles_lower) for k in ["engineer", "developer", "scientist", "manager"]):
                return 0.5

        return 0.2  # baseline low

    def _score_experience(self, resume: ResumeProfile, job: Job) -> float:
        """
        Experience alignment: parse required years from job description.
        If resume has more years than required, high score; if less, lower.
        If no info, neutral 0.7.
        """
        if resume.experience_years is None:
            return 0.7

        # Extract years from job description
        text = job.description_plain or job.description or ""
        # Look for patterns like "3+ years", "5 years", "minimum 2 years"
        years_required = None
        patterns = [
            r"(\d+)\+?\s*years?\s+of\s+experience",
            r"(\d+)\+?\s*years?\s+experience",
            r"minimum\s+(\d+)\s+years?",
            r"at least\s+(\d+)\s+years?",
            r"(\d+)\s*-\s*\d+\s+years?\s+experience",
        ]
        for pat in patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                try:
                    years_required = int(m.group(1))
                    break
                except ValueError:
                    continue

        if years_required is None:
            return 0.7  # neutral

        resume_years = resume.experience_years
        if resume_years >= years_required:
            # Overqualified slightly okay, but not too much? Cap at 1.0
            # If resume has double required, still 1.0
            return 1.0
        else:
            # Underqualified: linear penalty
            # e.g., required 5, have 3 => 0.6
            return max(0.1, resume_years / years_required)

    def _score_keywords(self, resume: ResumeProfile, job: Job) -> float:
        """
        Keyword overlap: simple bag-of-words cosine-like.
        Compute overlap of important tokens (excluding stopwords).
        """
        STOPWORDS = {
            "the", "and", "or", "a", "an", "in", "on", "for", "with", "to", "of", "is", "are", "be", "will",
            "you", "we", "our", "your", "this", "that", "as", "at", "by", "from", "it", "we're", "you're",
        }

        def tokenize(text: str) -> set[str]:
            tokens = re.findall(r"\b[a-zA-Z]{3,}\b", text.lower())
            return {t for t in tokens if t not in STOPWORDS}

        resume_tokens = tokenize(resume.raw_text)
        job_tokens = tokenize(job.searchable_text)

        if not resume_tokens or not job_tokens:
            return 0.0

        overlap = resume_tokens & job_tokens
        # Jaccard
        jaccard = len(overlap) / len(resume_tokens | job_tokens) if (resume_tokens | job_tokens) else 0.0
        # Also consider recall: how much of job tokens covered by resume
        recall = len(overlap) / len(job_tokens) if job_tokens else 0.0

        # Combine: weighted
        return 0.4 * jaccard + 0.6 * recall

    def _generate_explanation(
        self,
        resume: ResumeProfile,
        job: Job,
        matched: set[str],
        missing: set[str],
        job_skills: set[str],
        skills_score: float,
        title_score: float,
        exp_score: float,
        keyword_score: float,
        final_score: float,
    ) -> str:
        parts = []

        # Overall assessment
        if final_score >= 80:
            parts.append(f"Excellent match ({final_score:.0f}/100).")
        elif final_score >= 60:
            parts.append(f"Strong match ({final_score:.0f}/100).")
        elif final_score >= 40:
            parts.append(f"Moderate match ({final_score:.0f}/100).")
        else:
            parts.append(f"Weak match ({final_score:.0f}/100).")

        # Skills
        if job_skills:
            parts.append(f"Skills: matched {len(matched)}/{len(job_skills)}")
            if matched:
                parts.append(f"✅ {', '.join(sorted(matched)[:5])}")
            if missing:
                # Show top 3 missing
                missing_preview = ", ".join(sorted(missing)[:4])
                parts.append(f"Missing: {missing_preview}")
        else:
            parts.append(f"Skills: no specific tech stack detected in posting (resume has {len(resume.skills)} skills)")

        # Title
        if title_score >= 0.8:
            parts.append(f"Title alignment high: '{job.title}' matches your background {resume.titles[:2]}")
        elif title_score >= 0.5:
            parts.append(f"Title somewhat relevant: '{job.title}'")
        else:
            parts.append(f"Title alignment low for '{job.title}' vs {resume.titles[:2] if resume.titles else 'profile'}")

        # Experience
        if exp_score >= 0.9:
            parts.append("Experience level fits.")
        elif exp_score < 0.5 and resume.experience_years is not None:
            parts.append(f"May require more experience than your {resume.experience_years}y")

        # Keyword
        if keyword_score > 0.5:
            parts.append("Good keyword overlap with resume.")

        return " | ".join(parts)


def rank_jobs(resume: ResumeProfile, jobs: List[Job], weights: RankingWeights | None = None) -> List[RankedJob]:
    engine = RankingEngine(weights=weights)
    return engine.rank_all(resume, jobs)
