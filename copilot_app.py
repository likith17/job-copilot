"""Job Copilot: the daily-run interface.

    streamlit run copilot_app.py

1. Upload your resume and cover letter (the latest upload is the default).
2. Run: crawl every board, rank the jobs, and write a tailored resume and
   cover letter for the top N automatically.
3. Generate documents for any other job with one click; track status here or
   in the Excel sheet (edits in either place are kept).
"""
import os
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from copilot import db, export, inputs, latex, llm, pipeline, runlock
from copilot.latex import untoken
from copilot.config import load_config
from copilot.tailor import file_sha

st.set_page_config(page_title="Job Copilot", page_icon="🎯", layout="wide")
cfg = load_config()


@st.cache_resource
def _warm_up():
    """Load the embedding model once per app process, in the background, so the
    first Run or Generate doesn't wait ~40s for it."""
    import threading
    from copilot import scoring
    threading.Thread(target=scoring._agent, daemon=True).start()
    return True


@st.cache_resource
def _autofill_api():
    """The local API the autofill extension talks to; lives as long as the app.
    None if it's already running (e.g. `python -m copilot serve`)."""
    from copilot import applyserver
    return applyserver.serve(inputs.apply(load_config()), background=True)


_warm_up()
_autofill_api()

H1B = export.H1B_LABEL
CAN_OPEN = sys.platform == "win32"


def open_local(path):
    """Open a file with its default app (Word, Excel). The app runs locally."""
    if CAN_OPEN and path and os.path.exists(path):
        os.startfile(path)  # noqa: S606 - local desktop tool opening its own outputs


LOG_PATH = cfg.data_dir / "logs" / "background.log"


APPLY_LOG = cfg.data_dir / "logs" / "apply.log"


def start_background(args, log_path=LOG_PATH, marker="bg_started"):
    """Runs, document generation and applying live in their own process: clicking
    around or refreshing this page can't stop them. Progress is read back from the log."""
    log_path.parent.mkdir(exist_ok=True)
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
    command = [sys.executable, "-m", "copilot", *args, "--source", "app"]
    with open(log_path, "w", encoding="utf-8") as log:
        kwargs = dict(cwd=str(Path(__file__).parent), stdout=log, stderr=subprocess.STDOUT, env=env)
        if sys.platform != "win32":
            subprocess.Popen(command, start_new_session=True, **kwargs)
        else:
            # Its own process, outside the app's: closing or restarting the app (or whatever started
            # it) no longer closes the browser and the applications in it.
            detached = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            try:
                subprocess.Popen(command, creationflags=detached | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kwargs)
            except OSError:      # the app runs inside a job that forbids breaking away
                subprocess.Popen(command, creationflags=detached, **kwargs)
    st.session_state[marker] = time.time()


def autofill_jobs(ids):
    """Open these jobs' applications in Chrome, filled. You review and submit."""
    if runlock.current(cfg.data_dir, "apply") is not None or "apply_started" in st.session_state:
        st.session_state["flash"] = ("warning", "Applications are already open in a browser window. "
                                                "Finish there and close it, then try again.")
        return
    start_background(["apply"] + [arg for j in ids for arg in ("--job", j)], APPLY_LOG, "apply_started")
    st.session_state["flash"] = ("info", f"Opening {len(ids)} application(s) in Chrome. Progress is in the sidebar.")


@st.cache_data(ttl=60, show_spinner=False)
def _schedule_status():
    from copilot import auto
    return auto.schedule_status()


@st.fragment(run_every=3)
def _apply_progress():
    started = st.session_state.get("apply_started")
    running = runlock.current(cfg.data_dir, "apply") is not None or (started and time.time() - started < 15)
    lines = APPLY_LOG.read_text(encoding="utf-8", errors="replace").splitlines() if APPLY_LOG.exists() else []
    if not running:
        if started:
            del st.session_state["apply_started"]
            st.session_state["flash"] = ("info", "Applying finished. " + (lines[-1] if lines else ""))
        st.rerun()      # the whole page: the buttons unlock
    st.info("⏳ Browser is open. Review and submit there; each job is marked Applied when its page confirms. "
            "Close the browser when you're done.")
    for line in lines[-8:]:
        st.caption(line)


def generate(ids, status, instruction=""):
    """Write documents in the background; progress shows at the top of the page."""
    start_background(["generate", *ids] + (["--instruction", instruction] if instruction.strip() else []))
    return {"background": True}


@st.fragment(run_every=3)
def _progress():
    started = st.session_state.get("bg_started")
    running = runlock.current(cfg.data_dir) is not None or (started and time.time() - started < 15)
    if not running:
        if started:
            del st.session_state["bg_started"]
            st.session_state["flash"] = ("success", "Finished. Results are below; the log is in data/logs/.")
        st.rerun()      # the whole page: new results, and the buttons unlock
    lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines() if LOG_PATH.exists() else []
    st.info("⏳ Working in the background. You can keep using the page, or close it; the work continues.")
    st.code("\n".join(lines[-12:]) or "Starting...", language=None)


def flash_result(result, asked):
    """Keep a generate outcome visible across the st.rerun() that follows it."""
    if result.get("background"):
        st.session_state["flash"] = ("info", f"Writing documents for {asked} job(s) in the background.")
    elif result.get("busy"):
        st.session_state["flash"] = ("warning", result["busy"])
    elif result.get("llm_unavailable"):
        st.session_state["flash"] = ("error", f"**AI provider unavailable:** {result['llm_unavailable']}")
    elif result["errors"]:
        st.session_state["flash"] = ("warning", f"{result['tailored']} of {asked} written; "
                                                f"{result['errors']} failed (see the job's Error in Excel).")
    else:
        st.session_state["flash"] = ("success", f"{result['tailored']} application pack(s) written.")


