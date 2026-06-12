/* Oceano web client */
const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const api = (p, o) => fetch(p, o).then(r => { if (r.status === 401) { showLogin(); throw new Error("unauthorized"); } return r.json(); });
const escapeHtml = s => (s || "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

/* themed confirm dialog — returns a Promise<boolean> */
function confirmAction(title, msg, okLabel = "Delete") {
  return new Promise(resolve => {
    $("#confirmTitle").textContent = title;
    $("#confirmMsg").textContent = msg || "";
    $("#confirmOk").textContent = okLabel;
    $("#confirmBox").classList.add("open"); $("#confirmScrim").classList.add("open");
    const close = v => {
      $("#confirmBox").classList.remove("open"); $("#confirmScrim").classList.remove("open");
      document.removeEventListener("keydown", onKey);
      resolve(v);
    };
    const onKey = e => { if (e.key === "Escape") close(false); else if (e.key === "Enter") close(true); };
    $("#confirmOk").onclick = () => close(true);
    $("#confirmCancel").onclick = () => close(false);
    $("#confirmScrim").onclick = () => close(false);
    document.addEventListener("keydown", onKey);
  });
}

/* themed text-input dialog — replaces window.prompt. Resolves to the string, or null
   if cancelled. opts: {value, placeholder, okLabel, message}. */
function promptDialog(title, opts = {}) {
  return new Promise(resolve => {
    const box = $("#promptBox"), input = $("#promptInput"), msg = $("#promptMsg");
    $("#promptTitle").textContent = title;
    if (opts.message) { msg.textContent = opts.message; msg.style.display = "block"; } else { msg.style.display = "none"; }
    input.value = opts.value || "";
    input.placeholder = opts.placeholder || "";
    $("#promptOk").textContent = opts.okLabel || "OK";
    box.classList.add("open"); $("#promptScrim").classList.add("open");
    const close = v => {
      box.classList.remove("open"); $("#promptScrim").classList.remove("open");
      document.removeEventListener("keydown", onKey);
      input.onkeydown = null;
      resolve(v);
    };
    const submit = () => close(input.value.trim());   // "" on empty-OK; null only on cancel/escape
    const onKey = e => { if (e.key === "Escape") { e.stopPropagation(); close(null); } };
    input.onkeydown = e => { if (e.key === "Enter") { e.preventDefault(); submit(); } };
    $("#promptOk").onclick = submit;
    $("#promptCancel").onclick = () => close(null);
    $("#promptScrim").onclick = () => close(null);
    document.addEventListener("keydown", onKey);
    // focus + select the suggested value so editing/replacing is one keystroke
    setTimeout(() => { input.focus(); input.select(); }, 30);
  });
}

/* transient, non-blocking notification (replaces alert()) */
function toast(msg, kind = "info") {
  let host = $("#toastHost");
  if (!host) { host = document.createElement("div"); host.id = "toastHost"; document.body.appendChild(host); }
  const t = document.createElement("div");
  t.className = "toast " + kind;
  t.textContent = msg;
  host.appendChild(t);
  requestAnimationFrame(() => t.classList.add("show"));
  setTimeout(() => { t.classList.remove("show"); setTimeout(() => t.remove(), 250); }, 3200);
}

const state = { session: null, model: null, baseUrl: null, agent: false, models: [], busy: false, view: "chat", cwd: "", file: null };
let _chatAbort = null;   // AbortController for the in-flight /api/chat stream

/* ---------------- markdown (sanitized) ---------------- */
function renderMD(el, text, highlight = false) {
  const html = marked.parse(text || "");
  el.innerHTML = window.DOMPurify ? DOMPurify.sanitize(html) : escapeHtml(html);
  if (highlight) $$("pre code", el).forEach(b => { try { hljs.highlightElement(b); } catch {} });
  $$("img", el).forEach(img => {                       // images: resolve workspace paths, make savable
    const src = img.getAttribute("src") || "";
    if (!/^(https?:|data:|\/api\/raw)/.test(src)) img.src = "/api/raw?path=" + encodeURIComponent(src.replace(/^\.?\/*/, ""));
    img.classList.add("chat-img"); img.loading = "lazy";
    img.addEventListener("click", () => window.open(img.src, "_blank"));
  });
}

/* ================= CHAT (persisted server-side in dated folders) ================= */
const LS = {   // legacy localStorage — read once, only to migrate old browser chats to Oceano
  index: () => { try { return JSON.parse(localStorage.getItem("oceano.sessions") || "[]"); } catch { return []; } },
  transcript: id => { try { return JSON.parse(localStorage.getItem("oceano.t." + id) || "[]"); } catch { return []; } },
};
const uid = () => "v" + Math.random().toString(36).slice(2, 9);
const toBottom = () => { const t = $("#thread"); if (t) t.scrollTop = t.scrollHeight; };

let _chats = [];          // session metas from the server: [{id,title,date,updated,count}]
let _curT = [];           // the open chat's transcript (in memory; saved at the end of each turn)
let _curTitle = "New voyage";

async function loadChats() {
  try { _chats = (await api("/api/chats")).chats || []; } catch { _chats = []; }
  renderSessions();
}
function newVoyage() {
  setView("chat");
  state.session = uid(); _curT = []; _curTitle = "New voyage";
  localStorage.setItem("oceano.active", state.session);
  const thread = $("#thread"); thread.innerHTML = ""; thread.appendChild(welcomeNode());
  renderSessions(); $("#input").focus();
}
async function openVoyage(id) {
  setView("chat");
  state.session = id; localStorage.setItem("oceano.active", id);
  let data; try { data = await api("/api/chats/" + encodeURIComponent(id)); } catch { data = { messages: [] }; }
  _curT = data.messages || []; _curTitle = data.title || "New voyage";
  const thread = $("#thread"); thread.innerHTML = "";
  if (!_curT.length) thread.appendChild(welcomeNode());
  else _curT.forEach(m => {
    if (m.role === "user") addUser(m.content, false);
    else if (m.role === "thinking") { const c = addThinkCard(); appendThink(c, m.text); finalizeThink(c); }
    else if (m.role === "tool") fillTool(addTool(m.name, m.args), m.result);
    else if (m.role === "tools") m.items.forEach(it => fillTool(addTool(it.name, it.args), it.result));  // old format
    else { const bb = addAssistant(m.content, true); if (m.meta) renderMeta(bb, m.meta); }
  });
  renderSessions(); $("#input").focus();
}
function _fmtChatDate(d) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(d)) return d;
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const diff = Math.round((today - new Date(d + "T00:00")) / 86400000);
  if (diff === 0) return "Today"; if (diff === 1) return "Yesterday";
  return new Date(d + "T00:00").toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}
function renderSessions() {
  const box = $("#sessions"); if (!box) return; box.innerHTML = "";
  const groups = {};                               // group by date → dated folders
  _chats.forEach(s => (groups[s.date || "—"] ||= []).push(s));
  Object.keys(groups).sort().reverse().forEach(date => {
    const h = document.createElement("div"); h.className = "s-date"; h.textContent = _fmtChatDate(date);
    box.appendChild(h);
    groups[date].forEach(s => {
      const el = document.createElement("div");
      el.className = "session" + (s.id === state.session ? " active" : "");
      el.innerHTML = `<span class="s-title"></span><button class="s-del" title="delete voyage">✕</button>`;
      $(".s-title", el).textContent = s.title;
      el.onclick = () => openVoyage(s.id);
      $(".s-del", el).onclick = e => { e.stopPropagation(); deleteVoyage(s.id); };
      box.appendChild(el);
    });
  });
  if (state.session && !_chats.some(s => s.id === state.session)) {   // brand-new chat, not yet saved
    const el = document.createElement("div"); el.className = "session active";
    el.innerHTML = `<span class="s-title"></span>`; $(".s-title", el).textContent = _curTitle;
    box.insertBefore(el, box.firstChild);
  }
}
async function deleteVoyage(id) {
  const s = _chats.find(x => x.id === id);
  if (!await confirmAction("Delete voyage?", `“${s?.title || "this chat"}” and its history will be permanently removed.`)) return;
  await fetch("/api/chats/" + encodeURIComponent(id), { method: "DELETE" }).catch(() => {});
  _chats = _chats.filter(x => x.id !== id);
  if (state.session === id) { if (_chats.length) openVoyage(_chats[0].id); else newVoyage(); }
  else renderSessions();
}
function touchTitle(text) {
  if (_curTitle === "New voyage" && text) { _curTitle = text.slice(0, 38); renderSessions(); }
}
function appendT(entry) { _curT.push(entry); }      // in memory during a turn; persistChat() writes it
async function persistChat() {
  if (!state.session || !_curT.length) return;
  try {
    await fetch("/api/chats/" + encodeURIComponent(state.session), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: _curTitle, messages: _curT }),
    });
  } catch {}
  const ex = _chats.find(s => s.id === state.session), iso = new Date().toISOString();
  if (ex) { ex.title = _curTitle; ex.count = _curT.length; ex.updated = iso; }
  else { _chats.unshift({ id: state.session, title: _curTitle, date: iso.slice(0, 10), updated: iso, count: _curT.length }); }
  renderSessions();
}
async function migrateLocalChats() {               // one-time: lift old browser chats into Oceano
  if (localStorage.getItem("oceano.migrated")) return;
  const old = LS.index();
  if (old.length && !_chats.length) {
    for (const s of old) {
      const t = LS.transcript(s.id);
      if (t.length) await fetch("/api/chats/" + encodeURIComponent(s.id), {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: s.title || "Imported chat", messages: t }),
      }).catch(() => {});
    }
    await loadChats();
  }
  localStorage.setItem("oceano.migrated", "1");
}

function welcomeNode() {
  const w = document.createElement("div");
  w.className = "welcome";
  w.innerHTML = `<div class="welcome-orb"></div><h2>Chart a course.</h2>
    <p>Ask anything, or flip on <b>Agent</b> to let Oceano use its tools — the workspace, the web, memory, your docs.</p>
    <div class="suggests">
      <button data-q="Summarize what you can do in 3 bullets.">What can you do?</button>
      <button data-q="Search the web for the latest on local LLM agents and give me 3 takeaways." data-agent="1">Research a topic ⚲</button>
      <button data-q="Write a haiku about the abyss and save it to workspace as abyss.txt" data-agent="1">Write to workspace ✎</button>
    </div>`;
  w.querySelectorAll(".suggests button").forEach(b => b.onclick = () => {
    if (b.dataset.agent) { state.agent = true; $("#agentToggle").checked = true; }
    $("#input").value = b.dataset.q; send();
  });
  return w;
}
const clearWelcome = () => { const w = $(".welcome"); if (w) w.remove(); };

function addUser(text, scroll = true) {
  clearWelcome();
  const el = document.createElement("div"); el.className = "msg user";
  const b = document.createElement("div"); b.className = "bubble"; b.textContent = text;
  el.appendChild(b); $("#thread").appendChild(el); if (scroll) toBottom(); return el;
}
function addThinking() {
  clearWelcome();
  const el = document.createElement("div"); el.className = "msg assistant thinking";
  el.innerHTML = `<div class="who"><span class="orb"></span><span>Oceano</span></div>
    <div class="bubble"><span class="sounding"><i></i><i></i><i></i></span></div>`;
  $("#thread").appendChild(el); toBottom(); return el;
}
function addAssistant(text = "", done = false) {
  clearWelcome();
  const el = document.createElement("div"); el.className = "msg assistant";
  el.innerHTML = `<div class="who"><span class="orb"></span><span>Oceano</span></div><div class="bubble"></div>`;
  renderMD($(".bubble", el), text, done);
  $("#thread").appendChild(el); toBottom(); return $(".bubble", el);
}
function renderMeta(bubble, s) {
  const msg = bubble.closest(".msg"); if (!msg) return;
  let meta = $(".msg-meta", msg);
  if (!meta) { meta = document.createElement("div"); meta.className = "msg-meta"; msg.appendChild(meta); }
  const parts = [];
  if (s.model) parts.push(s.model);
  if (s.tokens) parts.push(`${s.tokens} tok`);
  if (s.tok_s) parts.push(`${s.tok_s} tok/s`);
  meta.textContent = parts.join("  ·  ");
}
function addTool(name, args) {
  clearWelcome();
  const el = document.createElement("div"); el.className = "tool";
  el.innerHTML = `<div class="tool-card"><div class="th"><span class="sig"></span>
      <span class="name">${escapeHtml(name)}</span><span class="args">${escapeHtml(args || "")}</span>
      <span class="tstat run">running</span></div><div class="result"></div></div>`;
  const card = $(".tool-card", el);
  $(".th", card).onclick = () => card.classList.toggle("open");
  $("#thread").appendChild(el); toBottom(); return card;
}
function fillTool(card, result) {
  if (!card) return;
  $(".result", card).textContent = result;
  const st = $(".tstat", card); st.classList.remove("run"); st.textContent = "▾ result";
}
function addThinkCard() {
  clearWelcome();
  const el = document.createElement("div"); el.className = "think";
  el.innerHTML = `<div class="think-card"><div class="th2"><span class="tk-ic">✦</span>
      <span class="tk-label">Thinking</span><span class="tk-stat run">thinking…</span>
      <span class="tk-caret">▾</span></div><div class="tk-body"></div></div>`;
  const card = $(".think-card", el);
  $(".th2", card).onclick = () => card.classList.toggle("open");
  $("#thread").appendChild(el); toBottom(); return card;
}
function appendThink(card, text) {
  if (!card) return;
  const body = $(".tk-body", card);
  const stick = body.scrollHeight - body.scrollTop - body.clientHeight < 24;  // only follow if near bottom
  body.textContent += text;
  if (stick) body.scrollTop = body.scrollHeight;   // autoscroll the thinking box as it streams
  toBottom();
}
function finalizeThink(card) { if (!card) return; const st = $(".tk-stat", card); st.classList.remove("run"); st.textContent = ""; }

async function send() {
  const input = $("#input"), text = input.value.trim();
  if (!text || state.busy || !state.model) { if (!state.model) flashModel(); return; }
  state.busy = true; setSendMode(true);
  $("#send").classList.add("ping"); setTimeout(() => $("#send").classList.remove("ping"), 600);
  input.value = ""; autosize(input);
  addUser(text); touchTitle(text); appendT({ role: "user", content: text });

  const payload = { session: state.session, message: text, model: state.model, base_url: state.baseUrl, agent_mode: state.agent };
  let sounding = addThinking(), bubble = null, acc = "", thinkCard = null, thinkText = "", lastCard = null, lastTool = null, rafP = false, stats = null, livePopped = false;
  const killSounding = () => { if (sounding) { sounding.remove(); sounding = null; } };
  const draw = () => { rafP = false; if (bubble) renderMD(bubble, acc + " ▌"); };
  const flushThink = () => { if (thinkCard) { finalizeThink(thinkCard); appendT({ role: "thinking", text: thinkText }); thinkCard = null; thinkText = ""; } };
  // close the current answer bubble so the next segment (tool/think) doesn't slot UNDER it
  const flushBubble = () => { if (bubble) { renderMD(bubble, acc, true); appendT({ role: "assistant", content: acc }); bubble = null; acc = ""; } };

  _chatAbort = new AbortController();
  try {
    const resp = await fetch("/api/chat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload), signal: _chatAbort.signal });
    const reader = resp.body.getReader(), dec = new TextDecoder(); let buf = "";
    while (true) {
      const { value, done } = await reader.read(); if (done) break;
      buf += dec.decode(value, { stream: true }); let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const line = buf.slice(0, i); buf = buf.slice(i + 2);
        if (!line.startsWith("data: ")) continue;
        let ev; try { ev = JSON.parse(line.slice(6)); } catch { continue; }
        if (ev.type === "reasoning") {
          killSounding(); flushBubble();
          if (!thinkCard) thinkCard = addThinkCard();
          thinkText += ev.text; appendThink(thinkCard, ev.text);
        } else if (ev.type === "token") {
          killSounding(); flushThink();
          if (!bubble) bubble = addAssistant("");
          acc += ev.text; if (!rafP) { rafP = true; requestAnimationFrame(draw); } toBottom();
        } else if (ev.type === "tool_call") {
          killSounding(); flushThink(); flushBubble();
          if (!livePopped && /^(fetch_url|browser_)/.test(ev.name)) { openLiveView(); livePopped = true; }  // pop the Live view when it starts browsing
          lastCard = addTool(ev.name, ev.args); lastTool = { name: ev.name, args: ev.args };
        } else if (ev.type === "tool_result") {
          fillTool(lastCard, ev.result);
          if (lastTool) { appendT({ role: "tool", name: lastTool.name, args: lastTool.args, result: ev.result }); lastTool = null; }
          sounding = addThinking();                        // keep a "working" cue during the next step
        } else if (ev.type === "answer_done") {
          killSounding(); flushThink(); if (bubble) renderMD(bubble, acc, true);
        } else if (ev.type === "answer") {                 // fallback (max steps)
          killSounding(); flushThink(); if (!bubble) bubble = addAssistant(""); acc = ev.text; renderMD(bubble, acc, true); toBottom();
        } else if (ev.type === "stats") {
          stats = ev;
        } else if (ev.type === "error") {
          killSounding(); flushThink(); if (!bubble) bubble = addAssistant(""); acc = "⚠️ " + ev.message; renderMD(bubble, acc);
        }
      }
    }
    killSounding(); flushThink(); if (bubble) renderMD(bubble, acc, true);
  } catch (e) {
    killSounding(); flushThink();
    if (e.name === "AbortError") {                                   // user hit Stop
      if (bubble && acc) { acc += "\n\n*(stopped)*"; renderMD(bubble, acc, true); }
      else { bubble = bubble || addAssistant(""); acc = "_(stopped)_"; renderMD(bubble, acc, true); }
    } else if (bubble && acc) { acc += "\n\n*(stream interrupted)*"; renderMD(bubble, acc, true); }   // keep partial answer
    else { bubble = bubble || addAssistant(""); renderMD(bubble, "⚠️ Stream interrupted — tap send to retry.\n\n`" + (e.name || "Error") + ": " + e.message + "`"); }
  }
  if (stats && bubble) renderMeta(bubble, stats);
  if (bubble) appendT({ role: "assistant", content: acc, meta: stats });
  persistChat();                                   // save the whole turn to Oceano (dated folder)
  state.busy = false; setSendMode(false); _chatAbort = null; input.focus();
}
function setSendMode(stopping) {
  const b = $("#send"); if (!b) return;
  b.classList.toggle("stopping", stopping);
  b.setAttribute("aria-label", stopping ? "stop" : "send");
  b.title = stopping ? "Stop generating" : "Send";
}
function stopChat() {
  fetch("/api/chat/stop", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ session: state.session }) }).catch(() => {});
  if (_chatAbort) _chatAbort.abort();   // stops the client read; server cancels too
}

