# Job Copilot

**Finds the right jobs for you every day, writes an honest tailored resume and cover letter for each, and tracks every application, so you can spend your time on interview prep instead of job boards.**

It runs on your own computer. Your resume, documents, and job history stay in a local database.

---

## Why I built this

Job searching in the AI era is mostly volume work: scrolling boards, guessing which roles fit, rewriting the same resume twenty times, and losing track of where you applied. Recruiters screen with AI and applicants write with AI, so doing it all by hand mainly costs you time.

Job Copilot automates the repetitive part and keeps you in charge of the decisions. AI drafts, and code checks every draft so it never invents experience. You review and apply.

---

## What it does

1. **Finds jobs.** Pulls every opening from 223 company job boards (Greenhouse, Lever, and Ashby public APIs, no scraping). That's about 25,000 postings a day.
2. **Filters the noise.** Keeps only roles that fit your level, title, location, and years of experience.
3. **Ranks by real fit.** A local model scores each job against your resume, then an AI grader reads the shortlist the way a recruiter would and explains its score.
4. **Checks sponsorship.** Flags postings that rule out visa sponsorship, quoting the exact sentence.
5. **Writes your application.** For your top jobs, it creates a tailored resume, a cover letter, and a review copy with every change highlighted.
6. **Tracks everything.** A local database plus an Excel sheet with apply links, scores, status, and notes. Edits you make in either one are kept.

---

## Built for international students (and easy to change)

By default, Job Copilot is tuned for **international students who will need visa sponsorship** (F-1 OPT / STEM OPT / H-1B):

- Every shortlisted posting is checked for sponsorship language. A posting that rules it out ("must be authorized to work without sponsorship", US citizens only, security clearance) is quoted and **skipped**, so no resume is written for it.
- Employers with a history of H-1B sponsorship are favored when the posting says nothing either way.
- The search is limited to US-based and remote-US roles.

If you don't need sponsorship, or want different behavior, change these:

| What | Where | Change |
|---|---|---|
| Write documents even when a posting rules out sponsorship | `config.yaml` → `llm: skip_no_sponsorship` | set to `false` |
| How much sponsorship affects the score | `agent.py` → `final_score()` | the multipliers: `0.3` (rules it out) and `0.9` (unknown). Set both to `1.0` to ignore sponsorship |
| Known sponsoring employers | `sponsors.py` → `KNOWN_SPONSORS` | add or remove companies |
| Location | `config.yaml` → `prefilter: us_only` | `false` to keep all countries |
| Seniority, titles, years of experience | `config.yaml` → `prefilter:` | edit `title_include`, `title_exclude`, `max_years_required` |

---

## The honesty rules

Most AI resume tools rewrite freely. This one makes targeted edits, and **code** (not the AI) decides which edits are allowed:

| The AI can | The AI can never |
|---|---|
| Rephrase a bullet to use the job's wording | Invent a skill, tool, job, degree, or number |
| Reorder bullets, projects, and skills | Drop one of your metrics ("$2M saved", "30% faster", ...) |
| Rewrite the summary and cover letter | Move a keyword onto a role that never used it (no "REST APIs" on a project without APIs) |
| Mention coursework you list in `config.yaml` | Change your employers, titles, dates, or education |

Every draft also goes through a separate fact-checking pass. Unsupported phrases are fixed automatically, and anything left over is flagged for you to review before you send.

---

## Quick start

You need Python 3.11+.

```bash
git clone https://github.com/likith17/job-copilot.git
cd job-copilot
pip install -r requirements.txt
```

