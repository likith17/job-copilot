import os
from dotenv import load_dotenv
from anthropic import Anthropic

load_dotenv()
client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

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

if __name__ == "__main__":
    from resume_reader import read_resume
    resume = read_resume("resume.pdf")   # your real filename
    with open("job.txt", "r", encoding="utf-8") as f:
        job = f.read()

    print("---calling api---")
    draft_tailored_bullets(resume, job)
   