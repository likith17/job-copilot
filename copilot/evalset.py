"""A fixed evaluation set for the writer: measure every prompt or model change.

    python -m copilot eval --create 10        pick 10 jobs (one per company) as the set
    python -m copilot eval                    run the writer on the set with config.yaml settings
    python -m copilot eval --model X --effort low   try another model/effort

Each run is saved to data/evals/ and compared with the previous one. Nothing
is written to the jobs table and no documents are rendered.
"""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from difflib import SequenceMatcher

from copilot import db, inputs, llm, tailor


def _paths(cfg):
    folder = cfg.data_dir / "evals"
    folder.mkdir(exist_ok=True)
    return cfg.data_dir / "eval_jobs.json", folder


def create(cfg, n=10):
    """Pick n graded jobs with the best fit, one per company, as the eval set."""
    set_path, _ = _paths(cfg)
    conn = db.connect(cfg.db_path)
    rows = conn.execute("""SELECT id, company FROM jobs WHERE fit_score IS NOT NULL
                           AND length(description) > 500 ORDER BY fit_score DESC""").fetchall()
    picked, seen = [], set()
    for r in rows:
        if r["company"] not in seen:
            picked.append(r["id"])
            seen.add(r["company"])
        if len(picked) == n:
            break
    set_path.write_text(json.dumps(picked, indent=1), encoding="utf-8")
    return picked


def _similar(a, b):
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def run(cfg, model=None, effort=None, workers=3, say=print, mode=None, finetuned=False):
    cfg = inputs.apply(cfg)
    set_path, folder = _paths(cfg)
    if not set_path.exists():
        create(cfg)
    job_ids = json.loads(set_path.read_text(encoding="utf-8"))
    conn = db.connect(cfg.db_path)
    jobs = [db.decode(r) for r in (conn.execute("SELECT * FROM jobs WHERE id = ?", (j,)).fetchone()
                                   for j in job_ids) if r]
    settings = llm.task_settings(cfg, "tailor")
    settings.pop("fallback", None)
    if model and not model.startswith("claude"):
        # Any non-Claude model runs through the local/Ollama provider (free).
        settings = {**llm.DEFAULT_PROVIDERS["local"], **(cfg.llm.get("providers") or {}).get("local", {}),
                    "provider": "local", "model": model, "workers": 3}
    elif model:
        settings = {**settings, "model": model}
    if effort:
        settings["effort"] = effort
    if mode:
        settings["mode"] = mode
    if finetuned:
        settings.update(finetuned=True, mode="per_bullet")
    resume_text = inputs.read_text(cfg.resume)
    from copilot.training import master_for_training
    mdict = master_for_training(cfg, say)
    master = tailor.MasterResume.model_validate(mdict)
    base_letter = inputs.read_text(cfg.cover_letter) if cfg.cover_letter else ""
    examples = db.feedback_examples(conn)
    feedback = [dict(r) for r in conn.execute("SELECT * FROM edit_feedback")]
    master_metrics = tailor._metrics("\n".join(b for x in mdict["experience"] + mdict["projects"] for b in x["bullets"]))

    def one(job):
        t0 = time.perf_counter()
        try:
            resume, letter, changes, flags, report = tailor.tailor(
                master, job, job["matched_skills"], job["missing_skills"], settings, base_letter,
                writing=cfg.writing, examples=examples)
        except llm.LLMError as exc:
            return {"job": job["id"], "error": str(exc)[:200]}
        text = tailor.resume_to_text(resume)
        cov = report["coverage"]
        # Agreement with your own reviews of the same bullets (from earlier jobs).
        mine = [f for f in feedback if f["verdict"] in ("keep", "rewrite")]
        bad = [f for f in feedback if f["verdict"] == "reject"]
        agree = [max((_similar(a["after"], f["final"]) for f in mine if f["edit_id"] == a["id"]), default=None)
                 for a in report["accepted"]]
        repeats = sum(1 for a in report["accepted"] for f in bad
                      if f["edit_id"] == a["id"] and _similar(a["after"], f["proposed"]) > 0.9)
        return {
            "job": job["id"], "company": job["company"], "secs": round(time.perf_counter() - t0, 1),
            "edits": len(report["accepted"]),
            "cosmetic_rejects": sum(r["why"].startswith("cosmetic") for r in report["rejected"]),
            "rule_rejects": sum(not r["why"].startswith("cosmetic") for r in report["rejected"]),
            "keyword_gain": cov["after"] - cov["before"],
            "flags": len(flags),
            "metrics_lost": len(master_metrics - tailor._metrics(text)),
            "agreement_with_you": round(sum(a for a in agree if a is not None) / max(1, sum(a is not None for a in agree)), 2)
            if any(a is not None for a in agree) else None,
            "repeats_your_rejections": repeats,
            "role_in_summary": tailor.summary_problem(resume["summary"], report.get("role", ""), []) is None,
            "headline": resume.get("headline", ""),
        }

    say(f"Evaluating {settings['model']} (effort {settings.get('effort', 'default')}, "
        f"{settings.get('mode', 'plan')} mode) on {len(jobs)} jobs...")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(one, jobs))

    ok = [r for r in results if "error" not in r]
    agree = [r["agreement_with_you"] for r in ok if r["agreement_with_you"] is not None]
    summary = {
        "model": settings["model"], "effort": settings.get("effort"), "mode": settings.get("mode", "plan"), "jobs": len(jobs), "errors": len(results) - len(ok),
        "edits_per_job": round(sum(r["edits"] for r in ok) / max(len(ok), 1), 2),
        "keyword_gain_per_job": round(sum(r["keyword_gain"] for r in ok) / max(len(ok), 1), 2),
        "flags_per_job": round(sum(r["flags"] for r in ok) / max(len(ok), 1), 2),
        "cosmetic_rejects_per_job": round(sum(r["cosmetic_rejects"] for r in ok) / max(len(ok), 1), 2),
        "metrics_lost_total": sum(r["metrics_lost"] for r in ok),
        "agreement_with_you": round(sum(agree) / len(agree), 2) if agree else None,
        "repeats_your_rejections": sum(r["repeats_your_rejections"] for r in ok),
        "role_in_summary": sum(r["role_in_summary"] for r in ok),
        "secs_per_job": round(sum(r["secs"] for r in ok) / max(len(ok), 1), 1),
        "reviewed_examples_used": len(examples["approved"]) + len(examples["rejected"]),
    }
    previous = sorted(folder.glob("*.json"))
    out = folder / f"{datetime.now():%Y%m%d_%H%M%S}_{settings['model'].replace(':', '-')}.json"
    out.write_text(json.dumps({"summary": summary, "results": results}, indent=1), encoding="utf-8")

    say("")
    last = json.loads(previous[-1].read_text(encoding="utf-8"))["summary"] if previous else {}
    for k, v in summary.items():
        before = last.get(k)
        say(f"  {k:26} {v!s:>12}" + (f"   (previous: {before})" if before is not None and before != v else ""))
    say(f"\nSaved {out.name}")
    return summary
