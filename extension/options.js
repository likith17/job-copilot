const $ = (id) => document.getElementById(id);

chrome.storage.local.get({ token: "", port: 8765, autofill: true, suggest: true }, (s) => {
  $("token").value = s.token;
  $("port").value = s.port;
  $("autofill").checked = s.autofill;
  $("suggest").checked = s.suggest;
});

$("save").onclick = async () => {
  await chrome.storage.local.set({ token: $("token").value.trim(), port: Number($("port").value) || 8765,
                                   autofill: $("autofill").checked, suggest: $("suggest").checked });
  $("status").textContent = "Saved. Syncing your data from the app...";
  chrome.runtime.sendMessage({ type: "sync" }, (res) => {
    const ok = res && res.ok;
    $("status").textContent = ok
      ? `Connected. Saved ${res.answers} answers and ${res.files.length} resumes for offline use.`
      : `Settings saved, but the app isn't reachable (${res?.error || "is it running?"}). The extension keeps using its last saved copy.`;
    $("status").style.color = ok ? "#188038" : "#d93025";
  });
};