def _flags(job_id):
    """Review flags still on a job, read from the DB (callbacks run before the page rebuilds)."""
    c = db.connect(cfg.db_path)
    row = c.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    c.close()
    return [f for f in db.decode(row)["tailor_flags"] if not f.startswith("removed skills")] if row else []


def file_label(path):
    if not path:
        return "none yet"
    name = path.name
    # Uploaded files are stored as YYYY-MM-DD_HHMMSS_<original name>
    return f"{name[18:]}  (uploaded {name[:10]})" if path.parent.name in inputs.KINDS else name


# ---------------- 1. documents ----------------

st.title("🎯 Job Copilot")
st.caption("Find jobs, tailor your documents, and apply.")
if "flash" in st.session_state:
    level, text = st.session_state.pop("flash")
    getattr(st, level)(text)

resume_ok = inputs.current(cfg, "resume") is not None
# Only one run at a time across the terminal and the app (copilot/runlock.py).
busy = runlock.current(cfg.data_dir)
can_write = resume_ok and busy is None
if busy is not None or "bg_started" in st.session_state:
    if busy is not None:
        st.caption(f"Run in progress ({busy.get('source', 'another process')}, started {busy.get('started', '?')}). "
                   "Run and Generate are paused until it finishes.")
    _progress()
letter_ok = inputs.current(cfg, "cover_letter") is not None
if resume_ok and not letter_ok:
    st.info("No cover letter uploaded yet. Letters will still be written, but uploading one "
            "lets them match your own voice.")

tab_jobs, tab_setup, tab_info = st.tabs(["🎯  Jobs", "⚙️  Setup & run", "📋  Autofill info"])

with tab_setup:
    st.subheader("Your documents")
    left, right = st.columns(2)
    for col, kind, title in ((left, "resume", "Resume"), (right, "cover_letter", "Cover letter")):
        with col:
            up = st.file_uploader(f"{title} (PDF, DOCX, or LaTeX .tex)", type=["pdf", "docx", "tex"], key=f"up_{kind}",
                                  help="Upload your Overleaf .tex to keep your exact layout and links.")
            seen = f"saved_{kind}"
            if up is not None and st.session_state.get(seen) != (up.name, up.size):
                try:
                    path = inputs.save_upload(cfg, kind, up.name, up.getvalue(),
                                              force=st.session_state.get(f"force_{kind}", False))
                    st.session_state[seen] = (up.name, up.size)
                    st.toast(f"{title} saved and set as default.")
                    if kind == "resume":
                        st.session_state["flash"] = ("info", "New resume saved. The next run re-scores every "
                                                     "stored job against it; earlier documents are marked as old.")
                except inputs.LooksGenerated as exc:
                    st.warning(str(exc))
                    st.checkbox("This really is my master resume", key=f"force_{kind}")
                except ValueError as exc:
                    st.error(str(exc))
            current = inputs.current(cfg, kind)
            st.markdown(f"**Using:** {file_label(current)}")
            older = inputs.history(cfg, kind)
            if len(older) > 1:
                with st.expander("Use an earlier version"):
                    pick = st.selectbox("Version", older, format_func=file_label, key=f"pick_{kind}",
                                        index=older.index(current) if current in older else 0)
                    if pick != current and st.button("Make default", key=f"default_{kind}"):
                        inputs.set_current(cfg, kind, pick)
                        st.rerun()



    # ---------------- 2. run ----------------

    st.subheader("Run")
    c1, c2, c3 = st.columns([1, 1, 2])
    fetch_new = c1.checkbox("Fetch new jobs", value=True, help="Off: re-process jobs already in the database.")
    top_n = c2.number_input("Auto-write documents for top", min_value=0, max_value=50,
                            value=int(cfg.llm.get("max_tailor_per_run", 10)))
    c3.caption("Free: everything runs on open models. A new resume re-grades up to 300 jobs (~10 min); "
               "daily runs only process new postings." + (" **Set above 0 to write documents.**" if not top_n else ""))

    if st.button("▶  Run pipeline", type="primary", disabled=not can_write, width="stretch"):
        start_background(["run", "--limit", str(int(top_n))] + ([] if fetch_new else ["--no-fetch"]))
        st.rerun()


    # ---------------- manual job ----------------

    with st.expander("➕  Add a job manually (LinkedIn, a referral, any careers page)"):
        with st.form("manual_job", clear_on_submit=False):
            c1, c2 = st.columns(2)
            m_company = c1.text_input("Company *")
            m_title = c2.text_input("Job title *")
            c3, c4 = st.columns([3, 1])
            m_url = c3.text_input("Link to the posting or application")
            m_location = c4.text_input("Location")
            m_desc = st.text_area("Job description *", height=220,
                                  placeholder="Paste the full posting: responsibilities, requirements, about the company...")
            b1, b2 = st.columns(2)
            score_only = b1.form_submit_button("Score it", width="stretch", disabled=not can_write)
            score_write = b2.form_submit_button("Score + write resume & cover letter", type="primary",
                                                width="stretch", disabled=not can_write)
        if score_only or score_write:
            if not (m_company.strip() and m_title.strip()) or len(m_desc.strip()) < 100:
                st.error("Company, title, and the full job description are required.")
            else:
                with st.status("Scoring" + (" and writing documents" if score_write else "") + "...",
                               expanded=True) as status:
                    try:
                        new_id = pipeline.add_manual_job(cfg, company=m_company, title=m_title, description=m_desc,
                                                         url=m_url, location=m_location, generate=score_write,
                                                         say=status.write, source="app")
                        st.session_state["open_job"] = new_id
                        st.session_state["scroll_to_job"] = True
                        st.session_state["flash"] = ("success", f"Added {m_company} | {m_title}. It's open below.")
                    except runlock.RunInProgress as exc:
                        st.session_state["flash"] = ("warning", str(exc))
                st.rerun()