/* ---------------- models ---------------- */
async function loadPrefs() {
  try {
    const cfg = await api("/api/config");
    state.agent = !!(cfg.prefs && cfg.prefs.agent_mode);   // Agent mode persists across reloads
    const t = $("#agentToggle"); if (t) t.checked = state.agent;
  } catch {}
}
async function loadModels() {
  state.models = await api("/api/models");
  buildModelMenu();
  setStatus(state.models.some(m => m.base_url.includes("8081") && !m.error));
  if (!state.model) {
    const ok = state.models.filter(m => !m.error);
    if (ok.length) selectModel(ok.find(m => /qwen3-4b/i.test(m.id)) || ok[0]);
  }
}
function buildModelMenu() {
  const menu = $("#modelMenu"); menu.innerHTML = "";
  const groups = {}; state.models.forEach(m => (groups[m.endpoint] ||= []).push(m));
  for (const [ep, list] of Object.entries(groups)) {
    const g = document.createElement("div"); g.className = "mm-group"; g.textContent = ep; menu.appendChild(g);
    list.forEach(m => {
      const it = document.createElement("div");
      it.className = "mm-item" + (m.error ? " err" : "") + (m.id === state.model ? " sel" : "");
      it.innerHTML = `<span class="mp-dot" style="opacity:${m.error ? .3 : 1}"></span>${escapeHtml(m.id)}`;
      if (!m.error) it.onclick = () => { selectModel(m); $("#modelMenu").classList.remove("open"); };
      menu.appendChild(it);
    });
  }
}
function selectModel(m) {
  state.model = m.id; state.baseUrl = m.base_url;
  $("#modelLabel").textContent = m.id; $("#depthReadout").textContent = `${m.endpoint} · ${m.id}`;
  buildModelMenu();
}
function flashModel() { const p = $("#modelPill"); p.style.borderColor = "var(--coral)"; setTimeout(() => p.style.borderColor = "", 700); $("#modelMenu").classList.add("open"); }
const setStatus = up => { $("#statusDot").className = "dot " + (up ? "up" : "down"); $("#statusText").textContent = up ? "local stack online" : "local offline"; };

/* ================= VIEWS ================= */
function setView(v) {
  state.view = v; document.body.dataset.view = v;
  $$(".nav-item").forEach(n => n.classList.toggle("active", n.dataset.view === v));
  $$(".view").forEach(s => s.classList.toggle("active", s.id === "view-" + v));
  if (v === "files") { loadFiles(state.cwd); if (_cm) setTimeout(() => _cm.refresh(), 0); }
  if (v === "skills") loadSkills();
  if (v === "memory") loadMemory();
}

/* ---------------- files ---------------- */
async function loadFiles(path = "") {
  state.cwd = path;
  const d = await api("/api/files?path=" + encodeURIComponent(path));
  state.cwd = d.path;
  const crumbs = $("#crumbs"); crumbs.innerHTML = "";
  const root = document.createElement("span"); root.textContent = "workspace"; root.onclick = () => loadFiles("");
  crumbs.appendChild(root);
  let acc = "";
  (d.path ? d.path.split("/") : []).forEach(part => {
    acc = acc ? acc + "/" + part : part; const here = acc;
    crumbs.insertAdjacentText("beforeend", " / ");
    const s = document.createElement("span"); s.textContent = part; s.onclick = () => loadFiles(here); crumbs.appendChild(s);
  });
  const list = $("#fileList"); list.innerHTML = "";
  if (d.path) {
    const up = document.createElement("div"); up.className = "f-row dir";
    up.innerHTML = `<span class="fi">↰</span> ..`;
    up.onclick = () => loadFiles(d.path.split("/").slice(0, -1).join("/")); list.appendChild(up);
  }
  if (!d.entries.length && !d.path) list.innerHTML += `<div class="empty-note">workspace is empty</div>`;
  d.entries.forEach(e => {
    const row = document.createElement("div"); row.className = "f-row" + (e.dir ? " dir" : "");
    row.innerHTML = `<span class="fi">${e.dir ? "▸" : "·"}</span><span class="fn">${escapeHtml(e.name)}</span>` +
      (e.dir ? "" : `<span class="fsz">${fmtSize(e.size)}</span><button class="f-del" title="delete">✕</button>`);
    row.onclick = () => e.dir ? loadFiles(e.path) : openFile(e.path);
    const del = $(".f-del", row);
    if (del) del.onclick = async (ev) => {
      ev.stopPropagation();
      if (!await confirmAction("Delete file?", `“${e.name}” will be deleted.`)) return;
      await fetch("/api/file?path=" + encodeURIComponent(e.path), { method: "DELETE" });
      if (state.file === e.path) { $("#feOpen").style.display = "none"; $("#feEmpty").style.display = "block"; state.file = null; }
      loadFiles(state.cwd);
    };
    list.appendChild(row);
  });
}
const fmtSize = n => n < 1024 ? n + " B" : n < 1048576 ? (n / 1024).toFixed(1) + " K" : (n / 1048576).toFixed(1) + " M";
/* ---- CodeMirror code editor (Files view) ---- */
let _cm = null, _cmDirty = false;
const CM_LANGS = [["", "Plain text"], ["javascript", "JavaScript"], ["application/json", "JSON"],
  ["text/x-python", "Python"], ["text/x-csrc", "C"], ["text/x-c++src", "C++"], ["text/x-java", "Java"],
  ["css", "CSS"], ["xml", "XML"], ["htmlmixed", "HTML"], ["text/x-markdown", "Markdown"], ["shell", "Shell"],
  ["yaml", "YAML"], ["sql", "SQL"], ["rust", "Rust"], ["go", "Go"], ["php", "PHP"], ["ruby", "Ruby"],
  ["lua", "Lua"], ["dockerfile", "Dockerfile"]];
function _cmInit() {
  if (_cm) return _cm;
  _cm = CodeMirror($("#feCm"), {
    value: "", mode: null, theme: "material-darker", lineNumbers: true, lineWrapping: false,
    matchBrackets: true, autoCloseBrackets: true, styleActiveLine: true, indentUnit: 2, tabSize: 2,
    extraKeys: {
      "Ctrl-S": () => saveFile(), "Cmd-S": () => saveFile(),
      "Ctrl-F": "findPersistent", "Cmd-F": "findPersistent",
      "Alt-F": "replace", "Shift-Ctrl-F": "replaceAll",
      "Ctrl-/": "toggleComment", "Cmd-/": "toggleComment",
    },
  });
  _cm.on("change", () => { if (!_cmDirty) { _cmDirty = true; $("#feDirty").classList.add("on"); } });
  const sel = $("#feLang");
  sel.innerHTML = CM_LANGS.map(([v, l]) => `<option value="${v}">${l}</option>`).join("");
  sel.onchange = () => _cm.setOption("mode", sel.value || null);
  return _cm;
}
function _applyMode(path) {                       // pick syntax mode from the file's extension/name
  let mime = "";
  try { const info = CodeMirror.findModeByFileName(path.split("/").pop()); if (info) mime = info.mime || info.mode; } catch {}
  _cm.setOption("mode", mime || null);
  const sel = $("#feLang"); sel.value = [...sel.options].some(o => o.value === mime) ? mime : "";
}
async function openFile(path) {
  state.file = path;
  $("#feEmpty").style.display = "none"; $("#feOpen").style.display = "flex";
  $("#feName").textContent = path;
  const isImg = /\.(png|jpe?g|gif|webp|svg|bmp|ico|avif)$/i.test(path);
  $("#feImage").style.display = isImg ? "flex" : "none";
  $("#feCm").style.display = isImg ? "none" : "block";
  $("#feOpen").classList.toggle("is-image", isImg);
  if (isImg) { $("#feImg").src = "/api/raw?path=" + encodeURIComponent(path); return; }
  const d = await api("/api/file?path=" + encodeURIComponent(path));
  _cmInit();
  _cm.setOption("readOnly", !!d.binary);
  _cm.setValue(d.binary ? "(binary file — not editable here)" : d.content);
  _cm.clearHistory();
  _applyMode(path);
  _cmDirty = false; $("#feDirty").classList.remove("on");
  setTimeout(() => _cm.refresh(), 0);             // CM mis-measures in a freshly-shown flex box
}
async function newFolder() {
  const name = await promptDialog("New folder", { placeholder: "folder name", okLabel: "Create" }); if (!name) return;
  const path = state.cwd ? state.cwd + "/" + name : name;
  await fetch("/api/folder", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path }) });
  loadFiles(state.cwd);
}
async function saveFile() {
  if (!state.file || !_cm) return;
  await fetch("/api/file", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: state.file, content: _cm.getValue() }) });
  _cmDirty = false; $("#feDirty").classList.remove("on");
  const btn = $("#fSave"); btn.textContent = "Saved ✓"; setTimeout(() => btn.textContent = "Save", 1200);
  loadFiles(state.cwd);
}
async function saveFileAs() {
  if (!_cm) return;
  const suggested = state.file || (state.cwd ? state.cwd + "/untitled.txt" : "untitled.txt");
  const path = await promptDialog("Save as", { value: suggested, message: "Path relative to the workspace", okLabel: "Save" });
  if (!path || path === state.file) { if (path === state.file) saveFile(); return; }
  await fetch("/api/file", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path, content: _cm.getValue() }) });
  await loadFiles(state.cwd);
  openFile(path);
}
function toggleWrap() {
  if (!_cm) return;
  const w = !_cm.getOption("lineWrapping");
  _cm.setOption("lineWrapping", w); $("#feWrap").classList.toggle("on", w);
}
async function newFile() {
  const name = await promptDialog("New file", { placeholder: "file name (e.g. notes.md)", okLabel: "Create" }); if (!name) return;
  const path = state.cwd ? state.cwd + "/" + name : name;
  await fetch("/api/file", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path, content: "" }) });
  loadFiles(state.cwd); openFile(path);
}

/* ---------------- skills ---------------- */
let skillsCache = [];
async function loadSkills() {
  skillsCache = await api("/api/skills");
  const body = $("#skillsBody"); body.innerHTML = "";
  if (!skillsCache.length) body.innerHTML = `<div class="empty-note">No skills yet. Create one — teach Oceano a reusable procedure.</div>`;
  skillsCache.forEach(s => {
    const c = document.createElement("div"); c.className = "skill-card";
    c.innerHTML = `<h3>${escapeHtml(s.name)}</h3><div class="sc-desc">${escapeHtml(s.description)}</div>
      <div class="sc-snip">${escapeHtml(s.body.slice(0, 90))}…</div>`;
    c.onclick = () => openSkill(s);
    body.appendChild(c);
  });
}
function openSkill(s) {
  $("#skModalTitle").textContent = s ? `Edit skill${s.status && s.status !== "published" ? " · " + s.status : ""}` : "New skill";
  $("#skName").value = s ? s.name : ""; $("#skDesc").value = s ? s.description : ""; $("#skBody").value = s ? s.body : "";
  $("#skModal").dataset.dir = s ? s.dir : "";
  $("#skModal").dataset.status = s ? (s.status || "published") : "published";
  $("#skModal").dataset.notes = s ? (s.notes || "") : "";
  $("#skDelete").style.display = s ? "block" : "none";
  $("#skModal").classList.add("open"); $("#skModalScrim").classList.add("open");
}
const closeSkill = () => { $("#skModal").classList.remove("open"); $("#skModalScrim").classList.remove("open"); };
async function saveSkill() {
  const m = $("#skModal");
  const body = { name: $("#skName").value.trim(), description: $("#skDesc").value.trim(), body: $("#skBody").value,
    dir: m.dataset.dir || undefined, status: m.dataset.status || "published", notes: m.dataset.notes || "" };
  if (!body.name) return;
  await fetch("/api/skills", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  closeSkill(); loadBrainSkills();
}
async function deleteSkill() {
  const dir = $("#skModal").dataset.dir; if (!dir) return;
  if (!await confirmAction("Delete skill?", `“${$("#skName").value}” will be deleted.`)) return;
  await fetch("/api/skills/" + encodeURIComponent(dir), { method: "DELETE" });
  closeSkill(); loadBrainSkills();
}

/* ---------------- memory ---------------- */
async function loadMemory() {
  const mems = await api("/api/memories");
  const list = $("#memList"); list.innerHTML = "";
  if (!mems.length) { list.innerHTML = `<div class="empty-note">No memories yet.</div>`; return; }
  mems.forEach(m => {
    const row = document.createElement("div"); row.className = "mem-row";
    const tags = (m.tags || "").split(",").filter(Boolean).map(t => `<span class="tag">${escapeHtml(t.trim())}</span>`).join("");
    const date = (m.ts || "").slice(0, 10);
    row.innerHTML = `<div class="mr-body"><div class="mr-text">${escapeHtml(m.text)}</div><div class="mr-meta">${tags}${date}</div></div><button class="mr-del">✕</button>`;
    $(".mr-del", row).onclick = async () => { if (!await confirmAction("Delete memory?", m.text.slice(0, 100))) return; await fetch("/api/memories/" + m.id, { method: "DELETE" }); loadMemory(); };
    list.appendChild(row);
  });
}
async function addMemory() {
  const text = $("#memText").value.trim(); if (!text) return;
  await fetch("/api/memories", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text, tags: $("#memTags").value.trim() }) });
  $("#memText").value = ""; $("#memTags").value = ""; loadMemory();
}

/* ---------------- settings ---------------- */
async function loadProviders() {
  const provs = await api("/api/providers");
  $("#providerSelect").innerHTML = provs.map(p => `<option value="${p.base_url}" data-needs="${p.needs_key}" data-name="${p.name}">${p.name}</option>`).join("")
    + `<option value="" data-needs="false" data-name="">Custom (any OpenAI-compatible URL)…</option>`;
  $("#providerSelect").onchange = syncProviderFields; syncProviderFields();
}
function syncProviderFields() {
  const o = $("#providerSelect").selectedOptions[0]; if (!o) return;
  const custom = !o.value;
  $("#epUrl").value = o.value;
  $("#epUrl").placeholder = custom ? "base URL, e.g. http://192.168.1.20:11434/v1" : o.value;
  $("#epName").value = o.dataset.name;
  $("#epKey").style.display = (custom || o.dataset.needs === "true") ? "block" : "none"; $("#epKey").value = "";
  if (custom) $("#epUrl").focus();
}
async function loadEndpoints() {
  const cfg = await api("/api/config"); const box = $("#endpoints"); if (!box) return; box.innerHTML = "";
  cfg.endpoints.forEach(e => {
    const el = document.createElement("div"); el.className = "ep";
    el.innerHTML = `<div class="ep-info"><div class="ep-name">${escapeHtml(e.name)}</div><div class="ep-url">${escapeHtml(e.base_url)}</div>${e.has_key ? '<div class="ep-key">● key set</div>' : ''}</div><span class="ep-count" data-ep="${escapeHtml(e.name)}">…</span><button class="ep-del">✕</button>`;
    $(".ep-del", el).onclick = async () => { if (!await confirmAction("Remove endpoint?", `“${e.name}” will be removed.`)) return; await fetch("/api/endpoints/" + encodeURIComponent(e.name), { method: "DELETE" }); loadEndpoints(); loadModels(); };
    box.appendChild(el);
  });
  try {                                  // model count + reachability per endpoint
    const models = await api("/api/models");
    const counts = {}, errs = {};
    models.forEach(m => { if (m.error) errs[m.endpoint] = 1; else counts[m.endpoint] = (counts[m.endpoint] || 0) + 1; });
    $$(".ep-count", box).forEach(b => {
      if (errs[b.dataset.ep]) { b.textContent = "⚠ unreachable"; b.className = "ep-count err"; }
      else { const n = counts[b.dataset.ep] || 0; b.textContent = n + (n === 1 ? " model" : " models"); b.className = "ep-count ok"; }
    });
  } catch {}
}
async function addEndpoint() {
  const o = $("#providerSelect").selectedOptions[0], msg = $("#epMsg");
  const url = $("#epUrl").value.trim().replace(/\/+$/, "");
  if (!/^https?:\/\//.test(url)) {
    if (msg) { msg.textContent = "enter a base URL starting with http(s):// — usually ending in /v1"; msg.className = "kn-note err"; }
    return;
  }
  const name = $("#epName").value.trim() || o.dataset.name || url.replace(/^https?:\/\//, "").replace(/\/v\d+$/, "");
  await fetch("/api/endpoints", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name, base_url: url, api_key: $("#epKey").value }) });
  $("#epKey").value = "";
  if (msg) { msg.textContent = `added “${name}” ✓ — counting its models…`; msg.className = "kn-note ok"; }
  await loadEndpoints(); loadModels();
}

