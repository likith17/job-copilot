"""Auto-apply: open your top jobs' application forms, fill them, and (when you
allow it) submit the ones that need nothing from you.

    python -m copilot apply                  prepare: fill the top Ready jobs, one tab each; you submit
    python -m copilot apply --submit         also submit the jobs that pass every gate below
    python -m copilot apply --url URL        fill one application page (any job, in the DB or not)

The browser is your Chrome (or Edge) with its own profile in data/browser/, so
sign-ins you make there persist. Filling uses the same script as the extension.

An application is submitted on its own only when ALL of these hold; anything
else is left filled in its tab for you:
  - the documents are Ready (no review flags) and the fit clears apply.min_fit
  - the posting doesn't rule out sponsorship
  - your tailored resume was attached
  - every field came from your profile, your answer bank, or your documents:
    no AI drafts you haven't approved, nothing required left empty
  - no CAPTCHA or bot check on the page
  - under today's cap (apply.daily_cap)
Submission counts only when the page confirms it.
"""
import json
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from copilot import apply, applyserver, db, llm, runlock
from copilot.config import ROOT

SCRIPT = Path(__file__).resolve().parent.parent / "extension" / "content.js"
CONFIRMED = re.compile(r"thank you for (applying|your (application|interest))|application (has been |was )?"
                       r"(submitted|received)|we.?ve received your application|successfully submitted", re.I)
SUBMIT_BUTTON = re.compile(r"^\s*submit( your)?( application)?\s*$", re.I)

BRIDGE = """
window.__copilotBridge = (path, method, body, binary) =>
  window.__copilotApi(path, method || "GET", body || null, !!binary).then((r) => {
    if (r && r.error) throw new Error(r.error);
    return r;
  });
"""


def settings(cfg):
    defaults = {"daily_cap": 15, "per_company_per_day": 2, "min_fit": 80, "per_run": 10, "browser": "chrome",
                "wait_secs": 45, "auto_submit": False, "send_ai_answers": False}
    return {**defaults, **(cfg.apply or {})}


def set_apply_flag(name, on, path=None):
    """Save an on/off apply setting to config.yaml, keeping the file's comments."""
    path = Path(path) if path else ROOT / "config.yaml"
    text = path.read_text(encoding="utf-8")
    new, n = re.subn(rf"^(\s*{name}:\s*)(true|false)\b", lambda m: m.group(1) + ("true" if on else "false"),
                     text, count=1, flags=re.M)
    if not n:
        raise ValueError(f"no apply.{name} setting in {path}")
    path.write_text(new, encoding="utf-8")


def set_auto_submit(on, path=None):
    set_apply_flag("auto_submit", on, path)


def stop_file(cfg):
    return cfg.data_dir / "STOP_AUTO_APPLY"


def stopped(cfg):
    """The pause switch: while this file exists, nothing is submitted automatically."""
    return stop_file(cfg).exists()


def queue(conn, cfg, limit):
    """Ready jobs you haven't applied to, best first."""
    rows = conn.execute("""SELECT * FROM jobs WHERE status IN ('New', 'Ready', 'Review') AND resume_path IS NOT NULL
                           AND is_active = 1 ORDER BY COALESCE(tailored_final, final_score) DESC""").fetchall()
    done = {r["job_id"] for r in conn.execute("SELECT job_id FROM applications WHERE outcome = 'submitted'")}
    return [db.decode(r) for r in rows if r["id"] not in done][:limit]


def job_gates(job, opts, resume_sha=None):
    """The reasons that don't need the form: checked before opening anything."""
    why = []
    if job.get("status") in ("Applied", "Interviewing", "Offer", "Rejected", "Skipped"):
        why.append(f"status is {job['status']}")
    if resume_sha and job.get("docs_resume_sha") != resume_sha:
        why.append("documents were built from an older resume")
    open_flags = [f for f in job.get("tailor_flags") or [] if f not in apply.letter_flags(job)]
    if open_flags:       # a flagged letter isn't attached, so it can't hold the application back
        why.append(f"resume has {len(open_flags)} review flag(s)")
    fit = job.get("tailored_fit") or job.get("fit_score") or 0
    if fit < opts["min_fit"]:
        why.append(f"fit {fit} < {opts['min_fit']}")
    if job.get("sponsorship_status") == "no_sponsorship":
        why.append("posting rules out sponsorship")
    return why