# ---------------- autofill info ----------------

def _autofill_info():
    from copilot.config import ROOT
    profile_path = ROOT / "profile.yaml"
    conn_a = db.connect(cfg.db_path)
    inbox = db.open_questions(conn_a)
    with st.container():
        t_inbox, t_bank, t_story, t_profile, t_ext = st.tabs(
            [f"Questions forms asked ({len(inbox)})", "Saved answers", "Your story", "Profile", "Browser extension"])

        with t_inbox:
            st.caption("Questions application forms asked that nothing could answer yet. Answer once and every later "
                       "form with the same question fills itself. Leave an answer blank and tick **Skip** to stop "
                       "being asked. For choice questions, type the option you'd pick.")
            if not inbox:
                st.info("Nothing waiting. New questions appear here as the extension or `copilot apply` meets them.")
            else:
                frame = pd.DataFrame([{"Question": q["question"], "Required": bool(q["required"]), "Seen": q["seen"],
                                       "Options": " / ".join(q["options"])[:200], "Last seen at": q["company"],
                                       "Your answer": "", "Skip": False} for q in inbox])
                edited = st.data_editor(frame, hide_index=True, width="stretch", key="inbox_editor",
                                        disabled=["Question", "Required", "Seen", "Options", "Last seen at"],
                                        column_config={"Your answer": st.column_config.TextColumn(width="large"),
                                                       "Question": st.column_config.TextColumn(width="large")})
                if st.button("Save answers", type="primary", key="save_inbox"):
                    n = 0
                    for q, (_, row) in zip(inbox, edited.iterrows()):
                        if str(row["Your answer"]).strip() or row["Skip"]:
                            db.save_answer(conn_a, q["question"], str(row["Your answer"]).strip(), "you")
                            n += 1
                    st.session_state["flash"] = ("success", f"Saved {n} answer(s). Forms will use them from now on.")
                    st.rerun()

        with t_bank:
            st.caption("Every answer the autofill reuses: ones you gave here, and ones you submitted on a form. "
                       "Edit an answer, or delete a row to be asked again. A blank answer means: skip this question.")
            bank = db.all_answers(conn_a)
            if not bank:
                st.info("No saved answers yet.")
            else:
                frame = pd.DataFrame([{"id": a["id"], "Question": a["question"], "Answer": a["answer"],
                                       "Used": a["uses"], "From": a["source"]} for a in bank])
                edited = st.data_editor(frame, hide_index=True, width="stretch", num_rows="dynamic", key="bank_editor",
                                        disabled=["id", "Used", "From"], column_order=["Question", "Answer", "Used", "From"],
                                        column_config={"Answer": st.column_config.TextColumn(width="large"),
                                                       "Question": st.column_config.TextColumn(width="large")})
                if st.button("Save changes", key="save_bank"):
                    kept = set()
                    for _, row in edited.iterrows():
                        if not str(row["Question"] or "").strip():
                            continue
                        if pd.notna(row["id"]):
                            kept.add(int(row["id"]))
                            old = next(a for a in bank if a["id"] == int(row["id"]))
                            if old["question"] != row["Question"]:
                                db.delete_answer(conn_a, old["id"])
                        db.save_answer(conn_a, str(row["Question"]).strip(), str(row["Answer"] or "").strip(), "you")
                    for a in bank:
                        if a["id"] not in kept:
                            db.delete_answer(conn_a, a["id"])
                    st.session_state["flash"] = ("success", "Saved answers updated.")
                    st.rerun()

        with t_profile:
            st.caption("Your `profile.yaml`: contact, work authorization, referral, certifications, EEO, and "
                       "`custom_answers` (\"words in the question\": answer) for anything else. Blank values are "
                       "never guessed. Only on this computer; never committed.")
            if not profile_path.exists():
                (ROOT / "profile.example.yaml").exists() and profile_path.write_text(
                    (ROOT / "profile.example.yaml").read_text(encoding="utf-8"), encoding="utf-8")
            text = st.text_area("profile.yaml", profile_path.read_text(encoding="utf-8") if profile_path.exists() else "",
                                height=480, label_visibility="collapsed", key="profile_text")
            if st.button("Save profile", type="primary", key="save_profile"):
                import yaml
                try:
                    parsed = yaml.safe_load(text)
                    if not isinstance(parsed, dict):
                        raise ValueError("the file must be a list of `key: value` lines")
                except (yaml.YAMLError, ValueError) as exc:
                    st.error(f"Not saved: {exc}. Check the indentation near the line it mentions.")
                else:
                    profile_path.write_text(text, encoding="utf-8")
                    st.session_state["flash"] = ("success", "Profile saved. The extension uses it on the next form.")
                    st.rerun()

        with t_story:
            from copilot import apply
            st.caption("Why you do this work, what you want to build next, the problems and kinds of companies that "
                       "draw you, and why. One long paragraph is fine. It's used, with your cover letter and resume, "
                       "to answer questions like **\"Why this company?\"**: each answer connects what the posting "
                       "says about the company to your own words, and is fact-checked before it can go out. "
                       "Stays on this computer.")
            story = st.text_area("Your story", apply.read_story(cfg), height=320, key="story_text",
                                 label_visibility="collapsed",
                                 placeholder="I got into machine learning because... What I want to build next is... "
                                             "I'm drawn to teams that... In five years I want to...")
            if st.button("Save story", type="primary", key="save_story"):
                apply.story_path(cfg).parent.mkdir(parents=True, exist_ok=True)
                apply.story_path(cfg).write_text(story.strip() + "\n", encoding="utf-8")
                st.session_state["flash"] = ("success", "Story saved. New answers will use it.")
                st.rerun()

        with t_ext:
            from copilot import applyserver
            st.markdown("Fills applications in **your own Chrome** as you browse (the Autofill buttons on the Jobs "
                        "tab use a separate Chrome window instead).\n\n"
                        "1. Chrome → `chrome://extensions` → turn on **Developer mode** → **Load unpacked** → pick "
                        "the `extension` folder of this project. It's listed as **Job Copilot Autofill**.\n"
                        "2. Open its options and paste this token:")
            st.code(applyserver.get_token(cfg), language=None)
            st.caption("Open any Greenhouse, Lever or Ashby application: it fills itself and attaches that job's "
                       "documents. Keep only one autofill extension on: two would fill the same form at once.")
    conn_a.close()