/* ---------------- telegram ---------------- */
function renderTgStatus(tg) {
  const el = $("#tgStatus"); if (!el) return;
  const st = tg.status || {};
  if (st.error) { el.textContent = "⚠ " + st.error; el.className = "tg-status err"; }
  else if (st.running) { el.textContent = "● connected as @" + (st.username || "bot"); el.className = "tg-status on"; }
  else if (tg.enabled && !tg.has_token) { el.textContent = "○ enabled, no token yet"; el.className = "tg-status"; }
  else { el.textContent = "○ off"; el.className = "tg-status"; }
}
async function loadTelegram() {
  const cfg = await api("/api/config"); const tg = cfg.telegram || {};
  $("#tgEnabled").checked = !!tg.enabled;
  $("#tgAllowed").value = (tg.allowed || []).join(", ");
  $("#tgToken").value = "";
  $("#tgToken").placeholder = tg.has_token ? "● token set — paste to change" : "paste to set";
  renderTgStatus(tg);
}
async function saveTelegram(extra = {}) {
  const btn = $("#tgSave"); btn.disabled = true; btn.textContent = "Applying…";
  try {
    const r = await api("/api/telegram", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: $("#tgEnabled").checked, token: $("#tgToken").value, allowed: $("#tgAllowed").value, ...extra }) });
    renderTgStatus({ enabled: $("#tgEnabled").checked, has_token: r.status && r.status.username || !extra.clear_token, status: r.status });
    await loadTelegram(); loadServices();
  } finally { btn.disabled = false; btn.textContent = "Save & apply"; }
}

/* ---------------- services status ---------------- */
function _svc(label, ok, detail) {
  return `<div class="svc"><span class="svc-dot ${ok ? 'on' : 'off'}"></span><span class="svc-name">${label}</span><span class="svc-detail">${escapeHtml(detail)}</span></div>`;
}
async function loadServices() {
  const box = $("#svcList"); if (!box) return;
  try {
    const s = await api("/api/status");
    const beat = s.scheduler_beat_ago;
    const schedOk = beat != null && beat < 90;   // heartbeat is every 30s
    const tg = s.telegram || {};
    box.innerHTML =
      _svc("Web UI", true, "this page") +
      _svc("Embeddings (:8082)", s.embed, s.embed ? "reachable" : "down") +
      _svc("Scheduler", schedOk, beat == null ? "no heartbeat" : `beat ${Math.round(beat)}s ago`) +
      _svc("Telegram", tg.running, tg.running ? "@" + (tg.username || "bot") : (tg.error ? "error" : "off"));
  } catch { box.innerHTML = `<div class="svc"><span class="svc-dot off"></span><span class="svc-name">status unavailable</span></div>`; }
}

/* ================= SETTINGS WINDOW ================= */
const SETTINGS_TABS = [
  ["account", "◐", "Account"], ["endpoints", "◇", "Endpoints"], ["telegram", "✈", "Telegram"],
  ["memory", "✶", "Memory"], ["tools", "⚒", "Tools"], ["services", "◉", "Services"],
  ["wipe", "🗑", "Wipe"], ["about", "≈", "About"],
];
// each wipe target: [key, label, description, confirm-detail]
const WIPE_TARGETS = [
  ["chats", "Chats", "Every saved conversation (all dated folders).", "All chat history will be permanently deleted."],
  ["documents", "Documents", "Everything inside the workspace folder.", "All files & folders in the workspace will be deleted."],
  ["knowledge", "Indexed knowledge", "The RAG store of embedded document chunks.", "All indexed document chunks will be removed (re-index to restore)."],
  ["skills", "Learnt skills", "Skills the agent taught itself (not your published ones).", "All learning/staged skills will be deleted; published skills are kept."],
  ["memory", "Memories", "All long-term memories about you.", "Every stored memory will be permanently deleted."],
];
const SETTINGS_PAGES = {
  account: `
    <div class="drawer-section">
      <h3>Account</h3>
      <div class="acct-row">signed in as <span class="acct-who" id="acctWho">…</span></div>
      <label class="field-label">Username</label>
      <input id="acctUser" autocomplete="username" placeholder="username">
      <label class="field-label">Current password <span class="lbl-sub">required to save changes</span></label>
      <input id="acctCur" type="password" autocomplete="current-password" placeholder="current password">
      <label class="field-label">New password <span class="lbl-sub">leave blank to keep current</span></label>
      <input id="acctNew" type="password" autocomplete="new-password" placeholder="new password">
      <div class="acct-actions"><button class="ghost-btn sm" id="logoutBtn">Log out</button><span class="acct-msg" id="acctMsg"></span><button class="primary sm" id="acctSave">Save</button></div>
    </div>`,
  endpoints: `
    <div class="drawer-section">
      <h3>Model endpoints</h3>
      <p class="sub">Pick a provider — or choose <b>Custom</b> and point Oceano at any OpenAI-compatible server (another llama-swap box, Ollama, LM Studio, vLLM…). Each endpoint shows how many models it serves; they all appear in the composer.</p>
      <div class="endpoints" id="endpoints"></div>
      <div class="add-endpoint">
        <select id="providerSelect"></select>
        <input id="epUrl" placeholder="base URL, e.g. http://192.168.1.20:11434/v1" autocomplete="off" spellcheck="false">
        <input id="epName" placeholder="label (optional)">
        <input id="epKey" type="password" placeholder="API key" autocomplete="off">
        <button class="primary" id="addEndpoint">Add</button>
        <div class="kn-note" id="epMsg"></div>
      </div>
    </div>`,
  telegram: `
    <div class="drawer-section">
      <div class="sec-head"><h3>Telegram</h3>
        <label class="agent-switch sm" title="Run the Telegram bot inside this daemon"><input type="checkbox" id="tgEnabled"><span class="track"><span class="thumb"></span></span></label></div>
      <p class="sub">Chat with Oceano from your phone. Runs in this web process. <span id="tgStatus" class="tg-status">…</span></p>
      <label class="field-label">Bot token <span class="lbl-sub">from @BotFather</span></label>
      <input id="tgToken" type="password" autocomplete="off" placeholder="paste to set / change">
      <label class="field-label">Allowed user IDs <span class="lbl-sub">comma-separated · agent can run shell, keep tight</span></label>
      <input id="tgAllowed" placeholder="e.g. 123456789, 987654321">
      <div class="tg-actions"><button class="ghost-btn sm" id="tgClearToken">Clear token</button><button class="primary" id="tgSave">Save &amp; apply</button></div>
    </div>`,
  memory: `
    <div class="drawer-section">
      <h3>Memory injection</h3>
      <p class="sub">How each kind of memory reaches the model. <b>Always</b> = included every message; <b>When relevant</b> = only if it matches the prompt; <b>Off</b> = never. Pinned memories (📌 in Brain → Memory) are always included regardless.</p>
      <div class="mem-policy" id="memPolicy"></div>
      <div class="acct-actions"><span class="acct-msg" id="memPolMsg"></span><button class="primary sm" id="memPolSave">Save</button></div>
    </div>`,
  tools: `
    <div class="drawer-section">
      <h3>Tools <span class="tool-count" id="toolCount"></span></h3>
      <p class="sub">What the agent can reach when Agent mode is on.</p>
      <div class="tool-list" id="toolList"></div>
    </div>`,
  services: `
    <div class="drawer-section">
      <h3>Services</h3>
      <p class="sub">Everything Oceano runs on this box.</p>
      <div class="svc-list" id="svcList"></div>
    </div>`,
  wipe: `
    <div class="drawer-section">
      <h3>Wipe data</h3>
      <p class="sub">Permanently clear a category of Oceano's data. Each is separate and irreversible — you'll be asked to confirm.</p>
      <div class="wipe-list">${WIPE_TARGETS.map(([k, l, d]) =>
        `<div class="wipe-row"><div class="wipe-info"><div class="wipe-name">${l}</div><div class="wipe-desc">${d}</div></div><button class="danger-btn sm wipe-btn" data-wipe="${k}">Wipe</button></div>`).join("")}</div>
      <div class="kn-note" id="wipeMsg"></div>
    </div>`,
  about: `
    <div class="drawer-section">
      <h3>About</h3>
      <p class="sub">Oceano · self-hosted · everything runs on your box. The agent writes only inside its workspace; the web UI is bound to localhost.</p>
    </div>`,
};
function openSettings() {
  const { body, reused } = createWindow({ id: "win-settings", title: "Settings", icon: "⚙", width: 660, height: 560 });
  if (reused) { loadSettingsAll(); return; }
  body.classList.add("set-win");
  body.innerHTML = `
    <div class="set-layout">
      <div class="set-tabs">${SETTINGS_TABS.map((t, i) =>
        `<button class="set-tab${i === 0 ? " active" : ""}" data-page="${t[0]}"><span class="sti">${t[1]}</span>${t[2]}</button>`).join("")}</div>
      <div class="set-pane">${SETTINGS_TABS.map((t, i) =>
        `<div class="set-page${i === 0 ? " active" : ""}" data-page="${t[0]}">${SETTINGS_PAGES[t[0]]}</div>`).join("")}</div>
    </div>`;
  $$(".set-tab", body).forEach(t => t.onclick = () => {
    $$(".set-tab", body).forEach(x => x.classList.toggle("active", x === t));
    $$(".set-page", body).forEach(p => p.classList.toggle("active", p.dataset.page === t.dataset.page));
  });
  $("#addEndpoint", body).onclick = addEndpoint;
  $("#tgSave", body).onclick = () => saveTelegram();
  $("#tgClearToken", body).onclick = async () => { if (await confirmAction("Clear bot token?", "The Telegram bot will stop until you set a new token.", "Clear")) { $("#tgEnabled").checked = false; saveTelegram({ clear_token: true }); } };
  $("#acctSave", body).onclick = saveAccount;
  $("#logoutBtn", body).onclick = logout;
  $("#memPolSave", body).onclick = saveMemoryPolicy;
  $$(".wipe-btn", body).forEach(b => b.onclick = () => wipeTarget(b.dataset.wipe));
  loadSettingsAll();
}
async function wipeTarget(key) {
  const t = WIPE_TARGETS.find(x => x[0] === key); if (!t) return;
  if (!await confirmAction(`Wipe ${t[1].toLowerCase()}?`, t[3] + " This cannot be undone.", "Wipe")) return;
  const msg = $("#wipeMsg");
  try {
    const r = await api("/api/wipe/" + encodeURIComponent(key), { method: "POST" });
    if (msg) { msg.textContent = `✓ wiped ${r.removed} ${r.what}`; msg.className = "kn-note ok"; }
    if (key === "chats") { _chats = []; localStorage.removeItem("oceano.active"); newVoyage(); }
    if (key === "memory") { if (typeof loadBrainMem === "function") loadBrainMem(); }
    if (key === "documents" && typeof expLoad === "function" && typeof _expCwd === "string") expLoad("");
    if (key === "skills" && typeof loadBrainSkills === "function") loadBrainSkills();
  } catch { if (msg) { msg.textContent = "wipe failed"; msg.className = "kn-note err"; } }
}
function loadSettingsAll() { loadProviders(); loadEndpoints(); loadTelegram(); loadServices(); loadTools(); loadAccount(); loadMemoryPolicy(); }

const POLICY_OPTS = [["always", "Always inject"], ["relevant", "When relevant"], ["off", "Off"]];
async function loadMemoryPolicy() {
  const box = $("#memPolicy"); if (!box) return;
  let d; try { d = await api("/api/memory/policy"); } catch { return; }
  box.innerHTML = d.categories.map(cat => {
    const cur = d.policy[cat] || "relevant";
    const opts = POLICY_OPTS.map(([v, l]) => `<option value="${v}"${v === cur ? " selected" : ""}>${l}</option>`).join("");
    return `<div class="mp-row"><span class="mp-cat">${cat}</span><select class="mp-sel" data-cat="${cat}">${opts}</select></div>`;
  }).join("");
}
async function saveMemoryPolicy() {
  const msg = $("#memPolMsg");
  const policy = {}; $$("#memPolicy .mp-sel").forEach(s => policy[s.dataset.cat] = s.value);
  try {
    await api("/api/memory/policy", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(policy) });
    if (msg) { msg.textContent = "saved ✓"; msg.className = "acct-msg ok"; }
  } catch { if (msg) { msg.textContent = "save failed"; msg.className = "acct-msg err"; } }
}

async function loadTools() {
  const box = $("#toolList"); if (!box) return;
  try {
    const tools = await api("/api/tools");
    const cnt = $("#toolCount"); if (cnt) cnt.textContent = "· " + tools.length;
    box.innerHTML = tools.map(t => {
      const params = (t.params || []).length
        ? t.params.map(p => `<span class="tp${p.required ? " req" : ""}" title="${escapeHtml((p.required ? "required · " : "optional · ") + p.type + (p.description ? " · " + p.description : ""))}">${escapeHtml(p.name)}<i>${escapeHtml(p.type)}</i></span>`).join("")
        : `<span class="tp-none">no inputs</span>`;
      return `<div class="tool-row"><div class="th"><span class="tcat">${escapeHtml(t.category || "other")}</span><span class="tn">${escapeHtml(t.name)}</span></div>` +
             `<div class="td">${escapeHtml(t.description || "")}</div><div class="tparams">${params}</div></div>`;
    }).join("");
  } catch {}
}

/* ---------------- account / auth ---------------- */
async function loadAccount() {
  try {
    const me = await api("/api/me");
    const who = $("#acctWho"), u = $("#acctUser");
    if (who) who.textContent = me.user || "—";
    if (u) u.value = me.user || "";
  } catch {}
}
async function saveAccount() {
  const msg = $("#acctMsg"), cur = $("#acctCur").value, user = $("#acctUser").value.trim(), npw = $("#acctNew").value;
  if (!cur) { msg.textContent = "enter current password"; msg.className = "acct-msg err"; return; }
  const r = await fetch("/api/account", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ current_password: cur, user, new_password: npw }) });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) { msg.textContent = d.detail || "save failed"; msg.className = "acct-msg err"; return; }
  msg.textContent = "saved ✓"; msg.className = "acct-msg ok";
  $("#acctCur").value = ""; $("#acctNew").value = "";
  if ($("#acctWho")) $("#acctWho").textContent = d.user;
}
async function logout() {
  await fetch("/api/logout", { method: "POST" }).catch(() => {});
  location.reload();
}

