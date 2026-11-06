"""The daily job: crawl, grade, write documents, send what passes every gate, report.

    python -m copilot auto                 run it now
    python -m copilot schedule --time 09:00    run it every day (Windows Task Scheduler)
    python -m copilot schedule --remove

Sending is off until config.yaml says `apply: auto_submit: true`; until then the
run stops after writing documents. To pause sending at any time, use the app's
switch or create data/STOP_AUTO_APPLY. The digest of every run is in data/digests/.
"""
import json
import subprocess
from datetime import datetime

from copilot import autoapply, db, inputs, pipeline, runlock
from copilot.config import ROOT

TASK = "JobCopilotDaily"


def daily(cfg, say=print):
    cfg = inputs.apply(cfg)
    opts = autoapply.settings(cfg)
    stats, results, problems = {}, [], []
    try:
        stats = pipeline.run(cfg, say=say, source="schedule")
    except runlock.RunInProgress as exc:
        problems.append(str(exc))
    if stats.get("llm_unavailable"):
        problems.append(f"AI steps stopped: {stats['llm_unavailable']}")
    if not opts["auto_submit"]:
        say("Sending is off (apply.auto_submit in config.yaml).")
    elif autoapply.stopped(cfg):
        problems.append(f"Sending is paused: {autoapply.stop_file(cfg)} exists.")
    else:
        results = autoapply.run(cfg, submit_ok=True, unattended=True, say=say)
    path = write_digest(cfg, stats, results, problems, opts)
    say(f"Digest: {path}")
    return results


def write_digest(cfg, stats, results, problems, opts):
    conn = db.connect(cfg.db_path)
    sent = [r for r in results if r["outcome"] == "submitted"]
    held = [r for r in results if r["outcome"] != "submitted"]

    def label(r):
        row = conn.execute("SELECT company, title FROM jobs WHERE id = ?", (r["job"],)).fetchone()
        return f"{row['company']}: {row['title']}" if row else r["url"]

    lines = [f"# Job Copilot, {datetime.now():%A %Y-%m-%d %H:%M}", ""]
    lines += [f"**Problems:** {p}" for p in problems] + ([""] if problems else [])
    lines += [f"Crawl: {stats.get('new', 0)} new postings, {stats.get('shortlisted', 0)} shortlisted, "
              f"{stats.get('tailored', 0)} documents written, {stats.get('errors', 0)} errors.", ""]
    if not opts["auto_submit"]:
        lines += ["Sending is off (`apply.auto_submit: false`).", ""]
    lines += [f"## Sent ({len(sent)})"] + [f"- {label(r)}" for r in sent] + [""]
    lines += [f"## Needs you ({len(held)})"] + [f"- {label(r)}: {r['note']}\n  {r['url']}" for r in held] + [""]
    questions = db.open_questions(conn)
    required = [q["question"] for q in questions if q["required"]]
    lines += [f"## Questions to answer in the app ({len(questions)}; {len(required)} required)"]
    lines += [f"- {q}" for q in required[:15]]
    folder = cfg.data_dir / "digests"
    folder.mkdir(exist_ok=True)
    path = folder / f"{datetime.now():%Y-%m-%d_%H%M}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    conn.close()
    return path


def schedule(time_of_day, say=print):
    """Register the daily job. It runs at the next chance if the laptop was off or asleep."""
    python = ROOT / ".venv" / "Scripts" / "python.exe"
    script = (f"$a = New-ScheduledTaskAction -Execute '{python}' -Argument '-m copilot auto' -WorkingDirectory '{ROOT}'; "
              f"$t = New-ScheduledTaskTrigger -Daily -At '{time_of_day}'; "
              "$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries; "
              f"Register-ScheduledTask -TaskName '{TASK}' -Action $a -Trigger $t -Settings $s -Force | Out-Null")
    subprocess.run(["powershell", "-NoProfile", "-Command", script], check=True)
    say(f"Scheduled: {TASK} every day at {time_of_day}.")


def schedule_status():
    """{registered, next_run, last_run, last_result} of the daily task, read from Windows."""
    script = (f"$t = Get-ScheduledTask -TaskName '{TASK}' -ErrorAction SilentlyContinue; "
              "if ($t) { $i = $t | Get-ScheduledTaskInfo; @{ state = [string]$t.State; "
              "next_run = $(if ($i.NextRunTime) { $i.NextRunTime.ToString('ddd MMM d, h:mm tt') }); "
              "last_run = $(if ($i.LastRunTime -and $i.LastRunTime.Year -gt 2000) { $i.LastRunTime.ToString('ddd MMM d, h:mm tt') }); "
              "last_result = $i.LastTaskResult } | ConvertTo-Json }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True, timeout=20)
        info = json.loads(out.stdout) if out.stdout.strip() else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return {"registered": None}
    return {"registered": bool(info), **(info or {})}


def unschedule(say=print):
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    f"Unregister-ScheduledTask -TaskName '{TASK}' -Confirm:$false"], check=True)
    say(f"Removed {TASK}.")
