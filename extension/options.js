const $ = (id) => document.getElementById(id);

chrome.storage.local.get({ token: "", port: 8765, autofill: true }, (s) => {
  $("token").value = s.token;
  $("port").value = s.port;
  $("autofill").checked = s.autofill;
});

$("save").onclick = async () => {
  const s = { token: $("token").value.trim(), port: Number($("port").value) || 8765, autofill: $("autofill").checked };
  await chrome.storage.local.set(s);
  chrome.runtime.sendMessage({ type: "api", path: "/api/ping" }, (res) => {
    $("status").textContent = res && res.ok ? "Connected to Job Copilot." : `Not connected: ${res?.error || "is the app running?"}`;
    $("status").style.color = res && res.ok ? "#188038" : "#d93025";
  });
};
