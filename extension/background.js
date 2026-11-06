// Relays the content script's requests to the local copilot API with your token.
// (A page's own scripts can't call it: they don't have the token.)
const DEFAULTS = { token: "", port: 8765, autofill: true };

function toBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

async function handle(msg) {
  const settings = await chrome.storage.local.get(DEFAULTS);
  if (msg.type === "settings") return settings;
  if (msg.type === "pending-set") return chrome.storage.local.set({ pending: msg.pending });
  if (msg.type === "pending-get") return (await chrome.storage.local.get({ pending: null })).pending;
  if (!settings.token) throw new Error("no token set: open the extension's options");
  const res = await fetch(`http://127.0.0.1:${settings.port}${msg.path}`, {
    method: msg.method || "GET",
    headers: { "X-Copilot-Token": settings.token, "Content-Type": "application/json" },
    body: msg.body ? JSON.stringify(msg.body) : undefined,
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || `HTTP ${res.status}`);
  if (!msg.binary) return res.json();
  const name = (res.headers.get("Content-Disposition") || "").match(/filename="(.+)"/)?.[1] || "document";
  return { base64: toBase64(await res.arrayBuffer()), type: res.headers.get("Content-Type"), name };
}

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  handle(msg).then(reply, (e) => reply({ error: String(e.message || e) }));
  return true;
});