with tab_info:
    _autofill_info()


with tab_jobs:
    # ---------------- 3. jobs ----------------

    conn = db.connect(cfg.db_path)
    jobs = db.jobs_for_export(conn)
    if not jobs:
        st.info("No jobs yet. Run the pipeline in the Setup & run tab.")
        st.stop()

    df = pd.DataFrame(jobs)
    df["H1B"] = df["sponsorship_basis"].map(lambda b: H1B.get(b, "Not checked"))
    current_resume = inputs.current(cfg, "resume")
    current_sha = file_sha(current_resume) if current_resume else None


    def docs_state(row):
        p = row["resume_path"]
        if not (isinstance(p, str) and os.path.exists(p)):
            return ""
        # Packs from before resume tracking have no sha; treat them as current.
        return "⚠️ old" if row["docs_resume_sha"] and row["docs_resume_sha"] != current_sha else "✅"


    df["Docs"] = df.apply(docs_state, axis=1)

    m = st.columns(6)
    m[0].metric("Ready to apply", int(((df["Docs"] == "✅") & (df["status"] == "Ready")).sum()),
                help="Documents written and nothing flagged (or you reviewed them).")
    m[1].metric("Needs review", int((df["status"] == "Review").sum()),
                help="Documents written, but some phrases were flagged. Open the job's Fit & changes tab.")
    m[2].metric("Shortlisted", int((df["fit_score"].fillna(0) >= llm.min_fit(cfg)).sum()))
    m[3].metric("Applied", int((df["status"] == "Applied").sum()))
    m[4].metric("Interviewing", int((df["status"] == "Interviewing").sum()))
    m[5].metric("Scored", len(df))

    f1, f2, f3 = st.columns([2, 2, 1])
    view = f1.radio("Show", ["Have documents", "Shortlisted", "All graded", "Everything scored", "In progress"],
                    horizontal=True)
    search = f2.text_input("Search company or title", placeholder="e.g. RL, Anthropic, inference")
    hide_closed = f3.checkbox("Hide closed", value=True)

    min_fit = llm.min_fit(cfg)
    mask = {
        "Have documents": df["Docs"] != "",
        "Shortlisted": df["fit_score"].fillna(0) >= min_fit,
        "All graded": df["fit_score"].notna(),
        "Everything scored": pd.Series(True, index=df.index),
        "In progress": df["status"].isin(["Applied", "Interviewing", "Offer"]),
    }[view]
    if search:
        s = search.lower()
        mask &= df["company"].str.lower().str.contains(s, regex=False) | df["title"].str.lower().str.contains(s, regex=False)
    if hide_closed:
        mask &= df["is_active"] == 1
    view_df = df[mask]

    APPLIED_STATES = ("Applied", "Interviewing", "Offer", "Rejected")

    now = pd.Timestamp.now(tz="UTC")
    posted = pd.to_datetime(view_df["posted_at"], utc=True, errors="coerce", format="mixed")
    added = pd.to_datetime(view_df["first_seen"], utc=True, errors="coerce", format="mixed")

    table = pd.DataFrame({
        "Open": False,
        "Autofill": False,
        "Applied": view_df["status"].isin(APPLIED_STATES),
        "Status": view_df["status"],
        "Fit": view_df["fit_score"],
        "Tailored": view_df["tailored_fit"],
        "Company": view_df["company"],
        "Title": view_df["title"],
        "Posted": posted.dt.tz_localize(None),
        # Days since the company posted it (or since we first saw it, if the board gives no date).
        "Age (days)": ((now - posted.fillna(added)).dt.total_seconds() // 86400).astype("Int64"),
        "Added": added.dt.tz_localize(None),
        "Location": view_df["location"],
        "H1B": view_df["H1B"],
        "Docs": view_df["Docs"],
        "Apply": view_df["apply_url"],
        "Why": view_df["fit_reason"],
        "id": view_df["id"],
    })
    sort = st.radio("Sort by", ["Best fit", "Newest posted", "Recently added"], horizontal=True,
                    key="sort_jobs", help="Apply early: freshly posted roles get far fewer applicants.")
    if sort == "Newest posted":
        table = table.sort_values("Age (days)", na_position="last")
    elif sort == "Recently added":
        table = table.sort_values("Added", ascending=False, na_position="last")
    table = table.reset_index(drop=True)

    st.caption("**Open** shows a job below · **Autofill** opens its application in Chrome, filled, for you to "
               "review and submit · **Applied** once you've applied. Ticked Open boxes feed the buttons underneath.")
    table_key = f"table_{view}_{search}_{hide_closed}_{sort}"
    edited = st.data_editor(
        table,
        key=table_key,
        hide_index=True,
        width="stretch",
        height=min(36 * (len(table) + 1), 520),
        disabled=[c for c in table.columns if c not in ("Open", "Autofill", "Applied")],
        column_order=[c for c in table.columns if c != "id"],
        column_config={
            "Open": st.column_config.CheckboxColumn("Open", width="small", help="Show this job's details below"),
            "Autofill": st.column_config.CheckboxColumn("Autofill", width="small",
                                                        help="Open this application in Chrome, filled; you submit"),
            "Applied": st.column_config.CheckboxColumn("Applied", width="small"),
            "Status": st.column_config.TextColumn("Status", width="small"),
            "Fit": st.column_config.NumberColumn("Fit", help="AI fit grade 0-100 for your resume", width="small"),
            "Tailored": st.column_config.NumberColumn("Tailored", help="Fit grade of the tailored resume", width="small"),
            "Posted": st.column_config.DatetimeColumn("Posted", format="MMM D", help="When the company posted it"),
            "Age (days)": st.column_config.NumberColumn("Age", format="%d d", width="small",
                                                        help="Days since posting; apply early"),
            "Added": st.column_config.DatetimeColumn("Added", format="MMM D", help="When Job Copilot found it"),
            "Apply": st.column_config.LinkColumn("Apply", display_text="Apply ↗", width="small"),
            "Docs": st.column_config.TextColumn("Docs", width="small"),
            "Why": st.column_config.TextColumn("Why", width="large"),
        },
    )

    # Applied ticked or unticked in the table: save it right away (and to Excel).
    flipped = edited[edited["Applied"] != table["Applied"]]
    if len(flipped):
        for _, row in flipped.iterrows():
            current = df.loc[df["id"] == row["id"]].iloc[0]
            back = "Review" if _flags(row["id"]) and current["Docs"] else ("Ready" if current["Docs"] else "New")
            db.set_status(conn, row["id"], "Applied" if row["Applied"] else back)
        conn.commit()
        export.write_workbook(conn, cfg.excel_path)
        st.rerun()

    to_fill = edited.loc[edited["Autofill"], "id"].tolist()
    if to_fill:
        del st.session_state[table_key]      # untick, or every rerun would open them again
        autofill_jobs(to_fill)
        st.rerun()

    selected = edited.loc[edited["Open"], "id"].tolist()
    # The most recently ticked job becomes the open one, and the page scrolls to it.
    previous = st.session_state.get("prev_selection", [])
    newly = [j for j in selected if j not in previous]
    st.session_state["prev_selection"] = selected
    if newly:
        st.session_state["open_job"] = newly[-1]
        st.session_state["scroll_to_job"] = True
    row_ids = table["id"].tolist()

    b1, b2, b4, b3 = st.columns([2, 2, 2, 2])
    if b4.button(f"🧩 Autofill ({len(selected)})", disabled=not selected, width="stretch",
                 help="Open these applications in Chrome, filled; you review and submit"):
        autofill_jobs(selected)
        st.rerun()
    if b1.button(f"✍️ Generate resume + cover letter ({len(selected)})",
                 disabled=not selected or not can_write, width="stretch"):
        with st.status(f"Writing {len(selected)} application pack(s)...", expanded=True) as status:
            result = generate(selected, status)
        flash_result(result, len(selected))
        st.rerun()
    if b2.button(f"✅ Mark applied ({len(selected)})", disabled=not selected, width="stretch"):
        for jid in selected:
            db.set_status(conn, jid, "Applied")
        conn.commit()
        export.write_workbook(conn, cfg.excel_path)
        st.session_state["flash"] = ("success", f"Marked {len(selected)} job(s) as applied.")
        st.rerun()
    b3.caption("Regenerating uses your current resume and cover letter.")


    # ---------------- job detail ----------------

    def resume_markdown(r):
        """The tailored resume as readable markdown, for the in-app preview."""
        c = r["contact"]
        out = [f"### {c['name']}", " | ".join(x for x in [c["location"], c["phone"], c["email"]] if x),
               "", f"**Summary.** {r['summary']}", "", "**Technical skills**"]
        out += [f"- *{g['category']}:* {', '.join(g['items'])}" for g in r["skills"]]
        out += ["", "**Experience**"]
        for role in r["experience"]:
            out.append(f"\n*{role['title']}, {role['company']}* ({role['dates']})")
            out += [f"- {b}" for b in role["bullets"]]
        out += ["", "**Projects**"]
        for p in r["projects"]:
            out.append(f"\n*{p['name']}* ({p['dates']})")
            out += [f"- {b}" for b in p["bullets"]]
        out += ["", "**Education**"]
        out += [f"- {e['degree']}, {e['school']} ({e['dates']})" for e in r["education"]]
        return "\n".join(out)


    def docx_text(path):
        from docx import Document
        return "\n\n".join(p.text for p in Document(path).paragraphs if p.text.strip())


    def save_tracking(job_id, status, notes=None):
        db_conn = db.connect(cfg.db_path)
        db.set_status(db_conn, job_id, status, notes)
        db_conn.commit()
        export.write_workbook(db_conn, cfg.excel_path)
        db_conn.close()
        # Keep the sibling widgets in step, or the next rerun writes their stale value back.
        st.session_state[f"status_{job_id}"] = status
        st.session_state[f"applied_{job_id}"] = status in APPLIED_STATES




    def on_applied(job_id, has_docs):
        checked = st.session_state[f"applied_{job_id}"]
        save_tracking(job_id, "Applied" if checked else ("Review" if has_docs and _flags(job_id) else
                                                         "Ready" if has_docs else "New"))


    def on_status(job_id):
        save_tracking(job_id, st.session_state[f"status_{job_id}"])


    def on_notes(job_id):
        save_tracking(job_id, st.session_state[f"status_{job_id}"], st.session_state[f"notes_{job_id}"])


    st.subheader("Job details", anchor="job-details")
    open_id = st.session_state.get("open_job")
    if open_id not in set(df["id"]):
        open_id = row_ids[0] if row_ids else None
    if open_id is None:
        st.info("Click a job in the table to open it here.")
        st.stop()
    job = df[df["id"] == open_id].iloc[0]
    has_docs = job["Docs"] != ""

    top = st.columns([4, 1, 1, 1])
    top[0].markdown(f"### {job['title']}\n**{job['company']}** · {job['location'] or 'location n/a'} · "
                    f"posted {(job['posted_at'] or '')[:10] or 'n/a'}")
    top[1].metric("Fit", "—" if pd.isna(job["fit_score"]) else int(job["fit_score"]),
                  delta=None if pd.isna(job["tailored_fit"]) or pd.isna(job["fit_score"])
                  else int(job["tailored_fit"] - job["fit_score"]))
    top[2].metric("Match", "—" if pd.isna(job["match_score"]) else f"{job['match_score']:.2f}")
    top[3].markdown(f"**H1B**  \n{job['H1B']}")

    s1, s2, s3 = st.columns([1, 1, 3])
    # Seed widget state from the DB each run, so edits made elsewhere (the table's
    # bulk button, Excel) show up here instead of stale widget values.
    st.session_state[f"applied_{open_id}"] = job["status"] in APPLIED_STATES
    st.session_state[f"status_{open_id}"] = job["status"]
    st.session_state.setdefault(f"notes_{open_id}", job["notes"] or "")
    s1.checkbox("Applied", key=f"applied_{open_id}", on_change=on_applied, args=(open_id, has_docs))
    s2.selectbox("Status", db.STATUSES, key=f"status_{open_id}", on_change=on_status, args=(open_id,),
                 label_visibility="collapsed")
    s3.text_input("Notes", key=f"notes_{open_id}", on_change=on_notes, args=(open_id,),
                  placeholder="Notes (referral, recruiter name, follow-up date...) then press Enter",
                  label_visibility="collapsed")

    a = st.columns(6)
    if a[5].button("🧩 Autofill", type="primary", width="stretch", disabled=not has_docs or job["Docs"] == "⚠️ old",
                    help="Open this application in Chrome, filled with your profile and these documents; you submit"):
        autofill_jobs([open_id])
        st.rerun()
    a[0].link_button("Apply ↗", job["apply_url"] or job["url"], width="stretch")
    if has_docs:
        with open(job["resume_path"], "rb") as fh:
            a[1].download_button("⬇ Resume", fh.read(), file_name=os.path.basename(job["resume_path"]),
                                 width="stretch")
        with open(job["cover_letter_path"], "rb") as fh:
            a[2].download_button("⬇ Cover letter", fh.read(),
                                 file_name=os.path.basename(job["cover_letter_path"]), width="stretch")
        if CAN_OPEN and a[3].button("Open folder", width="stretch"):
            open_local(os.path.dirname(job["resume_path"]))
        extra = []
        if isinstance(job.get("review_resume_path"), str) and os.path.exists(job["review_resume_path"]):
            extra.append(("⬇ Review copy (changes in bold)", job["review_resume_path"]))
        if isinstance(job.get("resume_tex_path"), str) and os.path.exists(job["resume_tex_path"]):
            extra.append(("⬇ .tex for Overleaf", job["resume_tex_path"]))
        for col, (label, path) in zip(st.columns(4), extra):
            with open(path, "rb") as fh:
                col.download_button(label, fh.read(), file_name=os.path.basename(path), width="stretch",
                                    key=f"dl_{label}_{open_id}")
    if a[4].button("↻ Regenerate" if has_docs else "✍️ Generate", width="stretch", disabled=not can_write):
        with st.status("Writing application pack...", expanded=True) as status:
            result = generate([open_id], status)
        flash_result(result, 1)
        st.rerun()
    if job["Docs"] == "⚠️ old":
        st.warning("These documents were written from an older resume. Regenerate to use your current one.")
    if job["status"] == "Review":
        review_flags = [f for f in job["tailor_flags"] if not f.startswith("removed skills")]
        with st.container(border=True):
            st.markdown("**Needs your review before sending.** The checker flagged phrases it couldn't match "
                        "to your resume. Edit them (cover letter tab, or Revise), or confirm they're true:")
            for f in review_flags:
                st.markdown(f"- {f}".replace("$", "\\$"))
            if st.button("✓ I've reviewed these — mark Ready", key=f"reviewed_{open_id}"):
                db.set_status(conn, open_id, "Ready", job["notes"] or "")
                conn.commit()
                export.write_workbook(conn, cfg.excel_path)
                st.session_state["scroll_to_job"] = True
                st.rerun()

    tab_desc, tab_resume, tab_letter, tab_why = st.tabs(
        ["📄 Job description", "📝 Resume", "✉️ Cover letter", "🔍 Fit & changes"])

    with tab_desc:
        st.link_button("Open original posting ↗", job["url"])
        # Escape "$": Streamlit markdown reads "$2B ... $150K" as LaTeX math.
        st.markdown((job["description"] or "").replace("$", "\\$").replace("\n", "  \n"))

    with tab_resume:
        if not has_docs:
            st.info("No tailored resume yet. Use **Generate** above.")
        else:
            rep = job["tailor_report"]
            if rep:
                cov = rep["coverage"]
                k1, k2, k3 = st.columns(3)
                k1.metric("Posting keywords covered", f"{cov['after']}/{cov['total']}",
                          delta=cov["after"] - cov["before"] or None,
                          help="Counted by code: the posting's key terms that appear in the resume.")
                k2.metric("Bullets changed", len(rep["accepted"]),
                          help="Everything else is your original wording, word for word.")
                k3.metric("Edits blocked by the rules", len(rep["rejected"]),
                          help="Edits that would drop a metric or tool, or claim something the role doesn't support.")
                if cov["missing"]:
                    st.caption("Not on your resume (left unclaimed): " + ", ".join(cov["missing"][:20]))
                view = st.radio("View", ["Changes", "Full resume"], horizontal=True, key=f"rview_{open_id}",
                                label_visibility="collapsed")
            else:
                view = "Full resume"
            if view == "Changes":
                st.caption("Review each change: your verdicts update this resume and teach the writer "
                           "your style for future jobs.")
                for a in rep["accepted"]:
                    with st.container(border=True):
                        verdict = a.get("verdict")
                        st.caption(f"{a['id']} · {a['reason']}" + (f" · ✓ {verdict}" if verdict else ""))
                        st.markdown(f":red[~~{untoken(a['before'])}~~]".replace("$", "\\$"))
                        st.markdown(f":green[{untoken(a['after'])}]".replace("$", "\\$"))
                        if not verdict:
                            k1, k2, k3 = st.columns([1, 1, 4])
                            choice = None
                            if k1.button("👍 Keep", key=f"keep_{open_id}_{a['id']}", disabled=not can_write):
                                choice = ("keep", "")
                            if k2.button("👎 Reject", key=f"rej_{open_id}_{a['id']}", disabled=not can_write,
                                         help="Put your original bullet back"):
                                choice = ("reject", "")
                            with k3.popover("✏️ My version", disabled=not can_write):
                                mine = st.text_area("Your wording", value=a["after"], key=f"mine_{open_id}_{a['id']}",
                                                    help="Keep any [[link:...]] text exactly as it is.")
                                if st.button("Save my version", key=f"save_{open_id}_{a['id']}"):
                                    choice = ("rewrite", mine)
                            if choice:
                                try:
                                    pipeline.apply_feedback(cfg, open_id, a["id"], choice[0], choice[1])
                                    st.session_state["flash"] = ("success", f"{a['id']}: {choice[0]} saved.")
                                except (ValueError, runlock.RunInProgress, latex.LaTeXError) as exc:
                                    st.session_state["flash"] = ("warning", str(exc))
                                st.session_state["scroll_to_job"] = True
                                st.rerun()
                if rep["rejected"]:
                    with st.expander(f"Blocked edits ({len(rep['rejected'])}), kept your original"):
                        for r in rep["rejected"]:
                            st.markdown(f"**{r['id']}** · {r['why']}  \n_{r['proposed']}_".replace("$", "\\$"))
            elif job["tailored_resume"]:
                st.markdown(resume_markdown(job["tailored_resume"]).replace("$", "\\$"))
            else:
                st.text(docx_text(job["resume_path"]))
            st.divider()
            st.markdown("**Revise with instructions.** Describe the change; the AI rewrites the resume "
                        "and cover letter from your master resume. Claims that aren't on your resume "
                        "still get flagged.")
            instruction = st.text_area("Instruction", key=f"instr_{open_id}", label_visibility="collapsed",
                                       placeholder="e.g. Lead with the traffic-signal RL project; shorten the "
                                                   "TCS bullets; emphasize production reliability")
            if st.button("Revise", disabled=not instruction.strip() or not can_write, key=f"revise_{open_id}"):
                with st.status("Revising...", expanded=True) as status:
                    result = generate([open_id], status, instruction=instruction)
                flash_result(result, 1)
                st.session_state["scroll_to_job"] = True
                st.rerun()

    with tab_letter:
        if not has_docs:
            st.info("No cover letter yet. Use **Generate** above.")
        else:
            text = job["cover_letter_text"] or docx_text(job["cover_letter_path"])
            edited_letter = st.text_area("Cover letter", value=text, height=420, key=f"letter_{open_id}",
                                         label_visibility="collapsed")
            if st.button("💾 Save edits", disabled=edited_letter == text, key=f"save_letter_{open_id}"):
                pipeline.save_cover_letter(cfg, open_id, edited_letter)
                st.session_state["flash"] = ("success", "Cover letter saved; the DOCX was updated.")
                st.session_state["scroll_to_job"] = True
                st.rerun()

    with tab_why:
        if job["fit_reason"]:
            st.markdown(f"**Why this fit:** {job['fit_reason']}")
        if job["dealbreakers"]:
            st.warning("**Dealbreakers:** " + "; ".join(job["dealbreakers"]))
        if job["sponsorship_evidence"]:
            st.caption(f"Sponsorship evidence: “{job['sponsorship_evidence']}”")
        l, r = st.columns(2)
        l.markdown("**You have:** " + (" ".join(f"`{s}`" for s in job["matched_skills"]) or "_none detected_"))
        r.markdown("**Missing:** " + (" ".join(f"`{s}`" for s in job["missing_skills"]) or "_nothing missing_"))
        if has_docs:
            flags = [f for f in job["tailor_flags"] if not f.startswith("removed skills")]
            if flags:
                st.error("**Check before sending:** " + "; ".join(flags))
            st.markdown("**What tailoring changed:**")
            for c in job["tailor_changes"]:
                st.markdown(f"- {c}")

    if st.session_state.pop("scroll_to_job", False):
        # Streamlit has no scroll API; a zero-height script scrolls the parent page.
        # The nonce makes every request unique: Streamlit reuses an iframe whose
        # HTML is unchanged, so an identical script would only ever run once.
        st.session_state["scroll_nonce"] = st.session_state.get("scroll_nonce", 0) + 1
        components.html(f"<!-- {st.session_state['scroll_nonce']} --><script>setTimeout(() => "
                        "window.parent.document.getElementById('job-details')"
                        "?.scrollIntoView({behavior: 'smooth', block: 'start'}), 250);</script>", height=0)


# ---------------- sidebar ----------------

with st.sidebar:
    st.header("Writer quality")
    fb = db.feedback_stats(conn)
    if fb["total"]:
        st.metric("Your keep-rate", f"{fb['keep_rate']:.0%}",
                  help="Share of reviewed AI edits you kept as written. The main quality metric.")
        st.caption(f"{fb['total']} reviewed: {fb['keep']} kept, {fb['rewrite']} rewritten, {fb['reject']} rejected. "
                   "Your kept and rewritten edits are shown to the writer as style examples.")
    else:
        st.caption("Review edits in a job's Resume tab (👍 / 👎 / ✏️). Your keep-rate appears here, "
                   "and your reviews teach the writer your style.")
    st.divider()
    st.header("Apply")
    from copilot import autoapply
    opts = autoapply.settings(cfg)
    paused = autoapply.stopped(cfg)
    confirmed = db.confirmed_submissions(conn)
    gates_help = (f"Only jobs with documents from your current resume and no resume flags, fit at least "
                  f"{opts['min_fit']}, the resume attached, every answer from your profile or saved answers, and no "
                  f"CAPTCHA, up to {opts['daily_cap']} a day. Everything else stays for you.")

    applying = runlock.current(cfg.data_dir, "apply") is not None or "apply_started" in st.session_state
    if applying:
        _apply_progress()
    how_many = st.number_input("How many jobs", min_value=1, max_value=25, value=3,
                               help="Your top jobs by score, one browser tab each.")
    # Nothing is sent automatically until one real application has been confirmed by its own page.
    send_now = st.checkbox("Also submit the ones that need nothing from me", value=False,
                           disabled=not confirmed or paused, help=gates_help)
    if st.button("Prepare applications", width="stretch", disabled=applying,
                 help="Opens your top jobs in a browser window, fully filled. Unless the box above is ticked, "
                      "you review and click Submit yourself."):
        args = ["apply", "-n", str(int(how_many))] + (["--submit"] if send_now and confirmed and not paused else [])
        start_background(args, APPLY_LOG, "apply_started")
        st.rerun()

    st.subheader("Daily")
    daily_on = st.toggle("Send automatically every day", value=opts["auto_submit"],
                         disabled=not confirmed and not opts["auto_submit"], help=gates_help)
    if daily_on != opts["auto_submit"]:
        autoapply.set_auto_submit(daily_on)
        st.rerun()
    if not confirmed and not opts["auto_submit"]:
        st.caption("🔒 Sending unlocks after your first confirmed application. Prepare some, review them, and click "
                   "Submit yourself; the switches unlock once a page confirms.")
    if st.toggle("Pause all sending", value=paused,
                 help="While on, nothing is submitted automatically, whatever the settings say.") != paused:
        autoapply.stop_file(cfg).touch() if not paused else autoapply.stop_file(cfg).unlink()
        st.rerun()
    st.caption("Daily sending: **" + ("paused" if paused else "on" if opts["auto_submit"] else "off") +
               f"** (fit at least {opts['min_fit']}, up to {opts['daily_cap']} a day).")
    schedule = _schedule_status()
    if schedule["registered"]:
        st.caption(f"Schedule: next run **{schedule.get('next_run') or 'not set'}**"
                   + (f", last run {schedule['last_run']}" if schedule.get("last_run") else ""))
        if schedule.get("last_run") and schedule.get("last_result") not in (0, 267009):      # ok / still running
            st.warning(f"The last daily run failed (code {schedule['last_result']}). See the latest daily summary.")
    elif schedule["registered"] is False:
        st.caption("Schedule: **not set up**, so nothing runs by itself. In a terminal, run:")
        st.code(".venv/Scripts/python -m copilot schedule --time 09:00", language="bash")
    else:
        st.caption("Schedule: couldn't read it from Windows.")
    digests = sorted((cfg.data_dir / "digests").glob("*.md")) if (cfg.data_dir / "digests").exists() else []
    if digests:
        with st.expander("Latest daily summary"):
            st.markdown(digests[-1].read_text(encoding="utf-8").replace("$", "\\$"))
    sent = conn.execute("SELECT COUNT(*) FROM applications WHERE outcome = 'submitted'").fetchone()[0]
    bank = conn.execute("SELECT COUNT(*) FROM answers").fetchone()[0]
    st.caption(f"{sent} submitted through Copilot · {bank} saved answers in your bank")
    st.divider()
    st.header("Tracker")
    if cfg.excel_path.exists():
        with open(cfg.excel_path, "rb") as fh:
            st.download_button("⬇ Download Excel", fh.read(), file_name="jobs.xlsx", width="stretch")
        if CAN_OPEN and st.button("Open Excel", width="stretch"):
            open_local(str(cfg.excel_path))
    st.caption("Status and Notes edited in Excel sync back on the next run or Export.")
    st.divider()
    st.header("AI models")
    for task in ("grade", "sponsorship", "tailor"):
        try:
            s = llm.task_settings(cfg, task)
            st.caption(f"**{task}**: {s['provider']} · `{s.get('model', '?')}`")
        except llm.LLMUnavailable as exc:
            st.caption(f"**{task}**: {exc}")
    st.divider()
    st.caption(f"Database: `{cfg.db_path}`")
    st.caption("Edit `config.yaml` for filters, thresholds, and models; "
               "`companies.yaml` for which boards to crawl.")

conn.close()