def gates(job, state, opts, submitted_today, resume_sha=None, company_today=0):
    """Reasons this application can't be submitted without you ([] = it can)."""
    if job is None:
        return ["not in your job DB"]
    why = job_gates(job, opts, resume_sha)
    if not any(f["source"] == "documents" and f.get("kind") == "resume" for f in state.get("filled", [])):
        why.append("resume not attached")
    drafts = state.get("drafts") or []
    unchecked = [d for d in drafts if not d.get("checked")]
    if unchecked:
        why.append(f"{len(unchecked)} AI-drafted answer(s) need your review")
    elif drafts and not opts.get("send_ai_answers"):
        why.append(f"{len(drafts)} AI answer(s) passed the fact-check; sending them is off (apply.send_ai_answers)")
    if state.get("needs"):
        why.append(f"{len(state['needs'])} field(s) need you: " + "; ".join(state["needs"][:3]))
    if state.get("errors"):
        why.append(f"{len(state['errors'])} error(s) while filling")
    if state.get("captcha"):
        why.append("CAPTCHA on the page")
    if submitted_today >= opts["daily_cap"]:
        why.append(f"daily cap of {opts['daily_cap']} reached")
    if company_today >= opts.get("per_company_per_day", 2):
        why.append(f"already sent {company_today} to {job.get('company')} today")
    return why


