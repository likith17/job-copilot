// Job Copilot autofill: fills Greenhouse / Lever / Ashby application forms from
// your profile, your answer bank and this job's tailored documents.
//
// It never clicks Submit. The same script runs in two places:
//   - the Chrome extension (talks to the local API through background.js)
//   - the auto-apply runner (Playwright injects it and provides window.__copilotBridge)
// Its result is published on <html data-copilot="..."> for the runner to read.
(() => {
  if (window.__copilotLoaded) return;
  window.__copilotLoaded = true;

  const host = location.hostname;
  const ATS = /greenhouse\.io$/.test(host) ? "greenhouse" : /lever\.co$/.test(host) ? "lever"
    : /ashbyhq\.com$/.test(host) ? "ashby" : (window.__copilotBridge ? "other" : null);
  if (!ATS) return;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const clean = (s) => (s || "").replace(/[*✱]/g, "").replace(/\(required\)/i, "").replace(/\s+/g, " ").trim();
  const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);

  // ---------------- talking to the copilot ----------------
  function api(path, method = "GET", body = null, binary = false) {
    if (window.__copilotBridge) return window.__copilotBridge(path, method, body, binary);
    return new Promise((resolve, reject) => {
      chrome.runtime.sendMessage({ type: "api", path, method, body, binary }, (res) => {
        if (chrome.runtime.lastError) return reject(new Error(chrome.runtime.lastError.message));
        if (res && res.error) return reject(new Error(res.error));
        resolve(res);
      });
    });
  }
  const bg = (msg) => window.__copilotBridge ? Promise.resolve(null)
    : new Promise((r) => chrome.runtime.sendMessage(msg, r));

  // ---------------- reading the form ----------------
  const QUESTION_TEXT = "label, legend, .application-label, [class*='question-title'], [class*='label']";
  const CONTROLS = "select, option, input, textarea, button, [role='listbox'], [class*='select__']";

  // Visible text of a label, without the text of controls inside it or hidden helper text.
  function textOf(node) {
    let out = "";
    for (const c of node.childNodes) {
      if (c.nodeType === 3) out += c.textContent;
      else if (c.nodeType === 1 && !c.matches(CONTROLS)) {
        const st = getComputedStyle(c);
        if (st.display === "none" || st.visibility === "hidden") continue;
        out += " " + textOf(c) + " ";
      }
    }
    return clean(out);
  }

  function labelFor(el) {
    if (el.labels && el.labels.length && el.type !== "radio" && el.type !== "checkbox" && textOf(el.labels[0])) return textOf(el.labels[0]);
    const by = el.getAttribute("aria-labelledby");
    if (by) {
      const t = by.split(" ").map((id) => document.getElementById(id)?.innerText || "").join(" ");
      if (clean(t)) return clean(t);
    }
    // Walk up to the question's container and take its question text.
    let node = el.parentElement;
    for (let i = 0; i < 7 && node; i++, node = node.parentElement) {
      // Skip option labels ("Male", "Yes") that point at a radio or checkbox: they aren't the question.
      const q = [...node.querySelectorAll(QUESTION_TEXT)].find((l) => !l.contains(el)
        && !l.querySelector("input, select, textarea") && textOf(l).length > 1
        && !(l.htmlFor && document.getElementById(l.htmlFor)?.matches("input[type=radio], input[type=checkbox]")));
      if (q) return textOf(q);
    }
    return clean(el.getAttribute("aria-label") || el.placeholder || el.name || "");
  }

  function isRequired(el, label) {
    if (el.required || el.getAttribute("aria-required") === "true") return true;
    let node = el.parentElement;
    for (let i = 0; i < 5 && node; i++, node = node.parentElement) {
      const q = node.querySelector(QUESTION_TEXT);
      if (q && textOf(q) === label) {
        return /[*✱]/.test(q.innerText) || /\(required\)/i.test(q.innerText) || /required/i.test(q.className)
          || !!q.querySelector("[class*='required'], [class*='asterisk']");
      }
    }
    return false;
  }

  function optionText(input) {
    const l = input.labels?.[0] || input.closest("label");
    return l ? textOf(l) || clean(l.innerText) : clean(input.value);
  }

  // Every question on the form, as {label, kind, required, els, options}.
  function readForm() {
    const fields = [], groups = {};
    const controls = [...document.querySelectorAll("input, textarea, select")].filter((el) =>
      !["hidden", "submit", "button", "image", "reset", "search"].includes(el.type) && !el.closest("#copilot-panel"));
    for (const el of controls) {
      if (el.type !== "file" && !visible(el) && el.type !== "radio" && el.type !== "checkbox") continue;
      if (el.type === "radio" || el.type === "checkbox") {
        const key = el.name || labelFor(el);
        if (!groups[key]) {
          groups[key] = { label: labelFor(el), kind: el.type, els: [], options: [] };
          fields.push(groups[key]);
        }
        groups[key].els.push(el);
        groups[key].options.push(optionText(el));
        continue;
      }
      const label = labelFor(el);
      let kind = el.tagName === "SELECT" ? "select" : el.tagName === "TEXTAREA" ? "textarea" : el.type === "file" ? "file" : "text";
      if (el.getAttribute("role") === "combobox" || el.closest("[class*='select__control'], [role='combobox']")) kind = "combobox";
      fields.push({ label, kind, els: [el], options: kind === "select" ? [...el.options].map((o) => clean(o.text)).filter(Boolean) : [] });
    }
    // Ashby-style Yes/No button pairs (no input element).
    document.querySelectorAll("[class*='yesno'], [class*='YesNo']").forEach((box) => {
      const buttons = [...box.querySelectorAll("button")];
      if (buttons.length >= 2) fields.push({ label: labelFor(box), kind: "buttons", els: buttons, options: buttons.map((b) => clean(b.innerText)) });
    });
    for (const f of fields) {
      f.required = f.els.some((el) => isRequired(el, f.label));
      if (f.kind === "checkbox" && f.els.length === 1) f.kind = "consent";
    }
    // Same question twice (Ashby's Yes/No buttons over hidden checkboxes, Greenhouse's
    // combobox over a hidden text input): keep the control people actually use.
    const RANK = { buttons: 0, combobox: 1, select: 2, radio: 3, checkbox: 4, textarea: 5, text: 6, consent: 7, file: 8 };
    const best = {};
    for (const f of fields.filter((f) => f.label)) {
      // Greenhouse labels both uploads "Attach", so files are told apart by id.
      const key = f.kind === "file" ? `file:${f.els[0].id}:${f.label.toLowerCase()}` : f.label.toLowerCase();
      const prev = best[key];
      if (!prev) { best[key] = f; continue; }
      const required = f.required || prev.required;
      if (RANK[f.kind] < RANK[prev.kind]) best[key] = f;
      best[key].required = required;
    }
    const kept = new Set(Object.values(best));
    return fields.filter((f) => kept.has(f));
  }

  // ---------------- what to answer ----------------
  const yn = (v) => (v === true ? "Yes" : v === false ? "No" : undefined);
  const DECLINE = /decline|prefer not|don.?t (wish|want)|do not (wish|want)|not to (say|disclose|answer|self)|choose not|rather not/i;

  // [label pattern, value from profile, choice-only]. First match wins; order matters.
  const RULES = [
    [/preferred (first )?name/i, (p) => p.preferred_name || p.first_name],
    [/first name|given name|^first$/i, (p) => p.first_name],
    [/last name|family name|surname|^last$/i, (p) => p.last_name],
    [/^(full |legal )?name$|full name|legal name/i, (p) => p.name],
    [/referr.*e-?mail|e-?mail.*referr/i, (p) => p.referral?.email],
    [/were you referred|do you have a referral|referred by an? (current )?employee/i, (p) => yn(!!p.referral?.name), true],
    [/referr(er|al)('?s)? (full )?name|name of (the |your )?(person who )?referr|who referred you|referred by/i,
      (p) => p.referral?.name],
    [/e-?mail/i, (p) => p.email],
    [/phone|mobile/i, (p) => p.phone],
    [/linkedin/i, (p) => p.linkedin],
    [/github/i, (p) => p.github],
    [/portfolio|personal (web)?site|^website|other (website|link)/i, (p) => p.portfolio],
    [/pronoun/i, (p) => p.pronouns],
    [/sexual orientation|lgbt/i, (p) => p.eeo?.sexual_orientation, true],
    [/transgender/i, (p) => p.eeo?.transgender, true],
    [/hispanic|latin[oax]/i, (p) => p.eeo?.hispanic_latino, true],
    [/\brace\b|ethnicity/i, (p) => p.eeo?.race, true],
    [/veteran/i, (p) => p.eeo?.veteran, true],
    [/disabilit/i, (p) => p.eeo?.disability, true],
    [/gender/i, (p) => p.eeo?.gender, true],
    [/sponsor/i, (p) => yn(p.work_authorization?.requires_sponsorship)],
    [/authori[sz]ation status|current (work )?authori[sz]ation|visa (status|type)|immigration status/i,
      (p) => p.work_authorization?.visa_status],
    [/authori[sz]ed to work|eligible to work|right to work|legally (able|permitted) to work|work authori[sz]ation/i,
      (p) => yn(p.work_authorization?.authorized_us)],
    [/citizenship/i, (p) => p.work_authorization?.citizenship],
    [/relocat/i, (p) => yn(p.willing_to_relocate), true],
    [/18 years|over 18|at least 18|legal age/i, (p) => yn(p.over_18), true],
    [/(previously|ever) (been )?(employed|worked)|worked (here|for us|at .+) before|former employee|are you an? (current |former )?employee of/i,
      (p, job) => job ? yn((p.previously_employed_at || []).some((c) => c.toLowerCase() === job.company.toLowerCase())) : undefined, true],
    [/current (company|employer)|^company( name)?$|^organization$/i, (p) => p.current_company],
    [/current (job )?title|^(job )?title$/i, (p) => p.current_title],
    [/street|address line/i, (p) => p.address?.street],
    [/postal|zip|zip ?code/i, (p) => p.address?.postal_code],
    [/^state\b|province/i, (p) => p.address?.state],
    [/^country/i, (p) => p.address?.country],
    [/^city$/i, (p) => p.address?.city || (p.location || "").split(",")[0]],
    [/^location|current location|where are you (located|based)|city.*(live|reside|based)/i, (p) => p.location],
    [/school|university|college|institution/i, (p) => p.education?.[0]?.school],
    [/degree/i, (p) => p.education?.[0]?.degree],
    [/discipline|major|field of study/i, (p) => p.education?.[0]?.discipline],
    [/graduation|end (date )?year|year of completion/i, (p) => p.education?.[0]?.end_year],
    [/\bgpa\b/i, (p) => p.education?.[0]?.gpa],
    [/salary|compensation expectation|pay expectation|desired (pay|compensation)/i, (p) => p.salary_expectation],
    [/start date|when can you start|available to start|earliest.*start|notice period/i, (p) => p.start_date],
    [/how (did )?you (hear|heard|find|found|learn)|where did you (hear|find|learn)|referral source|^source$/i, (p) => p.how_did_you_hear],
    [/years of (professional |relevant |work )?experience/i, (p) => p.years_experience],
    [/security clearance|clearance level|active clearance/i, (p) => p.clearance],
    [/certification|certificate|licen[cs]e/i, (p) => (p.certifications || []).map((c) => c.name || c).join(", ")],
    [/languages? (do )?you speak|fluent in|language proficiency|languages spoken/i, (p) => (p.languages || []).join(", ")],
  ];

  function fromProfile(field, profile, job) {
    if (field.els[0].type === "password" || (/password|passcode/i.test(field.label) && field.label.length < 50)) return undefined;
    for (const [key, value] of Object.entries(profile.custom_answers || {})) {
      if (value !== null && value !== "" && field.label.toLowerCase().includes(key.toLowerCase())) return String(value);
    }
    if (field.kind === "textarea") return undefined;     // essays: your bank or a draft, never a profile field
    for (const [pattern, get, choiceOnly] of RULES) {
      if (!pattern.test(field.label)) continue;
      if (choiceOnly && !["select", "radio", "checkbox", "combobox", "buttons"].includes(field.kind)) return undefined;
      const v = get(profile, job);
      return v === null || v === undefined || v === "" ? undefined : String(v);
    }
    return undefined;
  }

  function pickOption(options, value) {
    const v = String(value).toLowerCase().trim();
    const opts = options.map((o) => o.toLowerCase());
    if (v === "decline") return opts.findIndex((o) => DECLINE.test(o));
    if (v === "yes" || v === "no") return opts.findIndex((o) => new RegExp(`^${v}\\b`).test(o));
    let i = opts.findIndex((o) => o === v);
    if (i < 0) i = opts.findIndex((o) => o.startsWith(v));
    if (i < 0) i = opts.findIndex((o) => o.includes(v) || (v.length > 3 && v.includes(o) && o.length > 3));
    return i;
  }

  // ---------------- writing to the form ----------------
  function setNative(el, value) {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value").set.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  // The options of THIS combobox (not, say, the phone field's hidden country list).
  function comboOptions(el) {
    const id = el.getAttribute("aria-controls") || el.getAttribute("aria-owns");
    let scope = id && document.getElementById(id);
    if (!scope) {
      let node = el.parentElement;
      for (let i = 0; i < 6 && node && !scope; i++, node = node.parentElement) {
        if (node.querySelector("[role='option'], [class*='select__option']")) scope = node;
      }
    }
    const found = [...(scope || document).querySelectorAll("[role='option'], [class*='select__option']")];
    return found.filter((o) => visible(o) && !o.closest(".iti, .iti__country-list"));
  }

  // Index of the option to pick in this combobox's open menu (-1 if none); the runner
  // opens the menu with real clicks and asks this.
  let comboSeq = 0;
  window.__copilotChoose = (key, value, fallbackFirst) => {
    const el = document.querySelector(`[data-copilot-id="${key}"]`);
    const options = el ? comboOptions(el) : [];
    let i = pickOption(options.map((o) => clean(o.innerText)), value);
    if (i < 0 && fallbackFirst && options.length) i = 0;
    if (i < 0) return null;
    options[i].scrollIntoView({ block: "nearest" });
    const r = options[i].getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2, text: clean(options[i].innerText) };
  };

  async function fillCombobox(el, value) {
    const control = el.closest("[class*='select__control']") || el;
    el.focus();
    control.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    el.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
    await sleep(250);
    let options = comboOptions(el);
    let i = pickOption(options.map((o) => clean(o.innerText)), value);
    if (i < 0 && !/^(yes|no|decline)$/i.test(value)) {   // long lists (locations, schools): type to search
      setNative(el, String(value));
      await sleep(800);
      options = comboOptions(el);
      i = pickOption(options.map((o) => clean(o.innerText)), value);
      if (i < 0 && options.length && /location|city/i.test(labelFor(el))) i = 0;
    }
    if (i < 0) {
      el.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      el.blur();
      if (!options.length) {        // the menu never opened: it only answers real clicks
        el.dataset.copilotId = el.dataset.copilotId || `c${++comboSeq}`;
        return "manual";
      }
      return false;
    }
    options[i].dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    options[i].click();
    await sleep(100);
    return true;
  }

  async function fill(field, value) {
    const el = field.els[0];
    switch (field.kind) {
      case "text": case "textarea":
        if (el.value && el.value.trim()) return "kept";      // never overwrite what's there
        setNative(el, value);
        el.dispatchEvent(new Event("blur", { bubbles: true }));
        return true;
      case "select": {
        const i = pickOption(field.options, value);
        if (i < 0) return false;
        const opt = [...el.options].filter((o) => clean(o.text))[i];
        el.value = opt.value;
        el.dispatchEvent(new Event("change", { bubbles: true }));
        return true;
      }
      case "radio": case "checkbox": case "buttons": {
        const i = pickOption(field.options, value);
        if (i < 0) return false;
        const target = field.els[i];
        if (!(target.checked)) target.click();
        return true;
      }
      case "consent":
        if (/^(yes|true|agree|i agree|checked)$/i.test(String(value)) && !el.checked) el.click();
        return true;
      case "combobox":
        return fillCombobox(el, value);
    }
    return false;
  }

  async function attach(field, job, kind) {
    const file = await api(`/api/file?job=${encodeURIComponent(job.id)}&kind=${kind}`, "GET", null, true);
    const bytes = Uint8Array.from(atob(file.base64), (c) => c.charCodeAt(0));
    const dt = new DataTransfer();
    dt.items.add(new File([bytes], file.name, { type: file.type }));
    const el = field.els[0];
    el.files = dt.files;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return file.name;
  }

  function mark(field, color, note) {
    const el = field.kind === "radio" || field.kind === "checkbox" || field.kind === "buttons"
      ? field.els[0].closest("fieldset, [class*='question'], [class*='field']") || field.els[0] : field.els[0];
    const target = el.closest("[class*='select__control']") || el;
    target.style.outline = `2px solid ${color}`;
    target.style.outlineOffset = "2px";
    if (note) target.title = `Job Copilot: ${note}`;
  }

  // ---------------- the whole fill ----------------
  const state = { ats: ATS, url: location.href, job: null, filled: [], drafts: [], needs: [], combos: [], errors: [], done: false };
  const publish = () => {
    document.documentElement.dataset.copilot = JSON.stringify(state);
    renderPanel();
  };

  const isQuestion = (f) => f.label.length >= 18 || /\?$/.test(f.label) || f.kind === "textarea";

  async function run() {
    Object.assign(state, { filled: [], drafts: [], needs: [], combos: [], errors: [], done: false });
    let profile;
    try {
      profile = await api("/api/profile");
      state.job = (await api(`/api/job?url=${encodeURIComponent(location.href)}`)).job;
    } catch (e) {
      state.errors.push(`Copilot API not reachable: ${e.message}. Start the app or "python -m copilot serve".`);
      state.done = true;
      return publish();
    }
    const fields = readForm();
    const later = [], attached = new Set();
    for (const f of fields) {
      try {
        if (f.kind === "file") {
          // Greenhouse labels its upload "Attach": the input's id says which file it wants.
          const el = f.els[0];
          const hint = `${f.label} ${el.id} ${el.name} ${el.getAttribute("aria-label") || ""}`;
          const kind = /autofill|parse/i.test(hint) ? null : /cover/i.test(hint) ? "cover_letter"
            : /resume|\bcv\b/i.test(hint) ? "resume" : null;
          if (!kind || attached.has(kind)) continue;
          attached.add(kind);
          if (!state.job || !state.job.documents[kind]) {
            if (f.required || kind === "resume") { state.needs.push(f.label); mark(f, "#d93025", "attach this yourself"); }
            continue;
          }
          const name = await attach(f, state.job, kind);
          state.filled.push({ label: f.label, value: name, source: "documents", kind });
          continue;
        }
        const value = fromProfile(f, profile, state.job);
        if (value !== undefined) {
          const ok = await fill(f, value);
          if (ok === "manual") {
            state.combos.push({ key: f.els[0].dataset.copilotId, label: f.label, value, required: f.required,
              search: /location|city/i.test(f.label) });
            mark(f, "#1a73e8", `choose: ${value}`);
            continue;
          }
          if (ok) { state.filled.push({ label: f.label, value, source: "profile" }); continue; }
        }
        later.push(f);     // not in the profile (or its value isn't an option here): ask your saved answers
      } catch (e) {
        state.errors.push(`${f.label}: ${e.message}`);
      }
    }
    publish();
    // Questions the profile doesn't cover: your saved answers first, then an AI draft (required
    // free text only). Anything still unanswered lands in the app's "Questions forms asked" inbox.
    await Promise.all(later.map(async (f) => {
      try {
        const options = ["text", "textarea"].includes(f.kind) ? null : f.options || [];
        const a = await api("/api/answer", "POST", { question: f.label, job_id: state.job?.id, kind: f.kind, options,
          draft: f.required && !options, required: f.required });
        const ok = a.answer ? await fill(f, a.answer) : false;
        if (ok === "manual") {
          state.combos.push({ key: f.els[0].dataset.copilotId, label: f.label, value: a.answer, required: f.required, search: false });
          mark(f, "#1a73e8", `choose: ${a.answer}`);
          return;
        }
        if (ok) {
          const item = { label: f.label, value: a.answer, source: a.source, checked: !!a.checked };
          if (a.source === "draft") {
            state.drafts.push(item);
            mark(f, "#f9ab00", a.checked ? "AI answer, fact-checked against your resume: read it before submitting"
                                         : `AI draft: ${a.note || "review before submitting"}`);
          }
          else state.filled.push(item);
          return;
        }
        if (f.required) { state.needs.push(f.label); mark(f, "#d93025", a.note || "needs your answer"); }
      } catch (e) {
        if (f.required) { state.needs.push(f.label); mark(f, "#d93025", "needs your answer"); }
        state.errors.push(`${f.label}: ${e.message}`);
      }
      publish();
    }));
    state.captcha = visibleChallenge();
    const filledLabels = new Set(state.filled.concat(state.drafts).map((f) => f.label));
    state.needs = [...new Set(state.needs)].filter((l) => !filledLabels.has(l));
    state.done = true;
    publish();
  }

  // Every ATS carries an invisible reCAPTCHA badge; only a challenge you can see counts.
  function visibleChallenge() {
    return [...document.querySelectorAll("iframe[src*='recaptcha'], iframe[src*='hcaptcha'], iframe[src*='turnstile']")]
      .some((f) => {
        const r = f.getBoundingClientRect();
        return r.width > 100 && r.height > 60 && !/size=invisible/.test(f.src) && getComputedStyle(f).visibility !== "hidden";
      });
  }
  window.__copilotChallenge = visibleChallenge;

  // ---------------- noticing the submission ----------------
  const CONFIRMED = /thank you for (applying|your (application|interest))|application (has been |was )?(submitted|received)|we.?ve received your application|successfully submitted/i;
  const confirmed = () => CONFIRMED.test(document.body.innerText.slice(0, 5000)) && !document.querySelector("input[type='file']");

  function answersToSave() {
    // Everything you submitted is approved: AI drafts you left in place, and your own answers.
    return readForm().filter((f) => isQuestion(f) && ["text", "textarea"].includes(f.kind) && f.els[0].value.trim())
      .map((f) => ({ question: f.label, answer: f.els[0].value.trim(), source: "you" }));
  }

  async function reportSubmitted(pending) {
    await api("/api/applied", "POST", { ...pending, outcome: "submitted", mode: "extension" });
    await bg({ type: "pending-set", pending: null });
    state.submitted = true;
    publish();
  }

  function watchSubmit() {
    document.addEventListener("click", async (e) => {
      const b = e.target.closest("button, input[type='submit']");
      if (!b || b.closest("#copilot-panel") || !/submit|apply/i.test(b.innerText || b.value || "")) return;
      const pending = { job_id: state.job?.id, url: location.href, ats: ATS, at: Date.now(),
        filled: state.filled.concat(state.drafts), unfilled: state.needs, answers: answersToSave() };
      await bg({ type: "pending-set", pending });
      for (let i = 0; i < 40; i++) {           // single-page forms confirm without a reload
        await sleep(500);
        if (confirmed()) return reportSubmitted(pending);
      }
    }, true);
  }

  async function checkPending() {
    const pending = await bg({ type: "pending-get" });
    if (pending && Date.now() - pending.at < 10 * 60 * 1000 && confirmed()) await reportSubmitted(pending);
  }

  // ---------------- the panel ----------------
  let shadow;
  function renderPanel() {
    if (window.__copilotBridge) return;     // the runner has no one to show it to
    if (!shadow) {
      const box = document.createElement("div");
      box.id = "copilot-panel";
      box.style.cssText = "position:fixed;right:16px;bottom:16px;z-index:2147483647";
      document.body.appendChild(box);
      shadow = box.attachShadow({ mode: "open" });
    }
    const j = state.job;
    const list = (items, color) => items.map((x) => `<li style="color:${color}">${esc(typeof x === "string" ? x : x.label)}</li>`).join("");
    shadow.innerHTML = `
      <style>
        .p{font:13px/1.4 system-ui,sans-serif;background:#fff;color:#202124;border:1px solid #dadce0;border-radius:10px;
           box-shadow:0 4px 16px rgba(0,0,0,.18);width:300px;max-height:60vh;overflow:auto;padding:12px}
        h3{margin:0 0 6px;font-size:14px} ul{margin:4px 0 8px 16px;padding:0} .m{color:#5f6368;font-size:12px}
        button{font:inherit;border:1px solid #1a73e8;background:#1a73e8;color:#fff;border-radius:6px;padding:5px 10px;cursor:pointer;margin-right:6px}
        button.s{background:#fff;color:#1a73e8} .x{float:right;border:0;background:none;color:#5f6368;padding:0;margin:0}
      </style>
      <div class="p">
        <button class="x" id="close">×</button>
        <h3>Job Copilot</h3>
        <div class="m">${j ? `${esc(j.company)}: ${esc(j.title)}${j.fit ? ` · fit ${j.fit}` : ""}` : "Not in your job DB: profile fields only."}</div>
        ${j && j.flags && j.flags.length ? `<div class="m" style="color:#b06000">Documents have ${j.flags.length} review flag(s) in the app.</div>` : ""}
        ${state.submitted ? `<p style="color:#188038"><b>Submitted and marked Applied.</b></p>` : ""}
        ${state.done ? `<p>Filled <b>${state.filled.length}</b> fields. Review, then submit yourself.</p>` : "<p>Filling...</p>"}
        ${state.drafts.length ? `<b>AI drafts to review (${state.drafts.length})</b><ul>${list(state.drafts, "#b06000")}</ul>` : ""}
        ${state.combos.length ? `<b>Pick in the dropdown (${state.combos.length})</b><ul>${state.combos.map((c) =>
          `<li style="color:#1a73e8">${esc(c.label)}: <b>${esc(c.value)}</b></li>`).join("")}</ul>` : ""}
        ${state.needs.length ? `<b>Needs you (${state.needs.length})</b><ul>${list(state.needs, "#d93025")}</ul>` : ""}
        ${state.captcha ? `<p class="m">This form has a CAPTCHA: solve it yourself.</p>` : ""}
        ${state.errors.length ? `<ul>${list(state.errors, "#5f6368")}</ul>` : ""}
        <button id="fill">Fill again</button><button class="s" id="applied">Mark applied</button>
      </div>`;
    shadow.getElementById("fill").onclick = () => run();
    shadow.getElementById("close").onclick = () => shadow.host.remove();
    shadow.getElementById("applied").onclick = () => reportSubmitted({ job_id: state.job?.id, url: location.href, ats: ATS,
      filled: state.filled.concat(state.drafts), unfilled: state.needs, answers: answersToSave(), note: "marked by hand" });
  }
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  // ---------------- start ----------------
  async function start() {
    await checkPending();
    if (state.submitted) return;
    for (let i = 0; i < 30; i++) {         // forms render late on single-page sites
      if (readForm().filter((f) => f.kind !== "file").length >= 3) break;
      await sleep(500);
    }
    if (readForm().length < 3) return;
    watchSubmit();
    const settings = (await bg({ type: "settings" })) || { autofill: true };
    if (settings.autofill) await run(); else publish();
  }
  window.__copilotRun = run;      // the runner calls this directly
  window.__copilotRead = () => readForm().map((f) => ({ label: f.label, kind: f.kind, required: f.required, options: f.options,
    id: f.els[0].id, name: f.els[0].name }));
  start();
})();
