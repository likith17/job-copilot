"""Stage 3: tailor the resume and draft a cover letter for one posting.

Honesty is enforced structurally, not just by prompt:
- Employers, titles, dates, education and publications are copied from the
  master resume by index. The model never writes them.
- The model may only rephrase bullets, reorder skills, pick projects, and
  write the summary and cover letter.
- A deterministic guard then checks every number and every known skill in the
  output against the master resume. Unsupported claims get one corrective
  retry; whatever survives is flagged for human review, never silently kept.
"""
import copy
import hashlib
import json
import re

from pydantic import BaseModel

from copilot import llm

from skills import SKILLS

# ---------------- schemas ----------------


class Link(BaseModel):
    label: str
    url: str


class Contact(BaseModel):
    name: str
    location: str
    phone: str
    email: str
    links: list[Link]


class Education(BaseModel):
    school: str
    degree: str
    dates: str
    details: list[str]


class SkillGroup(BaseModel):
    category: str
    items: list[str]


class Role(BaseModel):
    title: str
    company: str
    dates: str
    bullets: list[str]


class Project(BaseModel):
    name: str
    dates: str
    bullets: list[str]


class MasterResume(BaseModel):
    contact: Contact
    headline: str = ""            # the role next to the name, when the resume has one
    summary: str
    education: list[Education]
    skills: list[SkillGroup]
    experience: list[Role]
    projects: list[Project]
    publications: list[str]


# ---------------- master resume ----------------

STRUCTURE_PROMPT = """Convert the resume below into the given JSON structure.

Copy wording verbatim. The only edits allowed are repairing PDF text-extraction
damage: words split by stray spaces ("Y ork" -> "York", "V alidated" ->
"Validated"), hyphenated line breaks ("sce- narios" -> "scenarios"), and space
before punctuation ("OpenCV ," -> "OpenCV,"). Do not summarize, merge, drop, or
reword anything else. Keep every bullet, number, and project.

Put contact links (LinkedIn, GitHub, Portfolio, ...) in contact.links using the
URLs from the LINKS list when one matches; skip a link if no URL is given.
"""


def _pdf_links(path):
    try:
        from pypdf import PdfReader
        uris = []
        for page in PdfReader(str(path)).pages:
            for annot in page.get("/Annots") or []:
                uri = (annot.get_object().get("/A") or {}).get("/URI")
                if uri and uri not in uris:
                    uris.append(str(uri))
        return uris
    except Exception:
        return []


def file_sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_or_build_master(cfg, resume_text, log=print):
    """Structure the resume once and cache it as editable JSON.

    The JSON is the source of truth for tailoring, so review it after the first
    build. It is rebuilt only when the resume file itself changes.
    """
    path = cfg.master_resume_path
    sha = file_sha(cfg.resume)
    if cfg.resume.suffix.lower() == ".tex":
        # A LaTeX resume is read directly: exact text, links kept as locked tokens.
        from copilot import latex
        master = MasterResume.model_validate(latex.parse(cfg.resume.read_text(encoding="utf-8"))[0])
        path.write_text(json.dumps({**master.model_dump(), "_source_sha256": sha}, indent=2,
                                   ensure_ascii=False), encoding="utf-8")
        return master
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("_source_sha256") == sha:
            data.pop("_source_sha256", None)
            return MasterResume.model_validate(data)
        log(f"Resume file changed; rebuilding {path.name} (re-check it afterwards).")

    links = _pdf_links(cfg.resume) if cfg.resume.suffix.lower() == ".pdf" else []
    user = f"LINKS:\n{json.dumps(links)}\n\nRESUME:\n{resume_text}"
    # Structuring is extraction, so it runs on the tailor model at low effort.
    settings = {**llm.task_settings(cfg, "tailor"), "effort": "low"}
    master, _ = llm.parse(settings, [{"type": "text", "text": STRUCTURE_PROMPT}], user, MasterResume)
    payload = master.model_dump()
    payload["_source_sha256"] = sha
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"Built {path} from {cfg.resume.name}. Review it once; tailoring trusts it.")
    return master


# ---------------- text helpers ----------------


def resume_to_text(r):
    """Plain-text rendering, used for re-scoring and the guard. Locked LaTeX
    tokens ([[link:Foo]]) read as their text."""
    from copilot.latex import untoken
    text = _resume_text(r)
    return untoken(text) if "[[" in text else text


def _resume_text(r):
    c = r["contact"]
    lines = [", ".join(x for x in [c["name"], r.get("headline")] if x), " | ".join(x for x in [c["location"], c["phone"], c["email"]] if x), "", r["summary"], ""]
    for e in r["education"]:
        lines += [f"{e['degree']}, {e['school']} ({e['dates']})", *e["details"]]
    for g in r["skills"]:
        lines.append(f"{g['category']}: {', '.join(g['items'])}")
    for role in r["experience"]:
        lines.append(f"{role['title']} - {role['company']} ({role['dates']})")
        lines += [f"- {b}" for b in role["bullets"]]
    for p in r["projects"]:
        lines.append(f"{p['name']} ({p['dates']})")
        lines += [f"- {b}" for b in p["bullets"]]
    lines += r["publications"]
    return "\n".join(lines)


NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def _numbers(text):
    return {n.replace(",", "").rstrip(".") for n in NUMBER.findall(text)}


def _skill_terms(text):
    low = text.lower()
    return {s for s in SKILLS if re.search(rf"(?<!\w){re.escape(s)}(?!\w)", low)}


# ---------------- guard ----------------


