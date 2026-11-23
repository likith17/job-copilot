// Job Copilot Autofill: background.
//
// When the app (or `python -m copilot serve`) is running, requests go to the local API
// with your token; a page's own scripts can't call it, they don't have the token.
// The extension also keeps its own copy of what filling needs (the "vault": your
// profile, saved answers and resume files), refreshed whenever the app is reachable,
// so it fills pages with the app closed. The vault stays in this browser's extension
// storage on this computer.
const DEFAULTS = { token: "", port: 8765, autofill: true, suggest: true, resume: "" };
const settings = () => chrome.storage.local.get(DEFAULTS);
const getVault = async () => (await chrome.storage.local.get({ vault: null })).vault;

function toBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

class Offline extends Error {}

async function callApi(s, path, method = "GET", body = null, binary = false, timeoutMs = 2500) {
  if (!s.token) throw new Offline("no token set: open the extension's settings");
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  let res;
  try {
    res = await fetch(`http://127.0.0.1:${s.port}${path}`, {
      method, signal: ctl.signal,
      headers: { "X-Copilot-Token": s.token, "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (e) {
    throw new Offline("the app is closed");
  } finally {
    clearTimeout(timer);
  }
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || `HTTP ${res.status}`);
  if (!binary) return res.json();
  const name = (res.headers.get("Content-Disposition") || "").match(/filename="(.+)"/)?.[1] || "document";
  return { base64: toBase64(await res.arrayBuffer()), type: res.headers.get("Content-Type"), name };
}

// ---------------- the vault ----------------
async function sync() {
  const s = await settings();
  // The first sync compiles your resumes to PDF, which can take a minute.
  const vault = await callApi(s, "/api/vault", "GET", null, false, 180000);
  await chrome.storage.local.set({ vault });
  await flushOutbox(s);
  return { ok: true, synced_at: vault.synced_at, answers: vault.answers.length,
           files: vault.files.filter((f) => f.base64).map((f) => f.label) };
}

// Same normalisation as the app (db.question_key), so a saved answer matches here too.
const STOP = new Set(["please", "the", "a", "an", "your", "you", "do", "are"]);
const qkey = (q) => (q || "").toLowerCase().replace(/[^a-z0-9 ]/g, " ").split(/\s+/).filter((w) => w && !STOP.has(w)).join(" ");

function similarity(a, b) {          // 2·LCS / (|a| + |b|), close to Python's SequenceMatcher ratio
  if (!a.length || !b.length) return 0;
  if (a.length * b.length > 90000) return a === b ? 1 : 0;
  let prev = new Array(b.length + 1).fill(0);
  for (let i = 1; i <= a.length; i++) {
    const cur = [0];
    for (let j = 1; j <= b.length; j++) cur[j] = a[i - 1] === b[j - 1] ? prev[j - 1] + 1 : Math.max(prev[j], cur[j - 1]);
    prev = cur;
  }
  return (2 * prev[b.length]) / (a.length + b.length);
}

async function bankAnswer(question) {
  const vault = await getVault();
  if (!vault) return null;
  const key = qkey(question);
  let best = null, bestSim = 0;
  for (const a of vault.answers) {
    const sim = a.qkey === key ? 1 : similarity(a.qkey, key);
    if (sim > bestSim) { best = a; bestSim = sim; }
  }
  return best && bestSim >= 0.88 ? best : null;
}

// Reports made while the app was closed wait here and go out on the next sync.
async function queue(path, body) {
  const { outbox } = await chrome.storage.local.get({ outbox: [] });
  outbox.push({ path, body });
  await chrome.storage.local.set({ outbox });
}

async function flushOutbox(s) {
  const { outbox } = await chrome.storage.local.get({ outbox: [] });
  const left = [];
  for (const item of outbox) {
    try { await callApi(s, item.path, "POST", item.body); } catch (e) { left.push(item); }
  }
  await chrome.storage.local.set({ outbox: left });
}

// What the API would have answered, from the vault, while the app is closed.
async function offline(msg) {
  const vault = await getVault();
  const path = msg.path.split("?")[0];
  if (path === "/api/ping") throw new Error("the app is closed");
  if (!vault) throw new Error("the app is closed and nothing is saved yet: start the app once and press Sync in the extension");
  if (path === "/api/profile") return { ...vault.profile, _offline: vault.synced_at };
  if (path === "/api/job") return { job: null, offline: true };
  if (path === "/api/answer") {
    const saved = await bankAnswer(msg.body?.question);
    if (saved) return saved.answer.trim() ? { answer: saved.answer, source: "bank", note: "your saved answer" }
                                          : { answer: "", source: "", note: "you chose to skip this question" };
    return { answer: "", source: "", note: "the app is closed: only your saved answers are available" };
  }
  if (path === "/api/applied" || path === "/api/answers") { await queue(path, msg.body); return { ok: true, queued: true }; }
  throw new Error("the app is closed");
}

async function vaultFile(label) {
  const vault = await getVault();
  const files = (vault?.files || []).filter((f) => f.base64);
  const s = await settings();
  const pick = files.find((f) => f.label === (label || s.resume)) || files[0];
  return pick ? { base64: pick.base64, type: pick.type, name: pick.name, label: pick.label } : null;
}

// ---------------- messages ----------------
async function handle(msg, sender) {
  const s = await settings();
  switch (msg.type) {
    case "settings": return s;
    case "pending-set": return chrome.storage.local.set({ pending: msg.pending });
    case "pending-get": return (await chrome.storage.local.get({ pending: null })).pending;
    case "sync": return sync();
    case "vault-info": {
      const v = await getVault();
      const { outbox } = await chrome.storage.local.get({ outbox: [] });
      return v ? { synced_at: v.synced_at, answers: v.answers.length, name: v.profile?.name,
                   files: v.files.filter((f) => f.base64).map((f) => ({ label: f.label, roles: f.roles })),
                   outbox: outbox.length } : { outbox: outbox.length };
    }
    case "vault-profile": return (await getVault())?.profile || null;
    case "vault-file": return vaultFile(msg.label);
    case "bank": return bankAnswer(msg.question);
    case "fill-tab": return fillTab(msg.tabId);
    case "api":
      try {
        return await callApi(s, msg.path, msg.method || "GET", msg.body, msg.binary);
      } catch (e) {
        if (e instanceof Offline) return offline(msg);
        throw e;
      }
  }
  throw new Error(`unknown message ${msg.type}`);
}

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  handle(msg, sender).then(reply, (e) => reply({ error: String(e.message || e) }));
  return true;
});

// ---------------- fill on demand: toolbar, shortcut, right-click ----------------
async function fillTab(tabId) {
  if (!tabId) [{ id: tabId }] = await chrome.tabs.query({ active: true, currentWindow: true });
  try {
    await chrome.tabs.sendMessage(tabId, { type: "run" });
  } catch (e) {       // a tab opened before the extension was installed: add the script now
    await chrome.scripting.executeScript({ target: { tabId, allFrames: true }, files: ["content.js"] });
    await chrome.tabs.sendMessage(tabId, { type: "run" });
  }
  return { ok: true };
}

chrome.commands.onCommand.addListener((command, tab) => {
  if (command === "fill-page") fillTab(tab?.id).catch(() => {});
});

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({ id: "fill-page", title: "Fill this page with Job Copilot", contexts: ["page", "editable"] });
  chrome.alarms.create("sync", { periodInMinutes: 180 });
  sync().catch(() => {});
});

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId === "fill-page") fillTab(tab?.id).catch(() => {});
});

// Refresh the saved copy whenever the app happens to be running.
chrome.alarms.onAlarm.addListener((a) => { if (a.name === "sync") sync().catch(() => {}); });
chrome.runtime.onStartup.addListener(() => { sync().catch(() => {}); });
