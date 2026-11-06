"""The daily run, as a funnel where each stage only sees what survived the
cheaper stage before it:

    fetch -> prefilter -> embedding match -> LLM fit grade -> sponsorship
          -> tailored resume + cover letter (top N) -> Excel

Only new postings enter the funnel, so after the first run the paid stages
touch a few dozen jobs a day, not thousands. API calls run on a small thread
pool; all DB writes happen on the calling thread and commit as they go, so a
crash or Ctrl+C loses at most the jobs in flight and the next run resumes.
"""
import json
from pathlib import Path
import threading
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from copilot import db, export, fetch, inputs, latex, llm, runlock, scoring
from copilot.documents import render_cover_letter, render_resume, slug

log = logging.getLogger(__name__)
WORKERS = 4
ctx_lock = threading.Lock()   # guards one-time work shared by worker threads


def run(cfg, *, fetch_jobs=True, use_llm=True, limit=None, say=print, source="terminal"):
    """Full pipeline, holding the run lock so the terminal and the app can never
    run it twice at once (raises runlock.RunInProgress instead)."""
    with runlock.hold(cfg.data_dir, source):
        return _run(cfg, fetch_jobs=fetch_jobs, use_llm=use_llm, limit=limit, say=say)


def _run(cfg, *, fetch_jobs=True, use_llm=True, limit=None, say=print):
    """Full pipeline. `limit` overrides how many top jobs get documents."""
    cfg = inputs.apply(cfg)
    started = db.now()
    stats = {"fetched": 0, "new": 0, "filtered_out": 0, "scored": 0, "graded": 0, "shortlisted": 0,
             "tailored": 0, "errors": 0, "closed": 0, "boards_failed": {}}
    conn = db.connect(cfg.db_path)
    timing, clock = {}, [time.perf_counter()]

    def lap(stage):
        now = time.perf_counter()
        timing[stage] = round(now - clock[0], 1)
        clock[0] = now

    try:
        backup = db.daily_backup(cfg.db_path)
        if backup:
            say(f"Database backed up to {backup.name}.")
        synced = export.sync_status_from_excel(conn, cfg.excel_path)
        if synced:
            say(f"Pulled {synced} status/notes edits from {cfg.excel_path.name}.")

        resume_text = inputs.read_text(cfg.resume)
        say(f"Resume: {cfg.resume.name}" + (f" | Cover letter: {cfg.cover_letter.name}" if cfg.cover_letter else ""))
        _check_resume_changed(cfg, conn, say)
        lap("setup")

        if fetch_jobs:
            _fetch(cfg, conn, stats, say)
            lap("fetch")
        _prefilter(cfg, conn, stats, say)
        _score(cfg, conn, resume_text, stats, say)
        lap("filter+match")
        if use_llm:
            # A provider outage (no credit, Ollama not running, quota spent)
            # stops the AI steps but never the run: scores and Excel still save.
            try:
                _grade(cfg, conn, resume_text, stats, say)
                lap("AI grade")
                # Never auto-rewrite documents for a job already applied to.
                shortlist = sorted((j for j in db.jobs_in_stage(conn, "shortlisted") if j["is_active"]
                                    and j["status"] not in ("Applied", "Interviewing", "Offer", "Rejected")),
                                   key=lambda j: (j["fit_score"] or 0, j["match_score"] or 0), reverse=True)
                stats["shortlisted"] = len(shortlist)
                cap = limit if limit is not None else cfg.llm.get("max_tailor_per_run", 10)
                if shortlist and cap > 0:
                    say(f"{len(shortlist)} shortlisted; writing documents for the top {cap}.")
                    _make_applications(cfg, conn, resume_text, shortlist, cap, force=False, stats=stats, say=say)
                lap("AI documents")
            except llm.LLMUnavailable as exc:
                stats["llm_unavailable"] = str(exc)
                say(f"AI steps stopped: {exc}")

        stats["timing_s"] = timing
        db.prune_closed(conn)
        db.record_run(conn, started, stats)
        conn.commit()
        out = _export(cfg, conn, say)
        lap("export")
        say("Time: " + ", ".join(f"{k} {_fmt_secs(v)}" for k, v in timing.items()))
        say(f"Done. Workbook: {out}")
        return stats
    finally:
        conn.close()


