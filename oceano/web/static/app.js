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

/* ================= CHAT ================= */
const LS = {
  index: () => JSON.parse(localStorage.getItem("oceano.sessions") || "[]"),
  saveIndex: x => localStorage.setItem("oceano.sessions", JSON.stringify(x)),
  transcript: id => JSON.parse(localStorage.getItem("oceano.t." + id) || "[]"),
  saveT: (id, t) => localStorage.setItem("oceano.t." + id, JSON.stringify(t)),
};
const uid = () => "v" + Math.random().toString(36).slice(2, 9);
const toBottom = () => { const t = $("#thread"); if (t) t.scrollTop = t.scrollHeight; };

function newVoyage() {
  setView("chat");
  const id = uid(), idx = LS.index();
  idx.unshift({ id, title: "New voyage" });
  LS.saveIndex(idx); LS.saveT(id, []);
  openVoyage(id);
}
function openVoyage(id) {
  state.session = id;
  localStorage.setItem("oceano.active", id);
  const t = LS.transcript(id), thread = $("#thread");
  thread.innerHTML = "";
  if (!t.length) thread.appendChild(welcomeNode());
  else t.forEach(m => {
    if (m.role === "user") addUser(m.content, false);
    else if (m.role === "thinking") { const c = addThinkCard(); appendThink(c, m.text); finalizeThink(c); }
    else if (m.role === "tool") fillTool(addTool(m.name, m.args), m.result);
    else if (m.role === "tools") m.items.forEach(it => fillTool(addTool(it.name, it.args), it.result));  // old format
    else { const bb = addAssistant(m.content, true); if (m.meta) renderMeta(bb, m.meta); }
  });
  renderSessions(); $("#input").focus();
}
function renderSessions() {
  const box = $("#sessions"); box.innerHTML = "";
  LS.index().forEach(s => {
    const el = document.createElement("div");
    el.className = "session" + (s.id === state.session ? " active" : "");
    el.innerHTML = `<span class="s-title"></span><button class="s-del" title="delete voyage">✕</button>`;
    $(".s-title", el).textContent = s.title;
    el.onclick = () => openVoyage(s.id);
    $(".s-del", el).onclick = (e) => { e.stopPropagation(); deleteVoyage(s.id); };
    box.appendChild(el);
  });
}
async function deleteVoyage(id) {
  const s = LS.index().find(x => x.id === id);
  if (!await confirmAction("Delete voyage?", `“${s?.title || "this chat"}” and its history will be permanently removed.`)) return;
  const idx = LS.index().filter(x => x.id !== id);
  LS.saveIndex(idx);
  localStorage.removeItem("oceano.t." + id);
  fetch("/api/session/" + id, { method: "DELETE" }).catch(() => {});   // free the server-side Agent
  if (state.session === id) {
    if (idx.length) openVoyage(idx[0].id); else newVoyage();
  } else renderSessions();
}
function touchTitle(text) {
  const idx = LS.index(), s = idx.find(x => x.id === state.session);
  if (s && s.title === "New voyage") { s.title = text.slice(0, 38); LS.saveIndex(idx); renderSessions(); }
}
function appendT(entry) { const t = LS.transcript(state.session); t.push(entry); LS.saveT(state.session, t); }

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
  if (v === "files") loadFiles(state.cwd);
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
async function openFile(path) {
  state.file = path;
  $("#feEmpty").style.display = "none"; $("#feOpen").style.display = "flex";
  $("#feName").textContent = path;
  const isImg = /\.(png|jpe?g|gif|webp|svg|bmp|ico|avif)$/i.test(path);
  $("#feImage").style.display = isImg ? "flex" : "none";
  $("#feText").style.display = isImg ? "none" : "block";
  $("#fSave").style.display = isImg ? "none" : "";              // images aren't text-editable
  if (isImg) { $("#feImg").src = "/api/raw?path=" + encodeURIComponent(path); return; }
  const d = await api("/api/file?path=" + encodeURIComponent(path));
  $("#feText").value = d.binary ? "(binary file — not editable here)" : d.content;
  $("#feText").readOnly = !!d.binary;
}
async function newFolder() {
  const name = prompt("New folder name (relative to current folder):"); if (!name) return;
  const path = state.cwd ? state.cwd + "/" + name : name;
  await fetch("/api/folder", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path }) });
  loadFiles(state.cwd);
}
async function saveFile() {
  if (!state.file) return;
  await fetch("/api/file", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: state.file, content: $("#feText").value }) });
  const btn = $("#fSave"); btn.textContent = "Saved ✓"; setTimeout(() => btn.textContent = "Save", 1200);
  loadFiles(state.cwd);
}
async function newFile() {
  const name = prompt("New file path (relative to current folder):"); if (!name) return;
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
  $("#skModalTitle").textContent = s ? "Edit skill" : "New skill";
  $("#skName").value = s ? s.name : ""; $("#skDesc").value = s ? s.description : ""; $("#skBody").value = s ? s.body : "";
  $("#skModal").dataset.dir = s ? s.dir : "";
  $("#skDelete").style.display = s ? "block" : "none";
  $("#skModal").classList.add("open"); $("#skModalScrim").classList.add("open");
}
const closeSkill = () => { $("#skModal").classList.remove("open"); $("#skModalScrim").classList.remove("open"); };
async function saveSkill() {
  const body = { name: $("#skName").value.trim(), description: $("#skDesc").value.trim(), body: $("#skBody").value, dir: $("#skModal").dataset.dir || undefined };
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
  $("#providerSelect").innerHTML = provs.map(p => `<option value="${p.base_url}" data-needs="${p.needs_key}" data-name="${p.name}">${p.name}</option>`).join("");
  $("#providerSelect").onchange = syncProviderFields; syncProviderFields();
}
function syncProviderFields() {
  const o = $("#providerSelect").selectedOptions[0];
  $("#epName").value = o.dataset.name;
  $("#epKey").style.display = o.dataset.needs === "true" ? "block" : "none"; $("#epKey").value = "";
}
async function loadEndpoints() {
  const cfg = await api("/api/config"); const box = $("#endpoints"); box.innerHTML = "";
  cfg.endpoints.forEach(e => {
    const el = document.createElement("div"); el.className = "ep";
    el.innerHTML = `<div class="ep-info"><div class="ep-name">${escapeHtml(e.name)}</div><div class="ep-url">${escapeHtml(e.base_url)}</div>${e.has_key ? '<div class="ep-key">● key set</div>' : ''}</div><button class="ep-del">✕</button>`;
    $(".ep-del", el).onclick = async () => { if (!await confirmAction("Remove endpoint?", `“${e.name}” will be removed.`)) return; await fetch("/api/endpoints/" + encodeURIComponent(e.name), { method: "DELETE" }); loadEndpoints(); loadModels(); };
    box.appendChild(el);
  });
}
async function addEndpoint() {
  const o = $("#providerSelect").selectedOptions[0];
  await fetch("/api/endpoints", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: $("#epName").value || o.dataset.name, base_url: o.value, api_key: $("#epKey").value }) });
  $("#epKey").value = ""; loadEndpoints(); loadModels();
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
  ["memory", "✶", "Memory"], ["tools", "⚒", "Tools"], ["services", "◉", "Services"], ["about", "≈", "About"],
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
      <p class="sub">Add a provider, paste a key, and its models appear in the composer.</p>
      <div class="endpoints" id="endpoints"></div>
      <div class="add-endpoint">
        <select id="providerSelect"></select>
        <input id="epName" placeholder="label (optional)">
        <input id="epKey" type="password" placeholder="API key" autocomplete="off">
        <button class="primary" id="addEndpoint">Add</button>
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
  loadSettingsAll();
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
  $(".win-close", win).onclick = () => { if (opts.onClose) opts.onClose(); if (win._chip) win._chip.remove(); win.remove(); };
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
  stage.addEventListener("wheel", e => { e.preventDefault(); post("/api/browser/scroll", { dy: Math.round(e.deltaY) }); }, { passive: false });
  stage.addEventListener("keydown", e => {
    if (e.key.length === 1 && !e.ctrlKey && !e.metaKey) { post("/api/browser/type", { text: e.key }); e.preventDefault(); }
    else if (["Enter", "Backspace", "Tab", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Escape", "Delete", "Home", "End"].includes(e.key)) { post("/api/browser/key", { key: e.key }); e.preventDefault(); }
  });

  _liveES = new EventSource("/api/browser/stream");
  _liveES.onmessage = e => {
    let d; try { d = JSON.parse(e.data); } catch { return; }
    if (d.frame && img) { img.src = d.frame; img.style.display = "block"; const w = $("#liveWait", body); if (w) w.style.display = "none"; }
    const u = $("#liveUrl", body); if (u && d.url) { u.textContent = d.url || "browsing…"; u.classList.add("on"); }
    if (d.tabs) renderLiveTabs(d.tabs);
  };
}
const _post = (p, b) => fetch(p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b) });
function renderLiveTabs(tabs) {
  const bar = $("#liveTabs"); if (!bar) return;
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
  const { body, reused } = createWindow({ id: "win-explorer", title: "Files — workspace", icon: "▤", width: 600, height: 470 });
  if (reused) return;
  body.innerHTML = `
    <div class="exp-bar">
      <button class="exp-btn" id="expUp" title="up">↰</button>
      <div class="exp-crumbs" id="expCrumbs"></div>
      <button class="exp-btn" id="expNewDir">＋ folder</button>
      <button class="exp-btn" id="expNewFile">＋ file</button>
      <button class="exp-btn" id="expRefresh">↻</button>
    </div>
    <div class="exp-list" id="expList"></div>`;
  $("#expUp", body).onclick = () => expLoad(_expCwd.split("/").slice(0, -1).join("/"));
  $("#expNewDir", body).onclick = expNewFolder;
  $("#expNewFile", body).onclick = expNewFile;
  $("#expRefresh", body).onclick = () => expLoad(_expCwd);
  $("#expList", body).addEventListener("contextmenu", e => {
    if (e.target.closest(".exp-row")) return;
    e.preventDefault();
    showCtx(e.clientX, e.clientY, [{ label: "New folder", action: expNewFolder }, { label: "New file", action: expNewFile }, { label: "Refresh", action: () => expLoad(_expCwd) }]);
  });
  expLoad("");
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
    row.onclick = () => { $$(".exp-row", list).forEach(r => r.classList.remove("sel")); row.classList.add("sel"); };
    row.ondblclick = () => e.dir ? expLoad(e.path) : openFileWindow(e.path);
    row.oncontextmenu = ev => {
      ev.preventDefault();
      $$(".exp-row", list).forEach(r => r.classList.remove("sel")); row.classList.add("sel");
      const items = e.dir
        ? [{ label: "Open", action: () => expLoad(e.path) }]
        : [{ label: "Open", action: () => openFileWindow(e.path) }, { label: "Download", action: () => window.open("/api/raw?path=" + encodeURIComponent(e.path), "_blank") }];
      items.push({ label: "Rename", action: () => expRename(e) }, { sep: true }, { label: "Delete", danger: true, action: () => expDelete(e) });
      showCtx(ev.clientX, ev.clientY, items);
    };
    list.appendChild(row);
  });
}
async function expNewFolder() { const n = prompt("New folder name:"); if (!n) return; await fetch("/api/folder", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: _expCwd ? _expCwd + "/" + n : n }) }); expLoad(_expCwd); }
async function expNewFile() { const n = prompt("New file name:"); if (!n) return; await fetch("/api/file", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: _expCwd ? _expCwd + "/" + n : n, content: "" }) }); expLoad(_expCwd); }
async function expRename(e) { const n = prompt("Rename to:", e.name); if (!n || n === e.name) return; await fetch("/api/rename", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: e.path, to: _expCwd ? _expCwd + "/" + n : n }) }); expLoad(_expCwd); }
async function expDelete(e) { if (!await confirmAction("Delete " + (e.dir ? "folder" : "file") + "?", `“${e.name}” will be deleted${e.dir ? " with its contents" : ""}.`)) return; await fetch("/api/file?path=" + encodeURIComponent(e.path), { method: "DELETE" }); expLoad(_expCwd); }