/* ================= FLOATING WINDOWS ================= */
let _winZ = 60;
function createWindow(opts) {
  const existing = opts.id && document.getElementById(opts.id);
  if (existing) { existing.style.display = "flex"; existing.style.zIndex = ++_winZ; if (existing._chip) existing._chip.remove(); return { body: $(".win-body", existing), reused: true }; }
  const win = document.createElement("div");
  win.className = "win"; if (opts.id) win.id = opts.id;
  win.dataset.title = opts.title || "Window"; win.dataset.icon = opts.icon || "▢";
  const g = opts.id ? loadWinGeom(opts.id) : null;   // reopen where it was left
  if (g) {
    win.dataset.restoreW = g.rW; win.dataset.restoreH = g.rH; win.dataset.restoreX = g.rX; win.dataset.restoreY = g.rY;
    if (g.maximized) { _setRect(win, _zoneRect("full")); win.dataset.maximized = "1"; }
    else if (g.snapped) { _setRect(win, _zoneRect(g.snapped)); win.dataset.snapped = g.snapped; }
    else {
      win.style.width = g.w + "px"; win.style.height = g.h + "px";
      win.style.left = Math.max(0, Math.min(g.l, innerWidth - 80)) + "px";
      win.style.top = Math.max(0, Math.min(g.t, innerHeight - 60)) + "px";
    }
  } else {
    win.style.width = (opts.width || 520) + "px";
    win.style.height = (opts.height || 420) + "px";
    win.style.left = (opts.x ?? (150 + (_winZ % 6) * 26)) + "px";
    win.style.top = (opts.y ?? (84 + (_winZ % 6) * 26)) + "px";
  }
  win.style.zIndex = ++_winZ;
  win.innerHTML = `<div class="win-bar"><span class="win-ic">${escapeHtml(opts.icon || "▢")}</span><span class="win-title">${escapeHtml(opts.title || "Window")}</span><button class="win-min" title="minimize">–</button><button class="win-max" title="maximize">▢</button><button class="win-close" title="close">✕</button></div><div class="win-body"></div><div class="win-rz"></div>`;
  $("#windows").appendChild(win);
  win.addEventListener("mousedown", () => { win.style.zIndex = ++_winZ; });
  $(".win-min", win).onclick = e => { e.stopPropagation(); minimizeWindow(win); };
  $(".win-max", win).onclick = e => { e.stopPropagation(); maximizeWindow(win); };
  $(".win-bar", win).addEventListener("dblclick", e => { if (!e.target.closest("button")) maximizeWindow(win); });
  $(".win-close", win).onclick = async () => {
    if (opts.onClose && (await opts.onClose()) === false) return;   // onClose may veto the close
    if (win._chip) win._chip.remove();
    win.remove();
  };
  _dragify($(".win-bar", win), win, "move");
  _dragify($(".win-rz", win), win, "resize");
  return { body: $(".win-body", win), reused: false };
}
function minimizeWindow(win) {
  win.style.display = "none";
  const chip = document.createElement("button");
  chip.className = "dock-chip";
  chip.innerHTML = `<span class="dc-ic">${escapeHtml(win.dataset.icon || "▢")}</span><span class="dc-t">${escapeHtml(win.dataset.title || "Window")}</span>`;
  chip.onclick = () => { win.style.display = "flex"; win.style.zIndex = ++_winZ; chip.remove(); win._chip = null; };
  $("#winDock").appendChild(chip);
  win._chip = chip;
}
function _dragify(handle, win, mode) {
  handle.addEventListener("mousedown", e => {
    if (e.target.closest("button")) return;
    let zone = null;
    if (mode === "move" && (win.dataset.snapped || win.dataset.maximized)) _unsnapForDrag(win, e);
    const sx = e.clientX, sy = e.clientY, ol = win.offsetLeft, ot = win.offsetTop, ow = win.offsetWidth, oh = win.offsetHeight;
    const mv = ev => {
      if (mode === "move") {
        win.style.left = Math.max(0, ol + ev.clientX - sx) + "px";
        win.style.top = Math.max(0, ot + ev.clientY - sy) + "px";
        zone = _detectZone(ev.clientX, ev.clientY);
        _showSnap(zone, win);
      } else {
        win.style.width = Math.max(300, ow + ev.clientX - sx) + "px";
        win.style.height = Math.max(180, oh + ev.clientY - sy) + "px";
        win.dataset.snapped = ""; win.dataset.maximized = "";
      }
    };
    const up = () => {
      document.removeEventListener("mousemove", mv); document.removeEventListener("mouseup", up);
      if (mode === "move") { _showSnap(null); if (zone) _applySnap(win, zone); }
      saveWinGeom(win);
    };
    document.addEventListener("mousemove", mv); document.addEventListener("mouseup", up);
    e.preventDefault(); e.stopPropagation();
  });
}
/* ---- Aero-snap: drag to edges/corners → halves/quarters/maximize ---- */
function _snapRegion() { const r = $(".views").getBoundingClientRect(); return { left: r.left, top: r.top, w: r.width, h: r.height }; }
function _zoneRect(zone) {
  const R = _snapRegion(), hw = R.w / 2, hh = R.h / 2;
  const m = { full: [R.left, R.top, R.w, R.h], left: [R.left, R.top, hw, R.h], right: [R.left + hw, R.top, hw, R.h],
    bottom: [R.left, R.top + hh, R.w, hh], tl: [R.left, R.top, hw, hh], tr: [R.left + hw, R.top, hw, hh],
    bl: [R.left, R.top + hh, hw, hh], br: [R.left + hw, R.top + hh, hw, hh] };
  const [x, y, w, h] = m[zone]; return { x, y, w, h };
}
function _detectZone(cx, cy) {
  const R = _snapRegion(), x = cx - R.left, y = cy - R.top;
  if (x < -20 || y < -20 || x > R.w + 20 || y > R.h + 20) return null;
  const E = 28, C = 150;
  if (y <= E) return x <= C ? "tl" : x >= R.w - C ? "tr" : "full";
  if (y >= R.h - E) return x <= C ? "bl" : x >= R.w - C ? "br" : "bottom";
  if (x <= E) return "left";
  if (x >= R.w - E) return "right";
  return null;
}
function _applySnap(win, zone) {
  if (!win.dataset.snapped && !win.dataset.maximized) { win.dataset.restoreW = win.offsetWidth; win.dataset.restoreH = win.offsetHeight; win.dataset.restoreX = win.offsetLeft; win.dataset.restoreY = win.offsetTop; }
  const r = _zoneRect(zone);
  win.classList.add("snapping");
  win.style.left = r.x + "px"; win.style.top = r.y + "px"; win.style.width = r.w + "px"; win.style.height = r.h + "px";
  win.dataset.snapped = zone; win.dataset.maximized = "";
  setTimeout(() => win.classList.remove("snapping"), 140);
}
function _unsnapForDrag(win, e) {
  const rw = +(win.dataset.restoreW || 520), rh = +(win.dataset.restoreH || 420);
  win.style.width = rw + "px"; win.style.height = rh + "px";
  win.style.left = (e.clientX - rw / 2) + "px"; win.style.top = (e.clientY - 16) + "px";
  win.dataset.snapped = ""; win.dataset.maximized = "";
}
function maximizeWindow(win) {
  win.classList.add("snapping");
  if (win.dataset.maximized || win.dataset.snapped) {
    win.style.width = (win.dataset.restoreW || 520) + "px"; win.style.height = (win.dataset.restoreH || 420) + "px";
    win.style.left = (win.dataset.restoreX || 150) + "px"; win.style.top = (win.dataset.restoreY || 90) + "px";
    win.dataset.maximized = ""; win.dataset.snapped = "";
  } else {
    win.dataset.restoreW = win.offsetWidth; win.dataset.restoreH = win.offsetHeight; win.dataset.restoreX = win.offsetLeft; win.dataset.restoreY = win.offsetTop;
    const r = _zoneRect("full");
    win.style.left = r.x + "px"; win.style.top = r.y + "px"; win.style.width = r.w + "px"; win.style.height = r.h + "px";
    win.dataset.maximized = "1";
  }
  win.style.zIndex = ++_winZ;
  saveWinGeom(win);
  setTimeout(() => win.classList.remove("snapping"), 140);
}
function _setRect(win, r) { win.style.left = r.x + "px"; win.style.top = r.y + "px"; win.style.width = r.w + "px"; win.style.height = r.h + "px"; }
function saveWinGeom(win) {
  if (!win.id) return;
  localStorage.setItem("oceano.win." + win.id, JSON.stringify({
    l: win.offsetLeft, t: win.offsetTop, w: win.offsetWidth, h: win.offsetHeight,
    snapped: win.dataset.snapped || "", maximized: !!win.dataset.maximized,
    rW: +(win.dataset.restoreW || win.offsetWidth), rH: +(win.dataset.restoreH || win.offsetHeight),
    rX: +(win.dataset.restoreX || win.offsetLeft), rY: +(win.dataset.restoreY || win.offsetTop),
  }));
}
function loadWinGeom(id) { try { return JSON.parse(localStorage.getItem("oceano.win." + id)); } catch { return null; } }
function _showSnap(zone, win) {
  let el = $("#snapPreview");
  if (!zone) { if (el) el.style.display = "none"; return; }
  if (!el) { el = document.createElement("div"); el.id = "snapPreview"; document.body.appendChild(el); }
  const r = _zoneRect(zone);
  el.style.display = "block";
  el.style.left = r.x + "px"; el.style.top = r.y + "px"; el.style.width = r.w + "px"; el.style.height = r.h + "px";
  if (win) el.style.zIndex = (parseInt(win.style.zIndex) || 100) - 1;
}

/* context menu */
function showCtx(x, y, items) {
  hideCtx();
  const m = document.createElement("div"); m.className = "ctx-menu"; m.id = "ctxMenu";
  items.forEach(it => {
    if (it.sep) { const s = document.createElement("div"); s.className = "ctx-sep"; m.appendChild(s); return; }
    const b = document.createElement("div"); b.className = "ctx-item" + (it.danger ? " danger" : "");
    b.textContent = it.label;
    b.onclick = () => { hideCtx(); it.action(); };
    m.appendChild(b);
  });
  m.style.left = x + "px"; m.style.top = y + "px";
  document.body.appendChild(m);
  requestAnimationFrame(() => { const r = m.getBoundingClientRect(); if (r.right > innerWidth) m.style.left = (x - r.width) + "px"; if (r.bottom > innerHeight) m.style.top = (y - r.height) + "px"; });
}
function hideCtx() { const m = $("#ctxMenu"); if (m) m.remove(); }
document.addEventListener("click", hideCtx);

/* ---------- live browser window (interactive — you + the agent share it) ---------- */
let _liveES = null;
function _mapToPage(img, clientX, clientY) {           // displayed frame coords → page coords (handles letterbox)
  const r = img.getBoundingClientRect();
  const nW = img.naturalWidth || 1280, nH = img.naturalHeight || 800;
  const natAR = nW / nH, boxAR = r.width / r.height;
  let rw, rh, ox, oy;
  if (boxAR > natAR) { rh = r.height; rw = rh * natAR; ox = (r.width - rw) / 2; oy = 0; }
  else { rw = r.width; rh = rw / natAR; ox = 0; oy = (r.height - rh) / 2; }
  const fx = (clientX - r.left - ox) / rw, fy = (clientY - r.top - oy) / rh;
  if (fx < 0 || fx > 1 || fy < 0 || fy > 1) return null;
  return { x: Math.round(fx * nW), y: Math.round(fy * nH) };
}
function openLiveView() {
  const { body, reused } = createWindow({ id: "win-live", title: "Live browser — drive it, or watch Oceano", icon: "◫", width: 720, height: 600,
    onClose: () => { if (_liveES) { _liveES.close(); _liveES = null; } } });
  if (reused) return;
  body.innerHTML = `
    <div class="live-addr"><input id="liveInput" placeholder="type a URL and press Enter…" autocomplete="off"><button class="exp-btn" id="liveGo">Go</button></div>
    <div class="live-tabs" id="liveTabs" style="display:none"></div>
    <div class="live-url" id="liveUrl">idle — type a URL, click into the page, or let the agent browse</div>
    <div class="live-stage" id="liveStage" tabindex="0"><span class="live-wait" id="liveWait">No frames yet. Enter a URL above, click into the page, or ask the agent to browse.</span><img id="liveImg" alt="live" draggable="false" style="display:none"></div>`;
  const post = (p, b) => fetch(p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b) });
  const go = () => { const url = $("#liveInput", body).value.trim(); if (!url) return; $("#liveUrl", body).textContent = "loading " + url + " …"; post("/api/browser/go", { url }); };
  $("#liveGo", body).onclick = go;
  $("#liveInput", body).addEventListener("keydown", e => { if (e.key === "Enter") { e.stopPropagation(); go(); } });

  const img = $("#liveImg", body), stage = $("#liveStage", body);
  img.addEventListener("click", e => { const pt = _mapToPage(img, e.clientX, e.clientY); if (pt) post("/api/browser/click", pt); stage.focus(); });
  // throttle the wheel: trackpads fire dozens of events/sec — accumulate the delta
  // and post at most every 80ms, so the server isn't flooded with tiny scrolls
  let _wheelAcc = 0, _wheelTimer = null;
  stage.addEventListener("wheel", e => {
    e.preventDefault();
    _wheelAcc += e.deltaY;
    if (_wheelTimer) return;
    _wheelTimer = setTimeout(() => {
      const dy = Math.round(_wheelAcc); _wheelAcc = 0; _wheelTimer = null;
      if (dy) post("/api/browser/scroll", { dy });
    }, 80);
  }, { passive: false });
  stage.addEventListener("keydown", e => {
    if (e.key.length === 1 && !e.ctrlKey && !e.metaKey) { post("/api/browser/type", { text: e.key }); e.preventDefault(); }
    else if (["Enter", "Backspace", "Tab", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Escape", "Delete", "Home", "End"].includes(e.key)) { post("/api/browser/key", { key: e.key }); e.preventDefault(); }
  });

  _lastTabsSig = null;                       // force a tab-bar rebuild on (re)open
  _liveES = new EventSource("/api/browser/stream");
  _liveES.onmessage = e => {
    let d; try { d = JSON.parse(e.data); } catch { return; }
    if (d.frame && img) { img.src = d.frame; img.style.display = "block"; const w = $("#liveWait", body); if (w) w.style.display = "none"; }
    const u = $("#liveUrl", body);
    if (u && d.url && u.textContent !== d.url) { u.textContent = d.url; u.classList.add("on"); }
    if (d.tabs) renderLiveTabs(d.tabs);
  };
}
const _post = (p, b) => fetch(p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b) });
let _lastTabsSig = null;
function renderLiveTabs(tabs) {
  const bar = $("#liveTabs"); if (!bar) return;
  // frames arrive ~10×/s and carry the tab list — only rebuild the DOM when the
  // tabs actually changed, otherwise hover states die mid-hover (the flicker)
  const sig = JSON.stringify((tabs || []).map(t => [t.id, t.title, t.url, t.active]));
  if (sig === _lastTabsSig) return;
  _lastTabsSig = sig;
  if (!tabs || tabs.length <= 1) { bar.style.display = "none"; bar.innerHTML = ""; return; }
  bar.style.display = "flex";
  bar.innerHTML = "";
  tabs.forEach(t => {
    const el = document.createElement("div"); el.className = "live-tab" + (t.active ? " active" : "");
    el.title = t.url || "";
    el.innerHTML = `<span class="lt-title">${escapeHtml(t.title || t.url || "tab")}</span><button class="lt-close" title="close tab">✕</button>`;
    el.onclick = e => { if (e.target.closest(".lt-close")) return; _post("/api/browser/tab", { id: t.id }); };
    $(".lt-close", el).onclick = e => { e.stopPropagation(); _post("/api/browser/tab/close", { id: t.id }); };
    bar.appendChild(el);
  });
}

/* ---------- Explorer window ---------- */
let _expCwd = "";
function openExplorer() {
  const { body, reused } = createWindow({ id: "win-explorer", title: "Files — workspace", icon: "▤", width: 880, height: 580,
    onClose: async () => {                          // warn before discarding unsaved (dirty) tabs
      const dirty = _edTabs.filter(t => t.dirty);
      if (!dirty.length) return;
      const names = dirty.map(t => t.path.split("/").pop()).join(", ");
      const ok = await confirmAction("Close with unsaved changes?",
        `${dirty.length} file${dirty.length > 1 ? "s have" : " has"} unsaved edits (${names}). Closing the window loses them.`,
        "Close anyway");
      if (!ok) return false;                        // veto — keep the window open
    } });
  if (reused) return;
  body.classList.add("exp-win");
  const r = parseFloat(localStorage.getItem("oceano.exp.ratio"));
  const ratio = (r >= 15 && r <= 72) ? r : 35;                 // file tree / editor split, %
  body.innerHTML = `
    <div class="exp-split">
      <div class="exp-left" id="expLeft" style="width:${ratio}%">
        <div class="exp-bar">
          <button class="exp-btn" id="expUp" title="up a folder">↰</button>
          <div class="exp-crumbs" id="expCrumbs"></div>
          <button class="exp-btn" id="expNewDir" title="new folder">＋▱</button>
          <button class="exp-btn" id="expNewFile" title="new file">＋▤</button>
          <button class="exp-btn" id="expRefresh" title="refresh">↻</button>
        </div>
        <div class="exp-list" id="expList"></div>
      </div>
      <div class="exp-divider" id="expDivider" title="drag to resize"></div>
      <div class="exp-right" id="expRight">
        <div class="ed-tabs" id="edTabs" style="display:none"></div>
        <div class="ed-stack" id="edStack"><div class="exp-edit-empty">Select a file in the tree to open it. Open several — they become tabs.</div></div>
      </div>
    </div>`;
  _edTabs = []; _edActive = null;                  // fresh editor tabs for this window
  $("#expUp", body).onclick = () => expLoad(_expCwd.split("/").slice(0, -1).join("/"));
  $("#expNewDir", body).onclick = expNewFolder;
  $("#expNewFile", body).onclick = expNewFile;
  $("#expRefresh", body).onclick = () => expLoad(_expCwd);
  $("#expList", body).addEventListener("contextmenu", e => {
    if (e.target.closest(".exp-row")) return;
    e.preventDefault();
    showCtx(e.clientX, e.clientY, [{ label: "New folder", action: expNewFolder }, { label: "New file", action: expNewFile }, { label: "Refresh", action: () => expLoad(_expCwd) }]);
  });
  _wireExpDivider(body);
  expLoad("");
  _edRestore();                                    // reopen the tabs from last time
}
function _wireExpDivider(body) {
  const div = $("#expDivider", body), left = $("#expLeft", body), split = $(".exp-split", body);
  div.addEventListener("mousedown", e => {
    e.preventDefault();
    const move = ev => {
      const r = split.getBoundingClientRect();
      const pct = Math.max(15, Math.min(72, ((ev.clientX - r.left) / r.width) * 100));
      left.style.width = pct + "%";
    };
    const up = () => {
      document.removeEventListener("mousemove", move); document.removeEventListener("mouseup", up);
      document.body.style.userSelect = "";
      const pct = parseFloat(left.style.width); if (pct) localStorage.setItem("oceano.exp.ratio", pct.toFixed(0));
    };
    document.body.style.userSelect = "none";
    document.addEventListener("mousemove", move); document.addEventListener("mouseup", up);
  });
}
/* ---- editor tabs (multiple files open at once in the Files window, persisted) ---- */
let _edTabs = [], _edActive = null;
const ED_EMPTY = `<div class="exp-edit-empty">Select a file in the tree to open it. Open several — they become tabs.</div>`;
function _edPersist() {                                         // remember which files are open + active
  try {
    localStorage.setItem("oceano.exp.tabs", JSON.stringify({
      paths: _edTabs.map(t => t.path), active: _edActive ? _edActive.path : null,
    }));
  } catch {}
}
function expOpenFile(path, activate = true) {
  const stack = $("#edStack"); if (!stack) return null;        // Files window not open
  const existing = _edTabs.find(t => t.path === path);
  if (existing) { if (activate) _edActivate(existing); return existing; }
  if (!_edTabs.length) stack.innerHTML = "";                   // drop the empty placeholder
  const pane = document.createElement("div"); pane.className = "ed-pane"; pane.style.display = "none"; stack.appendChild(pane);
  const tabEl = document.createElement("div"); tabEl.className = "ed-tab"; tabEl.title = path;
  tabEl.innerHTML = `<span class="ed-tab-dot">●</span><span class="ed-tab-name">${escapeHtml(path.split("/").pop())}</span><button class="ed-tab-x" title="close">✕</button>`;
  const tab = { path, pane, tabEl, cm: null, dirty: false };
  tabEl.onclick = e => { if (!e.target.closest(".ed-tab-x")) _edActivate(tab); };
  $(".ed-tab-x", tabEl).onclick = e => { e.stopPropagation(); _edClose(tab); };
  $("#edTabs").appendChild(tabEl);
  _edTabs.push(tab);
  $("#edTabs").style.display = "flex";
  if (activate) _edActivate(tab);
  const isImg = /\.(png|jpe?g|gif|webp|svg|bmp|ico|avif)$/i.test(path);
  _mountEditor(pane, path, {
    onSaved: () => expLoad(_expCwd),
    onDirty: d => { tab.dirty = d; tabEl.classList.toggle("dirty", d); },
    onPathChange: np => { tab.path = np; $(".ed-tab-name", tabEl).textContent = np.split("/").pop(); tabEl.title = np; _edPersist(); },
  }).then(cm => {
    tab.cm = cm;
    if (!cm && !isImg) { _edClose(tab, true); toast("Couldn't open " + path.split("/").pop(), "err"); return; }  // file gone
    if (_edActive === tab && cm) setTimeout(() => cm.refresh(), 10);
  });
  _edPersist();
  return tab;
}
function _edActivate(tab) {
  _edActive = tab;
  _edTabs.forEach(t => { t.pane.style.display = t === tab ? "flex" : "none"; t.tabEl.classList.toggle("active", t === tab); });
  $("#edTabs").style.display = _edTabs.length ? "flex" : "none";
  if (tab.cm) setTimeout(() => { tab.cm.refresh(); tab.cm.focus(); }, 10);
  _edPersist();
}
async function _edClose(tab, force = false) {
  if (!force && tab.dirty && !await confirmAction("Close without saving?",
      `“${tab.path.split("/").pop()}” has unsaved changes — they'll be lost.`, "Discard")) return;
  const i = _edTabs.indexOf(tab);
  if (i < 0) return;
  tab.pane.remove(); tab.tabEl.remove(); _edTabs.splice(i, 1);
  if (_edActive === tab) {
    _edActive = null;
    const next = _edTabs[i] || _edTabs[i - 1];
    if (next) _edActivate(next);
    else { $("#edStack").innerHTML = ED_EMPTY; $("#edTabs").style.display = "none"; }
  } else {
    $("#edTabs").style.display = _edTabs.length ? "flex" : "none";
  }
  _edPersist();
}
function _edRestore() {                                         // reopen the tabs from the last session
  let saved; try { saved = JSON.parse(localStorage.getItem("oceano.exp.tabs") || "null"); } catch { saved = null; }
  if (!saved || !Array.isArray(saved.paths) || !saved.paths.length) return;
  saved.paths.forEach(p => expOpenFile(p, false));             // open all without stealing focus
  const act = _edTabs.find(t => t.path === saved.active) || _edTabs[_edTabs.length - 1];
  if (act) _edActivate(act);                                   // then focus the one that was active
}
async function expLoad(path) {
  const d = await api("/api/files?path=" + encodeURIComponent(path || ""));
  _expCwd = d.path;
  const cr = $("#expCrumbs"); cr.innerHTML = "";
  const root = document.createElement("span"); root.textContent = "workspace"; root.onclick = () => expLoad(""); cr.appendChild(root);
  let acc = "";
  (d.path ? d.path.split("/") : []).forEach(part => { acc = acc ? acc + "/" + part : part; const here = acc; cr.append(" / "); const s = document.createElement("span"); s.textContent = part; s.onclick = () => expLoad(here); cr.appendChild(s); });
  const list = $("#expList"); list.innerHTML = "";
  if (!d.entries.length) { list.innerHTML = `<div class="exp-empty">empty folder</div>`; return; }
  d.entries.forEach(e => {
    const row = document.createElement("div"); row.className = "exp-row" + (e.dir ? " dir" : "");
    row.innerHTML = `<span class="ei">${e.dir ? "▸" : "·"}</span><span class="en">${escapeHtml(e.name)}</span>` + (e.dir ? "" : `<span class="es">${fmtSize(e.size)}</span>`);
    row.onclick = () => {                                      // single-click: select; files open in the pane
      $$(".exp-row", list).forEach(r => r.classList.remove("sel")); row.classList.add("sel");
      if (!e.dir) expOpenFile(e.path);
    };
    row.ondblclick = () => { if (e.dir) expLoad(e.path); };     // double-click a folder to enter it
    row.oncontextmenu = ev => {
      ev.preventDefault();
      $$(".exp-row", list).forEach(r => r.classList.remove("sel")); row.classList.add("sel");
      const items = e.dir
        ? [{ label: "Open", action: () => expLoad(e.path) }]
        : [{ label: "Open here", action: () => expOpenFile(e.path) },
           { label: "Open in new window", action: () => openFileWindow(e.path) },
           { label: "Download", action: () => window.open("/api/raw?path=" + encodeURIComponent(e.path), "_blank") }];
      items.push({ label: "Rename", action: () => expRename(e) }, { sep: true }, { label: "Delete", danger: true, action: () => expDelete(e) });
      showCtx(ev.clientX, ev.clientY, items);
    };
    list.appendChild(row);
  });
}
async function expNewFolder() { const n = await promptDialog("New folder", { placeholder: "folder name", okLabel: "Create" }); if (!n) return; await fetch("/api/folder", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: _expCwd ? _expCwd + "/" + n : n }) }); expLoad(_expCwd); }
async function expNewFile() { const n = await promptDialog("New file", { placeholder: "file name (e.g. notes.md)", okLabel: "Create" }); if (!n) return; await fetch("/api/file", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: _expCwd ? _expCwd + "/" + n : n, content: "" }) }); expLoad(_expCwd); }
async function expRename(e) { const n = await promptDialog("Rename", { value: e.name, okLabel: "Rename" }); if (!n || n === e.name) return; await fetch("/api/rename", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: e.path, to: _expCwd ? _expCwd + "/" + n : n }) }); expLoad(_expCwd); }
async function expDelete(e) { if (!await confirmAction("Delete " + (e.dir ? "folder" : "file") + "?", `“${e.name}” will be deleted${e.dir ? " with its contents" : ""}.`)) return; await fetch("/api/file?path=" + encodeURIComponent(e.path), { method: "DELETE" }); expLoad(_expCwd); }

