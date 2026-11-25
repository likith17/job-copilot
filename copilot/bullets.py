"""Per-bullet tailoring: small, focused model calls, the task a local model is trained on.

The plan engine (tailor.TailorPlan) asks one big call to edit a whole resume.
This engine splits it so a small model can do it well:

1. one call reads the posting and the resume: the posting's requirements, and
   for each, which bullets truthfully support it (evidence);
2. for each bullet that supports a requirement whose terms it doesn't use yet,
   one call: work those terms in if true, or skip. Bullets with nothing to
   surface get no call (asked about every bullet, models just polish them all:
   measured 2026-10-21, 12.8 edits/job and +0.0 keywords);
3. optionally, one call for the summary and skill order.

An edit counts only if it surfaces at least one of its target terms and passes
every check in tailor.check_edit. Every tailoring run (this engine or the plan
engine) logs each bullet it saw to data/training/bullets.jsonl with what
happened to it, so normal use of the app builds the training set
(see copilot/training.py).
"""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from pydantic import BaseModel

from copilot import llm
from copilot.config import ROOT

LOG = ROOT / "data" / "training" / "bullets.jsonl"
MAX_TARGETS = 6          # bullets rewritten per job, most-supported first
SOFT = ("skill", "strong", "fundamental", "communicat", "careful", "systematic", "passion", "ability",
        "experience", "years", "demonstrat", "collaborat", "team player", "detail",
        # generic to the job title or a credential: not something a bullet can "surface"
        "software engineering", "computer science", "bachelor", "master", "phd", "degree", "engineer")


class JobRequirements(BaseModel):
    requirements: list["Requirement"]


class BulletRewrite(BaseModel):
    skip: bool          # true when no honest improvement exists
    text: str           # the improved bullet (or the original when skipping)
    reason: str         # which term it surfaces, or why it was skipped


class Framing(BaseModel):
    summary: str
    skills: list["SkillGroup"]
    orders: list["SectionOrder"]


REQ_SYSTEM = """Read the job posting and the candidate's resume below.
List the 8-12 things the posting most wants. For each:
- "need": the requirement in plain words;
- "keywords": 1-4 SHORT terms (1-3 words) from the posting for it, copied exactly as written:
  tools, languages, methods, domains ("PyTorch", "RAG", "data pipelines", "evaluation").
  Never soft-skill phrases ("strong fundamentals", "communication skills", "careful and systematic");
- "evidence": ids of resume bullets (E1.2, P3.1, ...) whose work truly involves it,
  judging by everything the resume says about that role or project. [] if none.
  Be strict: shared vocabulary is not evidence.
"""

BULLET_SYSTEM = """You tailor ONE resume bullet to a job posting.

You get the posting's requirements, everything the resume says about this role
or project, the bullet, and the posting terms to work in. Rewrite the bullet so
it uses one or more of those terms, but ONLY where this role or project truly
involved that thing. Otherwise set skip=true and return the original unchanged.
Skipping is a good answer.

Rules (code checks every one; breaking any discards your edit):
- Keep every number, metric, and named tool or system from the original, verbatim.
- Never add a tool, skill, scope, responsibility, seniority, or result. "Supported"
  never becomes "led"; "worked on" never becomes "owned".
- Work the term into what the bullet already says; never tack on a claim like
  "demonstrating strong X skills" or "showing Y".
- Keep the length within about 20% of the original. Plain ASCII punctuation.
- Copy every [[...]] token exactly.
- None of: leverage, utilize, spearhead, synergy, cutting-edge, world-class,
  revolutionary, game-changing, highly motivated, results-driven, passionate.
"""

FRAMING_SYSTEM = """You frame a resume for one job posting, without changing any bullet text.
- summary: 2-3 sentences. Open with the job's role title, then the candidate's most
  relevant experience, using the posting's own terms where the resume truly supports
  them. Only facts from the resume (numbers only if they appear there).
- skills: the same groups and the same items, reordered so the posting's needs
  come first. Never add or remove an item.
- orders: optionally reorder bullets within a role (section "E1", "E2", ...) or
  projects (section "PROJECTS", ids "P1", "P2", ...), most relevant first.
"""

_log_lock = threading.Lock()


def _resolve_models():
    # Requirement, SkillGroup and SectionOrder live in tailor.py, which imports this module lazily.
    from copilot.tailor import Requirement, SectionOrder, SkillGroup
    for m in (JobRequirements, Framing):
        m.model_rebuild(_types_namespace={"Requirement": Requirement, "SkillGroup": SkillGroup,
                                          "SectionOrder": SectionOrder})


def uses(text, term):
    from copilot import tailor
    return tailor._mentions(tailor._stems(text), term)


def is_term(keyword):
    """A concrete posting term worth surfacing (1-3 words, not a soft-skill phrase)."""
    words = keyword.split()
    return 0 < len(words) <= 3 and not any(s in keyword.lower() for s in SOFT)


def targets_for(bid, bullet, requirements, limit=3):
    """Concrete posting terms this bullet supports (per the evidence) but doesn't use yet."""
    terms = []
    for r in requirements:
        if bid in (r.get("evidence") or []):
            terms += [k for k in r["keywords"] if is_term(k) and k not in terms and not uses(bullet, k)]
    return terms[:limit]


