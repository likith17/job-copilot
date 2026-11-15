"""Company intelligence and project strategy for one job, made on demand.

Facts come only from sources you can open: the posting's own "about" section,
the company's job board as we crawled it, and Wikipedia (when the page is
clearly about this company). Every fact carries its link. The model then writes
"why I fit" and "why this company" from those facts and your resume; a company
point that cites no gathered fact is dropped in code.

Project strategy keeps done and proposed apart: the best project already on
your resume is picked in code, and the three ideas the model proposes are
labelled as proposals, never as experience.

One model call per job, cached; company facts are cached per company for 30 days.
"""
import json
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel

from copilot import ats, db, library, llm

TTL_DAYS = 30
AGENT = {"User-Agent": "JobCopilot/1.0 (local job search tool)"}
ABOUT = re.compile(r"^[\s#*]*(about (?!you\b|the (?:role|team|job|position))[\w .,&'’-]{0,40}|who we are|our mission|"
                   r"the company|company overview)[\s:*]*$", re.I)
COMPANY_WORDS = re.compile(r"\b(company|startup|corporation|inc\.|founded|headquartered|software|platform|technology|"
                           r"develops|provides|laboratory|firm)\b", re.I)


class CompanyPoint(BaseModel):
    point: str
    source: int          # number of the fact it rests on


class Idea(BaseModel):
    title: str
    what: str            # what to build, concretely
    shows: str           # which requirement of this posting it demonstrates


class Strategy(BaseModel):
    why_fit: list[str]
    why_company: list[CompanyPoint]
    quick_win: Idea          # 2-4 hours, improves an existing project
    short_project: Idea      # 1-3 days
    advanced_project: Idea   # a week or more


SYSTEM = """You help a job seeker prepare for ONE application. You get their resume, the job posting, and numbered
FACTS about the company. Be specific and plain; no flattery, no buzzwords.

why_fit: 3 short points. Each names something that is really on the resume (a project, tool, or result, in the resume's
own words and numbers) and the requirement of the posting it answers. Never add experience the resume does not state.

why_company: 2 or 3 short points on why this company is worth applying to for this person. Each MUST rest on one of the
numbered FACTS and give that number as `source`. If there are no facts, return an empty list. Never state anything about
the company that is not in the FACTS.

Then three PROPOSED projects the person has NOT done yet, each aimed at a gap between the resume and this posting:
quick_win: 2 to 4 hours, an improvement to a project already on the resume.
short_project: 1 to 3 days, new and small.
advanced_project: a week or more, ambitious.
For each: a title, what to build concretely (tools, data, the result to measure), and which requirement it shows."""


def _get(url, timeout=8):
    with urllib.request.urlopen(urllib.request.Request(url, headers=AGENT), timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def about_section(description):
    """The company's own words about itself, from the posting."""
    lines, out, on = (description or "").split("\n"), [], False
    for line in lines:
        s = line.strip()
        if ABOUT.match(s):
            on = True
            continue
        if on:
            if s and len(s.split()) <= 7 and not s.endswith(".") and out:     # the next heading
                break
            if s:
                out.append(s)
        if sum(len(x) for x in out) > 700:
            break
    return " ".join(out)[:800]


def wikipedia(company, get=_get):
    """(summary, url) when Wikipedia has a page that is clearly about this company, else None."""
    try:
        q = urllib.parse.quote(f"{company} company")
        hits = get(f"https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch={q}&srlimit=3&format=json")
        norm = re.sub(r"[^a-z0-9]", "", company.lower())
        for hit in hits.get("query", {}).get("search", []):
            title = hit["title"]
            if not re.sub(r"[^a-z0-9]", "", re.sub(r"\(.*?\)", "", title).lower()).startswith(norm):
                continue
            page = get("https://en.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(title.replace(" ", "_")))
            text = page.get("extract") or ""
            if page.get("type") == "standard" and COMPANY_WORDS.search(text):
                return text[:700], page.get("content_urls", {}).get("desktop", {}).get("page") or \
                    "https://en.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_"))
    except Exception:       # offline, blocked, or changed API: the other sources still stand
        return None
    return None