def guard(master, tailored_text, cover_letter, tailored_skill_groups, job_description="", base_letter=""):
    """Deterministic fabrication check. Returns (clean_skill_groups, flags).

    The cover letter may also cite numbers from the posting itself, and facts
    from the candidate's own uploaded cover letter.
    """
    master_text = resume_to_text(master)
    master_items = {i.strip().lower() for g in master["skills"] for i in g["items"]}
    flags = []

    clean_groups = []
    for g in tailored_skill_groups:
        kept = [i for i in g["items"] if i.strip().lower() in master_items]
        dropped = [i for i in g["items"] if i.strip().lower() not in master_items]
        if dropped:
            flags.append(f"removed skills not on master resume: {', '.join(dropped)}")
        if kept:
            clean_groups.append({"category": g["category"], "items": kept})

    allowed = {"resume": _numbers(master_text),
               "cover letter": _numbers(master_text) | _numbers(job_description) | _numbers(base_letter)}
    known_skills = {"resume": _skill_terms(master_text),
                    "cover letter": _skill_terms(master_text) | _skill_terms(base_letter)}
    for label, text in (("resume", tailored_text), ("cover letter", cover_letter)):
        new_numbers = sorted(_numbers(text) - allowed[label])
        if new_numbers:
            flags.append(f"{label}: numbers not in master resume: {', '.join(new_numbers)}")
        new_skills = sorted(_skill_terms(text) - known_skills[label])
        if label == "cover letter":
            # "I haven't used Kubernetes yet" is honest, not a claim: allow skills
            # named in a sentence that frames them as a gap or something to learn.
            new_skills = [s for s in new_skills if not _framed_as_gap(text, s)]
        if new_skills:
            flags.append(f"{label}: skills not in master resume: {', '.join(new_skills)}")
    return clean_groups, flags


_GAP_CUES = re.compile(r"\b(not|haven't|have not|hasn't|limited|new to|learn|learning|ramp up|eager to|"
                       r"look forward|exposure|familiarize|no professional|yet)\b", re.IGNORECASE)


def _framed_as_gap(text, skill):
    """True if every sentence naming the skill presents it as a gap or a goal."""
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text)
                 if re.search(rf"(?<!\w){re.escape(skill)}(?!\w)", s.lower())]
    return bool(sentences) and all(_GAP_CUES.search(s) for s in sentences)


# ---------------- claims audit ----------------

class ClaimAudit(BaseModel):
    unsupported: list[str]   # each: the quoted phrase, then what the source lacks


AUDIT_SYSTEM = """You fact-check a tailored resume and cover letter against the
candidate's source documents: their master resume and, if given, their own
cover letter. Those sources are the complete truth about the candidate.

List every phrase in the tailored text that states or implies experience,
responsibilities, systems, technologies, scale, domains, or outcomes that the
sources do not support. Watch especially for vocabulary borrowed from the job
posting and presented as the candidate's past work (for example "fleet
monitoring" when the source only mentions traffic routing).

Rephrasing, reordering, and emphasis of supported facts are fine. Enthusiasm
about the company or role is fine. Only unsupported facts about the candidate count.

Format each item as: "<exact phrase>": <what the source lacks>. Return an empty
list when everything is supported.
"""


def audit_claims(master, tailored_text, letter, settings, base_letter=""):
    """The guard catches invented numbers and skills; this catches invented
    phrases ("fleet-monitoring systems") that a deterministic check can't see."""
    source = resume_to_text(master) + (f"\n\nCANDIDATE'S OWN COVER LETTER:\n{base_letter}" if base_letter else "")
    system = [{"type": "text", "text": AUDIT_SYSTEM + "\nSOURCE DOCUMENTS:\n" + source,
               "cache_control": {"type": "ephemeral"}}]
    user = f"TAILORED RESUME:\n{tailored_text}\n\nTAILORED COVER LETTER:\n{letter}"
    try:
        # Reasoning models spend output tokens thinking, so leave plenty of room.
        result, _ = llm.parse(settings, system, user, ClaimAudit, max_tokens=16000)
    except llm.LLMUnavailable:
        raise
    except llm.LLMError as exc:
        # An unverified draft is never "Ready": surface it for human review.
        return [f"claims audit could not run ({exc}); check this draft by hand"]
    return [f"unsupported claim: {c}" for c in result.unsupported if c.strip()]


# ---------------- tailoring (edit-based) ----------------
#
# The model proposes targeted edits to individual bullets; code decides which
# edits survive. Unchanged bullets stay word for word, nothing is dropped, and
# an edit that loses a metric is rejected, so quantified results can't vanish.


class Requirement(BaseModel):
    need: str                     # one requirement from the posting, in plain words
    keywords: list[str]           # the posting's own terms for it (1-4)
    evidence: list[str]           # bullet ids that truthfully support it; [] if none


class BulletEdit(BaseModel):
    id: str                       # e.g. "E1.3"
    text: str                     # the full new bullet
    reason: str                   # which requirement it now surfaces


class SectionOrder(BaseModel):
    section: str                  # e.g. "E1" or "PROJECTS"
    order: list[str]              # bullet ids (or project ids) in the new order


class TailorPlan(BaseModel):
    requirements: list[Requirement]
    summary: str
    skills: list[SkillGroup]
    edits: list[BulletEdit]
    orders: list[SectionOrder]
    changes: list[str]


