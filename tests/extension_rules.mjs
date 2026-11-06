// Which profile rule each common form question hits (run by tests/test_apply.py).
import { readFileSync } from "node:fs";

const src = readFileSync(new URL("../extension/content.js", import.meta.url), "utf8");
const start = src.indexOf("const RULES = [");
const block = src.slice(start, src.indexOf("];", start) + 2).replace("const RULES", "globalThis.RULES");
const yn = (v) => (v === true ? "Yes" : v === false ? "No" : undefined);
eval(block);

const profile = {
  first_name: "A", last_name: "B", name: "A B", email: "a@b.c", how_did_you_hear: "Careers page",
  address: { postal_code: "13902" }, education: [{ school: "Binghamton University" }], linkedin: "li",
  work_authorization: { authorized_us: true, requires_sponsorship: true, visa_status: "F-1 OPT" },
  eeo: { race: "Decline", sexual_orientation: "Decline", gender: "Decline" }, previously_employed_at: [],
  referral: { name: "Jane Roe", email: "jane@acme.com" }, certifications: [{ name: "AWS ML Specialty" }],
  languages: ["English", "Hindi"], clearance: "None",
};
const cases = {
  "Tell us how you heard about Zipline and this job!": "Careers page",
  "Zip Code": "13902",
  "Postal code": "13902",
  "Which university do you attend?": "Binghamton University",
  "Will you now or in the future require visa sponsorship?": "Yes",
  "Please provide details on your current work authorization status": "F-1 OPT",
  "Are you legally authorized to work in the United States?": "Yes",
  "LinkedIn Profile": "li",
  "Race/Ethnicity": "Decline",
  "Do you identify as LGBTQIA+?": "Decline",
  "Are you an employee of Acme (Currently or Previously)?": "No",
  "Preferred First Name": "A",
  "Email": "a@b.c",
  "Referrer's email": "jane@acme.com",
  "Referral name": "Jane Roe",
  "Who referred you?": "Jane Roe",
  "Were you referred by a current employee?": "Yes",
  "Do you hold any certifications?": "AWS ML Specialty",
  "Which languages do you speak fluently?": "English, Hindi",
  "Do you have an active security clearance?": "None",
};
let failed = 0;
for (const [label, want] of Object.entries(cases)) {
  const rule = RULES.find(([re]) => re.test(label));
  const got = rule ? rule[1](profile, { company: "Acme" }) : undefined;
  if (got !== want) { failed++; console.log(`WRONG  ${label}  ->  ${got}  (rule ${rule && rule[0]})`); }
}
console.log(failed ? `${failed} wrong` : "ok");
process.exit(failed ? 1 : 0);
