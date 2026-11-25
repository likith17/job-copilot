import os
import json
from dotenv import load_dotenv
from anthropic import Anthropic

load_dotenv()
client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

SYSTEM_PROMPT = """You classify whether a job posting offers visa sponsorship.

Return ONLY a JSON object. No prose, no markdown fences:
{"status": "...", "evidence": "...", "reasoning": "..."}

"status" must be exactly one of:
- "sponsors": the posting states sponsorship is available, or that the
  employer supports visa or work-permit applications.
- "no_sponsorship": the posting rules out sponsorship now or in the future,
  restricts the role to US citizens, or requires a security clearance or
  ITAR/export-control eligibility.
- "not_mentioned": the posting says nothing decisive either way.

Critical distinction:
- "Must be authorized to work in the United States" on its own is NOT a
  rejection. Candidates on OPT are authorized. Classify as "not_mentioned".
- "Must be authorized to work without sponsorship, now or in the future"
  IS "no_sponsorship".
The deciding word is "sponsorship", not "authorized".

Other rules:
- If statements conflict, choose the more restrictive one and quote it.
- Ignore equal-opportunity boilerplate mentioning national origin or
  citizenship status. It is not a sponsorship statement.
- "evidence" must be the exact sentence from the posting that decided it,
  copied verbatim. Use an empty string when status is "not_mentioned".
- "reasoning" is one short sentence.
"""

def _parse(raw):
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return None
    
VALID = {"sponsors", "no_sponsorship", "not_mentioned"}

NEUTRAL = {
    "status": "not_mentioned",
    "evidence": "",
    "reasoning": "Extraction failed; defaulting to neutral.",
    "verified": False,
}


def extract_sponsorship(job_description, model="claude-sonnet-4-6"):
    response = client.messages.create(
        model=model,
        max_tokens=300,
        system=SYSTEM_PROMPT,
        messages=[
            {"role": "user", "content": f"JOB POSTING:\n{job_description}"}
        ],
    )

    data = _parse(response.content[0].text)
    if data is None:
        return dict(NEUTRAL)

    status = data.get("status", "not_mentioned")
    if status not in VALID:
        return dict(NEUTRAL)

    evidence = data.get("evidence", "") or ""
    verified = _verify(evidence, job_description)

    return {
        "status": status,
        "evidence": evidence,
        "reasoning": data.get("reasoning", ""),
        "verified": verified,
    }
    
def _verify(evidence, job_description):
    if not evidence:
        return True
    a = " ".join(evidence.lower().split())
    b = " ".join(job_description.lower().split())
    return a in b

if __name__ == "__main__":
    cases = [
        "We are unable to provide visa sponsorship for this role, now or in the future.",
        "Applicants must be authorized to work in the United States.",
        "We welcome international applicants and will sponsor H-1B visas.",
        "You will build data pipelines using Python and SQL.",
    ]
    for text in cases:
        result = extract_sponsorship(text)
        print(f"\n{text[:60]}...")
        print(f"  -> {result['status']} (verified={result['verified']})")
        if result["evidence"]:
            print(f"  evidence: {result['evidence']}")