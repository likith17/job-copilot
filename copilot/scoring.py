"""Stage 2 and 3 scoring, built on the scorer already validated in agent.py.

agent.py loads the embedding model and API client at import, so it is
imported lazily: fetch-only commands and tests stay fast.
"""
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel


@lru_cache(maxsize=1)
def _agent():
    import agent
    from copilot import embedcache
    agent.embed_cache = embedcache
    return agent


def match(resume_text, job_description):
    """Free, local hybrid score (embeddings + skill coverage)."""
    combined, matched, missing = _agent().hybrid_score(resume_text, job_description)
    return float(combined), matched, missing


class SponsorshipRead(BaseModel):
    status: Literal["sponsors", "no_sponsorship", "not_mentioned"]
    evidence: str
    reasoning: str


def sponsorship(job_description, settings):
    """jd_sponsorship.py's classifier, run on whichever provider the task uses.

    Same prompt and the same rule: a rejection quote that can't be found in the
    posting is not trusted (final_score treats it as not_mentioned).
    """
    import jd_sponsorship as js
    from copilot import llm

    try:
        out, _ = llm.parse(settings, [{"type": "text", "text": js.SYSTEM_PROMPT}],
                           f"JOB POSTING:\n{job_description}", SponsorshipRead, max_tokens=8000)
    except llm.LLMUnavailable:
        raise
    except llm.LLMError:
        return dict(js.NEUTRAL)
    evidence = out.evidence if out.status != "not_mentioned" else ""
    return {"status": out.status, "evidence": evidence, "reasoning": out.reasoning,
            "verified": js._verify(evidence, job_description)}


def sponsorship_cached(cfg, job_description, settings):
    """Sponsorship depends only on the posting, so it's asked once per posting text."""
    import jd_sponsorship as js
    from copilot import db

    key = f"sponsorship:{settings.get('model')}:{db.text_key(job_description)}"
    conn = db.connect(cfg.db_path)
    try:
        hit = db.cache_get(conn, key)
        if hit is not None:
            return hit
        result = sponsorship(job_description, settings)
        if result != js.NEUTRAL:       # a failed call isn't an answer
            db.cache_put(conn, key, result)
            conn.commit()
        return result
    finally:
        conn.close()


def final(resume_text, job_description, company, jd_result):
    """Sponsorship-weighted score. jd_result is passed in so the LLM runs once per job."""
    return _agent().final_score(resume_text, job_description, company, jd_result=jd_result)