def generate_for_jobs(cfg, job_ids, say=print, instruction="", source="terminal"):
    """On-demand documents, under the same run lock as a full run."""
    with runlock.hold(cfg.data_dir, source):
        return _generate_for_jobs(cfg, job_ids, say=say, instruction=instruction)


def _generate_for_jobs(cfg, job_ids, say=print, instruction=""):
    """The on-demand button: documents for specific jobs, whatever their rank.

    Unlike the automatic run, this also writes documents for postings that rule
    out sponsorship, since the user asked for them by name; the H1B column
    still shows the warning.
    """
    cfg = inputs.apply(cfg)
    conn = db.connect(cfg.db_path)
    try:
        marks = ",".join("?" * len(job_ids))
        jobs = [db.decode(r) for r in conn.execute(f"SELECT * FROM jobs WHERE id IN ({marks})", list(job_ids))]
        resume_text = inputs.read_text(cfg.resume)
        stats = {"tailored": 0, "errors": 0}
        try:
            _make_applications(cfg, conn, resume_text, jobs, len(jobs), force=True, stats=stats, say=say,
                               instruction=instruction)
        except llm.LLMUnavailable as exc:
            stats["llm_unavailable"] = str(exc)
            say(f"AI step stopped: {exc}")
        _export(cfg, conn, say)
        return stats
    finally:
        conn.close()


def _check_resume_changed(cfg, conn, say):
    """Scores belong to one resume. When it changes, every stored job is
    re-scored against the new one, not just tomorrow's new postings."""
    from copilot.tailor import file_sha

    sha = file_sha(cfg.resume)
    previous = db.get_meta(conn, "resume_sha")
    # A DB scored before this check existed has no record of its resume.
    scored_before = conn.execute("SELECT 1 FROM jobs WHERE match_score IS NOT NULL LIMIT 1").fetchone()
    if previous != sha and (previous or scored_before):
        n = db.reset_for_new_resume(conn)
        say(f"New resume detected: re-scoring {n} stored jobs against it. "
            f"Documents written earlier are marked as built from the old resume.")
    if previous != sha:
        db.set_meta(conn, "resume_sha", sha)


def _export(cfg, conn, say):
    out = export.write_workbook(conn, cfg.excel_path)
    if out != cfg.excel_path:
        say(f"{cfg.excel_path.name} is open elsewhere; wrote {out.name} instead. "
            f"Close Excel and rename it, or edits in it won't sync back.")
    return out


# ---------------- stages ----------------

def _fetch(cfg, conn, stats, say):
    companies = fetch.load_companies(cfg.companies)
    say(f"Fetching {len(companies)} job boards...")
    jobs, ok, failures = fetch.fetch_all(companies)
    stats["fetched"], stats["boards_failed"] = len(jobs), failures
    if failures:
        say(f"  {len(failures)} boards failed: {', '.join(sorted(failures))}")
    stats["new"] = sum(db.upsert_seen(conn, job) for job in jobs)
    stats["closed"] = db.mark_closed(conn, ok, {j["id"] for j in jobs})
    conn.commit()
    say(f"  {stats['fetched']:,} postings: {stats['new']:,} new, {stats['closed']} closed since last run.")


def _prefilter(cfg, conn, stats, say):
    fresh = db.jobs_in_stage(conn, "new")
    for job in fresh:
        # A job you added yourself is never filtered out by the automatic rules.
        reason = None if job["source"] == "manual" else fetch.prefilter_reason(job, cfg.prefilter)
        if reason:
            db.update(conn, job["id"], stage="filtered_out", filter_reason=reason)
            stats["filtered_out"] += 1
        else:
            db.update(conn, job["id"], stage="pending", filter_reason=None)
    conn.commit()
    if fresh:
        say(f"Prefilter: {len(fresh) - stats['filtered_out']:,} of {len(fresh):,} look relevant.")