TAILOR_SYSTEM = """You tailor a candidate's resume to one job posting by making
targeted edits, the way a strong recruiter would, and you write a cover letter.

The master resume below is the complete truth about the candidate.

Work in this order:
1. requirements: the 8-12 things this posting most wants. For each, give the
   posting's own keywords and the ids of bullets that truthfully support it
   (evidence). A requirement with no supporting bullet gets evidence [].
2. edits: rewrite only bullets where a requirement's keywords can be surfaced
   truthfully, typically 4-8 bullets. Every other bullet stays exactly as it is,
   so don't return it. Each edit must either bring in a posting keyword the
   bullet didn't already contain, or make the bullet clearly tighter (at least
   10% shorter) with a stronger verb or a clearer result. Same-length synonym
   swaps are discarded; leave already-strong bullets alone. Look hard for work that IS the requirement under
   another name (e.g. "converted raw map data into validated simulation
   scenarios" is "environment generation from raw data"). For each edited bullet:
   - keep every number, metric, and named system from the original, verbatim;
   - never more than about 20% longer than the original (shorter is welcome),
     so the page layout holds;
   - lead with the strongest action and result; use the posting's vocabulary
     only where it truthfully describes the same work;
   - never add a tool, skill, scope, responsibility, or result the original
     bullet does not support. A posting keyword may go into a bullet only if
     that same role or project already mentions it (e.g. don't add "REST APIs"
     to a project that never mentions APIs). Requirements with no evidence
     stay unclaimed: say so in changes rather than stretching a bullet.
   - text in double brackets, like [[link:Emergency Routing]], is a link or
     formatting from the candidate's resume: copy it exactly, unchanged.
3. orders: optionally reorder bullets within a role (section "E1", "E2", ...)
   or reorder projects (section "PROJECTS", ids "P1", "P2", ...) so the most
   relevant come first. Never omit an id from an order you give.
4. skills: the same groups and the same items as the master resume, reordered
   so the posting's needs come first. Do not add or remove items.
5. summary: 2-3 sentences that open with this job's role title and use the posting's
   terms for what the candidate truly has, built only from facts in the
   master resume (numbers included only if they appear there).
6. cover letter (written in its own request, only when asked): follow the
   candidate's cover letter instructions below for
   tone, structure and length. Format: plain text, addressed "Dear Hiring
   Team," (or the hiring manager if the posting names one), blank lines between
   paragraphs, no bullet points, signed with the candidate's name. If the
   candidate's own cover letter is given, match its voice and reuse its
   strongest true points; do not copy its company-specific lines.
   For a requirement the candidate lacks, say so plainly ("I haven't used X
   yet"). Mention coursework, self-study or exposure to it ONLY if the master
   resume or the candidate's stated facts show it; otherwise state the gap and
   the intent to learn, nothing more.
7. changes: one short line per meaningful edit, saying what changed and why.

MASTER RESUME (bullet ids in brackets):
"""


def bullet_ids(master):
    """{bullet id: (section key, item index, bullet index)} for every bullet."""
    ids = {}
    for i, role in enumerate(master["experience"]):
        for k, _ in enumerate(role["bullets"]):
            ids[f"E{i + 1}.{k + 1}"] = ("experience", i, k)
    for i, proj in enumerate(master["projects"]):
        for k, _ in enumerate(proj["bullets"]):
            ids[f"P{i + 1}.{k + 1}"] = ("projects", i, k)
    return ids


def master_view(master):
    """The master resume as the model sees it: every bullet labelled with its id."""
    lines = [f"SUMMARY: {master['summary']}", "", "SKILLS:"]
    lines += [f"  {g['category']}: {', '.join(g['items'])}" for g in master["skills"]]
    lines += ["", "EXPERIENCE:"]
    for i, role in enumerate(master["experience"]):
        lines.append(f"[E{i + 1}] {role['title']}, {role['company']} ({role['dates']})")
        lines += [f"  [E{i + 1}.{k + 1}] {b}" for k, b in enumerate(role["bullets"])]
    lines += ["", "PROJECTS:"]
    for i, proj in enumerate(master["projects"]):
        lines.append(f"[P{i + 1}] {proj['name']} ({proj['dates']})")
        lines += [f"  [P{i + 1}.{k + 1}] {b}" for k, b in enumerate(proj["bullets"])]
    lines += ["", "EDUCATION:"]
    lines += [f"  {e['degree']}, {e['school']} ({e['dates']})" for e in master["education"]]
    return "\n".join(lines)


METRIC = re.compile(r"\$?\d+(?:[.,]\d+)*\s*(?:%|[kKmMbB]\+?|x)?")


def _metrics(text):
    """Numbers as written ("$500K+", "20.8%", "3.27"), normalized for comparison."""
    return {m.strip().replace(",", "").rstrip(".").lower() for m in METRIC.findall(text)}


def _has(text_low, term):
    return re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text_low) is not None


# No "-ation" rule: stripping "-ion" maps validation/validate both to "validat".
_SUFFIXES = ("ings", "ing", "ions", "ion", "ities", "ity", "ables", "able", "ed", "es", "s", "e")


def _stem(word):
    """Crude stemmer: validate/validated/validation -> validat, tools/tooling -> tool."""
    for suf in _SUFFIXES:
        if word.endswith(suf) and len(word) - len(suf) >= 3:
            return word[: -len(suf)]
    return word


def _stems(text):
    return {_stem(w) for w in re.findall(r"[a-z0-9+#]+", text.lower())}


def _mentions(text_stems, keyword):
    """Does text contain the keyword, allowing other word forms and word order?"""
    words = [w for w in re.findall(r"[a-z0-9+#]+", keyword.lower()) if len(w) > 2 or w in ("ai", "ml", "c#")]
    return bool(words) and all(_stem(w) in text_stems for w in words)


def _is_technical(keyword):
    """Tools, acronyms and named skills ("REST APIs", "gRPC", "Kubernetes") are
    claims of experience. Descriptive words ("raw data", "repeatable") are framing."""
    if re.search(r"[+#/.]", keyword) or any(_mentions(_stems(keyword), s) for s in SKILLS):
        return True
    return any((t.isupper() and len(t) > 1) or any(c.isupper() for c in t[1:])
               for t in re.findall(r"[A-Za-z0-9]+", keyword))


