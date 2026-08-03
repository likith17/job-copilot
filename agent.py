import os
from dotenv import load_dotenv
from anthropic import Anthropic
from sentence_transformers import SentenceTransformer
import numpy as np

load_dotenv()
client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
model_embed = SentenceTransformer("all-MiniLM-L6-v2")
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
    }
]   

def run_agent(user_request, resume_text):
    messages = [{"role": "user", "content": user_request}]

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        system="You are a job-application assistant. When you need to use a tool, call it directly with the required inputs. Do not narrate what you're about to do.",
        tools=tools,
        messages=messages
    )

    print("Stop reason:", response.stop_reason)
    for block in response.content:
        if block.type == "text":
            print("Model says:", block.text)
        elif block.type == "tool_use":
            print("Model wants to call:", block.name)
            print("With input:", block.input)

    return response

if __name__ == "__main__":
    from resume_reader import read_resume
    resume = read_resume("resume.pdf")   # your real filename
    with open("job.txt", "r", encoding="utf-8") as f:
        job = f.read()

    run_agent("Score this job against my resume: " + job, resume)