def _score(cfg, conn, resume_text, stats, say):
    pending = db.jobs_in_stage(conn, "pending")
    if not pending:
        return
    say(f"Embedding match for {len(pending)} postings (local, free)...")
    floor = cfg.scoring.get("min_match", 0.45)
    min_fit = llm.min_fit(cfg)
    for i, job in enumerate(pending, 1):
        score, matched, missing = scoring.match(resume_text, job["description"])
        # Keep an existing LLM grade (e.g. after `rescore`): don't pay for it twice.
        if score < floor and job["source"] != "manual":   # manual jobs always reach the grader
            stage = "scored"
        elif job["fit_score"] is not None:
            stage = "shortlisted" if job["fit_score"] >= min_fit else "graded"
        else:
            stage = "candidate"
        db.update(conn, job["id"], stage=stage, match_score=score,
                  matched_skills=matched, missing_skills=missing)
        stats["scored"] += 1
        if i % 100 == 0:
            conn.commit()
            say(f"  {i}/{len(pending)}")
    conn.commit()


def _grade(cfg, conn, resume_text, stats, say):
    from copilot import grade

    # A closed posting can't be applied to: not worth a grade.
    candidates = sorted((j for j in db.jobs_in_stage(conn, "candidate") if j["is_active"]),
                        key=lambda j: j["match_score"], reverse=True)
    settings = llm.task_settings(cfg, "grade")
    min_fit = llm.min_fit(cfg)
    resume_key = db.text_key(resume_text)

    def cache_key(job):
        return f"grade:{settings.get('model')}:{resume_key}:{db.text_key(job['description'])}"

    # A grade depends only on the resume, the posting and the model: going back to a
    # resume graded before costs nothing.
    missing = []
    for job in candidates:
        hit = db.cache_get(conn, cache_key(job))
        if hit is None:
            missing.append(job)
        else:
            db.update(conn, job["id"], stage="shortlisted" if hit["fit"] >= min_fit else "graded",
                      fit_score=hit["fit"], fit_reason=hit["reason"], dealbreakers=hit["dealbreakers"])
            stats["graded"] += 1
    if len(missing) < len(candidates):
        conn.commit()
        say(f"{len(candidates) - len(missing)} grades reused from earlier runs.")
    todo = missing[:cfg.llm.get("max_grade_per_run", 300)]
    if not todo:
        return
    say(f"AI fit grade for {len(todo)} of {len(missing)} candidates ({settings['provider']}: {settings['model']})...")
    workers = settings.get("workers", WORKERS)

    # Grade one first: it caches the shared resume prefix, and it fails fast
    # if the provider is unavailable instead of failing the whole batch.
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {pool.submit(grade.grade, resume_text, j, settings): j for j in todo[:1]}
        for fut in as_completed(futures):
            _save_grade(conn, futures[fut], fut, min_fit, stats, cache_key)
        futures = {pool.submit(grade.grade, resume_text, j, settings): j for j in todo[1:]}
        for i, fut in enumerate(as_completed(futures), 2):
            _save_grade(conn, futures[fut], fut, min_fit, stats, cache_key)
            if i % 25 == 0:
                conn.commit()
                say(f"  {i}/{len(todo)} graded")
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
        conn.commit()


def _save_grade(conn, job, fut, min_fit, stats, cache_key):
    try:
        g, _ = fut.result()
    except llm.LLMUnavailable:
        raise
    except Exception as exc:
        log.warning("grading failed for %s: %s", job["id"], exc)
        stats["errors"] += 1
        return
    db.update(conn, job["id"], stage="shortlisted" if g.fit >= min_fit else "graded",
              fit_score=g.fit, fit_reason=g.reason, dealbreakers=g.dealbreakers)
    db.cache_put(conn, cache_key(job), {"fit": g.fit, "reason": g.reason, "dealbreakers": g.dealbreakers})
    stats["graded"] += 1


# ---------------- applications ----------------