# Words the candidate never wants in their documents (AI-sounding filler).
BANNED = ("leverage", "leveraged", "leveraging", "utilize", "utilized", "utilizing", "spearhead",
          "spearheaded", "synergize", "synergized", "synergy", "cutting-edge", "world-class",
          "revolutionary", "game-changing", "highly motivated", "results-driven",
          "passionate professional", "i am thrilled", "i am passionate", "dynamic environment",
          "fast-paced", "innovative solutions", "i am excited to apply")

# Typography that marks text as machine-written; the candidate wants plain ASCII.
_ASCII = {
    "—": ", ", "–": "-", "‑": "-", "‐": "-", "−": "-",
    "‘": "'", "’": "'", "“": '"', "”": '"', "…": "...",
    "→": " to ", " ": " ", "•": "-", "·": "-",
}


def plain(text):
    """Writer output in plain ASCII punctuation: no em/en dashes, smart quotes,
    arrows or non-breaking hyphens. An em dash becomes a comma, an arrow "to"."""
    text = re.sub(r"\s*—\s*", ", ", text)
    for k, v in _ASCII.items():
        text = text.replace(k, v)
    return re.sub(r"[ \t]{2,}", " ", text).replace(" ,", ",")


def banned_phrases(text):
    low = text.lower()
    return [b for b in BANNED if _has(low, b)]


def check_edit(original, new, item_text, posting_keywords=()):
    """Why an edit must be rejected, or None. Pure code: the model can't argue.

    item_text is everything the resume says about this one role or project.
    A technical keyword (tool, acronym, named skill) newly added to a bullet
    must already appear there: "REST APIs" can't move onto a project that never
    mentions them, even if the candidate lists it elsewhere. Descriptive
    framing ("raw data", "validated") is allowed here; the claims audit judges it.
    Word forms count as the same word (validate / validation, tools / tooling).
    """
    from copilot.latex import token_problem
    broken = token_problem(original, new)
    if broken:
        return broken
    lost = _metrics(original) - _metrics(new)
    if lost:
        return f"drops metric(s) {', '.join(sorted(lost))}"
    added = _metrics(new) - _metrics(original)
    if added:
        return f"adds number(s) {', '.join(sorted(added))}"
    item_low = item_text.lower()
    dropped = sorted(_skill_terms(original) - _skill_terms(new))
    if dropped:
        return f"drops {', '.join(dropped)}, named in the original"
    new_skills = sorted(s for s in _skill_terms(new) - _skill_terms(original) if not _has(item_low, s))
    if new_skills:
        return f"adds {', '.join(new_skills)}, which this role/project doesn't mention"

    orig_st, new_st, item_st = _stems(original), _stems(new), _stems(item_text)
    surfaced = [k for k in posting_keywords if _mentions(new_st, k) and not _mentions(orig_st, k)]
    borrowed = sorted({k for k in surfaced if _is_technical(k) and not _mentions(item_st, k)})
    if borrowed:
        return f"adds posting keyword(s) {', '.join(borrowed)}, which this role/project doesn't support"
    # An edit has to earn its place: surface something the posting asks for.
    # Synonym swaps ("improving" -> "boosting") just churn the candidate's wording.
    banned = banned_phrases(new)
    if banned:
        return f"uses banned phrase(s): {', '.join(banned)}"
    # An edit has to earn its place: surface something the posting asks for, or
    # make the bullet clearly tighter. Same-length synonym swaps ("improving" ->
    # "boosting") just churn the candidate's wording.
    tighter = len(new) <= 0.9 * len(original)
    if posting_keywords and not surfaced and not tighter:
        return "cosmetic: surfaces no posting keyword and isn't tighter"
    # Hold the layout: not much longer than the original (a little absolute slack
    # so two words added to a short bullet pass). Shorter is fine, within reason.
    too_long = len(new) > max(1.35 * len(original), len(original) + 25)
    if too_long or len(new) < 0.5 * len(original):
        return f"length changes {len(original)} -> {len(new)} chars"
    return None


def _reorder(items, ids, wanted):
    """Apply a requested order; ids the model left out keep their place at the end."""
    pos = {i: n for n, i in enumerate(ids)}
    picked = [i for i in dict.fromkeys(wanted) if i in pos]
    rest = [i for i in ids if i not in picked]
    return [items[pos[i]] for i in picked + rest]


def _merge_skills(master_groups, proposed):
    """Reorder groups and items as proposed, but never add or drop an item."""
    by_cat = {g["category"].lower(): g for g in master_groups}
    out, seen = [], set()
    for g in proposed:
        m = by_cat.get(g["category"].lower())
        if not m or m["category"] in seen:
            continue
        seen.add(m["category"])
        known = {x.lower(): x for x in m["items"]}
        items = [known[x.lower()] for x in dict.fromkeys(i.strip() for i in g["items"]) if x.lower() in known]
        items += [x for x in m["items"] if x not in items]
        out.append({"category": m["category"], "items": items})
    out += [g for g in master_groups if g["category"] not in seen]
    return out


def keyword_coverage(text, requirements):
    """How many of the posting's keywords appear in the resume, counted by code
    (word forms count: "validated" covers "validate")."""
    stems = _stems(text)
    kws = sorted({k.strip().lower() for r in requirements for k in r["keywords"] if k.strip()})
    hit = [k for k in kws if _mentions(stems, k)]
    return hit, kws


ALL_SECTIONS = ("summary", "experience", "projects", "skills")


