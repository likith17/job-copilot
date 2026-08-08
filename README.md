# Job Copilot — An Agentic Resume-to-Job Matcher

A command-line tool that reads your resume, scores it against real job postings using hybrid semantic + skill matching, and uses an LLM agent to draft tailored suggestions — with a human approving every action.

Built from scratch to understand every layer: text similarity, embeddings, databases, evaluation, and agentic tool-calling. No black boxes.

---

## What it does

Point it at your resume (PDF or DOCX) and a job posting, and it:

- **Scores the match** by blending semantic similarity (embeddings) with skill-coverage matching, so it captures both *meaning* ("ML" ≈ "machine learning") and *exact requirements* ("does this job want LangChain, and do I have it?").
- **Tells you your real strengths and gaps** — which of the job's required skills your resume covers, and which it's missing — instead of guessing.
- **Stores and ranks** every scored job in a local database, deduplicated by URL.
- **Drafts tailored resume suggestions** via an LLM agent, grounded only in your real experience.
- **Keeps you in control** — the agent proposes actions; nothing runs without your approval.

---

## Why it's built this way

The guiding principle throughout: **build the simple version first, measure it, then improve only where the measurement justifies it.** Every layer below beat a baseline before being kept.

### 1. Keyword baseline first

The first matcher compared word sets (Jaccard similarity). It's deliberately naive — it treats "ML" and "machine learning" as unrelated because they share no characters. This exists as the baseline the smarter version has to beat.

### 2. Semantic matching with embeddings

Text is converted to embeddings (`sentence-transformers`, all-MiniLM-L6-v2) and compared with cosine similarity. This captures meaning, not just exact words.

**Measured result:** on a real resume-job pair, the keyword baseline scored a genuine match at **0.07** while the embedding version scored **0.68** — a 10x gap that justifies the added complexity.

### 3. Hybrid search (semantic + skills)

Pure semantic matching has a blind spot: it under-weights hard requirements. A job demanding "LangChain" as a must-have doesn't move the semantic score much.

The fix blends two signals:
- **Semantic score** — meaning-based similarity.
- **Skill-coverage score** — of the skills a job requires (from a curated skill vocabulary), what fraction the resume actually contains.

**A finding worth reporting honestly:** the first hybrid attempt used full-document keyword overlap, which scored ~0.10 and *dragged the blend down without adding signal* — because full-document Jaccard is always diluted. Switching to skill-level matching produced a meaningful ~0.47 and, more usefully, an explicit matched/missing skill breakdown. The lesson: hybrid search only helps if the keyword component measures something meaningful.

### 4. Evaluation

The matcher was validated against hand-labeled jobs: on 5 real postings, the tool's ranking agreed with human expert judgment at a **Spearman rank correlation of 0.89**.

This is a sanity check, not a formal study — the sample is small and self-labeled, and that limitation is stated plainly rather than hidden. The labeled set grows as more jobs are scored.

### 5. Agentic layer

An LLM (Anthropic API) is wired as an agent with three tools: `score_job`, `draft_tailored_bullets`, and `save_job`. The model decides which to call and in what order; the code executes them.

Design choices, following the principle that a reliable agent is a constrained workflow, not an autonomous free-for-all:
- **Human-in-the-loop:** every tool call is shown and requires explicit approval before it runs.
- **Turn cap:** the loop can never run more than a fixed number of iterations, preventing runaway token use.
- **Prompt caching:** the repeated resume prefix is cached, verified via a measured drop in input-token usage on cached calls.
- **Grounded output:** the scoring tool passes the real matched/missing skills to the model, so its summary reports facts instead of inferring gaps.

---

## Architecture

```
resume (PDF/DOCX) ---> resume_reader ---> text
                                            |
job posting text --------------------------+
                                            |
                                            v
                              +-------------------------+
                              |      hybrid_score        |
                              |  semantic (embeddings)   |
                              |  + skill coverage        |
                              +-------------------------+
                                            |
                    score + matched/missing skills
                                            |
                                            v
                         +---------------------------------+
                         |         LLM agent loop           |
                         |  tools: score / draft / save     |
                         |  human approval on every call    |
                         +---------------------------------+
                                            |
                                            v
                              SQLite database (ranked, deduped)
```

---

## Tech

Python, sentence-transformers, NumPy, SQLite, Anthropic API (Claude), scipy (evaluation). Resume parsing via python-docx and pypdf.

---

## Running it

Requires Python 3.11+ and an Anthropic API key in a `.env` file (never committed — see `.gitignore`).

```bash
git clone https://github.com/likith17/job-copilot.git
cd job-copilot
pip install -r requirements.txt
python agent.py
```

You'll be asked to select your resume, then the agent scores the job in `job.txt` and, with your approval, saves it to the `jobs.db` database.

---

## What I know is incomplete

Being honest about the limits, because most of these are the obvious next questions:

- **Skill matching is literal.** "LLM" experience described only as "large language models" in the resume won't match the skill token "llm" — the matcher checks exact strings. Synonym handling would improve this.
- **Job metadata is manual.** Company, title, and URL are entered by hand. Automated ingestion from compliant job APIs would remove this step (naive scraping of major job boards violates their terms and breaks easily — the plan is APIs and feeds, not scraping).
- **URL deduplication is exact-match.** The same job with tracking parameters appended would look new. URL normalization would fix this.
- **Evaluation is small-sample.** 5 self-labeled jobs is a sanity check, not a benchmark.
- **The skill vocabulary is hand-curated.** It covers common AI/software skills but isn't exhaustive.

---

## Roadmap

- H-1B/OPT sponsorship likelihood signal (from public DOL disclosure data)
- Streamlit dashboard to replace terminal input
- Automated job ingestion from compliant sources
- Larger evaluation set