def _make_applications(cfg, conn, resume_text, jobs, cap, *, force, stats, say, instruction=""):
    """Write documents for up to `cap` jobs, in order. Jobs skipped for ruling out
    sponsorship don't count toward the cap, so the next job takes their place."""
    from copilot import tailor

    ctx = {
        "cfg": cfg,
        "resume_text": resume_text,
        "master": tailor.load_or_build_master(cfg, resume_text, log=say),
        "base_letter": inputs.read_text(cfg.cover_letter) if cfg.cover_letter else "",
        "resume_sha": tailor.file_sha(cfg.resume),
        "force": force,
        "instruction": instruction,
        # Checked once per run: the writer model, or its fallback if it can't serve.
        "writer": llm.preflight(llm.task_settings(cfg, "tailor"), say),
        "writing": cfg.writing,
        # Your reviewed edits, shown to the writer as style examples.
        "examples": db.feedback_examples(conn),
    }
    say(f"Writer: {ctx['writer']['model']}")
    queue, done, running = list(jobs), 0, {}
    width = ctx["writer"].get("workers", 3)
    pool = ThreadPoolExecutor(max_workers=width)
    try:
        while queue or running:
            while queue and len(running) < width and done + len(running) < cap:
                job = queue.pop(0)
                running[pool.submit(_application, ctx, job)] = job
            if not running:
                break
            fut = next(as_completed(running))
            job = running.pop(fut)
            label = f"{job['company']} | {job['title']}"
            try:
                updates = fut.result()
            except llm.LLMUnavailable:
                raise
            except Exception as exc:
                log.exception("documents failed for %s", job["id"])
                db.update(conn, job["id"], stage="error", error=str(exc)[:500])
                conn.commit()
                stats["errors"] += 1
                say(f"  FAIL  {label}: {exc}")
                continue
            db.update(conn, job["id"], **updates)
            conn.commit()
            if updates.get("stage") == "tailored":
                done += 1
                stats["tailored"] += 1
                cov = (updates.get("tailor_report") or {}).get("coverage", {})
                say(f"  done  {label}  fit {updates['fit_score']} -> {updates['tailored_fit']}"
                    + (f"  keywords {cov['before']}/{cov['total']} -> {cov['after']}/{cov['total']}" if cov else "")
                    + ("  [review flags]" if _hard(updates["tailor_flags"]) else ""))
            else:
                say(f"  skip  {label}  (posting rules out sponsorship)")
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