def apply_plan(master, plan, sections=ALL_SECTIONS):
    """Build the tailored resume from the model's plan. Returns (resume, report).
    `sections` are the parts the user allows to change; the rest stay as-is."""
    resume = copy.deepcopy(master)
    skill_terms = _skill_terms(resume_to_text(master))
    report = {"requirements": plan["requirements"], "accepted": [], "rejected": [],
              "posting_keywords": sorted(posting_keywords(plan["requirements"]))}
    allowed = {"E": "experience" in sections, "P": "projects" in sections}

    for e in plan["edits"]:
        if not allowed.get(e["id"].strip().strip("[]")[:1].upper(), False):
            continue   # the user locked this section
        try_edit(master, resume, report, e["id"], e["text"], e["reason"])
    rejected = report["rejected"]

    for o in plan["orders"]:
        name = o["section"].strip().upper()
        wanted = [x.strip().strip("[]").upper() for x in o["order"]]
        if name == "PROJECTS" and allowed["P"]:
            pids = [f"P{i + 1}" for i in range(len(resume["projects"]))]
            resume["projects"] = _reorder(resume["projects"], pids, wanted)
        elif re.fullmatch(r"E\d+", name) and allowed["E"]:
            i = int(name[1:]) - 1
            if 0 <= i < len(resume["experience"]):
                bids = [f"{name}.{k + 1}" for k in range(len(resume["experience"][i]["bullets"]))]
                resume["experience"][i]["bullets"] = _reorder(resume["experience"][i]["bullets"], bids, wanted)

    if "skills" in sections:
        resume["skills"] = _merge_skills(master["skills"], plan["skills"])

    summary = plain(" ".join(plan["summary"].split()))
    if "summary" not in sections or not summary:
        pass
    elif banned_phrases(summary):
        rejected.append({"id": "summary", "proposed": summary,
                         "why": f"uses banned phrase(s): {', '.join(banned_phrases(summary))}"})
    elif not (_metrics(summary) - _metrics(resume_to_text(master))) and not (_skill_terms(summary) - skill_terms):
        resume["summary"] = summary
    else:
        rejected.append({"id": "summary", "proposed": summary, "why": "adds numbers or skills not on the resume"})

    hit_before, kws = keyword_coverage(resume_to_text(master), plan["requirements"])
    hit_after, _ = keyword_coverage(resume_to_text(resume), plan["requirements"])
    report["coverage"] = {"before": len(hit_before), "after": len(hit_after), "total": len(kws),
                          "missing": [k for k in kws if k not in hit_after]}
    return resume, report


_SENIORITY = re.compile(r"\b(senior|sr\.?|staff|principal|lead|junior|jr\.?|founding|mid[- ]level|entry[- ]level|"
                        r"new grad(uate)?|early career|experienced|i{1,3}|iv|l[1-9])\b", re.I)
_ROLE_NOUN = re.compile(r"\b(engineer|scientist|developer|researcher|programmer|analyst)\b", re.I)


def role_headline(job_title, current=""):
    """The job's role for the header and summary: "Software Engineer II, AI Agents (NYC)"
    -> "Software Engineer". Levels are dropped so it never claims a seniority; anything
    that isn't a plain technical role keeps the resume's own headline."""
    role = re.split(r"\s*[,(|:]\s*|\s+[-\u2013\u2014/]\s+", job_title or "")[0]
    role = " ".join(_SENIORITY.sub(" ", role).split()).strip(" -")
    return role if _ROLE_NOUN.search(role) and 2 <= len(role.split()) <= 6 else current


class SummaryFix(BaseModel):
    summary: str


SUMMARY_SYSTEM = """You write the 2-3 sentence summary at the top of a resume, for one job.
- Open with the role title you're given, then the candidate's most relevant experience.
- Use the posting terms you're given, exactly as written, where the resume supports them.
- Use only facts from the resume. Add no number, tool, skill, or claim it doesn't contain.
- Plain, direct English; ASCII punctuation; no cliches.
"""


def summary_terms(requirements, master):
    """Posting terms the resume itself supports: what the summary should use."""
    from copilot import bullets
    text = resume_to_text(master)
    return [k for k in posting_keywords(requirements) if bullets.is_term(k) and bullets.uses(text, k)]


def summary_problem(summary, role, terms):
    """Why a summary doesn't reflect this job, or None."""
    stems = _stems(summary)
    if role and not _mentions(stems, role):
        return f'it does not state the role "{role}"'
    want = min(2, len(terms))
    if sum(_mentions(stems, t) for t in terms) < want:
        return f"it uses too few of the posting's terms ({', '.join(terms[:6])})"
    return None


def _summary_ok(summary, master):
    return not banned_phrases(summary) and not (_metrics(summary) - _metrics(resume_to_text(master))) \
        and not (_skill_terms(summary) - _skill_terms(resume_to_text(master)))


def tailor_header(master, resume, report, job, settings, sections=ALL_SECTIONS):
    """Make the headline and summary reflect this job: the role and its terms."""
    role = role_headline(job["title"], master.get("headline", ""))
    if master.get("headline") and role:
        resume["headline"] = role
    report["role"] = role
    if "summary" not in sections:
        return
    terms = summary_terms(report["requirements"], master)
    problem = summary_problem(resume["summary"], role, terms)
    if not problem:
        return
    try:
        out, _ = llm.parse(settings, [{"type": "text", "text": SUMMARY_SYSTEM}],
                           f"ROLE TITLE: {role}\nPOSTING TERMS: {', '.join(terms)}\n\nFIX THIS: {problem}\n\n"
                           f"RESUME:\n{resume_to_text(master)}", SummaryFix, max_tokens=4000)
        fixed = plain(" ".join(out.summary.split()))
        if fixed and _summary_ok(fixed, master) and not summary_problem(fixed, role, terms):
            resume["summary"] = fixed
            return
    except llm.LLMError as exc:
        if isinstance(exc, llm.LLMUnavailable):
            raise
    # The model didn't manage it: at least put the role first, in the candidate's own words.
    headline = master.get("headline", "")
    if role and headline and master["summary"].startswith(headline):
        resume["summary"] = role + master["summary"][len(headline):]


