import os
from dotenv import load_dotenv
from anthropic import Anthropic
from sentence_transformers import SentenceTransformer
from database import save_job as db_save_job 
from matcher import match_score
from skills import SKILLS
from sponsors import sponsorship_signal
import numpy as np

load_dotenv()
client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
model_embed = SentenceTransformer("all-MiniLM-L6-v2")

def skill_match(resume_text, job_description):
    resume_lower = resume_text.lower()
    job_lower = job_description.lower()

    # Which skills does the JOB mention? (its requirements)
    job_skills = [skill for skill in SKILLS if skill in job_lower]

    if not job_skills:
        return 0.0, [], []   # job mentioned none of our known skills

    # Of those, which does the RESUME also have?
    matched = [skill for skill in job_skills if skill in resume_lower]
    missing = [skill for skill in job_skills if skill not in resume_lower]

    score = len(matched) / len(job_skills)
    return score, matched, missing

def draft_tailored_bullets(resume_text, job_description):
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        system=[
            {
                "type": "text",
                "text": "You are helping tailor a resume to a job. Suggest 3 ways the candidate could tailor their resume bullets to better match the job. Base every suggestion only on real experience already in the resume — do not invent anything. Be specific and concise.",
            },
            {
                "type": "text",
                "text": f"RESUME:\n{resume_text}",
                "cache_control": {"type": "ephemeral"},
            },
        ],
        messages=[
            {"role": "user", "content": f"JOB POSTING:\n{job_description}"}
        ],
    )
    return response.content[0].text

def score_job(resume_text, job_description):
    resume_vec = model_embed.encode(resume_text)
    job_vec = model_embed.encode(job_description)
    score = np.dot(resume_vec, job_vec) / (np.linalg.norm(resume_vec) * np.linalg.norm(job_vec))
    return float(score)

tools = [
    {
        "name": "score_job",
        "description": "Compute how well the resume matches a job posting. Returns a similarity score between 0 and 1.",
        "input_schema": {
            "type": "object",
            "properties": {
                "job_description": {"type": "string", "description": "The full text of the job posting"}
            },
            "required": ["job_description"]
        }
    },
    {
        "name": "draft_tailored_bullets",
        "description": "Suggest 3 ways to tailor the resume to a specific job. Only use real experience from the resume.",
        "input_schema": {
            "type": "object",
            "properties": {
                "job_description": {"type": "string", "description": "The full text of the job posting"}
            },
            "required": ["job_description"]
        }
    },
    {
        "name": "save_job",
        "description": "Save a scored job to the database for later ranking. Requires the job's company, title, url, and description, plus its match score.",
        "input_schema": {
            "type": "object",
            "properties": {
                "company": {"type": "string", "description": "Company name"},
                "title": {"type": "string", "description": "Job title"},
                "url": {"type": "string", "description": "Job posting URL"},
                "job_description": {"type": "string", "description": "Full job posting text"},
                "match_score": {"type": "number", "description": "The match score from score_job, between 0 and 1"}
            },
            "required": ["company", "title", "url", "job_description", "match_score"]
        }
    }
]   


def run_agent(user_request, resume_text):
    messages = [{"role": "user", "content": user_request}]

    for _ in range(5):   # turn cap: never loop more than 5 times
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            system="You are a job-application assistant. When you need a tool, call it directly with the required inputs. When reporting a match, use ONLY the matched and missing skills the score tool returns — do not guess or invent gaps. State the score, the skills the candidate has, and the specific skills they're missing.",
            tools=tools,
            messages=messages
        )

        if response.stop_reason != "tool_use":
            for block in response.content:
                if block.type == "text":
                    print("\nAgent:", block.text)
            break

        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                print(f"\nThe agent wants to call: {block.name}")
                approval = input("Approve? (yes/no): ").strip().lower()

                if approval != "yes":
                    result = "The user denied permission to run this tool."
                    print("Denied.")
                else:
                    if block.name == "score_job":
                        combined, matched, missing = hybrid_score(resume_text, block.input["job_description"])
                        result = (
                            f"Match score: {round(combined, 3)}\n"
                            f"Skills the resume HAS that the job wants: {matched}\n"
                            f"Skills the job wants that the resume is MISSING: {missing}"
                        )
                    elif block.name == "draft_tailored_bullets":
                        result = draft_tailored_bullets(resume_text, block.input["job_description"])
                    elif block.name == "save_job":
                        db_save_job(
                            block.input["company"],
                            block.input["title"],
                            block.input["url"],
                            block.input["job_description"],
                            block.input["match_score"]
                        )
                        result = "Job saved to database."
                    print("Done.")

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result
                })

        messages.append({"role": "user", "content": tool_results})
        
def hybrid_score(resume_text, job_description, weight=0.6):
    semantic = score_job(resume_text, job_description)
    skill, matched, missing = skill_match(resume_text, job_description)
    combined = (weight * semantic) + ((1 - weight) * skill)
    return combined, matched, missing

def final_score(resume_text, job_description, company):
    match, matched, missing = hybrid_score(resume_text, job_description)
    sponsors = sponsorship_signal(company)

    # Non-sponsors aren't hidden, just ranked lower: keep 70% of their score.
    sponsor_multiplier = 1.0 if sponsors == 1.0 else 0.7
    final = match * sponsor_multiplier

    return final, match, sponsors, matched, missing

if __name__ == "__main__":
    from resume_reader import read_resume
    resume = read_resume("resume.pdf")
    with open("job.txt", "r", encoding="utf-8") as f:
        job = f.read()

    run_agent(
        "Score this job against my resume, and if it scores above 0.35, save it. "
        "Company: Columbia University, title: Associate AI Engineer, url: https://opportunities.columbia.edu/jobs/associate-ai-engineer . Job: " + job,
        resume
    )

    # Test sponsorship-weighted final score
    f, m, s, matched, missing = final_score(resume, job, "Columbia University")
    print(f"\nColumbia -> Match: {round(m,3)}, Sponsor: {s}, Final: {round(f,3)}")
    f, m, s, matched, missing = final_score(resume, job, "eClerx")
    print(f"eClerx   -> Match: {round(m,3)}, Sponsor: {s}, Final: {round(f,3)}")