/* ---------- file viewer / editor window (CodeMirror code editor) ---------- */
// Major languages to offer in the dropdown. We list one only if its CodeMirror mode
// is actually loaded, and we key options on the MIME meta returns — so the dropdown
// always matches what extension-detection picks (that's why Rust now shows as Rust,
// not "Plain text"). clike covers C/C++/C#/Java/Kotlin/Scala/Objective-C; javascript
// covers JS/TS/JSON — so they appear without separate mode files.
const ED_MAJOR = ["Plain Text", "Python", "JavaScript", "TypeScript", "JSON", "HTML", "CSS", "Java",
  "C", "C++", "C#", "Go", "Rust", "Ruby", "PHP", "Swift", "Kotlin", "Scala", "Objective-C",
  "Shell", "SQL", "YAML", "TOML", "Markdown", "Lua", "Dockerfile", "XML"];
let _edLangCache = null;
function _edLangOptions() {
  if (_edLangCache) return _edLangCache;
  const out = [["", "Plain text"]], seen = new Set();
  (CodeMirror.modeInfo || []).forEach(m => {
    if (seen.has(m.name) || !ED_MAJOR.includes(m.name)) return;
    if (!m.mode || !CodeMirror.modes[m.mode]) return;   // mode script not loaded → don't offer it
    seen.add(m.name); out.push([m.mime || m.mode, m.name]);
  });
  out.splice(1, out.length, ...out.slice(1).sort((a, b) => a[1].localeCompare(b[1])));  // alpha, after Plain text
  _edLangCache = out;
  return out;
}
function _fwMime(path) {
  try { const i = CodeMirror.findModeByFileName(path.split("/").pop()); if (i) return i.mime || i.mode; } catch {}
  return "";
}
/* Build a full CodeMirror editor (toolbar + buffer) inside `container`. Reused by the
   Files window's editor pane AND the standalone file window. opts.onSaved fires after
   a successful save/save-as (used to refresh the tree). Returns the CM instance. */
async function _mountEditor(container, path, opts = {}) {
  const onSaved = opts.onSaved || (() => {});
  const onDirty = opts.onDirty || (() => {});       // (bool) — for the tab's unsaved dot
  const onPathChange = opts.onPathChange || (() => {});   // (newPath) — after Save as…
  const isImg = /\.(png|jpe?g|gif|webp|svg|bmp|ico|avif)$/i.test(path);
  if (isImg) { container.innerHTML = `<div class="fw-img"><img src="/api/raw?path=${encodeURIComponent(path)}"></div>`; return null; }
  const d = await api("/api/file?path=" + encodeURIComponent(path));
  if (d.content == null && !d.binary) {            // missing/unreadable (e.g. a remembered file that was deleted)
    container.innerHTML = `<div class="exp-edit-empty">⚠ Couldn't open <b>${escapeHtml(path)}</b> — it may have been moved or deleted.</div>`;
    return null;
  }
  const langs = _edLangOptions().map(([v, l]) => `<option value="${v}">${escapeHtml(l)}</option>`).join("");
  container.innerHTML = `
    <div class="fw-bar">
      <span class="fw-name" title="${escapeHtml(path)}">${escapeHtml(path)}</span>
      <span class="fe-dirty fw-dirty" title="unsaved changes">●</span>
      <span class="fe-spacer"></span>
      <select class="fe-lang fw-lang" title="syntax mode">${langs}</select>
      <button class="ed-btn fw-find" title="Find / replace (Ctrl-F)">⌕</button>
      <button class="ed-btn fw-wrap" title="Toggle line wrap">⏎</button>
      <button class="ed-btn fw-saveas" title="Save as a new file">Save as…</button>
      <button class="primary sm fw-save">Save</button>
    </div>
    <div class="fw-cm"></div>`;
  const cm = CodeMirror($(".fw-cm", container), {
    value: d.binary ? "(binary file — not editable here)" : d.content,
    mode: _fwMime(path) || null, theme: "material-darker", lineNumbers: true, lineWrapping: false,
    readOnly: !!d.binary, matchBrackets: true, autoCloseBrackets: true, styleActiveLine: true,
    indentUnit: 2, tabSize: 2,
    extraKeys: { "Ctrl-S": () => save(), "Cmd-S": () => save(),
                 "Ctrl-F": "findPersistent", "Cmd-F": "findPersistent", "Alt-F": "replace",
                 "Shift-Ctrl-F": "replaceAll", "Ctrl-/": "toggleComment", "Cmd-/": "toggleComment" } });
  let curPath = path, dirty = false;
  const dot = $(".fw-dirty", container);
  cm.on("change", () => { if (!dirty) { dirty = true; dot.classList.add("on"); onDirty(true); } });
  const sel = $(".fw-lang", container), mime = _fwMime(path);
  sel.value = [...sel.options].some(o => o.value === mime) ? mime : "";
  sel.onchange = () => cm.setOption("mode", sel.value || null);
  async function write(p) {
    await fetch("/api/file", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: p, content: cm.getValue() }) });
  }
  async function save() {
    await write(curPath); dirty = false; dot.classList.remove("on"); onDirty(false);
    const b = $(".fw-save", container); b.textContent = "Saved ✓"; setTimeout(() => b.textContent = "Save", 1000);
    onSaved();
  }
  $(".fw-save", container).onclick = save;
  $(".fw-saveas", container).onclick = async () => {
    const np = await promptDialog("Save as", { value: curPath, message: "Path relative to the workspace", okLabel: "Save" }); if (!np || np === curPath) { if (np === curPath) save(); return; }
    await write(np); curPath = np;
    $(".fw-name", container).textContent = np; $(".fw-name", container).title = np;
    dirty = false; dot.classList.remove("on"); onDirty(false); onPathChange(np); onSaved();
  };
  $(".fw-wrap", container).onclick = () => { const w = !cm.getOption("lineWrapping"); cm.setOption("lineWrapping", w); $(".fw-wrap", container).classList.toggle("on", w); };
  $(".fw-find", container).onclick = () => { cm.focus(); cm.execCommand("findPersistent"); };
  setTimeout(() => cm.refresh(), 30);             // CM mis-measures in a freshly-shown box
  if (window.ResizeObserver) new ResizeObserver(() => cm.refresh()).observe($(".fw-cm", container));  // re-layout on resize
  return cm;
}
async function openFileWindow(path) {                          // standalone pop-out editor window
  const isImg = /\.(png|jpe?g|gif|webp|svg|bmp|ico|avif)$/i.test(path);
  const name = path.split("/").pop();
  const { body, reused } = createWindow({ id: "fw-" + path.replace(/[^a-z0-9]/gi, "_"), title: name, icon: isImg ? "▦" : "ℜ", width: 640, height: 520 });
  if (reused) return;
  await _mountEditor(body, path, { onSaved: () => { if (typeof _expCwd === "string") expLoad(_expCwd); } });
}

/* ---------- Brain window (memory + skills) ---------- */
const BRAIN_TABS = [["mem", "✶", "Memory"], ["kn", "◈", "Knowledge"], ["skills", "⚒", "Skills"], ["rivers", "🌊", "Rivers"], ["evals", "⚖", "Evals"]];
function openBrain(tab) {
  const { body, reused } = createWindow({ id: "win-brain", title: "Brain", icon: "✶", width: 720, height: 580,
    onClose: () => { if (_riverTimer) { clearInterval(_riverTimer); _riverTimer = null; } if (_skillEvalTimer) { clearTimeout(_skillEvalTimer); _skillEvalTimer = null; } if (_evalTimer) { clearTimeout(_evalTimer); _evalTimer = null; } } });
  if (!reused) {
    body.classList.add("set-win");
    body.innerHTML = `
      <div class="set-layout">
        <div class="set-tabs">${BRAIN_TABS.map((t, i) =>
          `<button class="set-tab${i === 0 ? " active" : ""}" data-tab="${t[0]}"><span class="sti">${t[1]}</span>${t[2]}</button>`).join("")}</div>
        <div class="set-pane brain-pane" id="brainBody"></div>
      </div>`;
    $$(".set-tab", body).forEach(t => t.onclick = () => {
      $$(".set-tab", body).forEach(x => x.classList.toggle("active", x === t));
      brainTab(t.dataset.tab);
    });
  }
  const want = tab || (reused ? null : "mem");
  if (want) {
    const btn = $$(".set-tab", body).find(x => x.dataset.tab === want);
    if (btn) btn.click(); else if (!reused) brainTab("mem");
  }
}
function brainTab(which) {
  const c = $("#brainBody"); if (!c) return;
  if (_riverTimer) { clearInterval(_riverTimer); _riverTimer = null; }   // stop riverbook polling when leaving the tab
  if (_skillEvalTimer) { clearTimeout(_skillEvalTimer); _skillEvalTimer = null; }
  if (_evalTimer) { clearTimeout(_evalTimer); _evalTimer = null; }
  if (which === "mem") {
    c.innerHTML = `<div class="mem-add"><input id="bMemText" placeholder="Teach Oceano a durable fact…"><input id="bMemTags" class="mem-tags" placeholder="tags"><button class="primary sm" id="bMemAdd">Remember</button></div><div class="mem-list" id="bMemList"></div>`;
    const add = async () => { const t = $("#bMemText").value.trim(); if (!t) return; await fetch("/api/memories", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: t, tags: $("#bMemTags").value.trim() }) }); $("#bMemText").value = ""; $("#bMemTags").value = ""; loadBrainMem(); };
    $("#bMemAdd").onclick = add;
    $("#bMemText").addEventListener("keydown", e => { if (e.key === "Enter") add(); });
    loadBrainMem();
  } else if (which === "kn") {
    renderKnowledge(c);
  } else if (which === "rivers") {
    renderRivers(c);
  } else if (which === "evals") {
    renderEvals(c);
  } else {
    c.innerHTML = `
      <div class="brain-head">
        <div class="sk-tabs">
          <button class="sk-tab on" data-f="published">Published<span class="sk-cnt" id="skCntPub"></span></button>
          <button class="sk-tab" data-f="review">In review<span class="sk-cnt" id="skCntRev"></span></button>
        </div>
        <span style="flex:1"></span>
        <button class="exp-btn" id="bSkEval" title="review learning skills now — independent review by Claude Code, then the local model publishes from staging">⚖ Evaluate now</button>
        <button class="exp-btn" id="bSkNew">＋ New skill</button>
      </div>
      <div class="kn-note" id="skMsg"></div>
      <div class="brain-skills" id="bSkBody"></div>`;
    $("#bSkNew").onclick = () => openSkill(null);
    $("#bSkEval").onclick = startSkillEval;
    $$(".sk-tab", c).forEach(b => b.onclick = () => {
      $$(".sk-tab", c).forEach(x => x.classList.toggle("on", x === b));
      _skillFilter = b.dataset.f; loadBrainSkills();
    });
    _skillFilter = "published";
    loadBrainSkills(); refreshSkillEval(false);
  }
}
async function loadBrainMem() {
  const list = $("#bMemList"); if (!list) return;
  const mems = await api("/api/memories"); list.innerHTML = "";
  if (!mems.length) { list.innerHTML = `<div class="empty-note">No memories yet.</div>`; return; }
  const CATS = ["identity", "preference", "project", "fact", "task"];
  mems.forEach(m => {
    const row = document.createElement("div"); row.className = "mem-row" + (m.pinned ? " pinned" : "");
    const catSel = `<select class="mr-cat" title="memory type">${CATS.map(c => `<option value="${c}"${c === m.category ? " selected" : ""}>${c}</option>`).join("")}</select>`;
    row.innerHTML = `<button class="mr-pin${m.pinned ? " on" : ""}" title="${m.pinned ? "pinned — always injected" : "pin (always inject)"}">📌</button>` +
      `<div class="mr-body"><div class="mr-text">${escapeHtml(m.text)}</div><div class="mr-meta">${catSel}<span class="mr-date">${(m.ts || "").slice(0, 10)}</span></div></div><button class="mr-del">✕</button>`;
    $(".mr-pin", row).onclick = async () => { await fetch("/api/memories/" + m.id, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ pinned: !m.pinned }) }); loadBrainMem(); };
    $(".mr-cat", row).onchange = e => fetch("/api/memories/" + m.id, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ category: e.target.value }) });
    $(".mr-del", row).onclick = async () => { if (!await confirmAction("Delete memory?", m.text.slice(0, 100))) return; await fetch("/api/memories/" + m.id, { method: "DELETE" }); loadBrainMem(); };
    list.appendChild(row);
  });
}
let _skillFilter = "published", _skillEvalTimer = null;
const patchSkill = (dir, status, notes) =>
  fetch("/api/skills/" + encodeURIComponent(dir), { method: "PATCH", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(notes === undefined ? { status } : { status, notes }) });
