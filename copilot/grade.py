"""Stage 2b: LLM fit grade, the precision step after embedding recall.

The embedding score is good at "is this the same field" and bad at "is this
job actually for me": it ranks chip verification near ML engineering because
the vocabulary overlaps. A short LLM read of each surviving posting fixes the
ordering for a few cents, and only new postings are ever graded.
"""
from pydantic import BaseModel

from copilot import llm


class FitGrade(BaseModel):
    fit: int                  # 0-100
    reason: str               # one sentence
    dealbreakers: list[str]   # hard blockers found in the posting


GRADE_SYSTEM = """You screen job postings for one candidate. Grade how strong a
fit each posting is for the candidate's background and level, from 0 to 100:

90-100  core skills and level both match; a strong application
70-89   good match with minor gaps
50-69   adjacent field or notable gaps
0-49    different discipline, wrong level, or blocked

Judge the actual work, not shared buzzwords: chip design, verification, IT
support, data-center operations, sales and recruiting are different
disciplines from ML/software engineering even when they mention AI.

dealbreakers: list only hard blockers the posting states, such as a required
PhD, an active security clearance, US citizenship, or a minimum years of
experience the candidate clearly lacks. Leave it empty when there are none.
Sponsorship is checked separately; do not grade on it.

reason: one sentence naming the deciding factor.

CANDIDATE RESUME:
"""


def grade(resume_text, job, settings):
    """settings: llm.task_settings(cfg, "grade")."""
    # Resume first with the breakpoint on it: every grade in a run shares it.
    system = [{"type": "text", "text": GRADE_SYSTEM + resume_text, "cache_control": {"type": "ephemeral"}}]
    user = f"COMPANY: {job['company']}\nROLE: {job['title']}\nLOCATION: {job['location']}\n\n{job['description']}"
    result, usage = llm.parse(settings, system, user, FitGrade, max_tokens=8000)
    result.fit = max(0, min(100, result.fit))
    return result, usage