def posting_keywords(requirements):
    """The posting's keywords, deduplicated ignoring case but keeping the
    original casing: "REST" and "gRPC" are recognized as technical by their case."""
    seen = {}
    for r in requirements:
        for k in r["keywords"]:
            k = k.strip()
            if len(k) > 2 or k.lower() in ("ai", "ml"):
                seen.setdefault(k.lower(), k)
    return set(seen.values())


def _item_text(master, sec, i):
    item = master[sec][i]
    head = f"{item['title']} {item['company']}" if sec == "experience" else item["name"]
    return head + "\n" + "\n".join(item["bullets"])


def _replace_bullet(resume, sec, old, new):
    """Swap a bullet by its text: orders may have moved items and bullets since
    the master's indexes were assigned."""
    for item in resume[sec]:
        if old in item["bullets"]:
            item["bullets"][item["bullets"].index(old)] = new
            return True
    return False


def try_edit(master, resume, report, bid, text, reason):
    """Validate one proposed bullet edit and apply it, or record why not.
    The bullet currently in the resume may itself be an earlier accepted edit."""
    bid = bid.strip().strip("[]").upper()
    loc = bullet_ids(master).get(bid)
    if not loc:
        report["rejected"].append({"id": bid, "proposed": text, "why": "unknown bullet id"})
        return False
    sec, i, k = loc
    original = master[sec][i]["bullets"][k]
    new = plain(" ".join(text.split()))
    if new == original:
        return False
    why = check_edit(original, new, _item_text(master, sec, i), set(report["posting_keywords"]))
    if why:
        # One entry per bullet: the latest attempt replaces earlier ones.
        report["rejected"] = [r for r in report["rejected"] if r["id"] != bid]
        report["rejected"].append({"id": bid, "proposed": new, "why": why})
        return False
    prior = next((a for a in report["accepted"] if a["id"] == bid), None)
    _replace_bullet(resume, sec, prior["after"] if prior else original, new)
    if prior:
        report["accepted"].remove(prior)
    report["rejected"] = [r for r in report["rejected"] if r["id"] != bid]
    report["accepted"].append({"id": bid, "before": original, "after": new, "reason": reason})
    return True


def _quoted(flag):
    """The phrase an audit flag points at: the quoted part, else the text before ':'."""
    m = re.search(r'["“]([^"”]{6,})["”]', flag)
    if m:
        return m.group(1).strip()
    body = flag.removeprefix("unsupported claim:").strip()
    head = body.split(":")[0].strip()
    return head if 6 <= len(head) <= 120 else None


def revert_flagged(master, resume, report, audit_flags):
    """A flagged phrase inside an edited bullet reverts that bullet to the
    original; flags that don't point at an edit are returned for a human."""
    ids = bullet_ids(master)
    leftover = []
    for f in audit_flags:
        phrase = _quoted(f)
        if phrase and phrase.lower() in resume["summary"].lower() and resume["summary"] != master["summary"]:
            # The summary is rewritten freely, so an unsupported claim there
            # sends it back to the candidate's own summary.
            report["rejected"].append({"id": "summary", "proposed": resume["summary"],
                                       "why": "claims audit: " + f.removeprefix("unsupported claim: ")})
            resume["summary"] = master["summary"]
            continue
        hit = next((a for a in report["accepted"] if phrase and phrase.lower() in a["after"].lower()), None)
        if not hit:
            leftover.append(f)
            continue
        _replace_bullet(resume, ids[hit["id"]][0], hit["after"], hit["before"])
        report["accepted"].remove(hit)
        report["rejected"].append({"id": hit["id"], "proposed": hit["after"],
                                   "why": "claims audit: " + f.removeprefix("unsupported claim: ")})
    return leftover


class BulletFix(BaseModel):
    id: str
    text: str


class Repair(BaseModel):
    fixes: list[BulletFix]


REPAIR_SYSTEM = """You fix resume bullet edits that broke a rule. For each item
you get the original bullet, a proposed rewrite, the rule it broke, and
everything the resume says about that role or project.

Return a corrected rewrite for each id that keeps the proposal's improvement
but obeys every rule:
- every number and metric from the original, verbatim (e.g. "$500K+", "20.8%");
- every tool or technology named in the original;
- no tool, skill, or keyword that the role/project text doesn't already contain;
- not more than about 20% longer than the original (shorter is fine);
- none of these words: """ + ", ".join(BANNED) + """;
- plain ASCII punctuation only: no em or en dashes, no smart quotes, no arrows;
- every [[...]] token (a link or formatting) copied exactly, unchanged.
If no rewrite can satisfy the rules, return the original text unchanged.
"""


def repair_edits(master, resume, report, settings, rounds=2):
    """Send rejected bullets back one by one with the exact rule they broke.
    Narrow, specific fixes are where smaller models do well."""
    ids = bullet_ids(master)
    for _ in range(rounds):
        todo = [r for r in report["rejected"]
                if r["id"] in ids and not r["why"].startswith(("unknown", "claims audit"))]
        if not todo:
            return
        blocks = []
        for r in todo:
            sec, i, k = ids[r["id"]]
            blocks.append(f"[{r['id']}]\nORIGINAL: {master[sec][i]['bullets'][k]}\nPROPOSED: {r['proposed']}\n"
                          f"RULE BROKEN: {r['why']}\nROLE/PROJECT TEXT:\n{_item_text(master, sec, i)}")
        try:
            out, _ = llm.parse(settings, [{"type": "text", "text": REPAIR_SYSTEM}],
                               "\n\n".join(blocks), Repair, max_tokens=16000)
        except llm.LLMUnavailable:
            raise
        except llm.LLMError:
            return
        reasons = {r["id"]: r for r in todo}
        for fix in out.fixes:
            bid = fix.id.strip().strip("[]").upper()
            if bid in reasons:
                try_edit(master, resume, report, bid, fix.text, "repaired: " + reasons[bid]["why"])