/* ---------- file viewer / editor window ---------- */
async function openFileWindow(path) {
  const isImg = /\.(png|jpe?g|gif|webp|svg|bmp|ico|avif)$/i.test(path);
  const name = path.split("/").pop();
  const { body, reused } = createWindow({ id: "fw-" + path.replace(/[^a-z0-9]/gi, "_"), title: name, icon: isImg ? "▦" : "ℜ", width: 560, height: 460 });
  if (reused) return;
  if (isImg) { body.innerHTML = `<div class="fw-img"><img src="/api/raw?path=${encodeURIComponent(path)}"></div>`; return; }
  const d = await api("/api/file?path=" + encodeURIComponent(path));
  body.innerHTML = `<div class="fw-bar"><span class="fw-name">${escapeHtml(path)}</span><button class="primary sm fw-save">Save</button></div><textarea class="fw-text" spellcheck="false"></textarea>`;
  const ta = $(".fw-text", body); ta.value = d.binary ? "(binary file — not editable here)" : d.content; ta.readOnly = !!d.binary;
  $(".fw-save", body).onclick = async () => { await fetch("/api/file", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path, content: ta.value }) }); const b = $(".fw-save", body); b.textContent = "Saved ✓"; setTimeout(() => b.textContent = "Save", 1000); };
}