1. **Install the free model.** Install [Ollama](https://ollama.com), then run `ollama signin` and `ollama pull gpt-oss:120b-cloud`. It ranks jobs and writes documents with no API key and no bill (Ollama's free tier has usage limits; the app waits and retries when it hits one).
2. **Optional: use a paid model.** To write with Claude or another provider, copy `.env.example` to `.env`, add the key, and change `llm.tasks.tailor` in `config.yaml`. `.env` is git-ignored.
3. **Set your preferences** in `config.yaml` (see [Configuration](#configuration)).
4. **Start the app:**

```bash
streamlit run copilot_app.py
```

5. Upload your resume and cover letter, click **Run pipeline**, and come back in about 10 minutes.

---

## Using the app

1. **Upload** your resume and cover letter. The latest upload becomes the default.
2. **Run.** It fetches, filters, ranks, and writes documents for your top N jobs (10 by default).
3. **Click a job** to see the full posting, why it fits, what changed in your resume, the cover letter (editable), and download buttons.
4. **Apply** with the Apply button, which opens the application form directly, then tick **Applied**.

**Job statuses**

| Status | Meaning |
|---|---|
| New | No documents yet. Select it and click Generate. |
| Review | Documents written, but some phrases were flagged. Check them, then click "I've reviewed these". |
| Ready | Documents written and nothing flagged. Ready to send. |
| Applied, Interviewing, Offer, Rejected | Your progress. |
| Skipped | Not worth applying to, for example the posting rules out sponsorship. |

**The Excel tracker** (`data/jobs.xlsx`) is rebuilt after every run and every change in the app. The **Status** and **Notes** columns are yours: edit them in Excel and the next run picks them up. Close the file during a run, or the run saves a dated copy instead.

**Only one run at a time.** If a run is already going (from the terminal or the app), the Run and Generate buttons pause until it finishes. A crashed run never leaves this stuck.

---

## Applying: autofill and auto-apply

Every crawled job comes from Greenhouse, Lever or Ashby. Their forms are standard, so applying can be automated in three steps, each one optional.

**1. Your profile, once.** Copy `profile.example.yaml` to `profile.yaml` and fill it in: contact details, work authorization, sponsorship, start date, EEO answers ("Decline" picks the decline option). Anything left blank is **never guessed**; the form leaves it for you.

**2. The Chrome extension (you click Submit).**
1. Open `chrome://extensions`, turn on **Developer mode**, click **Load unpacked**, and pick the `extension/` folder.
2. Open the extension's options and paste the token shown in the app's sidebar (**Apply → Autofill extension setup**).
3. Keep the app running (or run `python -m copilot serve`).

Open any application page. The extension:
- fills every field it can from your profile;
- attaches *that job's* tailored resume and cover letter;
- answers custom questions from your **answer bank**, or with an AI draft for required questions (highlighted in amber for you to review);
- outlines in red whatever still needs you.

You review it and click Submit. When the confirmation page appears, the job is marked Applied, and your answers are saved to the bank so the next form with the same question fills itself.

**The new-resume guard.** Upload your master resume (ideally the Overleaf `.tex`), not a tailored one: the app refuses files that look like its own outputs, because tailoring a tailored resume drifts further from the real one, and every resume change re-grades your jobs.

**3. The auto-apply runner.**
```bash
python -m copilot apply              # open your top Ready jobs, one tab each, fully filled; you submit
python -m copilot apply --submit     # also submit the ones that need nothing from you
python -m copilot apply --url URL    # fill one application page
```
The runner uses the same filling script as the extension, driving your installed Chrome (profile kept in `data/browser/`). With `--submit`, an application is sent on its own **only if all of these hold**:
- the documents are Ready, with no review flags;
- they were built from your current resume;
- the fit is at least `apply.min_fit`;
- the posting doesn't rule out sponsorship;
- the resume was attached;
- every answer came from your profile or your answer bank (no unreviewed AI drafts);
- nothing required was left empty;
- no CAPTCHA appeared;
- today's `apply.daily_cap` and `apply.per_company_per_day` haven't been reached;
- sending isn't paused.

**4. Fully automatic, every day.**
```bash
python -m copilot auto                     # crawl, grade, write, send what passes, write a digest
python -m copilot schedule --time 09:00    # do that every day (Windows Task Scheduler)
python -m copilot schedule --remove
```
Sending stays off until `apply: auto_submit: true` in `config.yaml`, so run a supervised pilot first. The daily summary is in the app's sidebar and `data/digests/`. To stop sending at any time, flip **Pause automatic sending** in the sidebar (or create `data/STOP_AUTO_APPLY`). The database is backed up daily to `data/backups/` (last 7 kept); copy that folder somewhere else if you want protection from a dead disk.

It counts a submission only when the page confirms it. Everything else stays filled in its tab for you, and is marked Applied when you submit it. Every application, with what was filled, is logged in the `applications` table.

The more you apply, the more becomes automatic: each answer you approve goes into the bank, so the next form with that question needs nothing from you.

**Adding more autofill info.** Open **📋 Autofill info** in the app:
- **Questions forms asked:** every question a form asked that nothing could answer (a referral name, a certification, "how many days can you be in office?"). It shows how often it came up and its options. Type your answer once, or tick Skip, and every later form uses it.
- **Saved answers:** your whole answer bank. Edit answers, add rows, or delete a row to be asked again.
- **Profile:** edit `profile.yaml` in place. It has sections for referral, certifications, languages and clearance, plus `custom_answers` ("words in the question": answer) for anything else.

---

## Training your own writer model (no API bills)

The goal: a model on your own GPU that tailors resumes as well as Claude, for free. The pipeline:

1. **A small-model-sized task.** `per_bullet` mode asks "improve this one bullet for these requirements, or skip" instead of "rewrite the whole resume". Set `mode: per_bullet` on the tailor task, or try it with `python -m copilot eval --mode per_bullet`.
2. **Data you don't pay for.** Every tailoring run logs each bullet with what happened to it (`data/training/bullets.jsonl`). A free Ollama cloud model can generate more:
   ```bash
   python -m copilot train-data --teacher kimi-k3:cloud --jobs 150
   python -m copilot train-data --build
   ```
   Only edits that pass every honesty check *and* the claims audit become examples. Your 👍/✏️ reviews in the app count triple, and untouched bullets teach the model when to skip. The eval jobs are never trained on.
3. **Fine-tune** Qwen3-8B with QLoRA on an 8 GB GPU (WSL/Linux):
   ```bash
   bash training/setup_wsl.sh
   ~/jc-train/bin/python training/train_qlora.py
   ollama create jc-writer -f data/training/model/Modelfile --quantize q4_K_M
   ```
4. **Measure before trusting:** `python -m copilot eval --model jc-writer --finetuned`, compared with the saved Claude and gpt-oss runs. Switch the tailor task to it only if it wins.

---

## Configuration

Everything is set once, in two files:

**`config.yaml`**
- `writing.instructions`: how to tailor your resume (style, tone, focus).
- `writing.letter_instructions`: how to write cover letters.
- `writing.letter_points`: facts every cover letter must include (visa status, start date, relocation).
- `writing.coursework`: tools you studied but haven't used professionally. Letters may mention these as coursework, never as job experience.
- `writing.sections`: which resume sections the AI may change.
- `prefilter`: which titles, locations, and experience levels to keep.
- `llm.tasks`: which model does which job.
- `apply`: browser, jobs per run, and the auto-submit gates (`min_fit`, `daily_cap`).

**`profile.yaml`**: your answers to application forms (see above). Git-ignored.

**`companies.yaml`**: the job boards to crawl. Add a company by its Greenhouse, Lever, or Ashby board name, then check it with `python -m copilot companies`.

---

## Commands

```bash
streamlit run copilot_app.py           # the app
python -m copilot run                  # a full run from the terminal (or a scheduler)
python -m copilot run --no-llm         # fetch and rank only, no API cost
python -m copilot generate <job_id>    # documents for one job
python -m copilot companies            # check every job board responds
python -m copilot rescore              # re-rank after changing config.yaml
python -m copilot serve                # the autofill API, if the app isn't running
python -m copilot apply [--submit]     # fill (and optionally submit) your top applications
python -m copilot auto                 # the whole daily job
python -m pytest tests -q              # run the test suite
```

---

## How it works

Each stage only sees what survived the cheaper stage before it, so the paid AI work stays small:

```
223 job boards ──> 24,751 postings        free, about 80 s
       │
   prefilter  ──>  ~300 relevant           free (title, level, location, age, years)
       │
 local match  ──>  ~200 candidates         free (embeddings + skill coverage)
       │
  AI fit grade ──>  ~30 shortlisted        free with Ollama (reads each posting like a recruiter)
       │
 sponsorship check, tailored resume, cover letter, claims audit
       │
     top 10   ──>  ready to apply          free, about 2 minutes per job
```

Only new postings enter the funnel, so after the first run a daily run handles a few dozen jobs.

**Models are pluggable per task:** Claude, Ollama (local or cloud), Groq, Gemini, or any OpenAI-compatible endpoint. The default setup is free: an open model does the ranking and the writing.

---

## Results and measurements

The rule throughout: **build the simple version first, measure it, and only add complexity where the numbers justify it.**

| Question | Result |
|---|---|
| Do embeddings beat keyword matching? | On a real resume and job pair: keyword baseline 0.07, embeddings 0.68. |
| Does skill matching add signal? | Full-document keyword overlap scored about 0.10 and dragged the blend down. Skill-level matching scored 0.47 and explains matched and missing skills. |
| Does the ranking agree with a human? | Spearman 0.89 against my labels on 5 jobs. A sanity check, not a study. |
| Can embeddings alone rank 300 jobs? | No. Hardware verification and recruiter roles ranked near the top. The AI grader fixed this, scoring them 5 and 22. |
| Which free model grades like Claude? | On 26 jobs vs Claude Opus: gpt-oss-120b Spearman 0.79, local 8B models 0.16 to 0.36. |
| Does tailoring keep your numbers? | First version: 119 metrics lost across 21 drafts. Edit-based engine with code checks: 0. |
| Free model or Claude for writing? | Same rules, same jobs: the free model gained more keywords but left 7 flagged claims. Claude left 0. |

Two bugs found and fixed along the way: skill matching counted "rag" inside "storage" and "aws" inside "laws", and the embedding model only read the first 256 tokens (16% of the resume). The 0.89 evaluation predates these fixes and should be re-run.

---

## Project structure

```
copilot_app.py        the Streamlit app
copilot/              the pipeline
  pipeline.py         runs the stages in order
  fetch.py            job boards and the prefilter
  scoring.py          local matching and the sponsorship check
  grade.py            AI fit grade
  tailor.py           resume and cover letter writing, plus every honesty check
  llm.py              model providers (Claude, Ollama, Groq, Gemini)
  db.py, export.py    SQLite database and Excel tracker
  runlock.py          one run at a time
  apply.py            profile, job lookup by URL, question answering
  applyserver.py      local API for the extension (token-protected, localhost only)
  autoapply.py        the auto-apply runner and its submit gates
  auto.py             the daily job, its digest, and the Windows schedule
  embedcache.py       job embeddings on disk (data/embeddings.db, safe to delete)
  bullets.py          per-bullet writing (the task the local model learns)
  training.py         training data: teacher runs, filtering, SFT/preference sets
training/             fine-tuning scripts (QLoRA on Qwen3-8B)
extension/            the Chrome extension (content.js fills forms; also used by the runner)
jobmatcher/           job board crawlers (Greenhouse, Lever, Ashby)
agent.py              the original single-job matcher and agent
config.yaml           your preferences
profile.example.yaml  template for profile.yaml, your application answers
companies.yaml        the boards to crawl
tests/                150+ tests
data/                 your database, documents, and Excel sheet (git-ignored)
```

---

## Known limits

- **Sponsorship history** uses a list of about 150 large employers, so startups show "Unknown". Public H-1B employer data would fix this.
- **The resume layout** is a clean single-column Word template. Keeping your exact LaTeX/Overleaf layout is planned.
- **AI grades vary** by about 6 points between runs. Keyword coverage, counted in code, is the more reliable measure of what tailoring changed.
- **Some dropdowns need a real click.** Greenhouse's newer forms ignore scripted input in dropdowns (country, city, EEO). The runner handles them with real clicks; the extension shows you the exact choice to pick.
- **Auto-submit is new.** Start with `copilot apply` (you submit) for a week, and watch which answers end up in the bank, before turning on `--submit`.
- **Other job sites** (Workday, LinkedIn, Indeed) aren't supported. LinkedIn and Indeed forbid automation.
