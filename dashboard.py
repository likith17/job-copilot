import os
import tempfile
import streamlit as st

from resume_reader import read_resume
from agent import final_score
from database import save_job as db_save_job

st.set_page_config(page_title="Job Copilot", page_icon="🎯", layout="wide")

BASIS_STYLE = {
    "posting says sponsors":          ("✅", "success"),
    "employer has sponsored before":  ("✅", "success"),
    "unknown":                        ("❓", "info"),
    "posting rules it out":           ("🚫", "error"),
}


def load_resume(uploaded):
    """Parse the resume once per uploaded file, not once per rerun."""
    key = f"{uploaded.name}:{uploaded.size}"
    if st.session_state.get("resume_key") == key:
        return st.session_state["resume_text"]

    suffix = os.path.splitext(uploaded.name)[1].lower()
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(uploaded.getvalue())
            tmp_path = tmp.name
        text = read_resume(tmp_path)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)

    st.session_state["resume_key"] = key
    st.session_state["resume_text"] = text
    return text


# ---------------- sidebar: inputs ----------------

with st.sidebar:
    st.header("Your profile")
    uploaded = st.file_uploader("Resume", type=["pdf", "docx"])
    if uploaded:
        st.caption(f"Loaded: {uploaded.name}")

    st.divider()
    st.header("The job")
    company = st.text_input("Company")
    title = st.text_input("Job title")
    url = st.text_input("Posting URL")


# ---------------- main: description + action ----------------

st.title("Job Copilot")
st.caption("Scores a posting against your resume, and tells you why.")

job_text = st.text_area("Job description", height=280,
                        placeholder="Paste the full posting here…")

if st.button("Score this job", type="primary", use_container_width=True):
    if not (uploaded and job_text and company):
        st.warning("Resume, company, and job description are all required.")
    else:
        with st.spinner("Reading resume, scoring, checking sponsorship…"):
            resume_text = load_resume(uploaded)
            st.session_state["result"] = final_score(resume_text, job_text, company)
            st.session_state["saved"] = False


# ---------------- results ----------------

result = st.session_state.get("result")

if result:
    st.divider()

    penalty = result["final"] - result["match"]
    c1, c2, c3 = st.columns(3)
    c1.metric("Final score", round(result["final"], 3),
              delta=round(penalty, 3) if penalty else None)
    c2.metric("Match score", round(result["match"], 3))
    c3.metric("Sponsorship weight", f"{result['multiplier']}x")

    st.progress(min(max(result["final"], 0.0), 1.0))

    icon, level = BASIS_STYLE.get(result["basis"], ("❓", "info"))
    getattr(st, level)(f"{icon}  Sponsorship: {result['basis']}")

    jd = result["jd"]
    if jd["evidence"]:
        with st.expander("Evidence from the posting"):
            st.markdown(f"> {jd['evidence']}")
            if not jd["verified"]:
                st.warning("This quote could not be found in the posting — treat it with suspicion.")
            if jd.get("reasoning"):
                st.caption(jd["reasoning"])

    left, right = st.columns(2)
    with left:
        st.subheader("You have")
        st.markdown(" ".join(f"`{s}`" for s in result["matched"]) or "_Nothing matched_")
    with right:
        st.subheader("You're missing")
        st.markdown(" ".join(f"`{s}`" for s in result["missing"]) or "_Nothing missing_")

    st.divider()
    if st.session_state.get("saved"):
        st.success("Saved to your database.")
    elif st.button("Save this job"):
        if not url:
            st.error("A posting URL is required — it's the deduplication key.")
        else:
            try:
                db_save_job(company, title or "Unknown", url, job_text,
                            result["match"], result["final"])
                st.session_state["saved"] = True
                st.rerun()
            except Exception as exc:
                st.error(f"Could not save: {exc}")