def _application(ctx, job):
    """API calls and file writes for one job. Runs on a worker thread, so it
    returns the DB updates instead of writing them."""
    from copilot import grade, tailor

    cfg, resume_text, master = ctx["cfg"], ctx["resume_text"], ctx["master"]
    llm_cfg = cfg.llm
    grade_settings = llm.task_settings(cfg, "grade")
    updates = {}

    # On demand, always re-grade the original first: a stored grade may come from
    # another model or an older resume, which would make "before -> after" meaningless.
    # Grading, the sponsorship check and loading the embedding model don't depend
    # on the writing, so they run alongside it instead of before it.
    side = ThreadPoolExecutor(max_workers=3)
    try:
        side.submit(scoring._agent)   # warm the embedding model used afterwards
        regrade = (side.submit(grade.grade, resume_text, job, grade_settings)
                   if job["fit_score"] is None or ctx["force"] else None)
        sponsor = side.submit(scoring.sponsorship_cached, cfg, job["description"],
                              llm.task_settings(cfg, "sponsorship"))

        def sponsorship_result():
            jd = sponsor.result()
            f = scoring.final(resume_text, job["description"], job["company"], jd)
            updates.update(sponsorship_status=jd["status"], sponsorship_evidence=jd["evidence"],
                           sponsorship_verified=int(bool(jd["verified"])), sponsorship_basis=f["basis"],
                           multiplier=f["multiplier"], final_score=f["final"])
            return jd, f

        if not ctx["force"]:
            # The automatic run checks sponsorship first: it decides whether to write at all.
            jd, f = sponsorship_result()
            if llm_cfg.get("skip_no_sponsorship", True) and f["basis"] == "posting rules it out":
                return {**updates, "stage": "graded", "status": "Skipped",
                        "notes": "Posting rules out sponsorship: " + (jd["evidence"] or "")[:200]}

        resume, letter, changes, flags, report = tailor.tailor(
            master, job, job["matched_skills"], job["missing_skills"],
                ctx["writer"], base_letter=ctx["base_letter"], instruction=ctx["instruction"],
            writing=ctx["writing"], examples=ctx["examples"])

        if ctx["force"]:
            jd, f = sponsorship_result()
        if regrade:
            g, _ = regrade.result()
            updates.update(fit_score=g.fit, fit_reason=g.reason, dealbreakers=g.dealbreakers)
            job = {**job, "fit_score": g.fit}
    finally:
        side.shutdown(wait=False)

    folder = cfg.output_dir / f"{datetime.now():%Y-%m-%d}_{slug(job['company'], 20)}_{slug(job['title'])}"
    folder.mkdir(parents=True, exist_ok=True)
    name = slug(master.contact.name, 30)
    stem = slug(job["company"], 20)
    letter_path = folder / f"{name}_Cover_Letter_{stem}.docx"
    tex_path = None
    if cfg.resume.suffix.lower() == ".tex":
        # Your own LaTeX layout: edits written back into the source, compiled to PDF.
        try:
            resume, tex_path, resume_path, review_path = _render_latex(
                ctx, master.model_dump(), resume, report, folder, f"{name}_Resume_{stem}")
        except latex.LaTeXError as exc:
            flags.append(f"LaTeX build failed ({exc}); a Word version was made instead")
            tex_path = None
    if tex_path is None:
        resume_path = folder / f"{name}_Resume_{stem}.docx"
        review_path = folder / f"REVIEW_changes_in_bold_{stem}.docx"
        render_resume(resume, resume_path)                       # the copy you send
        render_resume(resume, review_path, changed=_changed_texts(report, resume, master))  # for you
    render_cover_letter(letter, resume["contact"], job["company"], letter_path)

    # Judge the tailoring with the same grader that ranked the job. The
    # embedding match is reported too, but it falls whenever a resume gets
    # more focused, so it can't be the measure of "better".
    tailored_text = tailor.resume_to_text(resume)
    t_match, _, _ = scoring.match(tailored_text, job["description"])
    t_fit = grade.grade(tailored_text, job, grade_settings)[0].fit
    # No flag on fit before/after: the same resume graded 78-90 across runs on
    # one job (Oct 2026, gpt-oss), so the difference is mostly grader noise.
    # Keyword coverage, counted in code, is the measure of what tailoring changed.

    (folder / "job.json").write_text(json.dumps(
        {**{k: job[k] for k in ("id", "company", "title", "location", "url", "apply_url", "description")},
         "fit_score": job["fit_score"], "tailored_fit": t_fit, "changes": changes, "review_flags": flags},
        indent=2, ensure_ascii=False), encoding="utf-8")

    updates.update(stage="tailored", fit_score=job["fit_score"], tailored_match=t_match,
                   tailored_fit=t_fit, tailored_final=t_match * f["multiplier"],
                   resume_path=str(resume_path), cover_letter_path=str(letter_path),
                   review_resume_path=str(review_path),
                   resume_tex_path=str(tex_path) if tex_path else None,
                   tailor_flags=flags, tailor_changes=changes, tailored_at=db.now(),
                   docs_resume_sha=ctx["resume_sha"], error=None,
                   tailored_resume=resume, cover_letter_text=letter, tailor_report=report,
                   revision=(job.get("revision") or 0) + 1)
    # Regenerating never rolls back progress the user recorded (Applied, ...).
    if job.get("status") in (None, "New", "Review", "Ready", "Skipped"):
        updates["status"] = "Review" if _hard(flags) else "Ready"
    return updates


def _hard(flags):
    """Guard flags that need a human look (auto-removed skills don't)."""
    return [f for f in flags if not f.startswith("removed skills")]


def retry_errors(conn):
    """Send failed jobs back to the shortlist so the next run retries them."""
    n = conn.execute("UPDATE jobs SET stage = 'shortlisted', error = NULL WHERE stage = 'error'").rowcount
    conn.commit()
    return n


