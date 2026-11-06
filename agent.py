import os
import re
from functools import lru_cache
from dotenv import load_dotenv
from anthropic import Anthropic
from sentence_transformers import SentenceTransformer
from database import save_job as db_save_job 
from matcher import match_score
from skills import SKILLS
from jd_sponsorship import extract_sponsorship
from sponsors import sponsorship_signal
import numpy as np

load_dotenv()
client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
model_embed = SentenceTransformer("all-MiniLM-L6-v2")

def _mentions(text_lower, skill):
    # Whole-term match: plain `in` let "rag" hit "storage" and "aws" hit "laws".
    return re.search(rf"(?<![\w+#]){re.escape(skill)}(?![\w+#])", text_lower) is not None

def skill_match(resume_text, job_description):
    resume_lower = resume_text.lower()
    job_lower = job_description.lower()

    # Which skills does the JOB mention? (its requirements)
    job_skills = [skill for skill in SKILLS if _mentions(job_lower, skill)]

    if not job_skills:
        return 0.0, [], []   # job mentioned none of our known skills

    # Of those, which does the RESUME also have?
    matched = [skill for skill in job_skills if _mentions(resume_lower, skill)]
    missing = [skill for skill in job_skills if not _mentions(resume_lower, skill)]

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

embed_cache = None     # set by copilot.scoring: embeddings kept on disk between runs

@lru_cache(maxsize=256)
def embed(text, words_per_chunk=150):
    stored = embed_cache.get(text) if embed_cache else None
    if stored is not None:
        return stored
    # The model reads at most 256 tokens and silently drops the rest, which was
    # 16% of the resume and only the "About us" intro of most postings. Embed
    # ~150-word chunks (under the limit) and average them so the whole text counts.
    words = text.split()
    chunks = [" ".join(words[i:i + words_per_chunk])
              for i in range(0, max(len(words), 1), words_per_chunk)]
    vec = model_embed.encode(chunks, normalize_embeddings=True).mean(axis=0)
    vec = vec / np.linalg.norm(vec)
    if embed_cache:
        embed_cache.put(text, vec)
    return vec

def score_job(resume_text, job_description):
    return float(np.dot(embed(resume_text), embed(job_description)))

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
                        r = final_score(
                            resume_text,
                            block.input["job_description"],
                            block.input["company"]
                        )
                        db_save_job(
                            block.input["company"],
                            block.input["title"],
                            block.input["url"],
                            block.input["job_description"],
                            r["match"],   # raw match score
                            r["final"]    # sponsorship-weighted final score
                        )
                        result = (
                            f"Job saved. Match: {round(r['match'], 3)}, "
                            f"Final: {round(r['final'], 3)} "
                            f"(sponsorship basis: {r['basis']})"
                        )

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

def final_score(resume_text, job_description, company, jd_result=None):
    match, matched, missing = hybrid_score(resume_text, job_description)

    if jd_result is None:
        jd_result = extract_sponsorship(job_description)

    status = jd_result["status"]

    # An unverifiable rejection quote is not trustworthy evidence.
    if status == "no_sponsorship" and not jd_result["verified"]:
        status = "not_mentioned"

    on_list = sponsorship_signal(company) == 1.0

    if status == "sponsors":
        basis, multiplier = "posting says sponsors", 1.0
    elif status == "no_sponsorship":
        basis, multiplier = "posting rules it out", 0.3
    elif on_list:
        basis, multiplier = "employer has sponsored before", 1.0
    else:
        basis, multiplier = "unknown", 0.9

    return {
        "final": match * multiplier,
        "match": match,
        "matched": matched,
        "missing": missing,
        "multiplier": multiplier,
        "basis": basis,
        "jd": jd_result,
    }

if __name__ == "__main__":
    from resume_reader import read_resume
    resume = read_resume("resume.pdf")
    with open("job.txt", "r", encoding="utf-8") as jd_file:
        job = jd_file.read()

    run_agent(
        "Score this job against my resume, and if it scores above 0.35, save it. "
        "Company: Columbia University, title: Associate AI Engineer, url: https://opportunities.columbia.edu/jobs/associate-ai-engineer . Job: " + job,
        resume
    )

    # Extract the posting's sponsorship signal once, then reuse it.
    jd_result = extract_sponsorship(job)
    print(f"\nPosting sponsorship signal: {jd_result['status']} "
          f"(verified={jd_result['verified']})")
    if jd_result["evidence"]:
        print(f"Evidence: {jd_result['evidence']}")

    for company in ["Columbia University", "eClerx"]:
        r = final_score(resume, job, company, jd_result=jd_result)
        print(f"\n{company}")
        print(f"  Match: {round(r['match'], 3)}  Final: {round(r['final'], 3)}")
        print(f"  Basis: {r['basis']} ({r['multiplier']}x)")