def _bridge(cfg):
    def call(source, path, method, body, binary):
        url = urlparse(path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        conn = db.connect(cfg.db_path)
        try:
            result = applyserver.ROUTES[(method, url.path)](cfg, conn, q, body or {})
            if isinstance(result, tuple):
                import base64
                data, ctype, name = result
                return {"base64": base64.b64encode(data).decode(), "type": ctype, "name": name}
            return result
        except (KeyError, ValueError, llm.LLMError) as exc:
            return {"error": str(exc)}
        finally:
            conn.close()
    return call


def _state(page):
    raw = page.evaluate("document.documentElement.dataset.copilot || ''")
    return json.loads(raw) if raw else None


def fill_page(page, url, wait_secs):
    page.goto(url, wait_until="domcontentloaded")
    if "ashbyhq.com" in url and not url.rstrip("/").endswith("/application"):
        page.goto(url.rstrip("/") + "/application", wait_until="domcontentloaded")
    deadline = time.time() + wait_secs
    while time.time() < deadline:
        state = _state(page)
        if state and state.get("done"):
            return finish_comboboxes(page, state)
        page.wait_for_timeout(500)
    return _state(page) or {"done": False, "errors": ["the form didn't load or has fewer than 3 fields"],
                            "filled": [], "drafts": [], "needs": []}


def finish_comboboxes(page, state):
    """Dropdowns that only answer real input (Greenhouse's new boards): open each
    with a real click and pick the option the fill script chose."""
    for c in state.get("combos", []):
        box = page.locator(f"[data-copilot-id='{c['key']}']")
        try:
            box.click(timeout=5000)
            page.wait_for_timeout(400)
            spot = page.evaluate("([k, v, f]) => window.__copilotChoose(k, v, f)", [c["key"], c["value"], False])
            if not spot:     # long lists (countries, cities): type to search, then pick
                page.keyboard.type(str(c["value"]).split(",")[0] if c["search"] else str(c["value"]), delay=40)
                page.wait_for_timeout(1200)
                spot = page.evaluate("([k, v, f]) => window.__copilotChoose(k, v, f)", [c["key"], c["value"], c["search"]])
            if spot:
                page.mouse.click(spot["x"], spot["y"])
                state["filled"].append({"label": c["label"], "value": spot["text"], "source": "profile"})
                continue
            page.keyboard.press("Escape")
        except Exception as exc:
            state.setdefault("errors", []).append(f"{c['label']}: {exc.__class__.__name__}")
        if c["required"]:
            state["needs"].append(c["label"])
    state["combos"] = []
    return state


def submit(page, wait_secs=25):
    """Click the form's Submit button and wait for the page to confirm. True only on confirmation."""
    buttons = [b for b in page.query_selector_all("button, input[type=submit]")
               if b.is_visible() and SUBMIT_BUTTON.match(b.inner_text() or b.get_attribute("value") or "")]
    if len(buttons) != 1:
        return False, f"found {len(buttons)} submit buttons"
    buttons[0].click()
    for _ in range(wait_secs * 2):
        page.wait_for_timeout(500)
        text = page.evaluate("document.body.innerText.slice(0, 5000)")
        if CONFIRMED.search(text) and not page.query_selector("input[type=file]"):
            return True, "confirmed"
        if page.evaluate("window.__copilotChallenge ? window.__copilotChallenge() : false"):
            return False, "CAPTCHA appeared after submit"
    errors = page.evaluate("""[...document.querySelectorAll('[class*=error], [role=alert]')]
                              .map(e => e.innerText.trim()).filter(t => t && t.length < 200).slice(0, 3)""")
    return False, "no confirmation" + (f": {'; '.join(errors)}" if errors else "")


def run(cfg, *, submit_ok=False, url=None, limit=None, unattended=False, job_ids=None, source="terminal", say=print):
    """Fill the top jobs (or the ones in job_ids), one browser at a time. unattended:
    skip what can't pass anyway, and never wait for a person."""
    with runlock.hold(cfg.data_dir, source, name="apply"):
        return _run(cfg, submit_ok, url, limit, unattended, say, job_ids)


def _run(cfg, submit_ok, url, limit, unattended, say, job_ids=None):
    from playwright.sync_api import sync_playwright
    from copilot import tailor

    opts = settings(cfg)
    conn = db.connect(cfg.db_path)
    resume_sha = tailor.file_sha(cfg.resume)
    wanted = limit or opts["per_run"]
    if url:
        jobs = [None]
    else:
        if job_ids:
            rows = (conn.execute("SELECT * FROM jobs WHERE id = ?", (j,)).fetchone() for j in job_ids)
            pool = [db.decode(r) for r in rows if r and r["resume_path"]]
        else:
            pool = queue(conn, cfg, 10_000)
        # Never upload documents from an older resume, or apply twice.
        stale = {"documents were built from an older resume", "status is Applied", "status is Interviewing",
                 "status is Offer", "status is Rejected", "status is Skipped"}
        ok = [j for j in pool if not (stale & set(job_gates(j, opts, resume_sha)))]
        for j in pool:
            if j not in ok:
                say(f"Skipped {j['company']}: {'; '.join(stale & set(job_gates(j, opts, resume_sha)))}. "
                    "Regenerate its documents first.")
        jobs = [j for j in ok if not (unattended and job_gates(j, opts, resume_sha))][:wanted if not job_ids else None]
    if not jobs:
        say("No jobs ready to apply to. Run the pipeline or generate documents first.")
        return []
    profile_dir = cfg.data_dir / "browser"
    results = []
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(profile_dir), channel=opts["browser"], headless=False,
                                                   bypass_csp=True, no_viewport=True)
        ctx.expose_binding("__copilotApi", _bridge(cfg))
        ctx.add_init_script(BRIDGE)
        ctx.add_init_script(path=str(SCRIPT))
        first = ctx.pages[0] if ctx.pages else None
        for i, job in enumerate(jobs, 1):
            target = url or job["apply_url"] or job["url"]
            page = first if (i == 1 and first) else ctx.new_page()
            label = f"{job['company']}: {job['title']}" if job else target
            say(f"[{i}/{len(jobs)}] {label}")
            state = fill_page(page, target, opts["wait_secs"])
            if job is None and state.get("job"):
                job = db.decode(conn.execute("SELECT * FROM jobs WHERE id = ?", (state["job"]["id"],)).fetchone())
            why = gates(job, state, opts, db.applications_today(conn), resume_sha,
                        db.company_applications_today(conn, job["company"]) if job else 0)
            if submit_ok and stopped(cfg):
                why.append(f"paused ({stop_file(cfg).name} exists)")
            say(f"    filled {len(state.get('filled', []))}, drafts {len(state.get('drafts', []))}, "
                f"needs you {len(state.get('needs', []))}")
            outcome, note = "prepared", "; ".join(why)
            if submit_ok and not why:
                ok, note = submit(page)
                outcome = "submitted" if ok else "needs_you"
                if ok:
                    db.set_status(conn, job["id"], "Applied")
            elif why:
                outcome = "needs_you"
            db.log_application(conn, job["id"] if job else None, target, state.get("ats", ""),
                               "auto" if submit_ok else "prepared", outcome,
                               state.get("filled", []) + state.get("drafts", []), state.get("needs", []), note)
            say(f"    {outcome}" + (f": {note}" if note else ""))
            results.append({"job": job["id"] if job else None, "url": target, "outcome": outcome, "note": note,
                            "page": page})
        open_pages = [r for r in results if r["outcome"] != "submitted"]
        if open_pages and not unattended:
            say(f"\n{len(open_pages)} application(s) are filled and waiting in the browser. Review and submit them;"
                " each one is marked Applied when its page confirms. Close the browser when you're done.")
            _watch(ctx, conn, open_pages, say)
        ctx.close()
    return [{k: v for k, v in r.items() if k != "page"} for r in results]


def _watch(ctx, conn, pending, say):
    """Until the browser closes, mark each application Applied when its page confirms submission."""
    waiting = {id(r["page"]): r for r in pending}
    while waiting and ctx.pages:
        for key, r in list(waiting.items()):
            page = r["page"]
            try:
                if page.is_closed():
                    waiting.pop(key)
                    continue
                text = page.evaluate("document.body ? document.body.innerText.slice(0, 5000) : ''")
            except Exception:
                continue
            if CONFIRMED.search(text) and not page.query_selector("input[type=file]"):
                if r["job"]:
                    db.set_status(conn, r["job"], "Applied")
                db.log_application(conn, r["job"], r["url"], "", "prepared", "submitted", note="you submitted")
                say(f"    Applied: {r['url']}")
                waiting.pop(key)
        try:
            ctx.pages[0].wait_for_timeout(2000)
        except Exception:
            break