def bullet_prompt(bullet, item_text, requirements, targets=(), instructions="", examples_text=""):
    """(system, user) for one bullet. This is the fine-tuning input format."""
    system = BULLET_SYSTEM
    if instructions.strip():
        system += "\nTHE CANDIDATE'S STYLE INSTRUCTIONS (the rules above always win):\n" + instructions.strip() + "\n"
    system += examples_text
    reqs = "\n".join(f"- {r['need']} (terms: {', '.join(r['keywords'])})" for r in requirements)
    user = (f"JOB REQUIREMENTS:\n{reqs}\n\nROLE/PROJECT (everything the resume says about it):\n{item_text}"
            f"\n\nBULLET:\n{bullet}\n\nTERMS TO WORK IN, IF TRUE: {', '.join(targets) or '(none)'}")
    return system, user


def extract_requirements(master, job, settings):
    from copilot import tailor
    _resolve_models()
    out, _ = llm.parse(settings, [{"type": "text", "text": REQ_SYSTEM}],
                       f"COMPANY: {job['company']}\nROLE: {job['title']}\n\nJOB POSTING:\n{job['description']}"
                       f"\n\nRESUME:\n{tailor.master_view(master)}", JobRequirements, max_tokens=8000)
    ids = tailor.bullet_ids(master)
    reqs = [r.model_dump() for r in out.requirements]
    for r in reqs:
        r["evidence"] = [e for e in r["evidence"] if e in ids]
    return reqs


def make_plan(master, job, settings, writing, examples, sections, frame=True):
    """Build a TailorPlan-shaped dict from per-bullet calls (see tailor.apply_plan)."""
    from copilot import tailor

    _resolve_models()
    requirements = extract_requirements(master, job, settings)
    compact = settings.get("finetuned", False)     # a fine-tuned model gets the bare task, as in training
    examples_text = "" if compact else tailor.examples_block(examples)
    instructions = "" if compact else writing.get("instructions", "")
    ids = tailor.bullet_ids(master)
    allowed = {"E": "experience" in sections, "P": "projects" in sections}
    targets = {}
    for bid, (sec, i, k) in ids.items():
        t = targets_for(bid, master[sec][i]["bullets"][k], requirements) if allowed[bid[0]] else []
        if t:
            targets[bid] = t
    todo = sorted(targets, key=lambda b: -len(targets[b]))[:MAX_TARGETS]
    rejected = []

    def one(bid):
        sec, i, k = ids[bid]
        bullet = master[sec][i]["bullets"][k]
        item = tailor._item_text(master, sec, i)
        system, user = bullet_prompt(bullet, item, requirements, targets[bid], instructions, examples_text)
        try:
            out, _ = llm.parse(settings, [{"type": "text", "text": system}], user, BulletRewrite, max_tokens=4000)
        except llm.LLMUnavailable:
            raise
        except llm.LLMError:
            return None
        text = tailor.plain(out.text)
        if out.skip or text == bullet:
            return None
        if not any(uses(text, t) for t in targets[bid]):
            rejected.append({"id": bid, "proposed": text, "why": "surfaces none of its target terms"})
            return None
        return {"id": bid, "text": out.text, "reason": out.reason}

    def framing():
        role = tailor.role_headline(job["title"], master.get("headline", ""))
        user = (f"JOB: {job['company']} | {job['title']}\nROLE TITLE TO OPEN WITH: {role}\nREQUIREMENTS:\n"
                + "\n".join(f"- {r['need']} (terms: {', '.join(r['keywords'])})" for r in requirements)
                + "\n\nRESUME:\n" + tailor.master_view(master))
        out, _ = llm.parse(settings, [{"type": "text", "text": FRAMING_SYSTEM}], user, Framing, max_tokens=8000)
        return out.model_dump()

    with ThreadPoolExecutor(max_workers=settings.get("workers", 4)) as pool:
        frame_future = pool.submit(framing) if frame else None
        edits = [e for e in pool.map(one, todo) if e]
        framed = frame_future.result() if frame else {"summary": master["summary"], "skills": master["skills"],
                                                      "orders": []}
    return {"requirements": requirements, "summary": framed["summary"], "skills": framed["skills"],
            "edits": edits, "orders": framed["orders"], "changes": [f"{e['id']}: {e['reason']}" for e in edits],
            "targets": {b: targets[b] for b in todo}, "pre_rejected": rejected}


def log_run(master, job, report, settings, mode):
    """One training record per bullet the writer worked on, with what happened to it."""
    from copilot import tailor
    ids = tailor.bullet_ids(master)
    accepted = {a["id"]: a for a in report.get("accepted", [])}
    rejected = {r["id"]: r for r in report.get("rejected", []) if r["id"] in ids}
    asked = report.get("targets")       # per-bullet mode: the bullets that got a call
    requirements = report.get("requirements", [])
    at = datetime.now().isoformat(timespec="seconds")
    for bid, (sec, i, k) in ids.items():
        if asked is not None and bid not in asked and bid not in accepted:
            continue                    # never asked: says nothing about this bullet
        bullet = master[sec][i]["bullets"][k]
        record = {"job_id": job.get("id"), "company": job.get("company"), "title": job.get("title"),
                  "bullet_id": bid, "bullet": bullet, "item": tailor._item_text(master, sec, i),
                  "requirements": requirements,
                  "targets": (asked or {}).get(bid) or targets_for(bid, bullet, requirements),
                  "model": settings.get("model"), "mode": mode, "at": at}
        if bid in accepted:
            record.update(verdict="accepted", text=accepted[bid]["after"], reason=accepted[bid].get("reason", ""))
        elif bid in rejected:
            record.update(verdict="rejected", text=rejected[bid].get("proposed", ""), reason=rejected[bid]["why"])
        else:
            record.update(verdict="untouched", text=bullet, reason="")
        _log(LOG, record)


def _log(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    with _log_lock, open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
