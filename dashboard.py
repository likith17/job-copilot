import streamlit as st
import tempfile
from resume_reader import read_resume
from agent import hybrid_score, final_score

st.title("Job Copilot")
st.write("Score jobs against your resume.")

uploaded = st.file_uploader("Upload your resume", type=["pdf", "docx"])
company = st.text_input("Company name")
job_text = st.text_area("Paste the job description here", height=300)

if st.button("Score this job"):
    if uploaded and job_text and company:
        # Save the uploaded file to a temp path so read_resume can open it
        suffix = ".pdf" if uploaded.name.endswith(".pdf") else ".docx"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(uploaded.getvalue())
            tmp_path = tmp.name

        resume_text = read_resume(tmp_path)
        final, match, sponsor, matched, missing = final_score(resume_text, job_text, company)

        st.metric("Final Score (sponsorship-weighted)", round(final, 3))
        st.metric("Match Score", round(match, 3))
        st.write("**Sponsorship:**", "Known sponsor ✅" if sponsor == 1.0 else "No sponsorship history on file ⚠️")

        st.write("**Skills you have that they want:**")
        st.write(", ".join(matched) if matched else "None matched")

        st.write("**Skills the job wants that you're missing:**")
        st.write(", ".join(missing) if missing else "None missing")
    else:
        st.warning("Please upload a resume, enter a company, and paste a job description.")