class LetterFix(BaseModel):
    cover_letter: str


def letter_problems(master, letter, job_description, trusted, writing):
    """Everything code can check about a cover letter, in one place, so the
    first draft and the repaired draft are held to the same rules."""
    _, flags = guard(master, "", letter, [], job_description, trusted)
    flags += [f"cover letter uses banned phrase: {b}" for b in banned_phrases(letter)]
    points = [p.strip() for p in writing.get("letter_points") or [] if p.strip()]
    flags += [f"cover letter is missing your point: {p}" for p in missing_points(letter, points)]
    length = letter_length_flag(letter, writing)
    return flags + ([length] if length else [])


def repair_letter(master, letter, flags, job_description, settings, trusted="", writing=None):
    """One focused rewrite of the cover letter without its listed problems, still
    following the candidate's letter instructions. Returns (letter, remaining
    problems); keeps the original if the fix isn't better."""
    from copilot.config import DEFAULT_WRITING
    writing = writing or DEFAULT_WRITING
    system = [{"type": "text", "text":
               "Rewrite this cover letter to fix every listed problem. Keep its voice and all "
               "supported points. Mention a skill or tool as experience only if the candidate's "
               "sources contain it; a gap may be named honestly as something to learn. Return the "
               "full letter." + letter_block(writing) + "\n\nCANDIDATE SOURCES:\n"
               + resume_to_text(master) + (f"\n\nCANDIDATE'S OWN WORDS AND FACTS:\n{trusted}" if trusted else "")}]
    user = "PROBLEMS:\n- " + "\n- ".join(flags) + f"\n\nLETTER:\n{letter}"
    try:
        out, _ = llm.parse(settings, system, user, LetterFix, max_tokens=16000)
    except llm.LLMUnavailable:
        raise
    except llm.LLMError:
        return letter, flags
    fixed = plain(out.cover_letter.strip())
    new_flags = (letter_problems(master, fixed, job_description, trusted, writing)
                 + audit_claims(master, "", fixed, settings, trusted))
    return (fixed, new_flags) if len(new_flags) < len(flags) else (letter, flags)


def missing_points(letter, points):
    """Must-include points the letter doesn't cover: most of a point's words
    (length > 3) have to appear, so rephrasing passes but omission doesn't."""
    low = letter.lower()
    out = []
    for p in points:
        words = [w for w in re.findall(r"[a-z0-9+#]+", p.lower()) if len(w) > 3]
        if words and sum(_has(low, w) for w in words) / len(words) < 0.6:
            out.append(p)
    return out


def writing_block(writing):
    """The user's standing instructions, placed after the rules they can't override."""
    sections = writing.get("sections") or list(ALL_SECTIONS)
    locked = [s for s in ALL_SECTIONS if s not in sections]
    text = ("\n\nTHE CANDIDATE'S RESUME INSTRUCTIONS (they shape style and focus; the rules "
            "above always win):\n" + (writing.get("instructions") or "").strip())
    if locked:
        text += f"\nDo not change these sections; they are locked: {', '.join(locked)}."
    text += letter_block(writing)
    return text


def letter_block(writing):
    """The candidate's cover letter instructions, length target and must-include points."""
    text = ""
    if (writing.get("letter_instructions") or "").strip():
        text += ("\n\nTHE CANDIDATE'S COVER LETTER INSTRUCTIONS (the honesty rules above always win):\n"
                 + writing["letter_instructions"].strip())
    lo, hi = writing.get("letter_words") or (250, 400)
    text += f"\nCover letter length: {lo}-{hi} words."
    points = [p.strip() for p in writing.get("letter_points") or [] if p.strip()]
    if points:
        text += ("\nThe cover letter MUST include each of these points from the candidate (they are "
                 "true facts):\n- " + "\n- ".join(points))
    course = coursework_note(writing)
    if course:
        text += ("\n" + course + "\nWhen the posting asks for one of these, the letter may say I studied "
                 "it in coursework. Never present it as professional or project experience. For "
                 "requirements not on this list or my resume, state the gap plainly.")
    return text


def coursework_note(writing):
    """The candidate's coursework-only exposure, as a trusted fact for the letter."""
    items = [c.strip() for c in writing.get("coursework") or [] if c.strip()]
    return ("Coursework exposure (studied in class, not professional experience): " + ", ".join(items)
            if items else "")


def letter_length_flag(letter, writing):
    """A flag if the letter body is outside the word range, else None."""
    lo, hi = writing.get("letter_words") or (250, 400)
    n = len(re.findall(r"[A-Za-z0-9']+", letter))
    return None if lo <= n <= hi else f"cover letter is {n} words; aim for {lo}-{hi}"


def examples_block(examples):
    """Edits the candidate reviewed: approved ones show the style to copy,
    rejected ones show what to avoid. Built from the app's feedback log."""
    if not examples or not (examples.get("approved") or examples.get("rejected")):
        return ""
    text = "\n\nEXAMPLES FROM THE CANDIDATE'S OWN REVIEWS (other jobs; match the approved style):"
    for e in examples.get("approved", []):
        text += f"\nAPPROVED\n  before: {e['before']}\n  after:  {e['final']}"
    for e in examples.get("rejected", []):
        text += f"\nREJECTED (don't write edits like this)\n  before: {e['before']}\n  after:  {e['proposed']}"
    return text