def company_facts(conn, job, online=True, get=_get):
    """[{"text", "source", "url"}] about the job's company, cached per company."""
    key = "company:" + (job.get("company") or "").lower().strip()
    hit = db.cache_get(conn, key)
    fresh = hit and datetime.fromisoformat(hit["at"]) > datetime.now(timezone.utc) - timedelta(days=TTL_DAYS)
    wiki = hit.get("wiki") if fresh else None
    if not fresh and online:
        wiki = wikipedia(job["company"], get)
        db.cache_put(conn, key, {"at": datetime.now(timezone.utc).isoformat(), "wiki": wiki})
        conn.commit()

    facts = []
    about = about_section(job.get("description"))
    if about:
        facts.append({"text": about, "source": "The posting (the company's own words)", "url": job.get("url")})
    if wiki:
        facts.append({"text": wiki[0], "source": "Wikipedia", "url": wiki[1]})
    rows = conn.execute("SELECT title, description, role_family FROM jobs WHERE company = ? AND is_active = 1",
                        (job["company"],)).fetchall()
    if len(rows) > 1:
        counts = {}
        for r in rows:
            fam = r["role_family"] or library.classify(r["title"])
            counts[fam] = counts.get(fam, 0) + 1
        top = ", ".join(f"{n} {library.FAMILIES[f]}" for f, n in sorted(counts.items(), key=lambda kv: -kv[1])[:3])
        facts.append({"text": f"{len(rows)} open engineering and data roles on its job board right now ({top}).",
                      "source": "Its job board, as last crawled", "url": job.get("url")})
    basis = job.get("sponsorship_basis")
    if basis in ("posting says sponsors", "employer has sponsored before"):
        facts.append({"text": {"posting says sponsors": "This posting says the company sponsors work visas.",
                               "employer has sponsored before": "The employer has filed H-1B petitions before."}[basis],
                      "source": "The posting" if basis.startswith("posting") else "Public H-1B employer records",
                      "url": job.get("url")})
    return facts


def best_existing(master, job):
    """The resume project or role that names most of what the posting asks for:
    {"name", "kind", "keywords"} or None."""
    parts = ats.split_posting(job.get("description"))
    wanted = ats.keywords(parts["required"] + "\n" + parts["preferred"])
    best = None
    for kind, items in (("project", master.get("projects") or []), ("role", master.get("experience") or [])):
        for item in items:
            name = (item.get("name") or f"{item.get('title')}, {item.get('company')}").strip(" ,")
            text = name + " " + " ".join(item.get("bullets") or [])
            found = [k for k in wanted if ats.VOCAB[k].search(text)]
            if found and (best is None or len(found) > len(best["keywords"])):
                best = {"name": name, "kind": kind, "keywords": found}
    return best


def strategy(cfg, conn, job, resume_text, resume_key, settings, online=True):
    """{"facts", "why_fit", "why_company", "ideas", "cached"} for one job. One model call, cached."""
    facts = company_facts(conn, job, online)
    key = f"intel:{settings.get('model')}:{job['id']}:{resume_key}:{len(facts)}"
    hit = db.cache_get(conn, key)
    if hit:
        return {**hit, "facts": facts, "cached": True}
    numbered = "\n".join(f"[{i}] ({f['source']}) {f['text']}" for i, f in enumerate(facts, 1)) or "(none found)"
    user = (f"COMPANY: {job['company']}\nROLE: {job['title']}\n\nFACTS:\n{numbered}\n\n"
            f"POSTING:\n{(job.get('description') or '')[:7000]}")
    out, _ = llm.parse(settings, [{"type": "text", "text": SYSTEM + "\n\nRESUME:\n" + resume_text}], user, Strategy,
                       max_tokens=4000)
    result = {
        "why_fit": [p.strip() for p in out.why_fit if p.strip()][:4],
        # A point about the company stands only on a fact we can link to.
        "why_company": [{"point": p.point.strip(), "source": p.source} for p in out.why_company
                        if 1 <= p.source <= len(facts) and p.point.strip()][:3],
        "ideas": [{"size": size, **idea.model_dump()} for size, idea in (
            ("Quick win (2 to 4 hours)", out.quick_win), ("Short project (1 to 3 days)", out.short_project),
            ("Advanced (a week or more)", out.advanced_project))],
    }
    db.cache_put(conn, key, result)
    conn.commit()
    return {**result, "facts": facts, "cached": False}


def cached_strategy(conn, job, resume_key, settings):
    """The stored strategy for a job, without calling the model (None if not made yet)."""
    facts = company_facts(conn, job, online=False)
    hit = db.cache_get(conn, f"intel:{settings.get('model')}:{job['id']}:{resume_key}:{len(facts)}")
    return {**hit, "facts": facts, "cached": True} if hit else None
