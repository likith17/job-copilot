const $ = (id) => document.getElementById(id);
const send = (msg) => new Promise((r) => chrome.runtime.sendMessage(msg, r));
const when = (iso) => iso ? new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "never";

function status(text, cls = "") {
  $("status").textContent = text;
  $("status").className = `m ${cls}`;
}

async function refresh() {
  const info = await send({ type: "vault-info" });
  const { resume } = await chrome.storage.local.get({ resume: "" });
  const files = info?.files || [];
  $("resume").innerHTML = files.length
    ? files.map((f) => `<option${f.label === resume ? " selected" : ""}>${f.label.replace(/</g, "&lt;")}</option>`).join("")
    : "<option>(none saved yet: press Sync)</option>";
  $("resume").disabled = !files.length;
  if (!info?.synced_at) return status("Nothing saved yet. Start the app once, then press Sync from app.", "bad");
  status(`Saved copy from ${when(info.synced_at)}: your profile, ${info.answers} saved answers, ${files.length} resumes.`
    + (info.outbox ? ` ${info.outbox} report(s) waiting for the app.` : ""), "ok");
}

$("fill").onclick = async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const res = await send({ type: "fill-tab", tabId: tab.id });
  if (res?.error) return status(`Can't fill this page: ${res.error}`, "bad");
  window.close();
};
$("resume").onchange = () => chrome.storage.local.set({ resume: $("resume").value });
$("settings").onclick = () => chrome.runtime.openOptionsPage();
$("sync").onclick = async () => {
  status("Syncing (the first time compiles your resumes, up to a minute)...");
  const res = await send({ type: "sync" });
  if (res?.error) return status(`Sync failed: ${res.error}. Is the app running?`, "bad");
  await refresh();
};
refresh();