def tailor(master: MasterResume, job, matched, missing, settings, base_letter="", instruction="",
           writing=None, examples=None):
    """Return (tailored_resume, cover_letter, changes, flags, report).

    base_letter is the candidate's own uploaded cover letter: a voice reference
    whose facts are trusted like the resume's. writing holds the user's standing
    instructions, which sections may change, and must-include cover letter
    points. instruction is a one-off request typed in the app ("lead with the
    RAG project"). None of these override the rules above or the checks in code.
    """
    from copilot.config import DEFAULT_WRITING
    writing = writing or DEFAULT_WRITING
    sections = tuple(writing.get("sections") or ALL_SECTIONS)
    points = [p.strip() for p in writing.get("letter_points") or [] if p.strip()]
    master_d = master.model_dump()
    reference = (f"\n\nCANDIDATE'S OWN COVER LETTER (voice reference; its facts are true):\n{base_letter}"
                 if base_letter.strip() else "")
    # Facts the candidate supplied directly (own letter, must-include points,
    # coursework list) are trusted sources for the letter checks.
    trusted = "\n".join(filter(None, [base_letter, *points, coursework_note(writing)]))
    # The breakpoint covers the rules, the user's instructions, and the master
    # resume: the part shared by every job, paid once per run and cached after.
    system = [{"type": "text",
               "text": (TAILOR_SYSTEM + master_view(master_d) + reference + writing_block(writing)
                        + examples_block(examples)),
               "cache_control": {"type": "ephemeral"}}]
    base_user = (
        f"COMPANY: {job['company']}\nROLE: {job['title']}\nLOCATION: {job['location']}\n"
        f"SKILLS THE POSTING WANTS THAT THE CANDIDATE LACKS: {', '.join(missing) or 'none detected'}\n\n"
        f"JOB POSTING:\n{job['description']}"
    )
    if instruction.strip():
        base_user += (f"\n\nTHE CANDIDATE ASKED FOR THIS VERSION: {instruction.strip()}\n"
                      "Follow it where the master resume supports it; if it asks for something the "
                      "master resume doesn't support, leave that out and say so in changes.")

    # The cover letter is its own call, started now so it writes while the
    # resume plan is being made. Both share the cached system prompt.
    from concurrent.futures import ThreadPoolExecutor
    pool = ThreadPoolExecutor(max_workers=1)
    letter_future = pool.submit(
        llm.parse, settings, system,
        base_user + "\n\nWrite ONLY the cover letter now, following the cover letter instructions "
                    "and every rule above. The resume edits are handled separately.",
        LetterFix, 16000)
    # Audits and bullet repairs are narrow checks: they don't need deep thinking.
    check = {**settings, "effort": "low"}

    user, best = base_user, None
    per_bullet = settings.get("mode") == "per_bullet"
    attempts = 1 if per_bullet else settings.get("attempts", 2)
    try:
        for _ in range(attempts):
            if per_bullet:     # one small call per bullet: the task the local model is trained on
                from copilot import bullets
                plan = bullets.make_plan(master_d, job, settings, writing, examples, sections)
            else:
                plan, _ = llm.parse(settings, system, user, TailorPlan)
                plan = plan.model_dump()
            resume, report = apply_plan(master_d, plan, sections)
            if per_bullet:
                report["rejected"] += plan["pre_rejected"]
                report["targets"] = plan["targets"]
            else:
                repair_edits(master_d, resume, report, check)

            tailor_header(master_d, resume, report, job, check, sections)

            # Fact-check only what changed: a flagged phrase in an edited bullet
            # reverts that bullet, one in the summary reverts the summary.
            changed = "\n".join([a["after"] for a in report["accepted"]] + [resume["summary"]])
            flags = revert_flagged(master_d, resume, report, audit_claims(master_d, changed, "", check, trusted))
            hit_after, kws = keyword_coverage(resume_to_text(resume), report["requirements"])
            report["coverage"].update(after=len(hit_after), missing=[k for k in kws if k not in hit_after])

            score = (len(report["accepted"]), -len(flags))
            if best is None or score > best[0]:
                best = (score, resume, plan["changes"], flags, report)
            # Retry only for a real problem: nothing usable, or claims still flagged.
            if report["accepted"] and not flags:
                break
            # If every rejected edit was merely cosmetic, the resume already fits this
            # posting well; another full attempt wouldn't find more honest changes.
            if not flags and report["rejected"] and all(
                    r["why"].startswith("cosmetic") or r["id"] == "summary" for r in report["rejected"]):
                break
            feedback = [f"{r['id']}: {r['why']}" for r in report["rejected"]] + flags
            user = (base_user + "\n\nYOUR PREVIOUS PLAN HAD THESE PROBLEMS; FIX THEM:\n- " + "\n- ".join(feedback)
                    + "\nKeep every metric verbatim and keep each edited bullet about the same length.")

        letter = plain(letter_future.result()[0].cover_letter.strip())
    finally:
        pool.shutdown(wait=False)

    # The letter: checks in code plus a claims audit; one focused rewrite if needed.
    letter_flags = (letter_problems(master_d, letter, job["description"], trusted, writing)
                    + audit_claims(master_d, "", letter, check, trusted))
    if letter_flags:
        letter, letter_flags = repair_letter(master_d, letter, letter_flags, job["description"],
                                             settings, trusted, writing)
    _, resume, changes, flags, report = best
    flags = flags + letter_flags
    best = (None, resume, letter, changes, flags, report)

    _, resume, letter, changes, flags, report = best
    report["writer"] = settings.get("model")
    if settings.get("fell_back_from"):
        report["writer"] += f" (fallback; {settings['fell_back_from']} unavailable)"
    if not settings.get("finetuned"):      # a model's own output isn't training data for itself
        try:
            from copilot import bullets
            bullets.log_run(master_d, job, report, settings, "per_bullet" if per_bullet else "plan")
        except OSError:
            pass
    return resume, letter, changes, flags, report