async function loadBrainSkills() {
  const body = $("#bSkBody"); if (!body) return;
  skillsCache = await api("/api/skills");
  const pub = skillsCache.filter(s => s.status === "published");
  const rev = skillsCache.filter(s => s.status !== "published");
  const cp = $("#skCntPub"), cr = $("#skCntRev");
  if (cp) cp.textContent = pub.length; if (cr) cr.textContent = rev.length;
  const list = _skillFilter === "review" ? rev : pub;
  body.innerHTML = "";
  if (!list.length) {
    body.innerHTML = `<div class="empty-note">${_skillFilter === "review"
      ? "Nothing in review. When the agent teaches itself something (learn_skill), it lands here for independent validation before going live."
      : "No published skills yet — create one, or let Oceano learn its own as it works."}</div>`;
    return;
  }
  list.forEach(s => {
    const c = document.createElement("div"); c.className = "skill-card st-" + s.status;
    const chip = s.status === "published" ? `<span class="sk-chip pub">published</span>`
               : s.status === "staged" ? `<span class="sk-chip stg">staged · approved</span>`
               : `<span class="sk-chip lrn">learning · awaiting review</span>`;
    c.innerHTML = `<div class="sk-head"><h3>${escapeHtml(s.name)}</h3>${chip}</div>
      <div class="sc-desc">${escapeHtml(s.description)}</div>
      ${s.notes ? `<div class="sk-notes">${escapeHtml(s.notes)}</div>` : ""}
      <div class="sc-snip">${escapeHtml((s.body || "").slice(0, 90))}…</div>
      <div class="sk-actions"></div>`;
    const acts = $(".sk-actions", c);
    const mk = (label, fn) => { const b = document.createElement("button"); b.className = "sr-btn"; b.textContent = label;
      b.onclick = e => { e.stopPropagation(); fn(); }; acts.appendChild(b); };
    if (s.status === "staged") {
      mk("publish", async () => { await patchSkill(s.dir, "published"); loadBrainSkills(); });
      mk("reject", async () => { await patchSkill(s.dir, "learning", "✗ rejected by user"); loadBrainSkills(); });
    } else if (s.status === "learning") {
      mk("publish anyway", async () => {
        if (!await confirmAction("Publish without review?", `“${s.name}” hasn't been validated by the independent reviewer.`, "Publish")) return;
        await patchSkill(s.dir, "published", "published manually — skipped review"); loadBrainSkills();
      });
    } else {
      mk("unpublish", async () => { await patchSkill(s.dir, "staged", "unpublished by user"); loadBrainSkills(); });
    }
    c.onclick = () => openSkill(s); body.appendChild(c);
  });
}
async function startSkillEval() {
  const msg = $("#skMsg");
  if (msg) { msg.textContent = "starting evaluation — delegating review to Claude Code (can take a few minutes)…"; msg.className = "kn-note run"; }
  await fetch("/api/skills/evaluate", { method: "POST" });
  refreshSkillEval(true);
}
async function refreshSkillEval(loop) {
  const msg = $("#skMsg"); if (!msg) { _skillEvalTimer = null; return; }
  let st; try { st = await api("/api/skills-eval"); } catch { _skillEvalTimer = null; return; }
  if (st.running) {
    msg.textContent = "evaluation running — independent review in progress…"; msg.className = "kn-note run";
    _skillEvalTimer = setTimeout(() => refreshSkillEval(loop), 3000);
  } else {
    _skillEvalTimer = null;
    if (loop) loadBrainSkills();
    if (st.last) { msg.textContent = "last evaluation: " + st.last; msg.className = "kn-note ok"; }
  }
}

/* ---------- Brain → Knowledge (embedding engine: stats, indexing, search) ---------- */
function renderKnowledge(c) {
  c.innerHTML = `
    <div class="kn-stats" id="knStats"></div>
    <div class="kn-embed" id="knEmbed">checking embedding engine…</div>
    <div class="kn-sec-label">Index documents</div>
    <div class="kn-row"><input id="knFolder" placeholder="folder path — absolute, or relative to workspace"><button class="primary sm" id="knIndex">Index</button></div>
    <div class="kn-note" id="knIndexNote"></div>
    <div class="kn-sec-label">Semantic search</div>
    <div class="kn-scope"><button data-scope="memory" class="on">Memories</button><button data-scope="docs">Documents</button></div>
    <div class="kn-row"><input id="knQuery" placeholder="search by meaning, not keywords…"><button class="primary sm" id="knSearch">Search</button></div>
    <div class="kn-note" id="knNote"></div>
    <div class="kn-results" id="knResults"></div>`;
  let scope = "memory";
  $$(".kn-scope button", c).forEach(b => b.onclick = () => { scope = b.dataset.scope; $$(".kn-scope button", c).forEach(x => x.classList.toggle("on", x === b)); });
  const doSearch = async () => {
    const q = $("#knQuery").value.trim(); if (!q) return;
    const note = $("#knNote"); note.textContent = "searching…"; note.className = "kn-note run";
    try {
      const r = await api("/api/brain/search", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ scope, query: q }) });
      renderKnResults(r.results || [], scope, note);
    } catch { note.textContent = "search failed"; note.className = "kn-note err"; }
  };
  $("#knSearch").onclick = doSearch;
  $("#knQuery").addEventListener("keydown", e => { if (e.key === "Enter") doSearch(); });
  $("#knIndex").onclick = async () => {
    const folder = $("#knFolder").value.trim(); if (!folder) return;
    const note = $("#knIndexNote"); note.textContent = "indexing… (embedding each chunk — may take a moment)"; note.className = "kn-note run";
    try {
      const r = await api("/api/brain/index", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ folder }) });
      note.textContent = r.result; note.className = "kn-note " + (r.ok ? "ok" : "err");
      loadKnStats();
    } catch { note.textContent = "index failed"; note.className = "kn-note err"; }
  };
  loadKnStats();
}
async function loadKnStats() {
  let s; try { s = await api("/api/brain/stats"); } catch { return; }
  const st = $("#knStats");
  if (st) st.innerHTML =
    `<div class="kn-stat"><div class="kv">${s.memories}</div><div class="kl">memories</div></div>` +
    `<div class="kn-stat"><div class="kv">${s.docs.files}</div><div class="kl">documents</div></div>` +
    `<div class="kn-stat"><div class="kv">${s.docs.chunks}</div><div class="kl">chunks</div></div>` +
    `<div class="kn-stat"><div class="kv">${s.embed.dims || "—"}</div><div class="kl">dimensions</div></div>`;
  const em = $("#knEmbed");
  if (em) em.innerHTML = `<span class="svc-dot ${s.embed.ok ? "on" : "off"}"></span><span>Embedding engine · <code>${escapeHtml(s.embed.model)}</code> · ${s.embed.ok ? "online" : "offline"} · <code>${escapeHtml(s.embed.url)}</code></span>`;
}
function renderKnResults(results, scope, note) {
  const box = $("#knResults"); box.innerHTML = "";
  if (!results.length) { note.textContent = "no matches"; note.className = "kn-note"; return; }
  note.textContent = `${results.length} result${results.length > 1 ? "s" : ""} · best first`; note.className = "kn-note ok";
  results.forEach(r => {
    const div = document.createElement("div"); div.className = "kn-hit";
    const src = scope === "memory" ? (r.tags || "memory") : r.name;
    const text = scope === "memory" ? r.text : r.chunk;
    div.innerHTML = `<div class="khh"><span class="ksrc">${escapeHtml(src)}</span><span class="kscore">${r.score}</span></div><div class="ktext">${escapeHtml(text)}</div>`;
    box.appendChild(div);
  });
}

/* ---------- Brain → Rivers (HF catalog · hwfit · download · serve) ---------- */
let _riverTimer = null, _riverHw = null;
const fmtNum = n => n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? (n / 1e3).toFixed(1) + "k" : "" + (n || 0);
const GB = 1073741824;
function fitClient(size) {                       // mirrors riverbook.fit() for installed models
  const v = _riverHw && _riverHw.vram_total; if (!v) return { verdict: "unknown", ngl: 99 };
  const u = v * 0.92;
  if (size <= u * 0.8) return { verdict: "fits", ngl: 99 };
  if (size <= u * 1.5) return { verdict: "partial", ngl: Math.max(1, Math.floor(99 * u / size)) };
  return { verdict: "cpu", ngl: 0 };
}
function renderRivers(c) {
  c.innerHTML = `
    <div class="river-hw" id="riverHw">detecting hardware…</div>
    <div class="river-sec">Recommended for your machine <span class="river-subtle" id="riverRecHint"></span></div>
    <div class="river-filters">
      <div class="river-chips" id="riverRecChips">
        <button class="river-chip on" data-f="all">All</button>
        <button class="river-chip" data-f="fits">Fits</button>
        <button class="river-chip" data-f="partial">Partial</button>
        <button class="river-chip" data-f="cpu">Won't fit</button>
      </div>
      <select class="river-sortsel" id="riverRecSort">
        <option value="score">order: score ▾</option>
        <option value="size">order: size ▾</option>
        <option value="name">order: name</option>
      </select>
    </div>
    <div class="river-rec" id="riverRec"></div>
    <div class="river-sec">Search Hugging Face</div>
    <div class="river-search"><input id="riverQ" placeholder="search for any GGUF model…" autocomplete="off"><button class="primary sm" id="riverGo">Search</button></div>
    <div class="river-note" id="riverNote"></div>
    <div class="river-repos" id="riverRepos"></div>
    <div id="riverJobsWrap"></div>
    <div class="river-sec">Installed models <span class="river-subtle" id="riverInstCount"></span></div>
    <input class="river-find" id="riverFind" placeholder="filter models on this device…" autocomplete="off">
    <div id="riverInstalled"></div>`;
  const go = () => riverSearch($("#riverQ").value.trim());
  $("#riverGo").onclick = go;
  $("#riverQ").addEventListener("keydown", e => { if (e.key === "Enter") go(); });
  $$("#riverRecChips .river-chip").forEach(b => b.onclick = () => {
    $$("#riverRecChips .river-chip").forEach(x => x.classList.toggle("on", x === b));
    _riverRecFilter = b.dataset.f; riverRenderRec();
  });
  $("#riverRecSort").onchange = e => { _riverRecSort = e.target.value; riverRenderRec(); };
  $("#riverFind").addEventListener("input", riverRenderInstalled);
  riverLoadHw(); riverLoadRecommended(); riverLoadInstalled(); riverPoll();
}
async function riverLoadHw() {
  try { _riverHw = await api("/api/rivers/hw"); } catch { return; }
  const el = $("#riverHw"); if (!el) return;
  const tot = _riverHw.vram_total ? (_riverHw.vram_total / GB).toFixed(1) + " GB" : "—";
  const free = _riverHw.vram_free ? " · " + (_riverHw.vram_free / GB).toFixed(1) + " GB free" : "";
  el.innerHTML = `<span class="svc-dot ${_riverHw.gpu ? "on" : "off"}"></span><span>Backend <code>${escapeHtml(_riverHw.backend)}</code> · <code>${escapeHtml(_riverHw.gpu || "CPU only")}</code></span><span class="vram">VRAM ${tot}${free}</span>`;
}
let _riverRec = [], _riverRecFilter = "all", _riverRecSort = "score";
async function riverLoadRecommended() {
  const box = $("#riverRec"); if (!box) return;
  box.innerHTML = `<div class="river-note run">scoring models against your hardware…</div>`;
  let d; try { d = await api("/api/rivers/recommended"); } catch { box.innerHTML = ""; return; }
  _riverRec = d.models || [];
  const hint = $("#riverRecHint"); if (hint) hint.textContent = "· auto-scored by GPU fit";
  riverRenderRec();
}
function riverRenderRec() {
  const box = $("#riverRec"); if (!box) return;
  let list = _riverRec.filter(m => _riverRecFilter === "all" || m.fit.verdict === _riverRecFilter);
  const by = { score: (a, b) => (b.fit.score || 0) - (a.fit.score || 0),
               size: (a, b) => b.size - a.size,
               name: (a, b) => a.label.localeCompare(b.label) };
  list = list.slice().sort(by[_riverRecSort] || by.score);
  if (!list.length) { box.innerHTML = `<div class="empty-note">No models in this category.</div>`; return; }
  box.innerHTML = "";
  list.forEach(m => {
    const v = m.fit.verdict, score = m.fit.score == null ? "—" : m.fit.score;
    const row = document.createElement("div"); row.className = "river-rec-row";
    row.innerHTML =
      `<div class="rr-score fit ${v}" title="${escapeHtml(m.fit.note)}"><b>${score}</b><span>${v}</span></div>` +
      `<div class="rr-main"><div class="rr-name">${escapeHtml(m.label)} <span class="rr-params">${escapeHtml(m.params)}</span></div>` +
      `<div class="rr-sub">${escapeHtml(m.quant)} · ${fmtSize(m.size)} · ${escapeHtml(m.repo)}</div></div>` +
      (m.downloaded ? `<span class="served">on disk</span>` : `<button class="btn-mini rr-dl">Download</button>`);
    const dl = $(".rr-dl", row);
    if (dl) dl.onclick = () => riverDownload(m.repo, m.filename, dl);
    box.appendChild(row);
  });
}
async function riverSearch(q) {
  if (!q) return;
  const note = $("#riverNote"), repos = $("#riverRepos");
  note.textContent = "searching Hugging Face…"; note.className = "river-note run"; repos.innerHTML = "";
  let r; try { r = await api("/api/rivers/search?q=" + encodeURIComponent(q)); } catch { return; }
  if (r.error) { note.textContent = r.error; note.className = "river-note err"; return; }
  if (!r.results.length) { note.textContent = "no GGUF repos found"; note.className = "river-note"; return; }
  note.textContent = `${r.results.length} repos · click one to see its quants`; note.className = "river-note ok";
  r.results.forEach(m => {
    const el = document.createElement("div"); el.className = "river-repo";
    el.innerHTML = `<span class="rn">${escapeHtml(m.repo)}</span><span class="rd">↓${fmtNum(m.downloads)} · ♥${fmtNum(m.likes)}</span>`;
    el.onclick = () => riverToggleRepo(el, m.repo);
    repos.appendChild(el);
  });
}
async function riverToggleRepo(el, repo) {
  const open = el.classList.toggle("open");
  const nx = el.nextElementSibling;
  if (nx && nx.classList.contains("river-files")) nx.remove();
  if (!open) return;
  const box = document.createElement("div"); box.className = "river-files";
  box.innerHTML = `<div class="river-note run">loading files…</div>`; el.after(box);
  let d; try { d = await api("/api/rivers/files?repo=" + encodeURIComponent(repo)); } catch { box.innerHTML = `<div class="river-note err">failed to load files</div>`; return; }
  if (d.gated || d.error) { box.innerHTML = `<div class="river-note err">${escapeHtml(d.error || "error")}</div>`; return; }
  if (!d.files.length) { box.innerHTML = `<div class="river-note">no single-file GGUFs here (may be sharded)</div>`; return; }
  box.innerHTML = "";
  d.files.forEach(f => {
    const row = document.createElement("div"); row.className = "river-file";
    row.innerHTML = `<span class="cq">${escapeHtml(f.quant)}</span><span class="cs">${fmtSize(f.size)}</span><span class="fit ${f.fit.verdict}" title="${escapeHtml(f.fit.note)}">${f.fit.verdict}</span><button class="btn-mini cdl">Download</button>`;
    $(".cdl", row).onclick = () => riverDownload(repo, f.filename, $(".cdl", row));
    box.appendChild(row);
  });
}
async function riverDownload(repo, filename, btn) {
  btn.disabled = true; btn.textContent = "…";
  let r; try { r = await api("/api/rivers/download", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repo, filename }) }); } catch { btn.disabled = false; btn.textContent = "Download"; return; }
  if (r.already) { btn.textContent = "on disk"; riverLoadInstalled(); return; }
  if (r.error) { btn.textContent = "error"; const n = $("#riverNote"); if (n) { n.textContent = r.error; n.className = "river-note err"; } return; }
  btn.textContent = "downloading"; riverPoll();
}
function riverPoll() {
  if (_riverTimer) return;
  const tick = async () => {
    let d; try { d = await api("/api/rivers/jobs"); } catch { return; }
    riverRenderJobs(d.jobs || []);
    if (!(d.jobs || []).some(j => j.status === "downloading") && _riverTimer) { clearInterval(_riverTimer); _riverTimer = null; }
  };
  tick(); _riverTimer = setInterval(tick, 1500);
}
function riverRenderJobs(jobs) {
  const wrap = $("#riverJobsWrap"); if (!wrap) return;
  if (!jobs.length) { wrap.innerHTML = ""; return; }
  wrap.innerHTML = `<div class="river-sec">Downloads</div>` + jobs.map(j => {
    const pct = j.total ? Math.round(100 * j.downloaded / j.total) : 0;
    const st = j.status === "downloading" ? `${pct}% · ${fmtSize(j.downloaded)} / ${fmtSize(j.total)}`
      : j.status === "done" ? "done ✓" : "error: " + (j.error || "");
    return `<div class="river-job"><div class="cjh"><span>${escapeHtml(j.filename)}</span><span>${escapeHtml(st)}</span></div><div class="river-bar"><i style="width:${j.status === "done" ? 100 : pct}%"></i></div></div>`;
  }).join("");
  if (jobs.some(j => j.status === "done")) riverLoadInstalled();
}
let _riverInstalled = [];
async function riverLoadInstalled() {
  let d; try { d = await api("/api/rivers/installed"); } catch { return; }
  _riverInstalled = d.models || [];
  riverRenderInstalled();
}
function riverRenderInstalled() {
  const box = $("#riverInstalled"); if (!box) return;
  const q = ($("#riverFind") ? $("#riverFind").value.trim().toLowerCase() : "");
  const list = q ? _riverInstalled.filter(m => m.filename.toLowerCase().includes(q)) : _riverInstalled;
  const cnt = $("#riverInstCount"); if (cnt) cnt.textContent = `· ${list.length}${q ? " of " + _riverInstalled.length : ""}`;
  if (!_riverInstalled.length) { box.innerHTML = `<div class="empty-note">No models on disk yet.</div>`; return; }
  if (!list.length) { box.innerHTML = `<div class="empty-note">No on-device model matches “${escapeHtml(q)}”.</div>`; return; }
  box.innerHTML = list.map(m =>
    `<div class="river-inst"><span class="in">${escapeHtml(m.filename)}</span><span class="cs">${fmtSize(m.size)}</span>` +
    (m.served ? `<span class="served">▶ ${escapeHtml(m.served)}</span>`
              : `<button class="btn-mini cserve" data-f="${escapeHtml(m.filename)}" data-sz="${m.size}">Serve</button>`) +
    `</div>`).join("");
  $$(".cserve", box).forEach(b => b.onclick = () => riverServeDialog(b.dataset.f, +b.dataset.sz));
}
function riverServeDialog(filename, size) {
  const fitc = fitClient(size);
  const defName = filename.replace(/\.gguf$/i, "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40);
  const { body } = createWindow({ id: "win-serve", title: "Serve model — parameters", icon: "🌊", width: 470, height: 440 });
  body.classList.add("set-win");
  body.innerHTML = `<div class="drawer-section">
    <h3>Serve <code>${escapeHtml(filename)}</code></h3>
    <label class="field-label">Name <span class="lbl-sub">how it shows in the model picker</span></label>
    <input id="svName" value="${escapeHtml(defName)}" autocomplete="off">
    <div class="serve-grid">
      <div><label class="field-label">Context (tokens)</label><input id="svCtx" type="number" value="8192" min="256" step="1024"></div>
      <div><label class="field-label">GPU layers (ngl)</label><input id="svNgl" type="number" value="${fitc.ngl}" min="0" max="999"></div>
      <div><label class="field-label">KV cache</label><select id="svKv"><option value="f16">f16 (fastest)</option><option value="q8_0">q8_0</option><option value="q4_0">q4_0 (smallest)</option></select></div>
      <div><label class="field-label">TTL (sec resident)</label><input id="svTtl" type="number" value="600" min="0"></div>
    </div>
    <label class="serve-fa"><input type="checkbox" id="svFa" checked> Flash attention (<code>-fa</code>)</label>
    <div class="serve-hint">Bigger context needs more VRAM — the KV cache grows with it. On AMD/Vulkan, <b>f16</b> KV is usually fastest; quantize KV only if a huge context won't otherwise fit.</div>
    <div class="acct-actions"><span class="acct-msg" id="svMsg"></span><button class="primary sm" id="svGo">Serve</button></div>
  </div>`;
  $("#svGo", body).onclick = async () => {
    const msg = $("#svMsg", body), go = $("#svGo", body); go.disabled = true;
    const payload = { filename, name: $("#svName", body).value.trim(), ctx: +$("#svCtx", body).value,
      ngl: +$("#svNgl", body).value, kv: $("#svKv", body).value, fa: $("#svFa", body).checked, ttl: +$("#svTtl", body).value };
    let r; try { r = await api("/api/rivers/serve", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }); }
    catch { go.disabled = false; msg.textContent = "request failed"; msg.className = "acct-msg err"; return; }
    if (!r.ok) { msg.textContent = r.error; msg.className = "acct-msg err"; go.disabled = false; return; }
    msg.textContent = `✓ serving as "${r.name}"`; msg.className = "acct-msg ok";
    const note = $("#riverNote"); if (note) { note.textContent = `✓ "${r.name}" — ngl ${r.ngl} · ctx ${r.ctx} · KV ${r.kv} · fa ${r.fa ? "on" : "off"} · on :8081 + the picker`; note.className = "river-note ok"; }
    riverLoadInstalled(); loadModels();
    setTimeout(() => { const w = document.getElementById("win-serve"); if (w) w.remove(); }, 800);
  };
  setTimeout(() => { const e = $("#svName", body); if (e) e.focus(); }, 40);
}