/* ---------- Brain window (memory + skills) ---------- */
const BRAIN_TABS = [["mem", "✶", "Memory"], ["kn", "◈", "Knowledge"], ["skills", "⚒", "Skills"], ["rivers", "🌊", "Rivers"]];
function openBrain() {
  const { body, reused } = createWindow({ id: "win-brain", title: "Brain", icon: "✶", width: 720, height: 580,
    onClose: () => { if (_riverTimer) { clearInterval(_riverTimer); _riverTimer = null; } } });
  if (reused) return;
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
  brainTab("mem");
}
function brainTab(which) {
  const c = $("#brainBody"); if (!c) return;
  if (_riverTimer) { clearInterval(_riverTimer); _riverTimer = null; }   // stop riverbook polling when leaving the tab
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
  } else {
    c.innerHTML = `<div class="brain-head"><button class="exp-btn" id="bSkNew">＋ New skill</button></div><div class="brain-skills" id="bSkBody"></div>`;
    $("#bSkNew").onclick = () => openSkill(null);
    loadBrainSkills();
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
async function loadBrainSkills() {
  const body = $("#bSkBody"); if (!body) return;
  skillsCache = await api("/api/skills"); body.innerHTML = "";
  if (!skillsCache.length) { body.innerHTML = `<div class="empty-note">No skills yet — teach Oceano a reusable procedure.</div>`; return; }
  skillsCache.forEach(s => {
    const c = document.createElement("div"); c.className = "skill-card";
    c.innerHTML = `<h3>${escapeHtml(s.name)}</h3><div class="sc-desc">${escapeHtml(s.description)}</div><div class="sc-snip">${escapeHtml(s.body.slice(0, 90))}…</div>`;
    c.onclick = () => openSkill(s); body.appendChild(c);
  });
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
  $$(".cserve", box).forEach(b => b.onclick = () => riverServe(b.dataset.f, +b.dataset.sz, b));
}
async function riverServe(filename, size, btn) {
  btn.disabled = true; btn.textContent = "…";
  const fitc = fitClient(size);
  const name = filename.replace(/\.gguf$/i, "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40);
  let r; try { r = await api("/api/rivers/serve", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ filename, name, ngl: fitc.ngl, ctx: 8192 }) }); } catch { return; }
  const n = $("#riverNote");
  if (!r.ok) { btn.disabled = false; btn.textContent = "Serve"; if (n) { n.textContent = r.error; n.className = "river-note err"; } return; }
  if (n) { n.textContent = `✓ serving as "${r.name}" (ngl ${r.ngl}, ctx ${r.ctx}) — now on :8081 + the model picker`; n.className = "river-note ok"; }
  riverLoadInstalled(); loadModels();
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
    row.innerHTML = `<label class="sw"><input type="checkbox" ${t.enabled ? "checked" : ""}><span></span></label>
      <div class="sr-body"><div class="sr-instr">${escapeHtml(t.instruction)}</div><div class="sr-meta"><code>${escapeHtml(t.cron)}</code> · next ${escapeHtml(nxt)}</div></div>
      <button class="sr-btn sr-edit">edit</button><button class="sr-btn sr-del">✕</button>`;
    $("input", row).onchange = async e => { await fetch("/api/tasks/" + t.id, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: e.target.checked }) }); loadScheduler(); };
    $(".sr-edit", row).onclick = () => editTask(t);
    $(".sr-del", row).onclick = async () => { if (!await confirmAction("Delete task?", t.instruction.slice(0, 90))) return; await fetch("/api/tasks/" + t.id, { method: "DELETE" }); loadScheduler(); };
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
function editTask(t) {
  const cron = prompt("Cron schedule:", t.cron); if (cron === null) return;
  const instr = prompt("Instruction:", t.instruction); if (instr === null) return;
  fetch("/api/tasks/" + t.id, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ cron, instruction: instr }) }).then(() => loadScheduler());
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
      try {
        const r = await fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ user: $("#loginUser").value, password: $("#loginPass").value }) });
        if (!r.ok) { err.textContent = "Invalid username or password."; $("#loginPass").value = ""; $("#loginPass").focus(); return; }
        gate.style.display = "none";
        initApp();
      } catch { err.textContent = "Could not reach the server."; }
      finally { btn.disabled = false; }
    });
  }
  setTimeout(() => { const p = $("#loginPass"); if (p && !p.value) p.focus(); }, 60);
}
function initApp() {
  if (_appStarted) return;        // idempotent — survives a mid-session re-login
  _appStarted = true;
  wire();
  loadModels();
  loadPrefs();
  const active = localStorage.getItem("oceano.active");
  if (active && LS.index().some(s => s.id === active)) openVoyage(active);
  else if (LS.index().length) openVoyage(LS.index()[0].id);
  else newVoyage();
  setView("chat");
  setInterval(loadModels, 30000);
}
async function boot() {
  try {
    const r = await fetch("/api/me");
    if (r.status === 401) { showLogin(); return; }
  } catch { /* server unreachable — fall through and let the UI surface errors */ }
  initApp();
}
boot();
