"""Stage 2b: LLM fit grade, the precision step after embedding recall.

The embedding score is good at "is this the same field" and bad at "is this
job actually for me": it ranks chip verification near ML engineering because
the vocabulary overlaps. A short LLM read of each surviving posting fixes the
ordering for a few cents, and only new postings are ever graded.
"""
import re
from typing import Literal

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


class Rubric(BaseModel):
    discipline: Literal["same", "adjacent", "different"]     # is the daily work the candidate's field?
    level: Literal["match", "stretch", "too_senior", "too_junior"]
    core_skills_have: int        # of the posting's 5 most important skills, how many the resume shows (0-5)
    domain_experience: Literal["direct", "related", "none"]
    dealbreakers: list[str]
    reason: str


# Small local models rank poorly when asked for one number, and much better when
# they answer concrete questions and code adds up the score. Measured on 24 jobs
# against Claude Opus grades (2026-11-13): qwen3:8b went from Spearman 0.48 to 0.60,
# at half the time per job.
RUBRIC_SYSTEM = """You screen one job posting for one candidate. Answer each field from the texts only.

discipline: is the DAILY WORK of this job the candidate's field (software / ML / AI engineering)?
  "same" = ML, AI, software or data engineering work. "adjacent" = related technical work with a
  different core (analytics, SRE, robotics hardware integration, solutions). "different" = chip
  design, verification, IT support, operations, sales, recruiting, manufacturing, even if it mentions AI.
level: compare the years and seniority the posting asks for with the candidate's own.
  "match", "stretch" (asks 1-2 years more), "too_senior" (asks far more, or staff/lead), "too_junior".
core_skills_have: pick the posting's 5 most important technical requirements; count how many the
  resume clearly shows (0-5).
domain_experience: has the candidate done this kind of work? "direct", "related" or "none".
dealbreakers: hard blockers the posting states (required PhD, clearance, citizenship, years the
  candidate clearly lacks). Empty if none. Do not consider visa sponsorship.
reason: one sentence naming the deciding factor.

CANDIDATE RESUME:
"""


def rubric_fit(r):
    """0-100 from the rubric answers: field 45, level 20, skills 25, domain 10; a dealbreaker costs 20."""
    score = {"same": 45, "adjacent": 22, "different": 0}[r.discipline]
    score += {"match": 20, "stretch": 12, "too_junior": 8, "too_senior": 0}[r.level]
    score += 5 * max(0, min(5, r.core_skills_have))
    score += {"direct": 10, "related": 5, "none": 0}[r.domain_experience]
    return max(0, score - 20 * bool(r.dealbreakers))


# Sections that say nothing about fit: dropped before grading (fewer tokens, faster).
_NOISE = re.compile(r"^\W*(?:benefits|perks|what we offer|our offer|compensation|salary|pay (?:range|transparency)|"
                    r"base pay|total rewards|equal (?:employment )?opportunit|eeo\b|accommodation|privacy|"
                    r"about (?!the (?:role|team|job|position|opportunity)|you\b|this role)|who we are|our (?:values|"
                    r"mission|culture|story)|why join|why work|life at|how to apply|disclaimer|"
                    r"(?:hybrid|remote|office|location) (?:policy|work)|u\.?s\.? (?:pay|salary)|"
                    r"(?:the )?interview process|recruit(?:ment|ing) process|notice)", re.I)
_DROP_LINE = re.compile(r"equal opportunity employer|without regard to (?:race|sex)|reasonable accommodation|"
                        r"e-verify|privacy (?:notice|policy)|pay transparency", re.I)
MAX_POSTING = 4500


def trim_posting(text, limit=MAX_POSTING):
    """The posting without benefits, legal notices and company boilerplate."""
    out, skipping = [], False
    for line in (text or "").split("\n"):
        s = line.strip()
        heading = bool(s) and len(s.split()) <= 8 and not s.endswith(".") and s[0] not in "-•*–·"
        if heading:
            skipping = bool(_NOISE.match(s))
        if skipping or _DROP_LINE.search(s):
            continue
        out.append(line)
    trimmed = re.sub(r"\n\s*\n\s*\n+", "\n\n", "\n".join(out)).strip()
    # Nothing left means the layout fooled us: grade the whole posting instead.
    return (trimmed if len(trimmed) > 40 else (text or ""))[:limit]


def grade(resume_text, job, settings):
    """settings: llm.task_settings(cfg, "grade"). `method: rubric` (for small models)
    asks concrete questions and scores them in code; the default asks for the grade."""
    user = (f"COMPANY: {job['company']}\nROLE: {job['title']}\nLOCATION: {job['location']}\n\n"
            f"{trim_posting(job['description'])}")
    if settings.get("method") == "rubric":
        system = [{"type": "text", "text": RUBRIC_SYSTEM + resume_text}]
        r, usage = llm.parse(settings, system, user[:9000], Rubric, max_tokens=3000)
        return FitGrade(fit=rubric_fit(r), reason=r.reason, dealbreakers=r.dealbreakers), usage
    # Resume first with the breakpoint on it: every grade in a run shares it.
    system = [{"type": "text", "text": GRADE_SYSTEM + resume_text, "cache_control": {"type": "ephemeral"}}]
    result, usage = llm.parse(settings, system, user, FitGrade, max_tokens=8000)
    result.fit = max(0, min(100, result.fit))
    return result, usage