/* ---------- Scheduler window (heartbeat + tasks) ---------- */
let _schedTimer = null;
const SCHED_PRESETS = { "every 5 min": "*/5 * * * *", "every 15 min": "*/15 * * * *", "hourly": "0 * * * *", "daily 8am": "0 8 * * *", "weekdays 9am": "0 9 * * 1-5", "weekly Mon 9am": "0 9 * * 1" };
function openScheduler() {
  const { body, reused } = createWindow({ id: "win-sched", title: "Scheduler — heartbeat & tasks", icon: "⏱", width: 660, height: 540,
    onClose: () => { if (_schedTimer) { clearInterval(_schedTimer); _schedTimer = null; } } });
  if (reused) return;
  body.innerHTML = `
    <div class="sched-beat"><span class="sb-dot"></span><span id="sbText">checking heartbeat…</span></div>
    <div class="sched-add">
      <select id="schedPreset" class="sched-preset"></select>
      <input id="schedCron" class="sched-cron" placeholder="cron · min hr day mon wkday">
      <input id="schedInstr" placeholder="what should the agent do?">
      <button class="primary sm" id="schedAdd">Add</button>
    </div>
    <div class="sched-list" id="schedList"></div>`;
  $("#schedPreset").innerHTML = `<option value="">preset…</option>` + Object.entries(SCHED_PRESETS).map(([k, v]) => `<option value="${v}">${k}</option>`).join("");
  $("#schedPreset").onchange = e => { if (e.target.value) $("#schedCron").value = e.target.value; };
  $("#schedAdd").onclick = async () => {
    const cron = $("#schedCron").value.trim(), instr = $("#schedInstr").value.trim();
    if (!cron || !instr) return;
    const r = await fetch("/api/tasks", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ cron, instruction: instr }) }).then(x => x.json());
    if (!r.ok) { const c = $("#schedCron"); c.style.borderColor = "var(--coral)"; setTimeout(() => c.style.borderColor = "", 900); return; }
    $("#schedCron").value = ""; $("#schedInstr").value = ""; loadScheduler();
  };
  loadScheduler();
  _schedTimer = setInterval(refreshBeat, 3000);
}
async function loadScheduler() {
  const list = $("#schedList"); if (!list) return;
  const d = await api("/api/scheduler"); renderBeat(d.beat_ago);
  list.innerHTML = "";
  if (!d.tasks.length) { list.innerHTML = `<div class="empty-note">No scheduled tasks. Pick a preset and add one above.</div>`; return; }
  d.tasks.forEach(t => {
    const row = document.createElement("div"); row.className = "sched-row" + (t.enabled ? "" : " off");
    const nxt = t.next_run ? t.next_run.slice(0, 16).replace("T", " ") : "—";
    const isSkills = (t.source || "").startsWith("skills");
    const mgrName = isSkills ? "Skills" : "Researcher";
    // Locked jobs (managed by Researcher/Skills): the schedule + on/off are yours to
    // change here; the instruction is owned by the manager and it can't be deleted.
    const lock = t.managed ? ` · <span class="sr-lock" title="created by ${mgrName} — schedule & on/off are editable here; managed there">🔒 ${mgrName}</span>` : "";
    row.innerHTML = `<label class="sw"><input type="checkbox" ${t.enabled ? "checked" : ""}><span></span></label>
      <div class="sr-body"><div class="sr-instr">${escapeHtml(t.instruction)}</div><div class="sr-meta"><code>${escapeHtml(t.cron)}</code> · next ${escapeHtml(nxt)}${lock}</div></div>` +
      `<button class="sr-btn sr-edit">${t.managed ? "schedule" : "edit"}</button>` +
      (t.managed ? `<button class="sr-btn sr-res" title="manage in ${mgrName}">${mgrName.toLowerCase()}</button>`
                 : `<button class="sr-btn sr-del">✕</button>`);
    $("input", row).onchange = async e => { await fetch("/api/tasks/" + t.id, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: e.target.checked }) }); loadScheduler(); };
    $(".sr-edit", row).onclick = () => t.managed ? editTaskSchedule(t) : editTask(t);
    if (t.managed) {
      $(".sr-res", row).onclick = isSkills ? () => openBrain("skills") : openResearcher;
    } else {
      $(".sr-del", row).onclick = async () => { if (!await confirmAction("Delete task?", t.instruction.slice(0, 90))) return; await fetch("/api/tasks/" + t.id, { method: "DELETE" }); loadScheduler(); };
    }
    list.appendChild(row);
  });
}
async function refreshBeat() { try { const d = await api("/api/scheduler"); renderBeat(d.beat_ago); } catch {} }
function renderBeat(ago) {
  const dot = $(".sb-dot"), txt = $("#sbText"); if (!txt) return;
  if (ago == null) { if (dot) dot.classList.remove("on"); txt.textContent = "✗ scheduler offline — no heartbeat"; }
  else if (ago < 90) { if (dot) dot.classList.add("on"); txt.textContent = `♥ heartbeat alive · last beat ${Math.round(ago)}s ago`; }
  else { if (dot) dot.classList.remove("on"); txt.textContent = `⚠ scheduler stale · last beat ${Math.round(ago)}s ago`; }
}
async function editTask(t) {
  const cron = await promptDialog("Edit schedule", { value: t.cron, message: "Cron · min hr day mon wkday", okLabel: "Next" }); if (cron === null) return;
  const instr = await promptDialog("Edit instruction", { value: t.instruction, okLabel: "Save" }); if (instr === null) return;
  fetch("/api/tasks/" + t.id, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ cron, instruction: instr }) }).then(() => loadScheduler());
}
async function editTaskSchedule(t) {  // locked job: only its schedule is user-editable here
  const cron = await promptDialog("Edit schedule", { value: t.cron, message: "Cron · min hr day mon wkday", okLabel: "Save" }); if (cron === null) return;
  fetch("/api/tasks/" + t.id, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ cron: cron.trim() }) })
    .then(r => r.json()).then(r => { if (!r.ok && r.error) toast(r.error, "err"); loadScheduler(); });
}

/* ---------- Brain → Evals (model eval harness · judged by Claude Code) ---------- */
let _evalTimer = null, _evalModels = [], _evalRunSel = null;
function renderEvals(c) {
  c.innerHTML = `
    <div class="brain-head">
      <div class="sk-tabs">
        <button class="sk-tab on" data-f="board">Leaderboard</button>
        <button class="sk-tab" data-f="cases">Cases<span class="sk-cnt" id="evCntCases"></span></button>
        <button class="sk-tab" data-f="history">History</button>
      </div>
      <span style="flex:1"></span>
      <button class="exp-btn" id="evRun" title="run the suite against the selected models — judged by Claude Code">⚖ Run now</button>
    </div>
    <div class="kn-note" id="evMsg"></div>
    <div id="evBody"></div>`;
  $("#evRun").onclick = startEvalRun;
  $$(".sk-tab", c).forEach(b => b.onclick = () => {
    $$(".sk-tab", c).forEach(x => x.classList.toggle("on", x === b));
    _evalTab = b.dataset.f; evalRenderTab();
  });
  _evalTab = "board";
  evalRenderTab();
  refreshEvalState(false);
}
let _evalTab = "board";
function evalRenderTab() {
  if (_evalTab === "cases") loadEvalCases();
  else if (_evalTab === "history") loadEvalHistory();
  else loadEvalBoard();
}
async function startEvalRun() {
  const msg = $("#evMsg");
  if (msg) { msg.textContent = "starting eval run — each model runs every case, graded by Claude Code (minutes)…"; msg.className = "kn-note run"; }
  await fetch("/api/evals/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({}) });
  refreshEvalState(true);
}
async function refreshEvalState(loop) {
  const msg = $("#evMsg"); if (!msg) { _evalTimer = null; return; }
  let st; try { st = await api("/api/evals/state"); } catch { _evalTimer = null; return; }
  if (st.running) {
    const pct = st.total ? Math.round(100 * st.done / st.total) : 0;
    msg.textContent = `running ${st.done}/${st.total} (${pct}%) · ${st.phase || ""}`; msg.className = "kn-note run";
    _evalTimer = setTimeout(() => refreshEvalState(loop), 3000);
  } else {
    _evalTimer = null;
    if (st.last) { msg.textContent = "last run: " + st.last; msg.className = "kn-note ok"; }
    else if (msg.classList.contains("run")) { msg.textContent = ""; }
    if (loop) evalRenderTab();
  }
}
async function loadEvalBoard() {
  const body = $("#evBody"); if (!body) return;
  let d; try { d = await api("/api/evals/leaderboard"); } catch { return; }
  if (!d.run_id || !d.rows.length) {
    body.innerHTML = `<div class="empty-note">No completed runs yet. Add cases, then hit <b>⚖ Run now</b> — Oceano runs every case on each local model and Claude Code grades the results.</div>`;
    return;
  }
  const rows = d.rows.map((r, i) => `
    <div class="ev-board-row${i === 0 ? " top" : ""}">
      <span class="ev-rank">${i + 1}</span>
      <span class="ev-model">${escapeHtml(r.model)}</span>
      <span class="ev-score" title="mean score 0–100">${r.score}</span>
      <span class="ev-bar"><i style="width:${r.score}%"></i></span>
      <span class="ev-meta">${r.pass_rate}% pass · ${r.cases} cases · ~${(r.avg_ms/1000).toFixed(1)}s · ${r.avg_steps} steps · ${fmtNum(r.tokens)} tok</span>
    </div>`).join("");
  body.innerHTML = `<div class="ev-board-head">Leaderboard · run #${d.run_id} · score = mean of Claude's 0–100 grades</div>${rows}`;
}
async function loadEvalCases() {
  const body = $("#evBody"); if (!body) return;
  let d; try { d = await api("/api/evals/cases"); } catch { return; }
  _evalCats = d.categories; _evalGraderTypes = d.grader_types;
  const cnt = $("#evCntCases"); if (cnt) cnt.textContent = d.cases.length;
  const head = `<div class="ev-cases-head"><button class="exp-btn" id="evNewCase">＋ New case</button></div>`;
  if (!d.cases.length) { body.innerHTML = head + `<div class="empty-note">No eval cases. Add one — a task plus how to grade it.</div>`; $("#evNewCase").onclick = () => openEvalCase(null); return; }
  body.innerHTML = head + d.cases.map(cs => `
    <div class="ev-case${cs.enabled ? "" : " off"}" data-id="${cs.id}">
      <div class="ev-case-main">
        <div class="ev-case-name"><span class="ev-cat">${escapeHtml(cs.category)}</span>${escapeHtml(cs.name)}</div>
        <div class="ev-case-prompt">${escapeHtml(cs.prompt.slice(0, 110))}</div>
        <div class="ev-case-graders">${cs.graders.map(g => `<span class="ev-grader">${escapeHtml(g.type)}</span>`).join("")}</div>
      </div>
      <label class="sw sm"><input type="checkbox" ${cs.enabled ? "checked" : ""}><span></span></label>
      <button class="sr-btn ev-edit">edit</button><button class="sr-btn ev-del">✕</button>
    </div>`).join("");
  $("#evNewCase").onclick = () => openEvalCase(null);
  $$(".ev-case", body).forEach(el => {
    const cs = d.cases.find(x => x.id == el.dataset.id);
    $("input", el).onchange = e => fetch("/api/evals/cases", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...cs, enabled: e.target.checked }) });
    $(".ev-edit", el).onclick = () => openEvalCase(cs);
    $(".ev-del", el).onclick = async () => { if (!await confirmAction("Delete eval case?", cs.name)) return; await fetch("/api/evals/cases/" + cs.id, { method: "DELETE" }); loadEvalCases(); };
  });
}
let _evalCats = ["qa"], _evalGraderTypes = ["judge"];
function openEvalCase(cs) {
  const cats = _evalCats.map(x => `<option value="${x}"${cs && cs.category === x ? " selected" : ""}>${x}</option>`).join("");
  const graders = JSON.stringify(cs ? cs.graders : [{ type: "judge" }], null, 0);
  const { body } = createWindow({ id: "win-evalcase", title: cs ? "Edit eval case" : "New eval case", icon: "⚖", width: 560, height: 560 });
  body.classList.add("set-win");
  body.innerHTML = `<div class="drawer-section">
    <label class="field-label">Name</label><input id="ecName" value="${cs ? escapeHtml(cs.name) : ""}" placeholder="capital-of-japan">
    <label class="field-label">Category</label><select id="ecCat">${cats}</select>
    <label class="field-label">Prompt <span class="lbl-sub">the task given to the agent</span></label>
    <textarea id="ecPrompt" spellcheck="false" style="min-height:64px">${cs ? escapeHtml(cs.prompt) : ""}</textarea>
    <label class="field-label">Rubric <span class="lbl-sub">what a good result looks like — the judge uses this</span></label>
    <textarea id="ecRubric" spellcheck="false" style="min-height:56px">${cs ? escapeHtml(cs.rubric) : ""}</textarea>
    <label class="field-label">Graders <span class="lbl-sub">JSON · types: ${_evalGraderTypes.join(", ")}</span></label>
    <textarea id="ecGraders" spellcheck="false" style="min-height:64px;font-family:var(--font-mono)">${escapeHtml(graders)}</textarea>
    <div class="ev-grader-hint">e.g. [{"type":"file_exists","path":"out.txt","nonempty":true},{"type":"contains","value":"hello"},{"type":"tool_called","name":"fetch_url"},{"type":"judge"}]</div>
    <div class="acct-actions"><span class="acct-msg" id="ecMsg"></span><button class="primary sm" id="ecSave">Save case</button></div>
  </div>`;
  $("#ecSave", body).onclick = async () => {
    const msg = $("#ecMsg");
    let graders;
    try { graders = JSON.parse($("#ecGraders").value); if (!Array.isArray(graders)) throw 0; }
    catch { msg.textContent = "graders must be a JSON array"; msg.className = "acct-msg err"; return; }
    const name = $("#ecName").value.trim(); if (!name) { msg.textContent = "name required"; msg.className = "acct-msg err"; return; }
    await fetch("/api/evals/cases", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: cs ? cs.id : null, name, category: $("#ecCat").value,
        prompt: $("#ecPrompt").value, rubric: $("#ecRubric").value, graders, enabled: cs ? cs.enabled : true }) });
    msg.textContent = "saved ✓"; msg.className = "acct-msg ok";
    if (_evalTab === "cases") loadEvalCases();
  };
}
async function loadEvalHistory() {
  const body = $("#evBody"); if (!body) return;
  let d; try { d = await api("/api/evals/runs"); } catch { return; }
  if (!d.runs.length) { body.innerHTML = `<div class="empty-note">No runs yet.</div>`; return; }
  body.innerHTML = d.runs.map(r => `
    <div class="ev-run" data-id="${r.id}">
      <div class="ev-run-main"><div class="ev-run-sum">${escapeHtml(r.summary || "(no summary)")}</div>
      <div class="ev-run-meta">#${r.id} · ${(r.ts || "").slice(0, 16).replace("T", " ")} · ${escapeHtml(r.status)} · ${r.models.length} model(s)</div></div>
      <button class="sr-btn ev-view">results</button></div>`).join("");
  $$(".ev-run", body).forEach(el => $(".ev-view", el).onclick = () => openEvalResults(+el.dataset.id));
}
async function openEvalResults(runId) {
  const { body } = createWindow({ id: "win-evalresults", title: "Eval results · run #" + runId, icon: "⚖", width: 680, height: 600 });
  body.innerHTML = `<div class="ev-results" id="evResults">loading…</div>`;
  let d; try { d = await api("/api/evals/results?run_id=" + runId); } catch { return; }
  const box = $("#evResults", body);
  if (!d.results.length) { box.innerHTML = `<div class="empty-note">No results for this run.</div>`; return; }
  box.innerHTML = d.results.map(r => {
    const v = r.verdict || {};
    const reason = v.reasoning ? `<div class="ev-reason">${escapeHtml(v.reasoning)}</div>` : "";
    const det = (v.deterministic || []).length ? `<div class="ev-det">${(v.deterministic).map(escapeHtml).join(" · ")}</div>` : "";
    return `<div class="ev-res ${r.passed ? "pass" : "fail"}">
      <div class="ev-res-head"><span class="ev-res-score">${Math.round(r.score)}</span>
        <span class="ev-res-case">${escapeHtml(r.case)}</span><span class="ev-res-model">${escapeHtml(r.model)}</span>
        <span class="ev-res-flag">${r.passed ? "✓" : "✗"}</span></div>
      ${det}${reason}
      ${r.error ? `<div class="ev-err">error: ${escapeHtml(r.error)}</div>` : ""}
      <div class="ev-res-meta">${r.tokens} tok · ${(r.ms/1000).toFixed(1)}s · ${r.steps} steps${r.tools.length ? " · tools: " + escapeHtml(r.tools.join(", ")) : ""}</div>
    </div>`;
  }).join("");
}

