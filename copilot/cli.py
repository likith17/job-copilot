"""Command line: python -m copilot <command>

  run            full daily pipeline (fetch, score, grade, tailor, export)
  run --no-llm   fetch and score locally only, no API spend
  rescore        re-run prefilter and scoring on stored jobs (after editing
                 resume or config); keeps tailored jobs and paid grades
  generate ID..  write a resume + cover letter for specific jobs
  add            score a job you found yourself (--generate also writes documents)
  eval           measure the writer on a fixed set of jobs (--create N to pick the set)
  export         rebuild the Excel sheet from the DB
  status ID S    set a job's status (Applied, Interviewing, ...)
  companies      check that every board in companies.yaml responds
  retry          re-queue jobs whose tailoring failed
  top            print the best jobs ready to apply to
"""
import argparse
import logging
import sys
from pathlib import Path

from copilot import db, export, fetch, inputs, pipeline, runlock
from copilot.config import load_config


def say(msg):
    print(msg, flush=True)  # stream progress even when output goes to a log file


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(prog="python -m copilot", description="Job Copilot daily pipeline")
    ap.add_argument("--config", help="path to config.yaml")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="fetch, score, tailor and export")
    p_run.add_argument("--no-fetch", action="store_true", help="skip crawling; process what's in the DB")
    p_run.add_argument("--no-llm", action="store_true", help="skip all paid LLM work")
    p_run.add_argument("--limit", type=int, help="override llm.max_tailor_per_run for this run")
    p_run.add_argument("--source", default="terminal", help=argparse.SUPPRESS)
    sub.add_parser("auto", help="the daily job: run the pipeline, send what passes, write a digest")
    p_sched = sub.add_parser("schedule", help="run the daily job automatically (Windows)")
    p_sched.add_argument("--time", default="09:00")
    p_sched.add_argument("--remove", action="store_true")

    p_add = sub.add_parser("add", help="score a job you found yourself")
    p_add.add_argument("--company", required=True)
    p_add.add_argument("--title", required=True)
    p_add.add_argument("--url", default="", help="posting or application link")
    p_add.add_argument("--location", default="")
    p_add.add_argument("--file", help="text file with the job description (default: read stdin)")
    p_add.add_argument("--generate", action="store_true", help="also write the resume and cover letter")
    p_eval = sub.add_parser("eval", help="measure the writer on a fixed set of jobs")
    p_eval.add_argument("--create", type=int, help="pick N jobs as the eval set, then exit")
    p_eval.add_argument("--model", help="override the writer model")
    p_eval.add_argument("--effort", help="override the effort level (low/medium/high)")
    p_eval.add_argument("--mode", choices=["plan", "per_bullet"], help="whole-resume plan or one call per bullet")
    p_eval.add_argument("--finetuned", action="store_true", help="the model is your fine-tuned writer (bare prompts)")
    p_td = sub.add_parser("train-data", help="generate / assemble training data for your own writer model")
    p_td.add_argument("--teacher", help="Ollama model that writes examples, e.g. kimi-k3:cloud")
    p_td.add_argument("--jobs", type=int, default=100, help="how many crawled jobs the teacher works through")
    p_td.add_argument("--build", action="store_true", help="assemble data/training/sft/ from everything logged")
    p_gen = sub.add_parser("generate", help="write documents for specific job ids")
    p_gen.add_argument("job_ids", nargs="+")
    p_gen.add_argument("--instruction", default="", help="a one-off request for the writer")
    p_gen.add_argument("--source", default="terminal", help=argparse.SUPPRESS)
    sub.add_parser("export", help="rebuild the Excel workbook")
    sub.add_parser("rescore", help="re-queue stored jobs for prefilter + scoring")
    p_status = sub.add_parser("status", help="set a job's status")
    p_status.add_argument("job_id")
    p_status.add_argument("status", choices=db.STATUSES)
    p_status.add_argument("--notes")
    sub.add_parser("companies", help="verify every configured board")
    sub.add_parser("retry", help="re-queue failed jobs")
    p_top = sub.add_parser("top", help="list the best tailored jobs")
    p_top.add_argument("-n", type=int, default=15)
    sub.add_parser("serve", help="run the local API for the autofill extension")
    p_apply = sub.add_parser("apply", help="open and fill your top applications in the browser")
    p_apply.add_argument("--submit", action="store_true", help="submit the ones that pass every gate")
    p_apply.add_argument("--url", help="fill this one application page")
    p_apply.add_argument("-n", type=int, help="how many jobs (default apply.per_run)")
    p_apply.add_argument("--source", default="terminal", help=argparse.SUPPRESS)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.ERROR,
                        format="%(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config)

    try:
        if args.cmd == "run":
            pipeline.run(cfg, fetch_jobs=not args.no_fetch, use_llm=not args.no_llm, limit=args.limit, say=say,
                         source=args.source)
            return
        if args.cmd == "add":
            text = Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read()
            if len(text.strip()) < 100:
                sys.exit("Paste the full job description (at least a few sentences).")
            job_id = pipeline.add_manual_job(cfg, company=args.company, title=args.title, description=text,
                                             url=args.url, location=args.location,
                                             generate=args.generate, say=say)
            say(f"Saved as {job_id}")
            return
        if args.cmd == "eval":
            from copilot import evalset
            if args.create:
                say(f"Eval set: {len(evalset.create(cfg, args.create))} jobs saved to data/eval_jobs.json")
            else:
                evalset.run(cfg, model=args.model, effort=args.effort, say=say, mode=args.mode,
                            finetuned=args.finetuned)
            return
        if args.cmd == "train-data":
            from copilot import training
            if args.teacher:
                training.generate(cfg, args.teacher, args.jobs, say=say)
            if args.build or not args.teacher:
                training.build(cfg, say=say)
            return
        if args.cmd == "generate":
            pipeline.generate_for_jobs(cfg, args.job_ids, say=say, instruction=args.instruction, source=args.source)
            return
    except runlock.RunInProgress as exc:
        sys.exit(str(exc))

    if args.cmd == "serve":
        from copilot import applyserver
        say(f"Autofill API on http://127.0.0.1:{applyserver.PORT}  (Ctrl+C to stop)")
        say(f"Extension token: {applyserver.get_token(cfg)}")
        if applyserver.serve(cfg) is None:
            say("Port in use: the API is already running (the app starts it too).")
        return

    if args.cmd == "auto":
        from copilot import auto
        auto.daily(cfg, say=say)
        return
    if args.cmd == "schedule":
        from copilot import auto
        auto.unschedule(say) if args.remove else auto.schedule(args.time, say)
        return
    if args.cmd == "apply":
        from copilot import autoapply
        try:
            autoapply.run(inputs.apply(cfg), submit_ok=args.submit, url=args.url, limit=args.n,
                          source=args.source, say=say)
        except runlock.RunInProgress:
            sys.exit("Applications are already open in another browser window. Finish there first.")
        return

    if args.cmd == "companies":
        companies = fetch.load_companies(cfg.companies)
        jobs, ok, failures = fetch.fetch_all(companies)
        counts = {}
        for j in jobs:
            counts[j["company"]] = counts.get(j["company"], 0) + 1
        for c in companies:
            if c.name in failures:
                print(f"FAIL  {c.name:22} {c.ats}:{c.identifier}  {failures[c.name][:80]}")
            else:
                n = counts.get(c.name, 0)
                print(f"{'ok  ' if n else 'EMPTY'}  {c.name:22} {c.ats}:{c.identifier}  {n} postings")
        return

    conn = db.connect(cfg.db_path)
    if args.cmd == "export":
        n = export.sync_status_from_excel(conn, cfg.excel_path)
        print(f"Synced {n} edits. Wrote {export.write_workbook(conn, cfg.excel_path)}")
    elif args.cmd == "status":
        if not conn.execute("SELECT 1 FROM jobs WHERE id = ?", (args.job_id,)).fetchone():
            sys.exit(f"No job with id {args.job_id}")
        db.set_status(conn, args.job_id, args.status, args.notes)
        conn.commit()
        export.write_workbook(conn, cfg.excel_path)
        print(f"{args.job_id} -> {args.status}")
    elif args.cmd == "rescore":
        print(f"Re-queued {db.reset_for_rescore(conn)} jobs. Run `python -m copilot run --no-fetch` to process them.")
    elif args.cmd == "retry":
        print(f"Re-queued {pipeline.retry_errors(conn)} jobs.")
    elif args.cmd == "top":
        rows = conn.execute(
            """SELECT company, title, fit_score, sponsorship_basis, apply_url, status FROM jobs
               WHERE stage = 'tailored' AND is_active = 1 AND status IN ('Ready', 'New')
               ORDER BY fit_score DESC, tailored_final DESC LIMIT ?""", (args.n,)).fetchall()
        for r in rows:
            print(f"{r['fit_score']:3}  {r['status']:5}  {r['company']:16} {r['title'][:45]:45} "
                  f"{export.H1B_LABEL.get(r['sponsorship_basis'], '?'):16} {r['apply_url']}")
    conn.close()


if __name__ == "__main__":
    main()