def save_cover_letter(cfg, job_id, text):
    """Store a hand-edited cover letter and re-render its DOCX."""
    conn = db.connect(cfg.db_path)
    try:
        job = db.decode(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())
        contact = (job["tailored_resume"] or {}).get("contact")
        if not contact:
            contact = json.loads(cfg.master_resume_path.read_text(encoding="utf-8"))["contact"]
        render_cover_letter(text, contact, job["company"], job["cover_letter_path"])
        db.update(conn, job_id, cover_letter_text=text, revision=(job["revision"] or 0) + 1)
        conn.commit()
    finally:
        conn.close()


def _fmt_secs(s):
    return f"{s:.0f}s" if s < 90 else f"{s / 60:.1f}min"


def _changed_texts(report, resume, master):
    """Texts to bold in the review copy: every accepted bullet edit, plus the
    summary when it differs from the master resume's."""
    changed = {a["after"] for a in report["accepted"]}
    original = master["summary"] if isinstance(master, dict) else master.summary
    if resume["summary"] != original:
        changed.add(resume["summary"])
    return frozenset(changed)


def _render_latex(ctx, master, resume, report, folder, stem, max_rollbacks=6):
    """Write the tailored resume into the candidate's own .tex and compile it.

    The page count must not grow: if it does, the accepted edits that added the
    most text are rolled back one at a time until it fits. Returns
    (resume, tex_path, pdf_path, review_pdf_path).
    """
    from copilot import tailor

    tex = ctx["cfg"].resume.read_text(encoding="utf-8")
    if "base_pages" not in ctx:
        with ctx_lock:
            if "base_pages" not in ctx:
                ctx["base_pages"] = latex.compile_pdf(tex, folder / "_original_check.pdf")
                (folder / "_original_check.pdf").unlink(missing_ok=True)
    pdf = folder / f"{stem}.pdf"
    for _ in range(max_rollbacks + 1):
        out = latex.render(tex, master, resume)
        pages = latex.compile_pdf(out, pdf)
        if pages <= ctx["base_pages"] or not report["accepted"]:
            break
        # Roll back the edit that grew the most, then try again.
        worst = max(report["accepted"], key=lambda a: len(a["after"]) - len(a["before"]))
        tailor._replace_bullet(resume, tailor.bullet_ids(master)[worst["id"]][0], worst["after"], worst["before"])
        report["accepted"].remove(worst)
        report["rejected"].append({"id": worst["id"], "proposed": worst["after"],
                                   "why": f"rolled back to keep the resume at {ctx['base_pages']} page(s)"})
    tex_path = folder / f"{stem}.tex"
    tex_path.write_text(out, encoding="utf-8")
    review_pdf = folder / f"REVIEW_changes_in_bold_{stem}.pdf"
    latex.compile_pdf(latex.render(tex, master, resume, changed=_changed_texts(report, resume, master)), review_pdf)
    return resume, tex_path, pdf, review_pdf


def add_manual_job(cfg, *, company, title, description, url="", location="", generate=False,
                   say=print, source="terminal"):
    """Add a job you found yourself (LinkedIn, a referral, any careers page),
    score it like any crawled job, and optionally write its documents.

    Manual jobs skip the title/location filters and are never marked closed by
    a crawl. Re-adding the same link updates the job. Returns its id.
    """
    import hashlib

    with runlock.hold(cfg.data_dir, source):
        cfg = inputs.apply(cfg)
        conn = db.connect(cfg.db_path)
        try:
            key = url.strip() or f"{company}|{title}|{description[:300]}"
            job_id = "manual:" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
            db.upsert_seen(conn, {
                "id": job_id, "source": "manual", "company": company.strip(), "title": title.strip(),
                "location": location.strip(), "country": None, "department": "", "url": url.strip(),
                "apply_url": url.strip(), "description": description.strip(), "posted_at": db.now()})
            conn.commit()

            resume_text = inputs.read_text(cfg.resume)
            score, matched, missing = scoring.match(resume_text, description)
            db.update(conn, job_id, stage="candidate", match_score=score,
                      matched_skills=matched, missing_skills=missing, filter_reason=None)
            conn.commit()
            say(f"Match {score:.2f}; {len(matched)} of the posting's skills on your resume.")

            job = db.decode(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())
            try:
                from copilot import grade
                g, _ = grade.grade(resume_text, job, llm.task_settings(cfg, "grade"))
                jd = scoring.sponsorship_cached(cfg, description, llm.task_settings(cfg, "sponsorship"))
                f = scoring.final(resume_text, description, company, jd)
                db.update(conn, job_id, stage="shortlisted" if g.fit >= llm.min_fit(cfg) else "graded",
                          fit_score=g.fit, fit_reason=g.reason, dealbreakers=g.dealbreakers,
                          sponsorship_status=jd["status"], sponsorship_evidence=jd["evidence"],
                          sponsorship_verified=int(bool(jd["verified"])), sponsorship_basis=f["basis"],
                          multiplier=f["multiplier"], final_score=f["final"])
                conn.commit()
                say(f"Fit {g.fit}/100: {g.reason}")
                if f["basis"] == "posting rules it out":
                    say(f"Note: this posting rules out sponsorship: \"{jd['evidence']}\"")
                if generate:
                    job = db.decode(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())
                    stats = {"tailored": 0, "errors": 0}
                    _make_applications(cfg, conn, resume_text, [job], 1, force=True, stats=stats, say=say)
            except llm.LLMUnavailable as exc:
                say(f"Saved with its match score; AI steps stopped: {exc}")
            _export(cfg, conn, say)
            return job_id
        finally:
            conn.close()


