# Job Copilot roadmap

**Vision:** an application engine you supervise. Each morning it has found the right jobs, written honest and tailored materials, and pre-filled every application. You spend five minutes approving, then your time goes to interview prep. Applications are submitted for you only where the system has earned that trust, and the inbox closes the loop.

Two principles hold at every level:

1. **Never claim something untrue.** Work authorization, sponsorship, EEO answers, and "I certify this is accurate" attestations come from answers you gave once and approved. They are never generated.
2. **Never fight a site's defenses.** If a site shows a CAPTCHA or bot check, the system hands that application back to you. It does not try to beat the check.

---

## Where we are (Oct 2026)

| Done | Notes |
|---|---|
| Daily crawl of 91 boards (Greenhouse, Lever, Ashby public APIs) | 15k postings, then ~300 relevant |
| Ranking funnel: prefilter → embeddings → AI fit grade | AI grade fixes the embedding blind spots |
| Tailored resume + cover letter, fabrication guard, fit before/after | Top 10 automatically, the rest on demand |
| Local SQLite as the source of truth; Excel view with two-way status sync | |
| Streamlit app: upload, run, click-to-open job, applied checkbox, revise by instruction | |
| Pluggable models: Claude / Ollama (local, free) / Groq / Gemini | per task in `config.yaml` |
| **Levels 1-5 built (Oct 21):** `profile.yaml`, Chrome extension, answer bank + AI drafts, `copilot apply` prepared tabs, gated `--submit` | Tested on live Greenhouse, Lever and Ashby forms (fake profile, no submission) |
| **Unattended operation (Nov 6):** `copilot auto` + daily schedule, digest, pause switch, backups, rate-limit retries, grade and sponsorship caches, free open-model writer | Sending stays off (`apply.auto_submit`) until a supervised pilot confirms real submissions |

---

## Autofill and auto-apply: simplest to fully automatic

All 91 boards run on three ATSs: **Greenhouse, Lever, and Ashby**. Their application forms are standardized, which is what makes real automation feasible. Tools like Simplify and LazyApply rely on the same fact.

### Level 1: one profile, one-click copy (days)
- A `profile.yaml`: name, contact, links, education, work authorization, sponsorship need, start date, salary range, relocation, and EEO answers (your choices, including "decline to answer").
- The app shows copy buttons next to each job. You still type into the form, but nothing needs looking up.

### Level 2: browser extension autofill (1–2 weeks)
- A Chrome extension recognizes Greenhouse, Lever, and Ashby forms.
- It fills the standard fields from `profile.yaml`.
- It attaches this job's tailored resume and cover letter, fetched from the local app by job ID. That's the link the DB already provides.
- You review and click Submit yourself. This removes about 80% of the typing.

### Level 3: AI answers to custom questions (1–2 weeks)
- Custom questions ("Why do you want to work here?", "Describe a hard bug") are drafted by the AI from your master resume plus the job description, under the same fabrication guard as the resumes.
- An **answer bank** in the DB saves every answer you approve. Similar questions later reuse your own wording; they're matched by embedding, so this costs nothing.
- Fixed-choice questions ("Are you 18+?", "Willing to relocate?") are answered from the profile and never guessed. An unknown fixed-choice question stops and asks you once, then remembers.

### Level 4: prepared applications (2–3 weeks)
- A local Playwright agent opens each top job's application page, fills every field, uploads the documents, and **stops just before Submit**.
- Each morning: 10 browser tabs, each fully filled. You read each one for about 30 seconds and click Submit.
- The app records exactly what was filled in each application, for audit and the answer bank.

### Level 5: supervised auto-submit (after Level 4 has run a few weeks)
The system submits on its own **only** when all of these hold:
- The ATS is Greenhouse, Lever, or Ashby, with a form shape it has filled successfully before.
- Every field was answered from the profile or the answer bank. No new free-text answers.
- No review flags on the documents, and fit ≥ your threshold.
- No CAPTCHA appeared. If one does, the application goes to your queue.
- A daily cap (for example 15/day), to protect quality and your reputation with recruiters.

Anything else goes to the Level 4 review queue. You can view and withdraw every submission.

**Can it be fully automatic?** Technically yes, for these three ATSs, and Level 5 *is* fully automatic for the cases it trusts. The limits are deliberate:
- **Truthfulness.** Legal attestations and new claims need your approval the first time.
- **Bot checks.** These are not bypassed.
- **Site terms.** LinkedIn Easy Apply and Indeed prohibit automation, so they stay out of scope. Company ATS forms do not have that problem.
- **Quality.** Mass-applying lowers response rates. A capped, high-fit, tailored queue beats volume.

### Level 6: closed loop with email (2–3 weeks, can run in parallel)
- **Gmail/Outlook integration (read-only scope)** classifies mail into: application received, rejection, interview request, assessment, offer.
- Each email is matched to its job in the DB by company, role, and ATS sender, and updates the status automatically (Applied → Interviewing / Rejected).
- Interview invites trigger an interview-prep pack: the job description, your tailored resume, likely questions, and company notes.
- **Learning:** response rates by company, role type, fit score, and sponsorship basis feed back into ranking. Jobs like the ones that called you back rise.

---

## Timeline

| When | Milestone |
|---|---|
| **Week 1 (now)** | Free local models wired in and benchmarked; resume-change re-scoring; app polish (done). Next: per-company cap on the auto top 10, a better level filter, and the full public H-1B employer list replacing the 150-name list. |
| **Weeks 2–3** | Level 1 profile + Level 2 extension autofill for Greenhouse/Lever/Ashby, with documents attached from the local DB. Daily scheduled run (Windows Task Scheduler). |
| **Weeks 4–5** | Level 3 custom-question drafting + answer bank. Level 6 email integration, read-only first. |
| **Weeks 6–8** | Level 4 Playwright "prepared tabs" queue. Re-run the matcher evaluation (Spearman) on a larger labeled set, including the scorer fixes. |
| **Weeks 9–10** | Level 5 supervised auto-submit behind the trust rules above. Response-rate learning loop. |
| **After** | More sources (Workday and SmartRecruiters company boards, YC jobs); interview-prep packs; a weekly analytics digest. |

Each milestone ships with tests and a measured result in the README: what it does, how we checked it, and its known limits.