/* ---------- Calendar window (local copy, one-way synced from ICS feeds) ---------- */
function openCalendar() {
  const { body, reused } = createWindow({ id: "win-cal", title: "Calendar — synced, read-only", icon: "◷", width: 680, height: 580 });
  if (reused) { loadCalendar(); return; }
  body.innerHTML = `
    <div class="sched-add">
      <input id="calName" placeholder="name · e.g. Personal" style="flex:0 0 140px">
      <input id="calUrl" placeholder="secret iCal address (…/basic.ics)" style="flex:1;min-width:160px" spellcheck="false">
      <button class="primary sm" id="calAdd">Add</button>
      <button class="exp-btn" id="calSync" title="sync all feeds now">↻ Sync now</button>
    </div>
    <div class="cal-hint">Google Calendar → Settings → your calendar → <b>Integrate calendar</b> → copy the <b>Secret address in iCal format</b> and paste it here. Sync is one-way: Oceano reads your calendar, it never writes to Google.</div>
    <div class="kn-note" id="calMsg"></div>
    <div class="cal-feeds" id="calFeeds"></div>
    <div class="cal-events" id="calEvents"></div>`;
  $("#calAdd", body).onclick = async () => {
    const name = $("#calName").value.trim(), url = $("#calUrl").value.trim(), msg = $("#calMsg");
    if (!url) return;
    const btn = $("#calAdd"); btn.disabled = true; btn.textContent = "syncing…";
    try {
      const r = await api("/api/calendar/feeds", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name, url }) });
      if (!r.ok) { msg.textContent = r.error || "could not add feed"; msg.className = "kn-note err"; return; }
      const s = r.sync || {};
      msg.textContent = s.ok ? `feed added ✓ · ${s.events} events synced` : `feed added, but first sync failed: ${s.error || "?"}`;
      msg.className = "kn-note " + (s.ok ? "ok" : "err");
      $("#calName").value = ""; $("#calUrl").value = "";
      loadCalendar();
    } finally { btn.disabled = false; btn.textContent = "Add"; }
  };
  $("#calSync", body).onclick = async () => {
    const btn = $("#calSync"), msg = $("#calMsg");
    btn.disabled = true; btn.textContent = "syncing…";
    try {
      const r = await api("/api/calendar/sync", { method: "POST" });
      msg.textContent = r.ok ? "all feeds synced ✓" : "some feeds failed to sync — see the feed list";
      msg.className = "kn-note " + (r.ok ? "ok" : "err");
      loadCalendar();
    } finally { btn.disabled = false; btn.textContent = "↻ Sync now"; }
  };
  loadCalendar();
}
function calDayLabel(iso) {
  const d = new Date(iso + "T00:00"), today = new Date(); today.setHours(0, 0, 0, 0);
  const diff = Math.round((d - today) / 86400000);
  const nice = d.toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" });
  return diff === 0 ? "Today · " + nice : diff === 1 ? "Tomorrow · " + nice : nice;
}
async function loadCalendar() {
  const feedsBox = $("#calFeeds"), evBox = $("#calEvents");
  if (!feedsBox || !evBox) return;
  let d; try { d = await api("/api/calendar?days=30"); } catch { return; }
  feedsBox.innerHTML = "";
  if (!d.feeds.length) feedsBox.innerHTML = `<div class="empty-note" style="padding:14px">No calendar feeds yet — paste your Google Calendar's secret iCal address above.</div>`;
  d.feeds.forEach(f => {
    const row = document.createElement("div"); row.className = "ep";
    const sync = f.last_error ? `⚠ ${f.last_error}` : f.last_sync ? `synced ${f.last_sync.slice(0, 16).replace("T", " ")} UTC` : "never synced";
    row.innerHTML = `<div class="ep-info"><div class="ep-name">${escapeHtml(f.name)}</div><div class="ep-url">${escapeHtml(sync)}</div></div><span class="ep-count ${f.last_error ? "err" : "ok"}">${f.event_count} events</span><button class="ep-del">✕</button>`;
    $(".ep-del", row).onclick = async () => { if (!await confirmAction("Remove feed?", `“${f.name}” and its local events will be removed. Google is not touched.`, "Remove")) return; await fetch("/api/calendar/feeds/" + f.id, { method: "DELETE" }); loadCalendar(); };
    feedsBox.appendChild(row);
  });
  evBox.innerHTML = "";
  if (!d.events.length) { if (d.feeds.length) evBox.innerHTML = `<div class="empty-note">No events in the next 30 days.</div>`; return; }
  let lastDay = null;
  d.events.forEach(e => {
    const day = e.start.slice(0, 10);
    if (day !== lastDay) { const h = document.createElement("div"); h.className = "cal-day"; h.textContent = calDayLabel(day); evBox.appendChild(h); lastDay = day; }
    const row = document.createElement("div"); row.className = "cal-ev";
    const when = e.all_day ? "all day" : e.start.slice(11, 16) + (e.end && e.end.slice(0, 10) === day ? "–" + e.end.slice(11, 16) : "");
    row.innerHTML = `<span class="cal-when">${escapeHtml(when)}</span><span class="cal-title">${escapeHtml(e.title || "(untitled)")}</span>${e.location ? `<span class="cal-loc">${escapeHtml(e.location)}</span>` : ""}`;
    if (e.description) row.title = e.description;
    evBox.appendChild(row);
  });
}

/* ---------- Researcher window (scheduled deep-dives → living docs) ---------- */
let _resTimer = null;
function openResearcher() {
  const { body, reused } = createWindow({ id: "win-research", title: "Researcher — scheduled deep-dives", icon: "⌖", width: 740, height: 580,
    onClose: () => { if (_resTimer) { clearInterval(_resTimer); _resTimer = null; } } });
  if (reused) { loadResearch(); return; }
  body.innerHTML = `
    <div class="cal-hint">Each topic runs on its own schedule, researches the web, and maintains a living document in <code>workspace/research/</code> — consultable by you (Files) and by the model (ask it, or it searches its knowledge). Runs appear in the Scheduler as <code>[ RESEARCH ]</code> entries, locked there — manage them here.</div>
    <div class="sched-add">
      <input id="resTopic" placeholder="topic · e.g. Solana MEV landscape" style="flex:1;min-width:150px">
      <input id="resFocus" placeholder="focus / guidance (optional)" style="flex:1;min-width:150px">
      <select id="resPreset" class="sched-preset"></select>
      <input id="resCron" class="sched-cron" placeholder="cron" value="0 8 * * *">
      <button class="primary sm" id="resAdd">Add</button>
    </div>
    <div class="kn-note" id="resMsg"></div>
    <div class="sched-list" id="resList"></div>`;
  const RES_PRESETS = { "daily 8am": "0 8 * * *", "every 12h": "0 */12 * * *", "weekdays 9am": "0 9 * * 1-5", "weekly Mon 9am": "0 9 * * 1", "monthly (1st, 9am)": "0 9 1 * *" };
  $("#resPreset", body).innerHTML = `<option value="">preset…</option>` + Object.entries(RES_PRESETS).map(([k, v]) => `<option value="${v}">${k}</option>`).join("");
  $("#resPreset", body).onchange = e => { if (e.target.value) $("#resCron").value = e.target.value; };
  $("#resAdd", body).onclick = async () => {
    const topic = $("#resTopic").value.trim(), focus = $("#resFocus").value.trim(), cron = $("#resCron").value.trim(), msg = $("#resMsg");
    if (!topic || !cron) return;
    const r = await api("/api/research", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ topic, focus, cron }) });
    if (!r.ok) { msg.textContent = r.error || "could not add topic"; msg.className = "kn-note err"; return; }
    msg.textContent = "research topic added ✓ — it will run on its schedule (or hit ▶ to run now)"; msg.className = "kn-note ok";
    $("#resTopic").value = ""; $("#resFocus").value = "";
    loadResearch();
  };
  loadResearch();
  _resTimer = setInterval(() => { if ($("#resList")) loadResearch(); }, 5000);   // keeps "running…" fresh
}
async function loadResearch() {
  const list = $("#resList"); if (!list) return;
  let topics; try { topics = await api("/api/research"); } catch { return; }
  list.innerHTML = "";
  if (!topics.length) { list.innerHTML = `<div class="empty-note">No research topics yet. Add one above — Oceano will study it on schedule and build up documentation.</div>`; return; }
  topics.forEach(t => {
    const row = document.createElement("div"); row.className = "sched-row" + (t.enabled ? "" : " off");
    const last = t.last_run ? t.last_run.slice(0, 16).replace("T", " ") + " UTC" : "never";
    const status = t.running ? `<span class="res-running">⟳ researching now…</span>` : `last run ${escapeHtml(last)}`;
    row.innerHTML = `<label class="sw"><input type="checkbox" ${t.enabled ? "checked" : ""}><span></span></label>
      <div class="sr-body"><div class="sr-instr">${escapeHtml(t.topic)}</div>
      <div class="sr-meta"><code>${escapeHtml(t.cron)}</code> · ${status}${t.focus ? `<div class="res-focus">focus: ${escapeHtml(t.focus)}</div>` : ""}</div></div>
      <button class="sr-btn res-run" title="run this research now" ${t.running ? "disabled" : ""}>▶ run</button>
      <button class="sr-btn res-doc" title="open the research document" ${t.doc_exists ? "" : "disabled"}>doc</button>
      <button class="sr-btn sr-edit">edit</button><button class="sr-btn sr-del">✕</button>`;
    $("input", row).onchange = async e => { await fetch("/api/research/" + t.id, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: e.target.checked }) }); loadResearch(); };
    $(".res-run", row).onclick = async () => { await fetch("/api/research/" + t.id + "/run", { method: "POST" }); loadResearch(); };
    $(".res-doc", row).onclick = () => openFileWindow(t.doc);
    $(".sr-edit", row).onclick = () => editResearch(t);
    $(".sr-del", row).onclick = async () => { if (!await confirmAction("Delete research topic?", `“${t.topic}” and its scheduler entry will be removed. The document in workspace/research/ is kept.`)) return; await fetch("/api/research/" + t.id, { method: "DELETE" }); loadResearch(); };
    list.appendChild(row);
  });
}
async function editResearch(t) {
  const topic = await promptDialog("Research topic", { value: t.topic, okLabel: "Next" }); if (topic === null) return;
  const focus = await promptDialog("Focus / guidance", { value: t.focus || "", message: "Optional — leave blank for none", okLabel: "Next" }); if (focus === null) return;
  const cron = await promptDialog("Schedule", { value: t.cron, message: "Cron · min hr day mon wkday", okLabel: "Save" }); if (cron === null) return;
  fetch("/api/research/" + t.id, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ topic, focus, cron }) }).then(() => loadResearch());
}

/* ---------------- wiring ---------------- */
const autosize = t => { t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, 200) + "px"; };
function wire() {
  const input = $("#input");
  input.addEventListener("input", () => autosize(input));
  input.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } });
  $("#send").onclick = () => state.busy ? stopChat() : send();
  $("#newVoyage").onclick = newVoyage;
  $("#agentToggle").onchange = e => {
    state.agent = e.target.checked;
    fetch("/api/prefs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ agent_mode: state.agent }) }).catch(() => {});
  };

  $$(".nav-item").forEach(n => n.onclick = () => {
    const v = n.dataset.view;
    if (v === "files") openExplorer();
    else if (v === "brain") openBrain();
    else if (v === "scheduler") openScheduler();
    else if (v === "calendar") openCalendar();
    else if (v === "researcher") openResearcher();
    else setView(v);
  });

  $("#modelPill").onclick = e => { e.stopPropagation(); $("#modelMenu").classList.toggle("open"); };
  document.addEventListener("click", () => $("#modelMenu").classList.remove("open"));
  $("#modelMenu").onclick = e => e.stopPropagation();

  $("#openSettings").onclick = openSettings;
  $("#toggleSidebar").onclick = () => $("#sidebar").classList.toggle("open");
  $("#liveBtn").onclick = openLiveView;

  // files
  $("#fRefresh").onclick = () => loadFiles(state.cwd);
  $("#fNew").onclick = newFile;
  $("#fNewDir").onclick = newFolder;
  $("#fSave").onclick = saveFile;
  $("#feSaveAs").onclick = saveFileAs;
  $("#feWrap").onclick = toggleWrap;
  $("#feFind").onclick = () => { if (_cm) { _cm.focus(); _cm.execCommand("findPersistent"); } };
  // skills
  $("#skNew").onclick = () => openSkill(null);
  $("#skClose").onclick = closeSkill; $("#skModalScrim").onclick = closeSkill;
  $("#skSave").onclick = saveSkill; $("#skDelete").onclick = deleteSkill;
  // memory
  $("#memAdd").onclick = addMemory;
  $("#memText").addEventListener("keydown", e => { if (e.key === "Enter") addMemory(); });
}

/* ---------------- auth gate ---------------- */
let _appStarted = false;
function showLogin() {
  const gate = $("#loginGate"); if (!gate) return;
  gate.style.display = "grid";
  const form = $("#loginForm");
  if (form && !form.dataset.wired) {
    form.dataset.wired = "1";
    form.addEventListener("submit", async e => {
      e.preventDefault();
      const btn = $("#loginBtn"), err = $("#loginErr");
      btn.disabled = true; err.textContent = "";
      const pw = $("#loginPass").value;
      try {
        const r = await fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ user: $("#loginUser").value, password: pw }) });
        if (!r.ok) { err.textContent = "Invalid username or password."; $("#loginPass").value = ""; $("#loginPass").focus(); return; }
        const d = await r.json().catch(() => ({}));
        gate.style.display = "none";
        if (d.must_change) { showPwChange(pw); return; }   // first login on the default password
        initApp();
      } catch { err.textContent = "Could not reach the server."; }
      finally { btn.disabled = false; }
    });
  }
  setTimeout(() => { const p = $("#loginPass"); if (p && !p.value) p.focus(); }, 60);
}
function showPwChange(currentPw) {
  const lg = $("#loginGate"); if (lg) lg.style.display = "none";
  const gate = $("#pwGate"); if (!gate) return;
  gate.style.display = "grid";
  if (currentPw) $("#pwCurrent").value = currentPw;
  const form = $("#pwForm");
  if (form && !form.dataset.wired) {
    form.dataset.wired = "1";
    form.addEventListener("submit", async e => {
      e.preventDefault();
      const btn = $("#pwBtn"), err = $("#pwErr");
      const cur = $("#pwCurrent").value, np = $("#pwNew").value, cf = $("#pwConfirm").value;
      err.textContent = "";
      if (!np || np.length < 4) { err.textContent = "Use at least 4 characters."; return; }
      if (np !== cf) { err.textContent = "The two new passwords don't match."; return; }
      if (np.trim().toLowerCase() === "admin") { err.textContent = "Choose something other than the default."; return; }
      btn.disabled = true;
      try {
        const r = await fetch("/api/account", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ current_password: cur, new_password: np }) });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) { err.textContent = d.detail || "Could not set the password."; $("#pwCurrent").focus(); return; }
        gate.style.display = "none";
        initApp();
      } catch { err.textContent = "Could not reach the server."; }
      finally { btn.disabled = false; }
    });
  }
  setTimeout(() => { const f = $(currentPw ? "#pwNew" : "#pwCurrent"); if (f) f.focus(); }, 60);
}
async function initApp() {
  if (_appStarted) return;        // idempotent — survives a mid-session re-login
  _appStarted = true;
  wire();
  loadModels();
  loadPrefs();
  setView("chat");
  await loadChats();              // chats now live on Oceano (dated folders), not the browser
  await migrateLocalChats();      // lift any pre-existing browser chats over, once
  const active = localStorage.getItem("oceano.active");
  if (active && _chats.some(s => s.id === active)) openVoyage(active);
  else if (_chats.length) openVoyage(_chats[0].id);
  else newVoyage();
  setInterval(loadModels, 30000);
}
async function boot() {
  try {
    const r = await fetch("/api/me");
    if (r.status === 401) { showLogin(); return; }
    const me = await r.json().catch(() => ({}));
    if (me.must_change) { showPwChange(); return; }   // already authed but still on the default pw
  } catch { /* server unreachable — fall through and let the UI surface errors */ }
  initApp();
}
boot();