def apply_feedback(cfg, job_id, edit_id, verdict, text="", source="app"):
    """Record your verdict on one AI edit and update the documents to match.

    keep: the edit stays. reject: your original bullet comes back. rewrite:
    your own wording replaces the AI's. Every verdict is logged, and the
    log feeds the writer as style examples for future jobs.
    """
    from copilot import tailor

    with runlock.hold(cfg.data_dir, source):
        cfg = inputs.apply(cfg)
        conn = db.connect(cfg.db_path)
        try:
            job = db.decode(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())
            report, resume = job["tailor_report"], job["tailored_resume"]
            edit = next(a for a in report["accepted"] if a["id"] == edit_id)
            sec = "experience" if edit_id.startswith("E") else "projects"
            final = {"keep": edit["after"], "reject": edit["before"]}.get(verdict) or tailor.plain(" ".join(text.split()))
            if verdict == "rewrite":
                broken = latex.token_problem(edit["before"], final)
                if broken:
                    raise ValueError(f"Keep the link text exactly as before ({broken}).")
            if final != edit["after"]:
                tailor._replace_bullet(resume, sec, edit["after"], final)
            db.add_feedback(conn, job, edit, verdict, final, (report.get("writer") or ""))
            if verdict == "reject":
                report["accepted"].remove(edit)
                report["rejected"].append({"id": edit_id, "proposed": edit["after"], "why": "rejected by you"})
            else:
                edit.update(after=final, verdict=verdict)
            if verdict != "keep":   # reject/rewrite change the text, so rebuild the files
                _rerender(cfg, job, resume, report)
            db.update(conn, job_id, tailored_resume=resume, tailor_report=report,
                      revision=(job["revision"] or 0) + 1)
            conn.commit()
            export.write_workbook(conn, cfg.excel_path)
        finally:
            conn.close()


def _rerender(cfg, job, resume, report):
    """Rebuild a pack's resume files after a feedback change."""
    from copilot import tailor

    master = json.loads(cfg.master_resume_path.read_text(encoding="utf-8"))
    master.pop("_source_sha256", None)
    changed = _changed_texts(report, resume, master)
    if job.get("resume_tex_path"):
        if tailor.file_sha(cfg.resume) != job["docs_resume_sha"]:
            raise ValueError("Your resume changed since this pack was made; click Regenerate instead.")
        tex = cfg.resume.read_text(encoding="utf-8")
        out = latex.render(tex, master, resume)
        Path(job["resume_tex_path"]).write_text(out, encoding="utf-8")
        latex.compile_pdf(out, job["resume_path"])
        latex.compile_pdf(latex.render(tex, master, resume, changed=changed), job["review_resume_path"])
    else:
        render_resume(resume, job["resume_path"])
        if job.get("review_resume_path"):
            render_resume(resume, job["review_resume_path"], changed=changed)
