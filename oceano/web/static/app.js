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
let _voiceSpeak = null;  // conversation mode: a sink that streams answer tokens to TTS (else null)

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
  selectDefaultModel();                              // a new chat adopts the configured primary
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
    else if (m.role === "tool") { const tc = addTool(m.name, m.args); fillTool(tc, m.result); maybePreviewChip(tc, m.name, m.args); }
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
// the sidebar's two panes slide: menu ⇄ chat history
function sideShowChats() { const t = $("#sideTrack"); if (t) t.classList.add("show-chats"); renderSessions(); }
function sideShowMain() { const t = $("#sideTrack"); if (t) t.classList.remove("show-chats"); }
let _foldClosed = new Set();                        // collapsed date-folders (by date key)
function renderSessions() {
  const box = $("#sessions"); if (!box) return; box.innerHTML = "";
  if (state.session && !_chats.some(s => s.id === state.session)) {   // brand-new chat, not yet saved
    const el = document.createElement("div"); el.className = "session active";
    el.innerHTML = `<span class="s-title"></span>`; $(".s-title", el).textContent = _curTitle || "New voyage";
    box.appendChild(el);
  }
  const groups = {};                               // group by date → dated "folders"
  _chats.forEach(s => (groups[s.date || "—"] ||= []).push(s));
  Object.keys(groups).sort().reverse().forEach(date => {
    const fold = document.createElement("div"); fold.className = "s-folder" + (_foldClosed.has(date) ? "" : " open");
    const h = document.createElement("div"); h.className = "s-folder-h";
    h.innerHTML = `<span class="s-fold-ic">▾</span><span class="s-fold-date"></span><span class="s-fold-n">${groups[date].length}</span>`;
    $(".s-fold-date", h).textContent = _fmtChatDate(date);
    h.onclick = () => { _foldClosed.has(date) ? _foldClosed.delete(date) : _foldClosed.add(date); fold.classList.toggle("open"); };
    const inner = document.createElement("div"); inner.className = "s-folder-body";
    groups[date].forEach(s => {
      const el = document.createElement("div");
      el.className = "session" + (s.id === state.session ? " active" : "");
      el.innerHTML = `<span class="s-title"></span><button class="s-del" title="delete voyage">✕</button>`;
      $(".s-title", el).textContent = s.title;
      el.onclick = () => openVoyage(s.id);
      $(".s-del", el).onclick = e => { e.stopPropagation(); deleteVoyage(s.id); };
      inner.appendChild(el);
    });
    fold.appendChild(h); fold.appendChild(inner); box.appendChild(fold);
  });
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
// Live progress from a streaming tool (the delegate): append its narration/tool-uses into the
// card's result area as it works, so a long delegation shows activity instead of freezing.
function appendToolProgress(card, ev) {
  if (!card) return;
  let line = "";
  if (ev.kind === "text" && ev.text) line = ev.text.trim();
  else if (ev.kind === "tool") line = "↳ " + (ev.tool || "tool") + (ev.detail ? " · " + ev.detail : "");
  if (!line) return;
  card.classList.add("open");                         // auto-expand so the stream is visible
  const res = $(".result", card);
  res.textContent += (res.textContent ? "\n" : "") + line;
  const st = $(".tstat", card); if (st) { st.classList.add("run"); st.textContent = "streaming…"; }
  toBottom();
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

/* ---------- chat attachments (drag & drop · paste · 📎) ---------- */
let _pendingAttachments = [];
const _rawURL = p => "/api/raw?path=" + encodeURIComponent(p);
async function uploadFile(file) {
  if (!file) return;
  const fd = new FormData(); fd.append("file", file, file.name || "file");
  let r; try { r = await fetch("/api/upload", { method: "POST", body: fd }).then(x => x.json()); }
  catch { toast("upload failed", "err"); return; }
  if (!r || !r.ok) { toast((r && r.error) || "upload failed", "err"); return; }
  _pendingAttachments.push(r); renderAttachTray();
}
function clearAttachments() { _pendingAttachments = []; renderAttachTray(); }
function _attChipHTML(a, removable) {
  return (a.kind === "image" ? `<img class="att-thumb" src="${_rawURL(a.path)}" alt="">` : `<span class="att-ic">📄</span>`)
    + `<span class="att-name">${escapeHtml(a.name)}</span>` + (removable ? `<button class="att-x" title="remove">✕</button>` : "");
}
function renderAttachTray() {
  const tray = $("#attachTray"); if (!tray) return;
  tray.innerHTML = ""; tray.style.display = _pendingAttachments.length ? "flex" : "none";
  _pendingAttachments.forEach((a, i) => {
    const chip = document.createElement("div"); chip.className = "att-chip att-" + a.kind; chip.innerHTML = _attChipHTML(a, true);
    $(".att-x", chip).onclick = () => { _pendingAttachments.splice(i, 1); renderAttachTray(); };
    tray.appendChild(chip);
  });
}
function renderMsgAttachments(el, atts) {
  if (!el || !atts.length) return;
  const box = document.createElement("div"); box.className = "msg-atts";
  atts.forEach(a => {
    const c = document.createElement("div"); c.className = "att-chip att-" + a.kind; c.innerHTML = _attChipHTML(a, false);
    if (a.kind === "image") c.onclick = () => openFileWindow(a.path);
    box.appendChild(c);
  });
  el.appendChild(box);
}
function wireAttach() {
  const btn = $("#attachBtn"), inp = $("#attachInput"); if (!btn || !inp) return;
  btn.onclick = () => inp.click();
  inp.onchange = () => { [...inp.files].forEach(uploadFile); inp.value = ""; };
  const composer = $(".composer");
  if (composer) {
    ["dragover", "dragenter"].forEach(ev => composer.addEventListener(ev, e => { e.preventDefault(); composer.classList.add("drop"); }));
    ["dragleave", "dragend"].forEach(ev => composer.addEventListener(ev, () => composer.classList.remove("drop")));
    composer.addEventListener("drop", e => { e.preventDefault(); composer.classList.remove("drop"); [...(e.dataTransfer.files || [])].forEach(uploadFile); });
  }
  $("#input").addEventListener("paste", e => {
    const imgs = [...(e.clipboardData.items || [])].filter(it => it.type.startsWith("image/"));
    if (imgs.length) { e.preventDefault(); imgs.forEach(it => uploadFile(it.getAsFile())); }
  });
}

async function send() {
  const input = $("#input"), text = input.value.trim();
  if ((!text && !_pendingAttachments.length) || state.busy) return;
  const sc = slashName(text);                       // composer command (/status, /compact, …)?
  if (sc) {
    input.value = ""; autosize(input); state.busy = true;
    try { await runSlash(sc, text); } finally { state.busy = false; input.focus(); }
    return;
  }
  if (!state.model) { flashModel(); return; }
  state.busy = true; setSendMode(true);
  $("#send").classList.add("ping"); setTimeout(() => $("#send").classList.remove("ping"), 600);
  input.value = ""; autosize(input);
  const atts = _pendingAttachments.slice(); clearAttachments();   // capture + reset the tray
  const ue = addUser(text); if (atts.length) renderMsgAttachments(ue, atts);
  touchTitle(text || (atts[0] && atts[0].name) || "attachment"); appendT({ role: "user", content: text });

  const payload = { session: state.session, message: text, model: state.model, base_url: state.baseUrl, agent_mode: state.agent,
                    attachments: atts.map(a => ({ path: a.path, name: a.name, kind: a.kind })) };
  let sounding = addThinking(), bubble = null, acc = "", thinkCard = null, thinkText = "", lastCard = null, lastTool = null, _lastDraw = 0, stats = null, livePopped = false;
  const killSounding = () => { if (sounding) { sounding.remove(); sounding = null; } };
  // throttle the live re-render to ~10/s — renderMD re-parses the WHOLE answer each call, so drawing
  // every token/frame is O(n²) on a long reply. Skipped frames are caught by the final full render.
  const draw = () => { if (bubble && performance.now() - _lastDraw >= 100) { _lastDraw = performance.now(); renderMD(bubble, acc + " ▌"); toBottom(); } };
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
          acc += ev.text; if (_voiceSpeak) _voiceSpeak.push(ev.text);   // conversation mode: stream to TTS
          draw();                                                        // throttled internally; final render catches the tail
        } else if (ev.type === "tool_call") {
          killSounding(); flushThink(); flushBubble();
          if (!livePopped && /^(fetch_url|browser_)/.test(ev.name)) { openLiveView(); livePopped = true; }  // pop the Live view when it starts browsing
          lastCard = addTool(ev.name, ev.args); lastTool = { name: ev.name, args: ev.args };
          maybePreviewChip(lastCard, ev.name, ev.args);   // ▶ Preview chip if it's an .html file
        } else if (ev.type === "tool_progress") {
          killSounding(); appendToolProgress(lastCard, ev);
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
        } else if (ev.type === "notice") {
          killSounding(); flushThink(); flushBubble(); addSysNote(escapeHtml(ev.text));
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

/* ---------- hands-free voice conversation (talk ↔ it talks back; reuses the chat turn) ----------
   Loop: energy-VAD listen → /api/voice/stt → [optional wake-word gate] → send() (the SAME agent turn,
   so tools + UI control fire) with answer tokens streamed sentence-by-sentence to /api/voice/tts →
   play → resume listening. Half-duplex (we pause listening while it speaks, so it can't hear itself). */
let _converse = null;

function cvStatus(t) { const e = $("#converseStatus"); if (e) e.textContent = t; }

function makeVoiceSpeaker(onPlay) {
  // accumulate the FULL answer, then speak it as ONE clip — smooth & continuous, no per-phrase gaps
  let buf = "", dead = false, done;
  const drained = new Promise(r => done = r);
  const audio = new Audio();
  return {
    push(t) { buf += t; },                 // just collect; nothing is synthesized until end()
    async end() {
      const text = buf.trim();
      if (dead || !text) { done(); return; }
      try {
        const r = await fetch("/api/voice/tts", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }) });
        if (r.ok && !dead) {
          if (onPlay) onPlay();
          const url = URL.createObjectURL(await r.blob());
          audio.src = url;
          await audio.play().catch(() => {});
          await new Promise(res => { audio.onended = res; audio.onerror = res; });
          URL.revokeObjectURL(url);
        }
      } catch {}
      done();
    },
    stop() { dead = true; try { audio.pause(); } catch {} done(); },
    drained,
  };
}

async function toggleConverse() {
  if (_converse) { stopConverse(); return; }
  if (!state.model) { flashModel(); return; }
  let s; try { s = await api("/api/voice/status"); } catch { s = {}; }
  if (!s.stt || !s.tts) { toast("voice conversation needs STT + TTS — run the installer or set up a voice", "err"); return; }
  let stream;
  try { stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } }); }
  catch { toast("microphone permission denied", "err"); return; }
  _converse = { stream, wake: !!s.wake, wakeWord: (s.wake_word || "oceano").toLowerCase(), busy: false };
  const btn = $("#converseBtn"); if (btn) btn.classList.add("on");
  const bar = $("#converseBar"); if (bar) bar.style.display = "flex";
  toast("conversation mode on — just talk", "info");
  startListening();
}
function stopConverse() {
  if (!_converse) return;
  if (_converse._stopListen) _converse._stopListen();
  if (_converse.speaker) _converse.speaker.stop();
  try { _converse.stream.getTracks().forEach(t => t.stop()); } catch {}
  _converse = null;
  const btn = $("#converseBtn"); if (btn) btn.classList.remove("on");
  const bar = $("#converseBar"); if (bar) bar.style.display = "none";
}
function startListening() {
  if (!_converse || _converse.busy) return;
  if (_converse._stopListen) { try { _converse._stopListen(); } catch {} _converse._stopListen = null; }  // close the previous AudioContext + interval before opening a new one
  cvStatus("listening…");
  const stream = _converse.stream;
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  const an = ctx.createAnalyser(); an.fftSize = 512;
  ctx.createMediaStreamSource(stream).connect(an);
  const data = new Uint8Array(an.fftSize);
  let rec, chunks = [], speaking = false, recording = false, t0 = 0, sil = 0;
  const SIL = 800, THRESH = 0.02;     // ~0.8s trailing silence ends the utterance
  const iv = setInterval(() => {
    if (!_converse) { clearInterval(iv); ctx.close().catch(() => {}); return; }
    an.getByteTimeDomainData(data);
    let sum = 0; for (let i = 0; i < data.length; i++) { const v = (data[i] - 128) / 128; sum += v * v; }
    const rms = Math.sqrt(sum / data.length), now = performance.now();
    if (rms > THRESH) {
      if (!speaking) { speaking = true; t0 = now; }
      sil = 0;
      if (!recording && now - t0 > 120) {
        rec = new MediaRecorder(stream); chunks = [];
        rec.ondataavailable = e => { if (e.data.size) chunks.push(e.data); };
        rec.onstop = async () => { clearInterval(iv); ctx.close().catch(() => {}); await handleUtterance(new Blob(chunks, { type: "audio/webm" })); };
        rec.start(); recording = true;
      }
    } else if (speaking) {
      if (!sil) sil = now;
      if (recording && now - sil > SIL) { try { rec.stop(); } catch {} }      // → onstop handles it
      else if (!recording && now - t0 > 1500) speaking = false;               // a blip, not speech
    }
  }, 50);
  _converse._stopListen = () => { clearInterval(iv); try { if (rec && rec.state === "recording") rec.stop(); } catch {} ctx.close().catch(() => {}); };
}
async function handleUtterance(blob) {
  if (!_converse) return;
  _converse.busy = true;
  cvStatus("transcribing…");
  let text = "";
  try { const r = await fetch("/api/voice/stt", { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: blob }); text = ((await r.json()).text || "").trim(); }
  catch {}
  const resume = () => { if (_converse) { _converse.busy = false; startListening(); } };
  if (!text) return resume();
  if (_converse.wake) {       // wake-word gate (transcript prefix) — configured in Settings → Voice
    const ww = _converse.wakeWord, low = text.toLowerCase();
    if (!low.startsWith(ww) && !low.startsWith("hey " + ww)) return resume();   // not addressed
    text = text.replace(new RegExp("^\\s*(hey\\s+)?" + ww.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "[\\s,.:]*", "i"), "").trim();
    if (!text) return resume();
  }
  cvStatus("thinking…");
  const speaker = makeVoiceSpeaker(() => cvStatus("speaking…"));
  _converse.speaker = speaker; _voiceSpeak = speaker;
  const input = $("#input"); input.value = text; input.dispatchEvent(new Event("input"));
  try { await send(); } catch {}
  _voiceSpeak = null;
  speaker.end();
  await speaker.drained;
  if (_converse) _converse.speaker = null;
  resume();
}
// After a reload, re-attach to a reply that was still being generated server-side (the turn keeps
// running even though the SSE dropped). Renders what was buffered, polls for the rest, then saves.
async function maybeReconnectChat() {
  const sid = state.session; if (!sid) return;
  let live; try { live = await api("/api/chat/live/" + encodeURIComponent(sid)); } catch { return; }
  if (!live || (!live.running && !(live.events || []).length)) return;
  const lastUser = [..._curT].reverse().find(m => m.role === "user");
  if (!live.running && lastUser && lastUser.content === live.message) return;   // already saved — skip
  reconnectChat(sid, live);
}
async function reconnectChat(sid, live) {
  if (state.busy) return;
  state.busy = true; setSendMode(true);
  addUser(live.message); appendT({ role: "user", content: live.message });
  addSysNote("↻ reconnected to a reply that was still being generated…");
  let bubble = null, acc = "", thinkCard = null, thinkText = "", lastCard = null, lastTool = null;
  const flushThink = () => { if (thinkCard) { finalizeThink(thinkCard); appendT({ role: "thinking", text: thinkText }); thinkCard = null; thinkText = ""; } };
  const flushBubble = () => { if (bubble) { renderMD(bubble, acc, true); appendT({ role: "assistant", content: acc }); bubble = null; acc = ""; } };
  const apply = ev => {
    if (ev.type === "reasoning") { flushBubble(); if (!thinkCard) thinkCard = addThinkCard(); thinkText += ev.text; appendThink(thinkCard, ev.text); }
    else if (ev.type === "token") { flushThink(); if (!bubble) bubble = addAssistant(""); acc += ev.text; renderMD(bubble, acc + " ▌"); toBottom(); }
    else if (ev.type === "tool_call") { flushThink(); flushBubble(); lastCard = addTool(ev.name, ev.args); lastTool = { name: ev.name, args: ev.args }; maybePreviewChip(lastCard, ev.name, ev.args); }
    else if (ev.type === "tool_progress") { appendToolProgress(lastCard, ev); }
    else if (ev.type === "tool_result") { fillTool(lastCard, ev.result); if (lastTool) { appendT({ role: "tool", name: lastTool.name, args: lastTool.args, result: ev.result }); lastTool = null; } }
    else if (ev.type === "answer_done") { flushThink(); if (bubble) renderMD(bubble, acc, true); }
    else if (ev.type === "answer") { flushThink(); if (!bubble) bubble = addAssistant(""); acc = ev.text; renderMD(bubble, acc, true); toBottom(); }
    else if (ev.type === "notice") { flushThink(); flushBubble(); addSysNote(escapeHtml(ev.text)); }
    else if (ev.type === "error") { flushThink(); if (!bubble) bubble = addAssistant(""); acc = "⚠️ " + (ev.message || "error"); renderMD(bubble, acc); }
  };
  (live.events || []).forEach(apply);
  let since = live.total != null ? live.total : (live.events || []).length, running = live.running;
  while (running) {
    await new Promise(r => setTimeout(r, 800));
    let d; try { d = await api("/api/chat/live/" + encodeURIComponent(sid) + "?since=" + since); } catch { break; }
    (d.events || []).forEach(apply);
    since = d.total != null ? d.total : since; running = d.running;
  }
  flushThink(); flushBubble();
  persistChat();
  state.busy = false; setSendMode(false);
}

/* ---------------- composer slash-commands (mirror Telegram, minus model selection) ---------------- */
// single source of truth — drives autocomplete, /help, and recognition
const CHAT_COMMANDS = [
  { name: "status", args: "", desc: "model, context size & live metrics" },
  { name: "context", args: "[n|off]", desc: "show context size, or set/clear auto-compact" },
  { name: "compact", args: "", desc: "summarize & shrink the context now" },
  { name: "tools", args: "", desc: "list the tools the agent can call" },
  { name: "agent", args: "[on|off]", desc: "toggle agent mode (tools)" },
  { name: "skill", args: "", desc: "distill this conversation into a reusable skill" },
  { name: "reset", args: "", desc: "start a fresh conversation" },
  { name: "help", args: "", desc: "show this list" },
];
const SLASH_CMDS = CHAT_COMMANDS.map(c => c.name).concat("clear");   // clear = hidden alias for reset
function slashName(text) {
  if (text[0] !== "/") return null;
  const c = text.slice(1).split(/\s+/)[0].toLowerCase();
  return SLASH_CMDS.includes(c) ? c : null;         // unknown /foo → falls through, sent to the model
}
// ---- composer command autocomplete (popover above the input) ----
let _acIdx = 0, _acMatches = [];
function ensureCmdAC() {
  let ac = $("#cmdAC");
  if (!ac) {
    ac = document.createElement("div"); ac.id = "cmdAC"; ac.className = "cmd-ac"; ac.style.display = "none";
    const host = $(".composer-inner"); if (host) host.appendChild(ac);
  }
  return ac;
}
function cmdACHide() { const ac = $("#cmdAC"); if (ac) ac.style.display = "none"; _acMatches = []; }
function cmdACPaint() {
  $$("#cmdAC .cmd-ac-item").forEach((el, i) => el.classList.toggle("active", i === _acIdx));
  const a = $(`#cmdAC .cmd-ac-item.active`); if (a) a.scrollIntoView({ block: "nearest" });
}
function cmdACUpdate() {
  const m = /^\/(\w*)$/.exec($("#input").value);    // only while typing the command name (no space/arg yet)
  if (!m) return cmdACHide();
  const partial = m[1].toLowerCase();
  const matches = CHAT_COMMANDS.filter(c => c.name.startsWith(partial));
  if (!matches.length) return cmdACHide();
  const ac = ensureCmdAC();
  _acMatches = matches; _acIdx = 0;
  ac.innerHTML = matches.map((c, i) =>
    `<div class="cmd-ac-item" data-i="${i}"><span class="cmd-ac-name">/${c.name}` +
    `${c.args ? ` <span class="cmd-ac-args">${escapeHtml(c.args)}</span>` : ""}</span>` +
    `<span class="cmd-ac-desc">${escapeHtml(c.desc)}</span></div>`).join("");
  ac.style.display = ""; cmdACPaint();
  $$("#cmdAC .cmd-ac-item").forEach(el => {
    el.onmousedown = e => { e.preventDefault(); cmdACAccept(+el.dataset.i); };  // mousedown beats blur
    el.onmouseenter = () => { _acIdx = +el.dataset.i; cmdACPaint(); };
  });
}
function cmdACAccept(i) {
  const c = _acMatches[i]; if (!c) return;
  const input = $("#input");
  input.value = "/" + c.name + " ";                 // leave a space so args can follow / Enter runs it
  cmdACHide(); autosize(input); input.focus();
}
function cmdACKey(e) {                               // returns true if it consumed the key
  const ac = $("#cmdAC");
  if (!ac || ac.style.display === "none" || !_acMatches.length) return false;
  if (e.key === "ArrowDown") { e.preventDefault(); _acIdx = (_acIdx + 1) % _acMatches.length; cmdACPaint(); return true; }
  if (e.key === "ArrowUp") { e.preventDefault(); _acIdx = (_acIdx - 1 + _acMatches.length) % _acMatches.length; cmdACPaint(); return true; }
  if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); cmdACAccept(_acIdx); return true; }
  if (e.key === "Escape") { e.preventDefault(); cmdACHide(); return true; }
  return false;
}
// display-only note in the thread (never persisted, never sent to the model)
function addSysNote(html) {
  clearWelcome();
  const el = document.createElement("div"); el.className = "sys-note";
  el.innerHTML = `<span class="sys-ic">⌘</span><div class="sys-body">${html}</div>`;
  $("#thread").appendChild(el); toBottom();
  return $(".sys-body", el);
}
function setSysNote(body, html) { if (body) { body.innerHTML = html; toBottom(); } }
const _postJ = (url, obj) => fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(obj) }).then(r => r.json());
function _ctxLine(d) {
  const tok = d.ctx_tokens ? `${fmtNum(d.ctx_tokens)} tok` : `~${fmtNum(d.approx_tokens)} tok (est.)`;
  const cap = d.cap ? `auto-compact at ${d.cap} msgs` : "auto-compact off";
  return `${d.messages} msgs · ${tok} · ${d.compactions} compaction(s) · ${cap}`;
}
async function runSlash(cmd, text) {
  const arg = text.slice(1).split(/\s+/).slice(1).join(" ").trim();
  const sid = state.session, qs = "?session=" + encodeURIComponent(sid);
  if (cmd === "help") {
    addSysNote("<b>Chat commands</b> <span class=\"sys-hint\">— type / for autocomplete</span><br>"
      + CHAT_COMMANDS.map(c => `<div class="sys-cmd"><code>/${c.name}${c.args ? " " + c.args : ""}</code> — ${c.desc}</div>`).join(""));
    return;
  }
  if (cmd === "reset" || cmd === "clear") { fetch("/api/session/" + encodeURIComponent(sid), { method: "DELETE" }).catch(() => {}); newVoyage(); return; }
  if (cmd === "skill") {
    const note = addSysNote("⚗ distilling this conversation into a skill… (a stronger model is reviewing it)");
    let d; try { d = await _postJ("/api/chats/" + encodeURIComponent(sid) + "/to-skill", {}); } catch { setSysNote(note, "⚗ skill distillation failed"); return; }
    if (!d.ok) setSysNote(note, "⚗ " + escapeHtml(d.error || "couldn't distill a skill"));
    else if (!d.saved) setSysNote(note, "⚗ Nothing reusable to save here — " + escapeHtml(d.reason || ""));
    else setSysNote(note, `⚗ Saved <b>${escapeHtml(d.name)}</b> as a <i>learning</i> skill — it goes live after an independent review (Brain → Skills).<br><span class="sys-hint">${escapeHtml(d.description || "")}</span>`);
    return;
  }
  if (cmd === "agent") {
    const on = /^(on|off)$/i.test(arg) ? /on/i.test(arg) : !state.agent;
    state.agent = on; const t = $("#agentToggle"); if (t) t.checked = on;
    _postJ("/api/prefs", { agent_mode: on }).catch(() => {});
    addSysNote(`Agent mode <b>${on ? "on" : "off"}</b> — ${on ? "tools enabled (browsing, files, shell, …)." : "plain chat, no tools."}`);
    return;
  }
  if (cmd === "tools") {
    const note = addSysNote("…");
    let d; try { d = await api("/api/chat/status" + qs); } catch { setSysNote(note, "unavailable"); return; }
    setSysNote(note, `<b>${d.tool_count}</b> tools available<br><span class="sys-tools">${d.tools.map(escapeHtml).join(", ")}</span>`);
    return;
  }
  if (cmd === "context") {
    if (arg) {
      const d = await _postJ("/api/chat/context", { session: sid, value: arg });
      if (d.ok === false) addSysNote(escapeHtml(d.error));
      else addSysNote(d.cap ? `Auto-compact set — I'll summarize once this chat passes <b>${d.cap}</b> messages.` : "Auto-compact <b>off</b>.");
    } else {
      let d; try { d = await api("/api/chat/context" + qs); } catch { return; }
      addSysNote("📜 <b>Context</b> · " + _ctxLine(d));
    }
    return;
  }
  if (cmd === "compact") {
    const note = addSysNote("🗜 compacting…");
    let d; try { d = await _postJ("/api/chat/compact", { session: sid }); } catch { setSysNote(note, "compact failed"); return; }
    if (d.ok === false) setSysNote(note, escapeHtml(d.error));
    else setSysNote(note, `🗜 Compacted — folded <b>${d.dropped}</b> messages into a summary.<br>Context now ${_ctxLine(d)}`);
    return;
  }
  if (cmd === "status") {
    const note = addSysNote("…");
    let d; try { d = await api("/api/chat/status" + qs); } catch { setSysNote(note, "status unavailable"); return; }
    const tok = d.ctx_tokens ? `${fmtNum(d.ctx_tokens)} tok` : `~${fmtNum(d.approx_tokens)} tok (est.)`;
    setSysNote(note, "<b>🌊 Oceano — status</b>" + [
      ["model", escapeHtml(d.model || "—")],
      ["mode", state.agent ? "agent (tools on)" : "plain chat"],
      ["context", `${d.messages} msgs · ${tok}`],
      ["auto-compact", d.cap ? `> ${d.cap} msgs` : "off"],
      ["compactions", `${d.compactions} this session`],
      ["tools", `${d.tool_count} available`],
      ["memory", `${d.memory} facts · ${d.docs} docs indexed`],
    ].map(([k, v]) => `<div class="sys-row"><span class="sys-k">${k}</span><span class="sys-v">${v}</span></div>`).join(""));
    return;
  }
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
  try { const md = await api("/api/mind"); state.claudeAvailable = !!md.claude_available; state.mind = md.mind; }
  catch { state.claudeAvailable = false; }
  buildModelMenu();
  setStatus(state.models.some(m => m.base_url.includes("8081") && !m.error));
  // Auto-pick only when nothing is chosen yet (or a chosen Claude has vanished) — never override
  // the user's live selection on the 30s refresh (that caused a mid-chat revert to Claude).
  if (!state.model || (state.model === "claude" && !state.claudeAvailable)) {
    if (state.mind === "claude" && state.claudeAvailable) selectClaude(false);
    else await selectDefaultModel();
  }
}
async function selectDefaultModel() {
  if (state.mind === "claude" && state.claudeAvailable) { selectClaude(false); return; }   // Claude is the chosen mind → stays the default everywhere
  const ok = (state.models || []).filter(m => !m.error);
  if (!ok.length) return;
  let d = {}; try { d = await api("/api/default-model"); } catch {}   // the configured primary
  const want = d.model || d.current || "", wantBase = d.base_url || "";
  const pick = (want && ok.find(m => m.id === want && (!wantBase || m.base_url === wantBase)))
            || (want && ok.find(m => m.id === want))                  // primary by id, any endpoint
            || ok[0];          // nothing configured → first available model (no hardcoded default)
  if (pick) selectModel(pick);
}
function buildModelMenu() {
  const menu = $("#modelMenu"); menu.innerHTML = "";
  if (state.claudeAvailable) {                         // Claude Code as the resident mind (your subscription)
    const g = document.createElement("div"); g.className = "mm-group"; g.textContent = "mind"; menu.appendChild(g);
    const it = document.createElement("div");
    it.className = "mm-item mm-claude" + (state.model === "claude" ? " sel" : "");
    it.innerHTML = `<span class="mp-dot"></span>🧠 Claude <span class="mm-sub">your subscription · Oceano's body</span>`;
    it.onclick = () => { selectClaude(); $("#modelMenu").classList.remove("open"); };
    menu.appendChild(it);
  }
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
  if (state.mind === "claude") {                       // picking a real model → hand the mind back to local
    state.mind = "local";
    api("/api/mind", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mind: "local" }) }).catch(() => {});
  }
  buildModelMenu();
}
// Claude as the mind: persists mind=claude so the turn routes to Claude Code (Oceano's persona +
// memory + workspace). state.model="claude" is a sentinel so send() proceeds even with no local model.
function selectClaude(persist = true) {
  state.model = "claude"; state.baseUrl = ""; state.mind = "claude";
  $("#modelLabel").textContent = "🧠 Claude";
  $("#depthReadout").textContent = "mind · Claude (your subscription)";
  if (persist) api("/api/mind", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mind: "claude" }) }).catch(() => {});
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
  $("#providerSelect").innerHTML = provs.map(p => `<option value="${p.base_url}" data-needs="${p.needs_key}" data-name="${escapeHtml(p.name)}" data-console="${escapeHtml(p.console || "")}">${escapeHtml(p.name)}</option>`).join("")
    + `<option value="" data-needs="false" data-name="" data-console="">Custom (any OpenAI-compatible URL)…</option>`;
  $("#providerSelect").onchange = syncProviderFields; syncProviderFields();
}
function syncProviderFields() {
  const o = $("#providerSelect").selectedOptions[0]; if (!o) return;
  const custom = !o.value, needsKey = custom || o.dataset.needs === "true";
  $("#epUrl").value = o.value;
  $("#epUrl").placeholder = custom ? "base URL, e.g. http://192.168.1.20:11434/v1" : o.value;
  $("#epName").value = o.dataset.name;
  $("#epKey").style.display = needsKey ? "block" : "none"; $("#epKey").value = "";
  const link = $("#epConsole");                         // "Get an API key →" for this provider
  if (link) {
    const url = o.dataset.console || "";
    if (needsKey && url) { link.href = url; link.textContent = "Get an API key for " + (o.dataset.name || "this provider") + " →"; link.style.display = "block"; }
    else link.style.display = "none";
  }
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
function _svc(label, ok, detail, restart) {
  const dot = ok === null ? "idle" : ok ? "on" : "off";
  const btn = restart ? `<button class="svc-restart" data-svc="${restart}" title="restart this service">⟳</button>` : "";
  return `<div class="svc"><span class="svc-dot ${dot}"></span><span class="svc-name">${label}</span><span class="svc-detail">${escapeHtml(detail)}</span>${btn}</div>`;
}
async function loadServices() {
  const box = $("#svcList"); if (!box) return;
  try {
    const s = await api("/api/status");
    const beat = s.scheduler_beat_ago;
    const schedOk = beat != null && beat < 90;   // heartbeat is every 30s
    const tg = s.telegram || {}, ls = s.llamaswap || {}, vo = s.voice || {};
    const lsDetail = ls.ok ? (ls.loaded ? "loaded: " + ls.loaded : `${(ls.models || []).length} served · idle`) : "down";
    box.innerHTML =
      _svc("Web UI", true, "this page") +
      _svc("Chat models (:8081)", ls.ok, lsDetail, "llamaswap") +                 // llama-swap — restartable via the polkit rule
      _svc("Embeddings (:8082)", s.embed, s.embed ? "reachable" : "down", "embeddings") +
      _svc("Web search (:8080)", s.searxng, s.searxng ? "SearXNG reachable" : "down") +
      _svc("Voice · speak (TTS)", vo.tts, vo.tts ? `${vo.tts_engine || "?"}${vo.tts_voice ? " · " + vo.tts_voice : ""}` : "unavailable", vo.tts ? "tts" : "") +
      _svc("Voice · listen (STT)", vo.stt, vo.stt ? (vo.stt_model || "whisper") : "unavailable", vo.stt ? "stt" : "") +
      _svc("Scheduler", schedOk, beat == null ? "no heartbeat" : `beat ${Math.round(beat)}s ago`) +
      _svc("Telegram", tg.running, tg.running ? "@" + (tg.username || "bot") : (tg.error ? "error" : "off"), (tg.running || tg.enabled) ? "telegram" : "");
    box.querySelectorAll(".svc-restart").forEach(b => b.onclick = () => restartService(b));
  } catch { box.innerHTML = `<div class="svc"><span class="svc-dot off"></span><span class="svc-name">status unavailable</span></div>`; }
}
async function restartService(btn) {
  const svc = btn.dataset.svc; btn.disabled = true; btn.classList.add("spin");
  let r; try {
    r = await api("/api/services/restart", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ service: svc }) });
  } catch { r = { ok: false, error: "request failed" }; }
  btn.classList.remove("spin");
  toast(r.ok ? (r.msg || "restarted") : (r.error || "restart failed"), r.ok ? "info" : "err");
  setTimeout(loadServices, 1500); setTimeout(loadServices, 5000);   // it's briefly down during a restart — re-poll twice so the status recovers
}

/* ================= SETTINGS WINDOW ================= */
const SETTINGS_TABS = [
  ["account", "◐", "Account"], ["endpoints", "◇", "Endpoints"], ["telegram", "✈", "Telegram"],
  ["memory", "✶", "Memory"], ["tools", "⚒", "Tools"], ["delegate", "⇅", "Delegation"],
  ["voice", "🔊", "Voice"], ["services", "◉", "Services"], ["wipe", "🗑", "Wipe"], ["about", "≈", "About"],
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
    </div>
    <div class="drawer-section">
      <h3>Two-factor authentication <span class="lbl-sub">optional</span></h3>
      <div id="twofaBody"><div class="acct-row">checking…</div></div>
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
        <a id="epConsole" class="ep-console" target="_blank" rel="noopener" style="display:none"></a>
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
      <h3>Execution</h3>
      <p class="sub">When on, Oceano runs background jobs — scheduled tasks, workflows, research, memory upkeep — <b>one at a time</b>. A job that starts while another is working waits in a queue instead of hitting the local model in parallel. The eval suite paces itself and isn't gated.</p>
      <label class="set-toggle"><input type="checkbox" id="serializeToggle"><span class="st-track"><span class="st-thumb"></span></span><span class="st-lbl">Queue background jobs (serialize model work)</span></label>
      <label class="set-toggle"><input type="checkbox" id="serializeChatToggle"><span class="st-track"><span class="st-thumb"></span></span><span class="st-lbl">Queue chat messages too <span class="st-note">— a chat turn also waits behind running work (share the gate; enable the option above for full serialization)</span></span></label>
    </div>
    <div class="drawer-section">
      <h3>Tools <span class="tool-count" id="toolCount"></span></h3>
      <p class="sub">Toggle what the agent can reach in Agent mode. Turning a tool off removes it from the model's prompt — handy to lower context (and cost) behind your tooling.</p>
      <div class="chat-tools">
        <div class="ct-head">⌘ Memory in chat-only mode</div>
        <p class="sub">Even with Agent mode off, the model can use these memory tools to recall, store, edit, or forget what it knows about you. Pick which are available in plain chat — reading your memories is always on; these add deliberate actions. Uncheck all to keep chat fully tool-free.</p>
        <div class="ct-list" id="chatToolList"></div>
      </div>
      <div class="tool-acts"><span class="lbl-sub" style="flex:1">All tools (both modes)</span><button class="exp-btn" id="toolAllOn">Enable all</button><button class="exp-btn" id="toolAllOff">Disable all</button></div>
      <div class="tool-list" id="toolList"></div>
    </div>`,
  delegate: `
    <div class="drawer-section">
      <h3>Primary intelligence</h3>
      <p class="sub">Who actually drives your chats. <b>Oceano is the body</b> — its memory, workspace, voice, and windows. This picks the <b>mind</b>.</p>
      <div class="dg-providers" id="mindPick">
        <label class="dg-prov"><input type="radio" name="oc-mind" value="local"><span><b>Local model</b><i>fully offline, on your box — the model you serve in Rivers</i></span></label>
        <label class="dg-prov"><input type="radio" name="oc-mind" value="claude"><span><b>Claude (your subscription)</b><i>Claude Code as the resident mind — Oceano's persona, memory & workspace, no API key</i></span></label>
      </div>
      <div class="dg-hint" id="mindNote"></div>
    </div>
    <div class="drawer-section">
      <h3>Delegation</h3>
      <p class="sub">Who handles delegated subtasks. The local model never reviews its own work. A cloud model runs through Oceano's own agent loop with your tools — it can read, write, and run things, just like a local model.</p>
      <div class="dg-status" id="dgStatus">checking…</div>

      <label class="dg-toggle"><input type="checkbox" id="dgEnabled"> <b>Delegation enabled</b>
        <span class="lbl-sub">— turn off to fully disable it: the delegate tool is withheld from the agent and delegated background jobs stop. (Also per-tool under Settings → Tools.)</span></label>

      <div class="dg-role" id="dgPrimary">
        <div class="dg-h">Primary model <span class="lbl-sub">— what Oceano uses everywhere: chat, Telegram, CLI, background jobs. Pick any model from any endpoint; local-first is optional.</span></div>
        <div class="dg-row">
          <select class="dg-model" id="dgDefaultModel" style="display:inline-block;min-width:220px"></select>
          <span class="dg-probe" id="dgDefaultMsg"></span>
        </div>
        <div class="dg-hint">Saved on change and used immediately by new conversations and jobs. Pick from models your local stack serves.</div>
      </div>

      <div class="dg-role">
        <div class="dg-h">General <span class="lbl-sub">— the agent's “delegate” tool</span></div>
        <div class="dg-providers">
          <label class="dg-prov"><input type="radio" name="dg-default" value="claude_cli"><span><b>Claude Code</b><i>CLI agent · your subscription (no API key)</i></span></label>
          <label class="dg-prov"><input type="radio" name="dg-default" value="api"><span><b>Cloud model</b><i>an endpoint you configured</i></span></label>
        </div>
        <select class="dg-model" id="dgModel-default" style="display:none"></select>
        <div class="dg-row"><button class="exp-btn dg-test" data-role="default">Test / Re-check</button><span class="dg-probe" id="dgProbe-default"></span></div>
      </div>

      <div class="dg-role">
        <div class="dg-h">Self-improvement jobs <span class="lbl-sub">— skills review · eval judging · memory maintenance</span></div>
        <div class="dg-providers">
          <label class="dg-prov"><input type="radio" name="dg-improve" value="inherit"><span><b>Same as general</b><i>follow whatever the general delegate is set to</i></span></label>
          <label class="dg-prov"><input type="radio" name="dg-improve" value="claude_cli"><span><b>Claude Code</b></span></label>
          <label class="dg-prov"><input type="radio" name="dg-improve" value="api"><span><b>Cloud model</b><i>an endpoint you configured</i></span></label>
        </div>
        <select class="dg-model" id="dgModel-improve" style="display:none"></select>
        <div class="dg-row"><button class="exp-btn dg-test" data-role="improve">Test / Re-check</button><span class="dg-probe" id="dgProbe-improve"></span></div>
      </div>

      <div class="dg-role">
        <div class="dg-h">Image recognition <span class="lbl-sub">— the local chat model is text-only; images go here</span></div>
        <div class="dg-providers">
          <label class="dg-prov"><input type="radio" name="dg-vision" value="inherit"><span><b>Same as general</b><i>follow whatever the general delegate is set to</i></span></label>
          <label class="dg-prov"><input type="radio" name="dg-vision" value="claude_cli"><span><b>Claude Code</b><i>reads the image file directly</i></span></label>
          <label class="dg-prov"><input type="radio" name="dg-vision" value="api"><span><b>Cloud model</b><i>a vision-capable endpoint you configured</i></span></label>
        </div>
        <select class="dg-model" id="dgModel-vision" style="display:none"></select>
        <div class="dg-row"><button class="exp-btn dg-test" data-role="vision">Test / Re-check</button><span class="dg-probe" id="dgProbe-vision"></span></div>
      </div>

      <div class="acct-actions"><span class="acct-msg" id="dgMsg"></span><button class="primary sm" id="dgSave">Save</button></div>
    </div>`,
  voice: `
    <div class="drawer-section">
      <h3>Voice <span class="tool-count" id="vcAvail"></span></h3>
      <p class="sub">The speak-out engine and the wake word for hands-free conversation (the 🎙 in the composer). Speech-in always uses faster-whisper. Changes apply on the next utterance / next time you start Converse — no restart.</p>
      <label class="field-label">Speech engine</label>
      <div class="dg-providers">
        <label class="dg-prov"><input type="radio" name="vc-engine" value="auto"><span><b>Auto</b><i>Kokoro if installed, else Piper</i></span></label>
        <label class="dg-prov"><input type="radio" name="vc-engine" value="kokoro"><span><b>Kokoro</b><i>natural neural voice · local · CPU</i></span></label>
        <label class="dg-prov"><input type="radio" name="vc-engine" value="piper"><span><b>Piper</b><i>lightweight · downloadable voices</i></span></label>
      </div>
      <div id="vcKokoroBlock">
        <label class="field-label">Voice <span class="lbl-sub">Kokoro voices · af_/am_ = US f/m · bf_/bm_ = UK</span></label>
        <select id="vcEngVoice" style="min-width:220px"></select>
        <label class="field-label">Speed <span class="lbl-sub" id="vcSpeedVal">1.0×</span></label>
        <input type="range" id="vcSpeed" class="vc-range" min="0.5" max="2" step="0.1" value="1">
      </div>
      <div id="vcPiperBlock" style="display:none">
        <label class="field-label">Piper voice <span class="lbl-sub" id="vcPiperCount"></span></label>
        <div class="dg-row"><select id="vcPiperVoice" style="min-width:220px"></select><button class="exp-btn" id="vcPiperBrowse">Browse &amp; download…</button></div>
        <div id="vcPiperCatalog" class="vc-catalog" style="display:none">
          <div class="dg-row"><select id="vcPiperLang" style="min-width:170px"></select><span class="lbl-sub">pick a language, then download a voice into assets/voice/</span></div>
          <div class="vc-cat-list" id="vcPiperList"><div class="empty-note">loading catalog…</div></div>
        </div>
      </div>
      <div class="dg-row" style="margin-top:10px"><button class="exp-btn" id="vcTestBtn">▶ Test voice</button><span class="dg-probe" id="vcTestMsg"></span></div>
      <label class="set-toggle" style="margin-top:14px"><input type="checkbox" id="vcWake"><span class="st-track"><span class="st-thumb"></span></span><span class="st-lbl">Require a wake word <span class="st-note">— in conversation mode, only act on speech that starts with the phrase below (otherwise every utterance is sent)</span></span></label>
      <label class="field-label">Wake word</label>
      <input id="vcWakeWord" placeholder="oceano" autocomplete="off" spellcheck="false">
      <div class="acct-actions"><span class="acct-msg" id="vcSetMsg"></span><button class="primary sm" id="vcSetSave">Save</button></div>
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
  const { body, reused } = createWindow({ id: "win-settings", title: "Settings", icon: "⚙", width: 660, height: 560, restoreKey: "settings" });
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
  $("#toolAllOn", body).onclick = () => toggleAllTools(true);
  $("#toolAllOff", body).onclick = () => toggleAllTools(false);
  $("#dgSave", body).onclick = saveDelegation;
  $$(".dg-test", body).forEach(b => b.onclick = () => testDelegation(b.dataset.role));
  $("#vcSetSave", body).onclick = saveVoiceSettings;
  $("#vcTestBtn", body).onclick = testVoiceSettings;
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
function loadSettingsAll() { loadProviders(); loadEndpoints(); loadTelegram(); loadServices(); loadTools(); loadDelegation(); loadMind(); loadAccount(); loadMemoryPolicy(); loadJobsSetting(); loadVoiceSettings(); }
async function loadMind() {
  let d; try { d = await api("/api/mind"); } catch { return; }
  const radios = $$('input[name="oc-mind"]'), note = $("#mindNote");
  const sel = radios.find(x => x.value === (d.mind || "local")); if (sel) sel.checked = true;
  const claudeR = radios.find(x => x.value === "claude");
  if (!d.claude_available) {
    if (claudeR) claudeR.disabled = true;
    if (note) { note.textContent = "Claude Code isn't detected on this box — install it (or set OCEANO_CLAUDE_BIN) to use Claude as the mind."; note.className = "dg-hint warn"; }
  } else if (note) {
    note.textContent = d.mind === "claude"
      ? "Claude is driving your chats — Oceano's memory + workspace, on your subscription (no API key, but it uses your Claude quota)."
      : "The local model drives your chats — fully offline. Switch to Claude for a sharper mind.";
    note.className = "dg-hint";
  }
  radios.forEach(x => x.onchange = async () => {
    if (!x.checked) return;
    try { const r = await api("/api/mind", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mind: x.value }) });
      toast("primary intelligence → " + (r.mind === "claude" ? "Claude" : "local model"), "info"); loadMind(); }
    catch { toast("couldn't change the mind", "err"); }
  });
}
async function loadVoiceSettings() {
  let d; try { d = await api("/api/voice/voices"); } catch { return; }
  const s = d.settings || {}, voices = d.voices || [];
  const avail = $("#vcAvail"); if (avail) avail.textContent = voices.length ? `${voices.length} Kokoro voices` : "Kokoro not installed";
  const er = $$('input[name="vc-engine"]').find(r => r.value === (s.engine || "auto")); if (er) er.checked = true;
  $$('input[name="vc-engine"]').forEach(r => r.onchange = vcSyncEngine);
  const sel = $("#vcEngVoice");
  if (sel) {
    if (voices.length) { sel.innerHTML = voices.map(v => `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`).join(""); sel.value = s.voice || sel.value; sel.disabled = false; }
    else { sel.innerHTML = `<option>—</option>`; sel.disabled = true; }
  }
  const sp = $("#vcSpeed"), spv = $("#vcSpeedVal");
  if (sp) { sp.value = s.speed || 1; if (spv) spv.textContent = (+sp.value).toFixed(1) + "×"; sp.oninput = () => { if (spv) spv.textContent = (+sp.value).toFixed(1) + "×"; }; }
  vcFillPiper(d.piper_installed || [], s.piper_voice);
  const browse = $("#vcPiperBrowse"); if (browse) browse.onclick = vcBrowsePiper;
  const wk = $("#vcWake"); if (wk) wk.checked = !!s.wake;
  const ww = $("#vcWakeWord"); if (ww) ww.value = s.wake_word || "oceano";
  vcSyncEngine();
}
function _voicePayload() {
  const eng = ($$('input[name="vc-engine"]').find(r => r.checked) || {}).value;
  const sel = $("#vcEngVoice"), sp = $("#vcSpeed"), wk = $("#vcWake"), ww = $("#vcWakeWord"), pv = $("#vcPiperVoice"), p = {};
  if (eng) p.engine = eng;
  if (sel && !sel.disabled && sel.value) p.voice = sel.value;
  if (sp) p.speed = +sp.value;
  if (pv && !pv.disabled && pv.value) p.piper_voice = pv.value;
  if (wk) p.wake = wk.checked;
  if (ww) p.wake_word = ww.value.trim() || "oceano";
  return p;
}
// show the Kokoro voice/speed block or the Piper voice block depending on the chosen engine
function vcSyncEngine() {
  const eng = ($$('input[name="vc-engine"]').find(r => r.checked) || {}).value || "auto";
  const kokoroOK = $("#vcEngVoice") && !$("#vcEngVoice").disabled;
  const usePiper = eng === "piper" || (eng === "auto" && !kokoroOK);
  const kb = $("#vcKokoroBlock"), pb = $("#vcPiperBlock");
  if (kb) kb.style.display = usePiper ? "none" : "";
  if (pb) pb.style.display = usePiper ? "" : "none";
}
function vcFillPiper(list, active) {
  const sel = $("#vcPiperVoice"), cnt = $("#vcPiperCount"); if (!sel) return;
  if (list && list.length) {
    sel.innerHTML = list.map(v => `<option value="${escapeHtml(v.file)}">${escapeHtml(v.name)}</option>`).join("");
    sel.value = active || (list.find(v => v.active) || {}).file || sel.value;
    sel.disabled = false; if (cnt) cnt.textContent = `${list.length} installed`;
  } else {
    sel.innerHTML = `<option>(none installed)</option>`; sel.disabled = true;
    if (cnt) cnt.textContent = "none installed — browse below";
  }
}
async function vcBrowsePiper() {
  const cat = $("#vcPiperCatalog"); if (!cat) return;
  const show = cat.style.display === "none";
  cat.style.display = show ? "" : "none";
  if (show && !cat.dataset.loaded) { cat.dataset.loaded = "1"; await loadPiperLangs(); }
}
async function loadPiperLangs() {
  const list = $("#vcPiperList");
  let d; try { d = await api("/api/voice/piper/languages"); }
  catch { if (list) list.innerHTML = `<div class="empty-note err">couldn't reach the Piper catalog (no internet?)</div>`; return; }
  const sel = $("#vcPiperLang"); if (!sel) return;
  sel.innerHTML = (d.languages || []).map(l => `<option value="${escapeHtml(l.code)}">${escapeHtml(l.name)} (${l.count})</option>`).join("");
  sel.onchange = () => loadPiperVoices(sel.value);
  if (sel.value) loadPiperVoices(sel.value);
}
async function loadPiperVoices(lang) {
  const list = $("#vcPiperList"); if (!list) return;
  list.innerHTML = `<div class="empty-note">loading…</div>`;
  let d; try { d = await api("/api/voice/piper/voices?lang=" + encodeURIComponent(lang)); }
  catch { list.innerHTML = `<div class="empty-note err">failed to load voices</div>`; return; }
  const voices = d.voices || [];
  if (!voices.length) { list.innerHTML = `<div class="empty-note">no voices for this language</div>`; return; }
  list.innerHTML = "";
  voices.forEach(v => {
    const row = document.createElement("div"); row.className = "vc-cat-row";
    row.innerHTML = `<div class="vcc-info"><span class="vcc-name">${escapeHtml(v.name)}</span><span class="vcc-meta">${escapeHtml(v.quality || "—")} · ${v.size_mb} MB${v.speakers > 1 ? " · " + v.speakers + " speakers" : ""}</span></div>`;
    const btn = document.createElement("button"); btn.className = "exp-btn";
    if (v.installed) { btn.textContent = "installed ✓"; btn.disabled = true; }
    else { btn.textContent = "Download"; btn.onclick = () => downloadPiper(v.key, btn); }
    row.appendChild(btn); list.appendChild(row);
  });
}
async function downloadPiper(key, btn) {
  btn.disabled = true; const old = btn.textContent; btn.textContent = "downloading…";
  let r; try { r = await (await fetch("/api/voice/piper/download", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ key }) })).json(); }
  catch { r = { ok: false, error: "network" }; }
  if (r && r.ok) {
    btn.textContent = "installed ✓";
    toast("voice downloaded — selected as the active Piper voice", "info");
    try { const d = await api("/api/voice/voices"); vcFillPiper(d.piper_installed || [], r.file); } catch {}
    saveVoiceSettings();   // persist the new active Piper voice immediately
  } else {
    btn.disabled = false; btn.textContent = old;
    toast("download failed: " + ((r && r.error) || "unknown"), "err");
  }
}
async function saveVoiceSettings() {
  const msg = $("#vcSetMsg"); if (msg) { msg.textContent = ""; msg.className = "acct-msg"; }
  try {
    const r = await fetch("/api/voice/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(_voicePayload()) });
    if (!r.ok) throw 0;
    const st = ((await r.json()).settings) || {};
    if (msg) { msg.textContent = "saved ✓"; msg.className = "acct-msg ok"; }
    if (_converse) { _converse.wake = !!st.wake; _converse.wakeWord = (st.wake_word || "oceano").toLowerCase(); }  // live-apply to an active conversation
    return st;
  } catch { if (msg) { msg.textContent = "save failed"; msg.className = "acct-msg err"; } }
}
// The name to greet in voice samples: the logged-in user (capitalized), or "" if Oceano doesn't
// really know it (empty / still the default 'admin' login). Cached after the first lookup.
let _meUser;
async function userGreetName() {
  if (_meUser === undefined) { try { _meUser = ((await api("/api/me")).user || "").trim(); } catch { _meUser = ""; } }
  return (_meUser && _meUser.toLowerCase() !== "admin") ? _meUser.charAt(0).toUpperCase() + _meUser.slice(1) : "";
}
async function testVoiceSettings() {
  const msg = $("#vcTestMsg"); if (msg) { msg.textContent = "synthesizing…"; msg.className = "dg-probe"; }
  await saveVoiceSettings();   // apply the current selection first so the sample uses it
  const name = await userGreetName();
  try {
    const r = await fetch("/api/voice/tts", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: `Hi${name ? " " + name : ""}, this is Oceano. How do I sound?` }) });
    if (!r.ok) throw 0;
    const a = new Audio(URL.createObjectURL(await r.blob())); a.play().catch(() => {});
    if (msg) msg.textContent = "";
  } catch { if (msg) { msg.textContent = "test failed"; msg.className = "dg-probe err"; } }
}
async function loadJobsSetting() {
  const t = $("#serializeToggle"), tc = $("#serializeChatToggle"); if (!t) return;
  let d; try { d = await api("/api/jobs"); } catch { return; }
  t.checked = !!d.serialize;
  if (tc) tc.checked = !!d.serialize_chat;
  t.onchange = () => _postJ("/api/jobs/serialize", { enabled: t.checked }).then(r => toast(r.serialize ? "Background jobs will queue" : "Background jobs run in parallel", "info")).catch(() => {});
  if (tc) tc.onchange = () => _postJ("/api/jobs/serialize", { chat: tc.checked }).then(r => toast(r.serialize_chat ? "Chat messages will queue" : "Chat messages run immediately", "info")).catch(() => {});
}

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
  let tools; try { tools = await api("/api/tools"); } catch { return; }
  const setCount = () => { const c = $("#toolCount"); if (c) c.textContent = `· ${$$(".tool-row input", box).filter(x => x.checked).length}/${tools.length} on`; };
  box.innerHTML = tools.map(t => {
    const params = (t.params || []).length
      ? t.params.map(p => `<span class="tp${p.required ? " req" : ""}" title="${escapeHtml((p.required ? "required · " : "optional · ") + p.type + (p.description ? " · " + p.description : ""))}">${escapeHtml(p.name)}<i>${escapeHtml(p.type)}</i></span>`).join("")
      : `<span class="tp-none">no inputs</span>`;
    return `<div class="tool-row${t.enabled ? "" : " off"}" data-tool="${escapeHtml(t.name)}">
        <div class="tr-main">
          <div class="th"><span class="tcat">${escapeHtml(t.category || "other")}</span><span class="tn">${escapeHtml(t.name)}</span></div>
          <div class="td">${escapeHtml(t.description || "")}</div><div class="tparams">${params}</div>
        </div>
        <label class="sw sm" title="enable / disable this tool"><input type="checkbox" ${t.enabled ? "checked" : ""}><span></span></label>
      </div>`;
  }).join("");
  setCount();
  $$(".tool-row", box).forEach(row => {
    $("input", row).onchange = async e => {
      row.classList.toggle("off", !e.target.checked); setCount();
      await fetch("/api/tools/toggle", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: row.dataset.tool, enabled: e.target.checked }) }).catch(() => {});
      loadChatMemoryTools();   // a globally-disabled memory tool must grey out in the chat section
    };
  });
  loadChatMemoryTools();
}
async function loadChatMemoryTools() {
  const box = $("#chatToolList"); if (!box) return;
  let d; try { d = await api("/api/tools/chat"); } catch { return; }
  box.innerHTML = d.tools.map(t => `
    <div class="ct-row${t.enabled ? "" : " goff"}" data-tool="${escapeHtml(t.name)}">
      <div class="ct-main"><span class="tn">${escapeHtml(t.name)}</span>
        <span class="ct-desc">${escapeHtml((t.description || "").split(".")[0].slice(0, 80))}</span>
        ${t.enabled ? "" : '<span class="ct-note">off globally ↓</span>'}</div>
      <label class="sw sm" title="${t.enabled ? "available in chat-only mode" : "enable it below first"}"><input type="checkbox" ${t.in_chat ? "checked" : ""} ${t.enabled ? "" : "disabled"}><span></span></label>
    </div>`).join("");
  $$(".ct-row", box).forEach(row => {
    const inp = $("input", row); if (!inp) return;
    inp.onchange = e => fetch("/api/tools/chat", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: row.dataset.tool, enabled: e.target.checked }) }).catch(() => {});
  });
}
async function toggleAllTools(on) {
  await fetch("/api/tools/toggle", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ all: on }) }).catch(() => {});
  loadTools();
}

/* ---------------- delegation (Claude Code readiness + per-role provider choice) ---------------- */
let _dgModels = [];
async function loadDelegation() {
  const box = $("#dgStatus"); if (!box) return;
  let d; try { d = await api("/api/delegate"); } catch { return; }
  dgRenderStatus(d);
  try { _dgModels = (await api("/api/models")).filter(m => !m.error); } catch { _dgModels = []; }
  ["default", "improve", "vision"].forEach(role => dgInitRole(role, d[role] || {}));
  const en = $("#dgEnabled");
  if (en) {
    en.checked = d.enabled !== false;
    en.onchange = async () => {
      try { await _postJ("/api/delegate/enabled", { enabled: en.checked }); } catch {}
      loadDelegation();                              // refresh (tool-sync + status)
    };
  }
  loadDefaultModel();
}
const _SEP = "";                               // model|base_url delimiter (never in an id/url)
async function loadDefaultModel() {
  const sel = $("#dgDefaultModel"); if (!sel) return;
  let cur; try { cur = await api("/api/default-model"); } catch { cur = {}; }
  const m0 = cur.model || "", b0 = cur.base_url || "";
  const opts = _dgModels.map(m => {
    const val = m.id + _SEP + (m.base_url || "");
    const on = (m.id === m0 && (m.base_url || "") === b0) ? " selected" : "";
    return `<option value="${escapeHtml(val)}"${on}>${escapeHtml(m.id)} — ${escapeHtml(m.endpoint)}</option>`;
  });
  // first option clears the override → config default model on the local endpoint
  sel.innerHTML = `<option value=""${m0 ? "" : " selected"}>Default · ${escapeHtml(cur.fallback || "config")} (local)</option>` + opts.join("");
  if (m0 && !_dgModels.some(m => m.id === m0 && (m.base_url || "") === b0)) {   // keep an unlisted current visible
    const o = document.createElement("option");
    o.value = m0 + _SEP + b0; o.textContent = m0 + " (current)"; o.selected = true;
    sel.insertBefore(o, sel.children[1] || null);
  }
  sel.onchange = async () => {
    const [model, base_url] = (sel.value || "").split(_SEP);
    const msg = $("#dgDefaultMsg"); if (msg) { msg.textContent = "saving…"; msg.className = "dg-probe"; }
    try {
      const r = await _postJ("/api/default-model", { model: model || "", base_url: base_url || "" });
      if (msg) { msg.textContent = "✓ now using " + (r.current || model || "default"); msg.className = "dg-probe ok"; }
    } catch { if (msg) { msg.textContent = "save failed"; msg.className = "dg-probe err"; } }
  };
}
function dgInitRole(role, cfg) {
  $$(`input[name="dg-${role}"]`).forEach(r => { r.checked = (r.value === cfg.provider); r.onchange = () => dgSyncRole(role); });
  const sel = $(`#dgModel-${role}`);
  if (sel) {
    sel.innerHTML = _dgModels.length
      ? _dgModels.map(m => `<option value="${escapeHtml(m.id)}" data-base="${escapeHtml(m.base_url)}"${(m.base_url === cfg.base_url && m.id === cfg.model) ? " selected" : ""}>${escapeHtml(m.id)} — ${escapeHtml(m.endpoint)}</option>`).join("")
      : `<option value="">no endpoints configured — add one under Endpoints</option>`;
  }
  dgSyncRole(role);
}
function dgSyncRole(role) {                                    // show the model picker only for "Cloud model"
  const prov = ($(`input[name="dg-${role}"]:checked`) || {}).value;
  const sel = $(`#dgModel-${role}`); if (sel) sel.style.display = prov === "api" ? "" : "none";
}
function dgRenderStatus(d) {
  const box = $("#dgStatus"); if (!box) return;
  const c = d.claude || {};
  box.innerHTML = (c.installed
    ? `<div class="dg-line ok">✓ Claude Code installed · <code>${escapeHtml(c.version || "")}</code></div>`
    : `<div class="dg-line err">✗ Claude Code not found</div><div class="dg-hint">Install — <code>npm i -g @anthropic-ai/claude-code</code> (or set <code>OCEANO_CLAUDE_BIN</code>), then restart Oceano.</div>`)
    + `<div class="dg-hint">Authentication is confirmed only when you press <b>Test / Re-check</b> in a section below.</div>`;
}
const DG_LABELS = { improve: "self-improvement", vision: "image-recognition" };
function dgRolePayload(role) {
  const prov = ($(`input[name="dg-${role}"]:checked`) || {}).value || (role === "default" ? "claude_cli" : "inherit");
  let base_url = "", model = "";
  if (prov === "api") {
    const opt = $(`#dgModel-${role}`) && $(`#dgModel-${role}`).selectedOptions[0];
    if (!opt || !opt.value) return { error: "pick a model for the " + (DG_LABELS[role] || "general") + " delegate" };
    model = opt.value; base_url = opt.dataset.base || "";
  }
  return { role, provider: prov, base_url, model };
}
async function saveDelegation() {
  const msg = $("#dgMsg");
  const payloads = ["default", "improve", "vision"].map(dgRolePayload);
  const bad = payloads.find(p => p.error);
  if (bad) { if (msg) { msg.textContent = bad.error; msg.className = "acct-msg err"; } return; }
  try {
    let last;
    for (const p of payloads) last = await _postJ("/api/delegate", p);
    dgRenderStatus(last);
    if (msg) { msg.textContent = "saved ✓"; msg.className = "acct-msg ok"; }
  } catch { if (msg) { msg.textContent = "save failed"; msg.className = "acct-msg err"; } }
}
async function testDelegation(role) {
  const box = $(`#dgProbe-${role}`);
  if (box) { box.innerHTML = "testing…"; box.className = "dg-probe"; }
  let r; try { r = await _postJ("/api/delegate/test", { role }); } catch { if (box) { box.innerHTML = "test failed"; box.className = "dg-probe err"; } return; }
  if (!box) return;
  if (r.ok) {
    box.className = "dg-probe ok";
    box.innerHTML = `✓ ${escapeHtml(r.provider === "api" ? "cloud model responded" : "Claude Code authenticated")}`;
  } else {
    box.className = "dg-probe err";
    const fix = r.provider === "claude_cli"
      ? ` — run <code>claude login</code> on the host (as the Oceano user), then re-check`
      : ` — check the endpoint/model/key under Endpoints`;
    box.innerHTML = `✗ ${escapeHtml(r.detail || "not ready")}${fix}`;
  }
}

/* ---------------- account / auth ---------------- */
async function loadAccount() {
  try {
    const me = await api("/api/me");
    const who = $("#acctWho"), u = $("#acctUser");
    if (who) who.textContent = me.user || "—";
    if (u) u.value = me.user || "";
  } catch {}
  load2fa();
}
async function load2fa() {
  const box = $("#twofaBody"); if (!box) return;
  let s; try { s = await api("/api/2fa/status"); } catch { return; }
  if (s.enabled) {
    box.innerHTML = `<div class="acct-row">🔒 On — a code from your authenticator app is required at login.</div>
      <label class="field-label">Current password <span class="lbl-sub">required to turn it off</span></label>
      <input id="twofaPw" type="password" autocomplete="current-password" placeholder="current password">
      <div class="acct-actions"><span class="acct-msg" id="twofaMsg"></span><button class="ghost-btn sm danger" id="twofaOff">Turn off 2FA</button></div>`;
    $("#twofaOff", box).onclick = twofaDisable;
  } else {
    box.innerHTML = `<div class="acct-row">Add a second factor: scan a QR with an authenticator app (Google Authenticator, Authy, …), then a 6-digit code is required at login.</div>
      <div class="acct-actions"><span class="acct-msg" id="twofaMsg"></span><button class="primary sm" id="twofaSetup">Set up 2FA</button></div>
      <div id="twofaSetupBox"></div>`;
    $("#twofaSetup", box).onclick = twofaSetup;
  }
}
async function twofaSetup() {
  const wrap = $("#twofaSetupBox"); if (!wrap) return;
  wrap.innerHTML = `<div class="acct-row">generating…</div>`;
  let d; try { d = await (await fetch("/api/2fa/setup", { method: "POST" })).json(); } catch { wrap.innerHTML = `<div class="acct-row">setup failed</div>`; return; }
  const qr = (window.DOMPurify && d.svg) ? DOMPurify.sanitize(d.svg, { USE_PROFILES: { svg: true, svgFilters: true } }) : "";
  wrap.innerHTML = `
    <div class="twofa-qr">${qr}</div>
    <div class="acct-row">Scan the QR, or type this key into your app:<br><code class="twofa-secret">${escapeHtml(d.secret || "")}</code></div>
    <label class="field-label">Confirm with your password and a 6-digit code</label>
    <input id="twofaPwc" type="password" autocomplete="current-password" placeholder="current password">
    <input id="twofaCode" inputmode="numeric" maxlength="7" placeholder="123456" autocomplete="one-time-code">
    <div class="acct-actions"><span class="acct-msg" id="twofaMsg2"></span><button class="primary sm" id="twofaEnable">Verify & turn on</button></div>
    <div class="acct-row lbl-sub">Lost your device later? On the host, set <code>totp_enabled</code> to false in <code>data/web.json</code> (or delete the <code>totp_*</code> keys) and restart.</div>`;
  $("#twofaEnable", wrap).onclick = async () => {
    const msg = $("#twofaMsg2"), code = $("#twofaCode").value.trim(), pw = $("#twofaPwc").value;
    if (!pw) { msg.textContent = "enter your current password"; msg.className = "acct-msg err"; return; }
    const r = await fetch("/api/2fa/enable", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code, current_password: pw }) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { msg.textContent = j.detail || "verification failed"; msg.className = "acct-msg err"; return; }
    msg.textContent = "2FA is on ✓"; msg.className = "acct-msg ok";
    setTimeout(load2fa, 800);
  };
}
async function twofaDisable() {
  const msg = $("#twofaMsg"), pw = ($("#twofaPw") || {}).value || "";
  if (!pw) { msg.textContent = "enter your current password"; msg.className = "acct-msg err"; return; }
  const r = await fetch("/api/2fa/disable", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ current_password: pw }) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { msg.textContent = j.detail || "could not disable"; msg.className = "acct-msg err"; return; }
  msg.textContent = "2FA turned off"; msg.className = "acct-msg ok";
  setTimeout(load2fa, 600);
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
  _meUser = undefined;                                     // re-fetch the greeting name (it may have changed)
}
async function logout() {
  await fetch("/api/logout", { method: "POST" }).catch(() => {});
  location.reload();
}

/* ================= FLOATING WINDOWS ================= */
let _winZ = 60;
// --- window-session persistence: remember which app windows are open, restore them on reload ---
function _winOpen() { try { return JSON.parse(localStorage.getItem("oceano.openwins") || "[]"); } catch { return []; } }
function _winSet(a) { try { localStorage.setItem("oceano.openwins", JSON.stringify(a)); } catch {} }
function _trackWin(id, key, arg) { const a = _winOpen().filter(w => w.id !== id); a.push({ id, key, arg }); _winSet(a); }
function _untrackWin(id) { _winSet(_winOpen().filter(w => w.id !== id)); }
function _setWinMin(id, on) { const a = _winOpen(), w = a.find(x => x.id === id); if (w && !!w.min !== on) { w.min = on; _winSet(a); } }
function restoreWindows() {
  const RESTORERS = { settings: openSettings, live: openLiveView, explorer: openExplorer,
                      brain: openBrain, workflows: openWorkflows, preview: openPreview,
                      file: openFileWindow, cal: openCalendar };
  _winOpen().forEach(w => {
    const fn = RESTORERS[w.key];
    if (!fn) return;
    try {
      fn(w.arg);                                                                   // reopen the window
      if (w.min) { const el = document.getElementById(w.id); if (el) minimizeWindow(el); }   // …but keep it docked if it was minimized
    } catch {}
  });
}
function createWindow(opts) {
  if (opts.restoreKey) _trackWin(opts.id, opts.restoreKey, opts.restoreArg);   // remember it's open
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
    if (opts.id) _untrackWin(opts.id);                              // forget it (don't restore next load)
    win.remove();
  };
  _dragify($(".win-bar", win), win, "move");
  _dragify($(".win-rz", win), win, "resize");
  return { body: $(".win-body", win), reused: false };
}
function minimizeWindow(win) {
  win.style.display = "none";
  _setWinMin(win.id, true);                                  // remember it's minimized across reloads
  const chip = document.createElement("button");
  chip.className = "dock-chip";
  chip.innerHTML = `<span class="dc-ic">${escapeHtml(win.dataset.icon || "▢")}</span><span class="dc-t">${escapeHtml(win.dataset.title || "Window")}</span>`;
  chip.onclick = () => { win.style.display = "flex"; win.style.zIndex = ++_winZ; chip.remove(); win._chip = null; _setWinMin(win.id, false); };
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
    top: [R.left, R.top, R.w, hh], bottom: [R.left, R.top + hh, R.w, hh],
    tl: [R.left, R.top, hw, hh], tr: [R.left + hw, R.top, hw, hh],
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
    restoreKey: "live", onClose: () => { if (_liveES) { _liveES.close(); _liveES = null; } } });
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
    const win = document.getElementById("win-live");
    if (win && win.style.display === "none") return;       // minimized → don't decode frames into a hidden view
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
    restoreKey: "explorer",
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
          <button class="exp-btn" id="expUpFilesBtn" title="upload files here">📤</button>
          <button class="exp-btn" id="expUpDirBtn" title="upload a folder here">📁↑</button>
          <button class="exp-btn" id="expRefresh" title="refresh">↻</button>
          <span class="exp-upmsg" id="expUpMsg"></span>
        </div>
        <input type="file" id="expUpFiles" multiple style="display:none">
        <input type="file" id="expUpDir" webkitdirectory directory multiple style="display:none">
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
  const upFiles = $("#expUpFiles", body), upDir = $("#expUpDir", body);
  $("#expUpFilesBtn", body).onclick = () => upFiles.click();
  $("#expUpDirBtn", body).onclick = () => upDir.click();
  upFiles.onchange = () => { expUpload([...upFiles.files].map(f => ({ file: f, rel: f.name })), body); upFiles.value = ""; };
  upDir.onchange = () => { expUpload([...upDir.files].map(f => ({ file: f, rel: f.webkitRelativePath || f.name })), body); upDir.value = ""; };
  const list = $("#expList", body);
  ["dragover", "dragenter"].forEach(ev => list.addEventListener(ev, e => { e.preventDefault(); list.classList.add("exp-drop"); }));
  ["dragleave", "dragend", "drop"].forEach(ev => list.addEventListener(ev, () => list.classList.remove("exp-drop")));
  list.addEventListener("drop", async e => {
    e.preventDefault();
    const items = e.dataTransfer.items, picked = [];
    if (items && items.length && items[0].webkitGetAsEntry) {
      const entries = [...items].map(it => it.webkitGetAsEntry && it.webkitGetAsEntry()).filter(Boolean);
      for (const ent of entries) picked.push(...await _walkEntry(ent, ""));
    } else {
      [...(e.dataTransfer.files || [])].forEach(f => picked.push({ file: f, rel: f.name }));
    }
    if (picked.length) expUpload(picked, body);
  });
  $("#expList", body).addEventListener("contextmenu", e => {
    if (e.target.closest(".exp-row")) return;
    e.preventDefault();
    showCtx(e.clientX, e.clientY, [{ label: "New folder", action: expNewFolder }, { label: "New file", action: expNewFile }, { label: "Refresh", action: () => expLoad(_expCwd) }]);
  });
  _wireExpDivider(body);
  expLoad("");
  _edRestore();                                    // reopen the tabs from last time
}
function _walkEntry(entry, prefix) {                 // recurse a dropped file/folder into {file, rel}
  return new Promise(resolve => {
    if (entry.isFile) entry.file(f => resolve([{ file: f, rel: prefix + entry.name }]), () => resolve([]));
    else if (entry.isDirectory) {
      const rd = entry.createReader(), all = [];
      const read = () => rd.readEntries(async ents => {
        if (!ents.length) { const nested = await Promise.all(all.map(en => _walkEntry(en, prefix + entry.name + "/"))); resolve(nested.flat()); }
        else { all.push(...ents); read(); }            // readEntries returns in batches — keep reading
      }, () => resolve([]));
      read();
    } else resolve([]);
  });
}
async function expUpload(items, body) {
  if (!items || !items.length) return;
  const msg = body && $("#expUpMsg", body); if (msg) msg.textContent = `uploading ${items.length}…`;
  const fd = new FormData();
  fd.append("dir", _expCwd || "");
  items.forEach(({ file, rel }) => { fd.append("files", file, file.name); fd.append("paths", rel || file.name); });
  let r = null; try { r = await fetch("/api/upload-to", { method: "POST", body: fd }).then(x => x.json()); } catch {}
  expLoad(_expCwd);
  if (msg) {
    msg.textContent = r ? `✓ ${r.saved} uploaded${r.skipped && r.skipped.length ? ` · ${r.skipped.length} skipped` : ""}` : "upload failed";
    setTimeout(() => { if (msg) msg.textContent = ""; }, 4000);
  }
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
        : [...(isPreviewable(e.name) ? [{ label: previewLabel(e.name), action: () => openPreview(e.path) }] : []),
           { label: "Open here", action: () => expOpenFile(e.path) },
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
      ${isPreviewable(path) ? `<button class="ed-btn fw-preview" title="Preview in a sandboxed window">${previewLabel(path)}</button>` : ""}
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
  { const pv = $(".fw-preview", container); if (pv) pv.onclick = () => openPreview(curPath); }   // curPath tracks Save as…
  setTimeout(() => cm.refresh(), 30);             // CM mis-measures in a freshly-shown box
  if (window.ResizeObserver) new ResizeObserver(() => cm.refresh()).observe($(".fw-cm", container));  // re-layout on resize
  return cm;
}
async function openFileWindow(path) {                          // standalone pop-out editor window
  const isImg = /\.(png|jpe?g|gif|webp|svg|bmp|ico|avif)$/i.test(path);
  const name = path.split("/").pop();
  const { body, reused } = createWindow({ id: "fw-" + path.replace(/[^a-z0-9]/gi, "_"), title: name, icon: isImg ? "▦" : "ℜ", width: 640, height: 520, restoreKey: "file", restoreArg: path });
  if (reused) return;
  await _mountEditor(body, path, { onSaved: () => { if (typeof _expCwd === "string") expLoad(_expCwd); } });
}

/* ---------- Preview: render a workspace web app in a sandboxed iframe (device simulator + live reload) ---------- */
const isWebPage = p => /\.html?$/i.test(p || "");
// previewable artifacts: finished web pages + source types the backend renders in the sandbox
// (markdown docs, mermaid diagrams, Chart.js specs, slide decks).
function isPreviewable(p) {
  const n = (p || "").toLowerCase();
  return /\.(html?|md|markdown|mmd|mermaid|slides)$/.test(n) || n.endsWith(".chart.json");
}
function previewLabel(p) {
  const n = (p || "").toLowerCase();
  if (n.endsWith(".slides.md") || n.endsWith(".slides")) return "▶ Slides";
  if (n.endsWith(".chart.json")) return "▶ Chart";
  if (n.endsWith(".mmd") || n.endsWith(".mermaid")) return "▶ Diagram";
  if (n.endsWith(".md") || n.endsWith(".markdown")) return "▶ View";
  return "▶ Preview";
}
const _previewURL = (token, p) => "/preview/" + token + "/" + p.split("/").map(encodeURIComponent).join("/");
let _previewTimer = null;
function openPreview(path) {
  const name = path.split("/").pop() || path, id = "win-preview";
  const { body } = createWindow({ id, title: "Preview — " + name, icon: "▶", width: 920, height: 660,
    restoreKey: "preview", restoreArg: path,
    onClose: () => { if (_previewTimer) { clearInterval(_previewTimer); _previewTimer = null; } } });
  body.classList.add("pv-win");
  body.innerHTML = `
    <div class="pv-bar">
      <div class="pv-devices">
        <button class="pv-dev on" data-w="100%">Desktop</button>
        <button class="pv-dev" data-w="820px">Tablet</button>
        <button class="pv-dev" data-w="390px">Phone</button>
      </div>
      <span class="pv-path" title="${escapeHtml(path)}">${escapeHtml(path)}</span>
      <span class="fe-spacer"></span>
      <label class="pv-auto" title="reload when the files change"><input type="checkbox" checked> auto-reload</label>
      <button class="ed-btn pv-reload" title="Reload now">↻</button>
    </div>
    <div class="pv-stage"><div class="pv-frame" style="width:100%"><iframe class="pv-iframe"
        sandbox="allow-scripts allow-forms allow-modals allow-popups allow-pointer-lock"></iframe></div></div>`;
  const iframe = $(".pv-iframe", body), frame = $(".pv-frame", body), auto = $(".pv-auto input", body);
  // Mint a fresh capability token each load: the iframe is sandboxed without same-origin, so it
  // can't send the session cookie — it authenticates to /preview/ by the token in the URL instead.
  const load = async () => {
    let t; try { t = (await api("/api/preview-token?path=" + encodeURIComponent(path))).token; } catch { return; }
    iframe.src = _previewURL(t, path) + "?t=" + Date.now();   // new URL each time → forces reload
  };
  load();
  $(".pv-reload", body).onclick = load;
  $$(".pv-dev", body).forEach(b => b.onclick = () => {
    $$(".pv-dev", body).forEach(x => x.classList.toggle("on", x === b));
    frame.style.width = b.dataset.w; frame.classList.toggle("device", b.dataset.w !== "100%");
  });
  if (_previewTimer) clearInterval(_previewTimer);
  let last = 0;
  const poll = async () => {
    if (!auto.checked) return;
    let d; try { d = await api("/api/preview-mtime?path=" + encodeURIComponent(path)); } catch { return; }
    if (last && d.mtime > last) load();
    last = d.mtime;
  };
  poll(); _previewTimer = setInterval(poll, 1500);
}
// artifact-style chip: when the agent writes a renderable file (.html/.md/.mmd/.chart.json/
// .slides), offer to open it in the Preview window straight from that tool card.
function maybePreviewChip(card, name, argsJson) {
  if (!card || name !== "write_file") return;
  let path; try { path = JSON.parse(argsJson).path; } catch { return; }
  if (!isPreviewable(path)) return;
  const th = $(".th", card); if (!th || $(".tool-preview", th)) return;
  const b = document.createElement("button");
  b.className = "tool-preview"; b.textContent = previewLabel(path);
  b.onclick = e => { e.stopPropagation(); openPreview(path); };
  th.appendChild(b);
}

/* ---------- Brain window (memory + skills) ---------- */
const BRAIN_TABS = [["mem", "✶", "Memory"], ["kn", "◈", "Knowledge"], ["skills", "⚒", "Skills"], ["rivers", "🌊", "Rivers"], ["evals", "⚖", "Evals"]];
function openBrain(tab) {
  const { body, reused } = createWindow({ id: "win-brain", title: "Brain", icon: "✶", width: 720, height: 580,
    restoreKey: "brain", restoreArg: tab,
    onClose: () => { if (_riverTimer) { clearInterval(_riverTimer); _riverTimer = null; } if (_skillEvalTimer) { clearTimeout(_skillEvalTimer); _skillEvalTimer = null; } if (_evalTimer) { clearTimeout(_evalTimer); _evalTimer = null; } if (_brainEvalDotTimer) { clearInterval(_brainEvalDotTimer); _brainEvalDotTimer = null; } } });
  if (!reused) {
    body.classList.add("set-win");
    body.innerHTML = `
      <div class="set-layout">
        <div class="set-tabs">${BRAIN_TABS.map((t, i) =>
          `<button class="set-tab${i === 0 ? " active" : ""}" data-tab="${t[0]}"><span class="sti">${t[1]}</span>${t[2]}${t[0] === "evals" ? '<span class="brain-run-dot" id="brainEvalDot" style="display:none" title="eval running"></span>' : ""}</button>`).join("")}</div>
        <div class="set-pane brain-pane" id="brainBody"></div>
      </div>`;
    $$(".set-tab", body).forEach(t => t.onclick = () => {
      $$(".set-tab", body).forEach(x => x.classList.toggle("active", x === t));
      brainTab(t.dataset.tab);
    });
  }
  startBrainEvalDot();   // keep the Evals tab's "running" dot live regardless of active tab
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
    c.innerHTML = `<div class="mem-add"><input id="bMemText" placeholder="Teach Oceano a durable fact…"><input id="bMemTags" class="mem-tags" placeholder="tags"><button class="primary sm" id="bMemAdd">Remember</button><button class="ghost-btn sm" id="bMemGraph" title="Explore the memory store as a graph">❄ Graph</button></div><div class="mem-list" id="bMemList"></div>`;
    const add = async () => { const t = $("#bMemText").value.trim(); if (!t) return; await fetch("/api/memories", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: t, tags: $("#bMemTags").value.trim() }) }); $("#bMemText").value = ""; $("#bMemTags").value = ""; loadBrainMem(); };
    $("#bMemAdd").onclick = add;
    $("#bMemGraph").onclick = openMemoryGraph;
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
          <button class="sk-tab" data-f="staged">Staged<span class="sk-cnt" id="skCntStg"></span></button>
          <button class="sk-tab" data-f="learning">Learning<span class="sk-cnt" id="skCntLrn"></span></button>
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
  const staged = skillsCache.filter(s => s.status === "staged");
  const learning = skillsCache.filter(s => s.status === "learning");
  const cp = $("#skCntPub"), cs = $("#skCntStg"), cl = $("#skCntLrn");
  if (cp) cp.textContent = pub.length;
  if (cs) cs.textContent = staged.length;
  if (cl) cl.textContent = learning.length;
  const list = _skillFilter === "staged" ? staged : _skillFilter === "learning" ? learning : pub;
  body.innerHTML = "";
  if (!list.length) {
    const notes = {
      published: "No published skills yet — create one, or let Oceano learn its own as it works.",
      staged: "Nothing staged. Skills that pass the independent review wait here — approved and ready. Publish them whenever you like.",
      learning: "Nothing learning. When the agent teaches itself something (learn_skill), it lands here for independent validation before going live.",
    };
    body.innerHTML = `<div class="empty-note">${notes[_skillFilter] || notes.published}</div>`;
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
let _riverTimer = null, _riverHw = null, _riverHwTimer = null;
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
function riverPaintHw() {
  const el = $("#riverHw"); if (!el || !_riverHw) return;
  const tot = _riverHw.vram_total ? (_riverHw.vram_total / GB).toFixed(1) + " GB" : "—";
  const free = _riverHw.vram_free != null ? (_riverHw.vram_free / GB).toFixed(1) + " GB free" : "";
  const used = (_riverHw.vram_total && _riverHw.vram_free != null)
    ? Math.max(0, _riverHw.vram_total - _riverHw.vram_free) : null;
  const pct = (used != null && _riverHw.vram_total) ? Math.round(100 * used / _riverHw.vram_total) : 0;
  el.innerHTML = `<span class="svc-dot ${_riverHw.gpu ? "on" : "off"}"></span>` +
    `<span>Backend <code>${escapeHtml(_riverHw.backend)}</code> · <code>${escapeHtml(_riverHw.gpu || "CPU only")}</code></span>` +
    `<span class="vram">VRAM ${tot}${free ? " · " + free : ""}` +
    (used != null ? `<span class="vram-bar ${pct >= 90 ? "hot" : ""}" title="${pct}% used"><i style="width:${pct}%"></i></span>` : "") +
    `</span>`;
}
async function riverLoadHw() {
  try { _riverHw = await api("/api/rivers/hw"); } catch { return; }
  riverPaintHw();
  riverMonitor();                          // start the live VRAM readout
}
function riverMonitor() {
  // Poll VRAM every 3s while the Rivers panel is on-screen; self-stops when its element is gone
  // (tab switched / Brain closed), so no leaked timer — same lifetime idea as the download poll.
  if (_riverHwTimer) return;
  _riverHwTimer = setInterval(async () => {
    if (!document.getElementById("riverHw")) { clearInterval(_riverHwTimer); _riverHwTimer = null; return; }
    try { _riverHw = await api("/api/rivers/hw"); riverPaintHw(); } catch {}
  }, 3000);
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
    (m.served
      ? `<span class="served">▶ ${escapeHtml(m.served)}</span>` +
        `<button class="btn-mini cedit" data-n="${escapeHtml(m.served)}">Edit</button>` +
        `<button class="btn-mini danger cunserve" data-n="${escapeHtml(m.served)}">Unserve</button>`
      : `<button class="btn-mini cserve" data-f="${escapeHtml(m.filename)}" data-sz="${m.size}">Serve</button>` +
        `<button class="btn-mini danger cdelete" data-f="${escapeHtml(m.filename)}" data-sz="${m.size}">Delete</button>`) +
    `</div>`).join("");
  $$(".cserve", box).forEach(b => b.onclick = () => riverServeDialog(b.dataset.f, +b.dataset.sz));
  $$(".cedit", box).forEach(b => b.onclick = () => riverEditDialog(b.dataset.n));
  $$(".cunserve", box).forEach(b => b.onclick = () => riverUnserve(b.dataset.n));
  $$(".cdelete", box).forEach(b => b.onclick = () => riverDelete(b.dataset.f, +b.dataset.sz));
}
async function riverDelete(filename, size) {
  if (!confirm(`Delete "${filename}" from disk? This frees ${fmtSize(size)} and cannot be undone — you'd have to re-download it.`)) return;
  let r; try { r = await api("/api/rivers/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ filename }) }); } catch { return; }
  const note = $("#riverNote");
  if (note) { note.textContent = r.ok ? `🗑 deleted "${filename}" — freed ${fmtSize(r.freed)}` : (r.error || "delete failed"); note.className = "river-note " + (r.ok ? "ok" : "err"); }
  riverLoadInstalled();
}
async function riverUnserve(name) {
  if (!confirm(`Stop serving "${name}"? The model file stays on disk — only its llama-swap entry is removed.`)) return;
  let r; try { r = await api("/api/rivers/unserve", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) }); } catch { return; }
  const note = $("#riverNote");
  if (note) { note.textContent = r.ok ? `✓ unserved "${name}"` : (r.error || "unserve failed"); note.className = "river-note " + (r.ok ? "ok" : "err"); }
  riverLoadInstalled(); loadModels();
}
const _KV_OPTS = `<option value="f16">f16 (fastest)</option><option value="q8_0">q8_0</option><option value="q4_0">q4_0 (smallest)</option>`;

function riverServeDialog(filename, size) {
  const fitc = fitClient(size);
  const defName = filename.replace(/\.gguf$/i, "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40);
  _riverDialog({ mode: "serve", filename, name: defName,
    vals: { ngl: fitc.ngl, ctx: 8192, kv: "f16", kv_v: "f16", ttl: 600, fa: true,
            threads: "", batch: "", ubatch: "", n_cpu_moe: "", parallel: 1, extra: "" } });
}
async function riverEditDialog(name) {
  let d; try { d = await api("/api/rivers/served"); } catch { return; }
  const m = (d.models || []).find(x => x.name === name);
  if (!m) { const n = $("#riverNote"); if (n) { n.textContent = `couldn't load "${name}"`; n.className = "river-note err"; } return; }
  _riverDialog({ mode: "edit", filename: m.filename, name: m.name,
    vals: { ngl: m.ngl, ctx: m.ctx, kv: m.kv, kv_v: m.kv_v, ttl: m.ttl, fa: m.fa,
            threads: m.threads || "", batch: m.batch || "", ubatch: m.ubatch || "",
            n_cpu_moe: m.n_cpu_moe || "", parallel: m.parallel || 1, extra: m.extra || "" } });
}
function riverPaintEst(el, e) {
  const g = x => (x / GB).toFixed(1);
  const kv = e.kv_bytes != null ? `${g(e.kv_bytes)} GB` : "n/a";
  const freeTxt = e.vram_free != null ? `${g(e.vram_free)} GB free`
    : (e.vram_total ? `${g(e.vram_total)} GB total` : "no GPU detected");
  let cls = "ok", verdict = "fits in VRAM";
  if (e.vram_total != null) {
    if (e.vram_free != null && e.total > e.vram_free) { cls = "warn"; verdict = "needs a model swap to fit"; }
    if (e.total > e.vram_total) { cls = "err"; verdict = "exceeds total VRAM"; }
  } else { cls = ""; verdict = ""; }
  el.className = "river-est " + cls;
  el.innerHTML = `<b>≈ ${g(e.total)} GB</b> <span class="est-bd">weights ${g(e.weights_gpu)} + KV ${kv} + overhead</span>` +
    `<span class="est-free">${escapeHtml(freeTxt)}${verdict ? " · " + verdict : ""}</span>` +
    (e.note ? `<span class="est-note">${e.approx ? "⚠ " : "ⓘ "}${escapeHtml(e.note)}</span>` : "");
}
function _riverDialog({ mode, filename, name, vals }) {
  const editing = mode === "edit";
  const { body } = createWindow({ id: "win-serve", title: (editing ? "Edit served model" : "Serve model") + " — parameters", icon: "🌊", width: 500, height: 640 });
  body.classList.add("set-win");
  body.innerHTML = `<div class="drawer-section">
    <h3>${editing ? "Edit" : "Serve"} <code>${escapeHtml(filename)}</code></h3>
    <label class="field-label">Name <span class="lbl-sub">${editing ? "rename isn't supported — unserve & serve again to change it" : "how it shows in the model picker"}</span></label>
    <input id="svName" value="${escapeHtml(name)}" autocomplete="off" ${editing ? "readonly" : ""}>
    <button class="exp-btn rec-btn" id="svRec" type="button">✨ Recommend settings for my hardware</button>
    <div class="rec-why" id="svRecWhy" style="display:none"></div>
    <div class="serve-grid">
      <div><label class="field-label">Context (tokens)</label><input id="svCtx" type="number" value="${+vals.ctx}" min="256" step="1024">
        <div class="preset-chips" id="svCtxChips">${[2048, 4096, 8192, 16384, 32768, 65536, 131072].map(c => `<button type="button" class="chip" data-v="${c}">${c / 1024}k</button>`).join("")}</div></div>
      <div><label class="field-label">GPU layers (ngl)</label><input id="svNgl" type="number" value="${+vals.ngl}" min="0" max="999">
        <div class="slider-row"><span class="sl-min">CPU</span><input type="range" id="svNglSlider" min="0" max="99" value="${Math.min(99, +vals.ngl)}"><span class="sl-max">all GPU</span></div></div>
      <div><label class="field-label">K cache</label><select id="svKv">${_KV_OPTS}</select></div>
      <div><label class="field-label">V cache</label><select id="svKvV">${_KV_OPTS}</select></div>
      <div><label class="field-label">TTL (sec resident)</label><input id="svTtl" type="number" value="${+vals.ttl}" min="0">
        <div class="preset-chips" id="svTtlChips">${[["300", "5m"], ["1800", "30m"], ["3600", "1h"], ["0", "∞"]].map(([v, l]) => `<button type="button" class="chip" data-v="${v}">${l}</button>`).join("")}</div></div>
      <div><label class="field-label">Parallel slots</label><input id="svPar" type="number" value="${+vals.parallel}" min="1" max="64"></div>
    </div>
    <label class="serve-fa"><input type="checkbox" id="svFa" ${vals.fa ? "checked" : ""}> Flash attention (<code>-fa</code>)</label>
    <div class="river-est" id="svEst">estimating…</div>
    <details class="serve-adv"${vals.extra || vals.threads || vals.batch || vals.n_cpu_moe ? " open" : ""}><summary>Advanced</summary>
      <div class="serve-grid">
        <div><label class="field-label">Threads (-t)</label><input id="svThreads" type="number" value="${vals.threads}" min="0" placeholder="auto"></div>
        <div><label class="field-label">MoE→CPU (--n-cpu-moe)</label><input id="svMoe" type="number" value="${vals.n_cpu_moe}" min="0" placeholder="off"></div>
        <div><label class="field-label">Batch (-b)</label><input id="svBatch" type="number" value="${vals.batch}" min="0" placeholder="default"></div>
        <div><label class="field-label">U-batch (-ub)</label><input id="svUbatch" type="number" value="${vals.ubatch}" min="0" placeholder="default"></div>
      </div>
      <label class="field-label">Extra flags</label>
      <input id="svExtra" value="${escapeHtml(vals.extra)}" autocomplete="off" placeholder="e.g. --rope-scaling yarn --rope-freq-scale 0.5">
      <div class="serve-hint">Appended verbatim. Allowed: letters, digits, space and <code>. _ : = + / -</code>.</div>
    </details>
    <div class="serve-hint">Bigger context needs more VRAM — the KV cache grows with it. On AMD/Vulkan, <b>f16</b> KV is usually fastest; quantize KV only if a big context won't otherwise fit.</div>
    <div class="acct-actions"><span class="acct-msg" id="svMsg"></span><button class="primary sm" id="svGo">${editing ? "Save changes" : "Serve"}</button></div>
  </div>`;
  $("#svKv", body).value = vals.kv; $("#svKvV", body).value = vals.kv_v;
  const est = $("#svEst", body);
  let estT = null;
  const recompute = () => {
    clearTimeout(estT);
    estT = setTimeout(async () => {
      const qs = new URLSearchParams({ filename, ctx: $("#svCtx", body).value || 8192,
        kv: $("#svKv", body).value, kv_v: $("#svKvV", body).value, ngl: $("#svNgl", body).value || 99 });
      let e; try { e = await api("/api/rivers/estimate?" + qs.toString()); } catch { return; }
      if (!est.isConnected) return;
      if (!e.ok) { est.textContent = e.error || "estimate unavailable"; est.className = "river-est"; return; }
      riverPaintEst(est, e);
    }, 250);
  };
  ["svCtx", "svNgl", "svKv", "svKvV"].forEach(id => {
    const el = $("#" + id, body); el.addEventListener("input", recompute); el.addEventListener("change", recompute);
  });
  // preset chips + the ngl slider, all kept in sync with their number inputs
  const markChips = (wrap, inp) => wrap && wrap.querySelectorAll(".chip").forEach(c => c.classList.toggle("on", String(c.dataset.v) === String(inp.value)));
  const ctxChips = $("#svCtxChips", body), ttlChips = $("#svTtlChips", body);
  const ctxIn = $("#svCtx", body), ttlIn = $("#svTtl", body), nglIn = $("#svNgl", body), sl = $("#svNglSlider", body);
  const syncControls = () => {
    if (sl && nglIn) sl.value = Math.min(99, Math.max(0, +nglIn.value || 0));
    markChips(ctxChips, ctxIn); markChips(ttlChips, ttlIn);
  };
  if (ctxChips) ctxChips.querySelectorAll(".chip").forEach(c => c.onclick = () => { ctxIn.value = c.dataset.v; markChips(ctxChips, ctxIn); recompute(); });
  if (ttlChips) ttlChips.querySelectorAll(".chip").forEach(c => c.onclick = () => { ttlIn.value = c.dataset.v; markChips(ttlChips, ttlIn); });
  if (sl && nglIn) {
    sl.addEventListener("input", () => { nglIn.value = sl.value; markChips(ctxChips, ctxIn); recompute(); });
    nglIn.addEventListener("input", syncControls);
  }
  ctxIn.addEventListener("input", () => markChips(ctxChips, ctxIn));
  ttlIn.addEventListener("input", () => markChips(ttlChips, ttlIn));
  syncControls();
  recompute();
  $("#svRec", body).onclick = async () => {
    const rb = $("#svRec", body), w = $("#svRecWhy", body);
    rb.disabled = true; const lbl = rb.textContent; rb.textContent = "analyzing your hardware…";
    let d; try { d = await api("/api/rivers/recommend?filename=" + encodeURIComponent(filename)); } catch { d = null; }
    rb.disabled = false; rb.textContent = lbl;
    if (!d || !d.ok) { if (w) { w.style.display = "block"; w.className = "rec-why err"; w.textContent = (d && d.error) || "couldn't analyze this model"; } return; }
    const r = d.rec;
    $("#svCtx", body).value = r.ctx; $("#svNgl", body).value = r.ngl;
    $("#svKv", body).value = r.kv; $("#svKvV", body).value = r.kv_v;
    $("#svFa", body).checked = !!r.fa;
    $("#svThreads", body).value = r.threads || ""; $("#svMoe", body).value = r.n_cpu_moe || "";
    if (r.threads || r.n_cpu_moe) { const adv = body.querySelector(".serve-adv"); if (adv) adv.open = true; }
    if (w) {
      const labels = { ngl: "GPU layers", ctx: "Context", kv: "KV cache", n_cpu_moe: "MoE→CPU", threads: "Threads", fa: "Flash attn" };
      const g = x => (x / GB).toFixed(0);
      const hw = `<div class="rw-hw">Your box: ${d.vram_total ? g(d.vram_total) + " GB VRAM" : "no GPU"}${d.ram_total ? " · " + g(d.ram_total) + " GB RAM" : ""} · ${d.cores} cores${d.is_moe ? " · MoE model" : ""}</div>`;
      const rows = ["ngl", "ctx", "kv", "n_cpu_moe", "threads", "fa"].filter(k => d.why[k])
        .map(k => `<div class="rw-row"><b>${labels[k]}</b><span>${escapeHtml(d.why[k])}</span></div>`).join("");
      const notes = (d.notes || []).map(n => `<div class="rw-note">⚠ ${escapeHtml(n)}</div>`).join("");
      w.innerHTML = hw + rows + notes; w.className = "rec-why"; w.style.display = "block";
    }
    syncControls();
    recompute();
  };
  $("#svGo", body).onclick = async () => {
    const msg = $("#svMsg", body), go = $("#svGo", body); go.disabled = true;
    const payload = { filename, name: $("#svName", body).value.trim(),
      ctx: +$("#svCtx", body).value, ngl: +$("#svNgl", body).value,
      kv: $("#svKv", body).value, kv_v: $("#svKvV", body).value, fa: $("#svFa", body).checked,
      ttl: +$("#svTtl", body).value, parallel: +$("#svPar", body).value,
      threads: $("#svThreads", body).value, batch: $("#svBatch", body).value,
      ubatch: $("#svUbatch", body).value, n_cpu_moe: $("#svMoe", body).value,
      extra: $("#svExtra", body).value.trim() };
    let r; try { r = await api(editing ? "/api/rivers/update" : "/api/rivers/serve", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }); }
    catch { go.disabled = false; msg.textContent = "request failed"; msg.className = "acct-msg err"; return; }
    if (!r.ok) { msg.textContent = r.error; msg.className = "acct-msg err"; go.disabled = false; return; }
    msg.textContent = editing ? `✓ updated "${r.name}"` : `✓ serving as "${r.name}"`; msg.className = "acct-msg ok";
    const note = $("#riverNote"); if (note) { note.textContent = `✓ "${r.name}" — ngl ${r.ngl} · ctx ${r.ctx} · KV ${r.kv}/${r.kv_v} · fa ${r.fa ? "on" : "off"}`; note.className = "river-note ok"; }
    riverLoadInstalled(); loadModels();
    setTimeout(() => { const w = document.getElementById("win-serve"); if (w) w.remove(); }, 800);
  };
  setTimeout(() => { const e = $("#svName", body); if (e && !editing) e.focus(); }, 40);
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
    row.dataset.tid = t.id; row.dataset.src = t.source || "";
    const nxt = t.next_run ? t.next_run.slice(0, 16).replace("T", " ") : "—";
    const isSkills = (t.source || "").startsWith("skills");
    const mgrName = isSkills ? "Skills" : "Researcher";
    // Locked jobs (managed by Researcher/Skills): the schedule + on/off are yours to
    // change here; the instruction is owned by the manager and it can't be deleted.
    const lock = t.managed ? ` · <span class="sr-lock" title="created by ${mgrName} — schedule & on/off are editable here; managed there">🔒 ${mgrName}</span>` : "";
    row.innerHTML = `<label class="sw"><input type="checkbox" ${t.enabled ? "checked" : ""}><span></span></label>
      <div class="sr-body"><div class="sr-instr">${escapeHtml(t.instruction)}</div><div class="sr-meta"><code>${escapeHtml(t.cron)}</code> · next ${escapeHtml(nxt)}${lock}</div></div>` +
      `<button class="sr-btn sr-run" title="run now, ignoring the schedule">▶ Run</button>` +
      `<button class="sr-btn sr-edit">${t.managed ? "schedule" : "edit"}</button>` +
      (t.managed ? `<button class="sr-btn sr-res" title="manage in ${mgrName}">${mgrName.toLowerCase()}</button>`
                 : `<button class="sr-btn sr-del">✕</button>`);
    $("input", row).onchange = async e => { await fetch("/api/tasks/" + t.id, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: e.target.checked }) }); loadScheduler(); };
    $(".sr-run", row).onclick = async ev => {
      const b = ev.currentTarget; if (b.disabled) return;
      b.disabled = true; b.textContent = "running…"; row.classList.add("running");
      try {
        const r = await _postJ("/api/tasks/" + t.id + "/run", {});
        if (r.ok) toast("Ran ✓ " + ((r.result || "done").trim().slice(0, 140)), "info");
        else toast("Run failed: " + (r.error || "unknown"), "err");
      } catch { toast("Run failed", "err"); }
      row.classList.remove("running"); loadScheduler();
    };
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
let _evalTimer = null, _evalModels = [], _evalRunSel = null, _brainEvalDotTimer = null;
function startBrainEvalDot() {
  if (_brainEvalDotTimer) clearInterval(_brainEvalDotTimer);
  pollBrainEvalDot();
  _brainEvalDotTimer = setInterval(pollBrainEvalDot, 3000);
}
async function pollBrainEvalDot() {
  const dot = $("#brainEvalDot");
  if (!dot) { if (_brainEvalDotTimer) { clearInterval(_brainEvalDotTimer); _brainEvalDotTimer = null; } return; }
  let st; try { st = await api("/api/evals/state"); } catch { return; }
  dot.style.display = st.running ? "" : "none";
  dot.classList.toggle("cancelling", !!st.cancelling);
  dot.title = st.running ? ("eval running — " + (st.phase || "")) : "";
}
function renderEvals(c) {
  c.innerHTML = `
    <div class="brain-head">
      <div class="sk-tabs">
        <button class="sk-tab on" data-f="board">Leaderboard</button>
        <button class="sk-tab" data-f="cases">Cases<span class="sk-cnt" id="evCntCases"></span></button>
        <button class="sk-tab" data-f="models">Models<span class="sk-cnt" id="evCntModels"></span></button>
        <button class="sk-tab" data-f="history">History</button>
      </div>
      <span style="flex:1"></span>
      <button class="exp-btn danger" id="evCancel" style="display:none" title="stop the in-progress run after the current case">✕ Cancel</button>
      <button class="exp-btn" id="evRun" title="run the suite against the selected models — judged by Claude Code">⚖ Run now</button>
    </div>
    <div class="kn-note" id="evMsg"></div>
    <div id="evBody"></div>`;
  $("#evRun").onclick = startEvalRun;
  $("#evCancel").onclick = cancelEvalRun;
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
  else if (_evalTab === "models") loadEvalModels();
  else if (_evalTab === "history") loadEvalHistory();
  else loadEvalBoard();
}
async function startEvalRun() {
  const msg = $("#evMsg");
  if (msg) { msg.textContent = "starting eval run — each model runs every case, graded by Claude Code (minutes)…"; msg.className = "kn-note run"; }
  await fetch("/api/evals/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({}) });
  refreshEvalState(true);
}
async function cancelEvalRun() {
  const c = $("#evCancel"); if (c) { c.disabled = true; c.textContent = "cancelling…"; }
  await fetch("/api/evals/cancel", { method: "POST" }).catch(() => {});
  refreshEvalState(true);
}
function evalRunControls(st) {
  const run = $("#evRun"), cancel = $("#evCancel");
  if (!run || !cancel) return;
  run.style.display = st.running ? "none" : "";
  cancel.style.display = st.running ? "" : "none";
  cancel.disabled = !!st.cancelling;
  cancel.textContent = st.cancelling ? "cancelling…" : "✕ Cancel";
}
async function refreshEvalState(loop) {
  const msg = $("#evMsg"); if (!msg) { _evalTimer = null; return; }
  let st; try { st = await api("/api/evals/state"); } catch { _evalTimer = null; return; }
  evalRunControls(st);
  if (st.running) {
    const pct = st.total ? Math.round(100 * st.done / st.total) : 0;
    msg.textContent = (st.cancelling ? "cancelling — " : `running ${st.done}/${st.total} (${pct}%) · `) + (st.phase || "");
    msg.className = "kn-note run";
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
async function loadEvalModels() {
  const body = $("#evBody"); if (!body) return;
  let d; try { d = await api("/api/evals/models"); } catch { return; }
  const sel = new Set(d.selected || []);
  const cnt = $("#evCntModels"); if (cnt) cnt.textContent = sel.size;
  const sch = d.scheduled
    ? `Scheduled run: <code>${escapeHtml(d.scheduled.cron)}</code> · ${d.scheduled.enabled ? "on" : "off"} — edit its time in the Scheduler.`
    : "No scheduled run configured.";
  if (!(d.available || []).length) {
    body.innerHTML = `<div class="empty-note">No local models served. Install one in Brain → Rivers.</div>`;
    return;
  }
  body.innerHTML = `
    <div class="ev-models-note">Target models for the eval suite. This selection drives both <b>⚖ Run now</b> and the scheduled run. If none are checked, <b>all</b> installed models run.<br>${sch}</div>
    ${d.available.map(m => `
      <div class="ev-model-row" data-m="${escapeHtml(m)}">
        <span class="ev-model-name">${escapeHtml(m)}</span>
        <label class="sw sm"><input type="checkbox" ${sel.has(m) ? "checked" : ""}><span></span></label>
      </div>`).join("")}`;
  $$(".ev-model-row", body).forEach(el => {
    $("input", el).onchange = async () => {
      const chosen = $$(".ev-model-row input", body).filter(x => x.checked)
        .map(x => x.closest(".ev-model-row").dataset.m);
      await fetch("/api/evals/models", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ models: chosen }) });
      const c = $("#evCntModels"); if (c) c.textContent = chosen.length;
    };
  });
}
async function loadEvalHistory() {
  const body = $("#evBody"); if (!body) return;
  let d; try { d = await api("/api/evals/runs"); } catch { return; }
  if (!d.runs.length) { body.innerHTML = `<div class="empty-note">No runs yet.</div>`; return; }
  const head = `<div class="ev-cases-head"><span style="flex:1"></span><button class="sr-btn" id="evClear">Clear history</button></div>`;
  body.innerHTML = head + d.runs.map(r => `
    <div class="ev-run" data-id="${r.id}">
      <div class="ev-run-main"><div class="ev-run-sum">${escapeHtml(r.summary || "(no summary)")}</div>
      <div class="ev-run-meta">#${r.id} · ${(r.ts || "").slice(0, 16).replace("T", " ")} · ${escapeHtml(r.status)} · ${r.models.length} model(s): ${escapeHtml((r.models || []).join(", ")) || "—"}</div></div>
      <button class="sr-btn ev-view">results</button><button class="sr-btn ev-del">✕</button></div>`).join("");
  $("#evClear", body).onclick = async () => {
    if (!await confirmAction("Clear eval history?", "Deletes all runs and their results. Cases and model selection are kept.")) return;
    const r = await (await fetch("/api/evals/runs/clear", { method: "POST" })).json().catch(() => ({}));
    if (r && r.error) toast(r.error, "err");
    loadEvalHistory();
  };
  $$(".ev-run", body).forEach(el => {
    $(".ev-view", el).onclick = () => openEvalResults(+el.dataset.id);
    $(".ev-del", el).onclick = async () => {
      if (!await confirmAction("Delete this run?", "Run #" + el.dataset.id)) return;
      const r = await (await fetch("/api/evals/runs/" + el.dataset.id, { method: "DELETE" })).json().catch(() => ({}));
      if (r && r.error) toast(r.error, "err");
      loadEvalHistory();
    };
  });
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

/* ---------- Calendar window — Outlook-style month / week / day grid ----------
   Local events (yours/the agent's) are editable; synced feed events are read-only (locked). */
let _calView = "month";              // month | week | day
let _calAnchor = new Date();         // a day inside the focused period
let _calCache = [];                  // events for the visible range
const _CAL_HOURPX = 44;              // px per hour in the week/day time grid
const _CAL_DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];   // Monday-first

const _cal = {
  ymd: d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`,
  parse: s => new Date(s.length <= 10 ? s + "T00:00" : s),
  addDays: (d, n) => { const x = new Date(d); x.setDate(x.getDate() + n); return x; },
  startOfWeek: d => { const x = new Date(d.getFullYear(), d.getMonth(), d.getDate()); x.setDate(x.getDate() - ((x.getDay() + 6) % 7)); return x; },
  sameDay: (a, b) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate(),
  mins: d => d.getHours() * 60 + d.getMinutes(),
};
const calEl = (t, c, txt) => { const e = document.createElement(t); if (c) e.className = c; if (txt != null) e.textContent = txt; return e; };
const calHourLabel = h => `${h % 12 || 12} ${h < 12 ? "AM" : "PM"}`;
const calTimeLabel = d => d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });

function openCalendar() {
  const { body, reused } = createWindow({ id: "win-cal", title: "Calendar", icon: "◷", width: 880, height: 660, restoreKey: "cal" });
  if (reused) { calRender(); return; }
  body.innerHTML = `
    <div class="cal-toolbar">
      <div class="cal-nav">
        <button class="cal-navbtn" id="calPrev" title="previous">‹</button>
        <button class="cal-navbtn" id="calTodayBtn">Today</button>
        <button class="cal-navbtn" id="calNext" title="next">›</button>
      </div>
      <div class="cal-period" id="calPeriod"></div>
      <div class="cal-views" id="calViews">
        <button data-v="month">Month</button><button data-v="week">Week</button><button data-v="day">Day</button>
      </div>
      <button class="primary sm" id="calNew">＋ New</button>
      <button class="exp-btn" id="calSync" title="sync external feeds now">↻</button>
      <span class="kn-note" id="calMsg"></span>
    </div>
    <div class="cal-form" id="calForm" hidden></div>
    <div class="cal-viewport" id="calViewport"></div>
    <details class="cal-feeds-wrap">
      <summary>External calendars <span class="lbl-sub">— subscribe to an .ics feed (read-only)</span></summary>
      <div class="cal-hint">Google Calendar → Settings → your calendar → <b>Integrate calendar</b> → copy the <b>Secret address in iCal format</b>. Sync is one-way: Oceano reads these and never writes back. Synced events show locked — the agent schedules <i>around</i> them.</div>
      <div class="sched-add">
        <input id="calName" placeholder="name · e.g. Work" style="flex:0 0 130px">
        <input id="calUrl" placeholder="secret iCal address (…/basic.ics)" style="flex:1;min-width:150px" spellcheck="false">
        <button class="primary sm" id="calAdd">Add feed</button>
      </div>
      <div class="cal-feeds" id="calFeeds"></div>
    </details>`;
  $("#calPrev", body).onclick = () => calNav(-1);
  $("#calNext", body).onclick = () => calNav(1);
  $("#calTodayBtn", body).onclick = () => { _calAnchor = new Date(); calRender(); };
  $("#calNew", body).onclick = () => calOpenForm(null);
  $$("#calViews button", body).forEach(b => b.onclick = () => { _calView = b.dataset.v; calRender(); });
  $("#calAdd", body).onclick = async () => {
    const name = $("#calName").value.trim(), url = $("#calUrl").value.trim(), msg = $("#calMsg");
    if (!url) return;
    const btn = $("#calAdd"); btn.disabled = true; btn.textContent = "syncing…";
    try {
      const r = await api("/api/calendar/feeds", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name, url }) });
      if (!r.ok) { msg.textContent = r.error || "could not add feed"; msg.className = "kn-note err"; return; }
      const s = r.sync || {};
      msg.textContent = s.ok ? `feed added ✓ · ${s.events} events` : `added, first sync failed: ${s.error || "?"}`;
      msg.className = "kn-note " + (s.ok ? "ok" : "err");
      $("#calName").value = ""; $("#calUrl").value = ""; calRender();
    } finally { btn.disabled = false; btn.textContent = "Add feed"; }
  };
  $("#calSync", body).onclick = async () => {
    const btn = $("#calSync"), msg = $("#calMsg"); btn.disabled = true; btn.textContent = "…";
    try {
      const r = await api("/api/calendar/sync", { method: "POST" });
      msg.textContent = r.ok ? "synced ✓" : "some feeds failed"; msg.className = "kn-note " + (r.ok ? "ok" : "err");
      calRender();
    } finally { btn.disabled = false; btn.textContent = "↻"; }
  };
  calRender();
}

function calNav(dir) {
  if (_calView === "month") _calAnchor = new Date(_calAnchor.getFullYear(), _calAnchor.getMonth() + dir, 1);
  else _calAnchor = _cal.addDays(_calAnchor, dir * (_calView === "day" ? 1 : 7));
  calRender();
}

function calVisibleRange() {
  if (_calView === "month") {
    const first = new Date(_calAnchor.getFullYear(), _calAnchor.getMonth(), 1);
    const last = new Date(_calAnchor.getFullYear(), _calAnchor.getMonth() + 1, 0);
    return { start: _cal.startOfWeek(first), end: _cal.addDays(_cal.startOfWeek(last), 7) };
  }
  if (_calView === "day") {
    const s = new Date(_calAnchor.getFullYear(), _calAnchor.getMonth(), _calAnchor.getDate());
    return { start: s, end: _cal.addDays(s, 1) };
  }
  const s = _cal.startOfWeek(_calAnchor);
  return { start: s, end: _cal.addDays(s, 7) };
}

function calPeriodLabel() {
  if (_calView === "month") return _calAnchor.toLocaleDateString(undefined, { month: "long", year: "numeric" });
  if (_calView === "day") return _calAnchor.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric", year: "numeric" });
  const s = _cal.startOfWeek(_calAnchor), e = _cal.addDays(s, 6);
  return `${s.toLocaleDateString(undefined, { month: "short", day: "numeric" })} – ${e.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" })}`;
}

async function calRender() {
  const vp = $("#calViewport"); if (!vp) return;
  const { start, end } = calVisibleRange();
  let d; try { d = await api(`/api/calendar?start=${_cal.ymd(start)}&end=${_cal.ymd(end)}`); } catch { return; }
  _calCache = d.events || [];
  const pl = $("#calPeriod"); if (pl) pl.textContent = calPeriodLabel();
  $$("#calViews button").forEach(b => b.classList.toggle("active", b.dataset.v === _calView));
  vp.innerHTML = "";
  if (_calView === "month") calRenderMonth(vp, _calCache);
  else calRenderTimeGrid(vp, _calCache, _calView === "day" ? 1 : 7);
  calRenderFeeds(d.feeds || []);
}

function calEventTouchesDay(e, day) {
  const s = _cal.parse(e.start), en = e.end ? _cal.parse(e.end) : new Date(s.getTime() + 30 * 60000);
  const sd = new Date(day.getFullYear(), day.getMonth(), day.getDate()), ed = _cal.addDays(sd, 1);
  return s < ed && en > sd;
}

function calChip(e) {
  const chip = calEl("div", "cal-chip " + (e.editable ? "local" : "feed"));
  const t = e.all_day ? "" : calTimeLabel(_cal.parse(e.start)) + " ";
  chip.innerHTML = `${e.editable ? "" : '<span class="cal-chip-lock">🔒</span>'}` +
    `<span class="cal-chip-t">${escapeHtml(t)}</span>${escapeHtml(e.title || "(untitled)")}`;
  if (e.description || e.location) chip.title = (e.location ? e.location + " — " : "") + (e.description || "");
  if (e.editable) { chip.classList.add("cal-clickable"); chip.onclick = ev => { ev.stopPropagation(); calOpenForm(e); }; }
  return chip;
}

function calRenderMonth(vp, events) {
  const wrap = calEl("div", "cal-month");
  const head = calEl("div", "cal-mo-head");
  _CAL_DOW.forEach(n => head.appendChild(calEl("div", null, n)));
  wrap.appendChild(head);
  const { start, end } = calVisibleRange();
  const grid = calEl("div", "cal-mo-grid");
  grid.style.gridTemplateRows = `repeat(${Math.round((end - start) / (7 * 864e5))}, minmax(76px, 1fr))`;
  const today = new Date(), monthIdx = _calAnchor.getMonth();
  const byDay = {};
  events.forEach(e => { (byDay[_cal.ymd(_cal.parse(e.start))] ||= []).push(e); });
  for (let day = new Date(start); day < end; day = _cal.addDays(day, 1)) {
    const dc = new Date(day);
    const cell = calEl("div", "cal-mo-cell" + (day.getMonth() !== monthIdx ? " out" : "") + (_cal.sameDay(day, today) ? " today" : ""));
    cell.appendChild(calEl("div", "cal-mo-num", day.getDate()));
    const evs = (byDay[_cal.ymd(day)] || []).slice().sort((a, b) => (a.all_day ? 0 : 1) - (b.all_day ? 0 : 1) || a.start.localeCompare(b.start));
    evs.slice(0, 3).forEach(e => cell.appendChild(calChip(e)));
    if (evs.length > 3) {
      const more = calEl("div", "cal-more", `+${evs.length - 3} more`);
      more.onclick = ev => { ev.stopPropagation(); _calAnchor = dc; _calView = "day"; calRender(); };
      cell.appendChild(more);
    }
    cell.onclick = () => calOpenForm(null, { date: _cal.ymd(dc) });
    grid.appendChild(cell);
  }
  wrap.appendChild(grid); vp.appendChild(wrap);
}

// greedy interval packing: assign each timed event a column within its overlap cluster
function calLayoutDay(items) {
  items.sort((a, b) => a.s - b.s || a.e - b.e);
  let i = 0;
  while (i < items.length) {
    let j = i, maxEnd = items[i].e;
    while (j + 1 < items.length && items[j + 1].s < maxEnd) { j++; maxEnd = Math.max(maxEnd, items[j].e); }
    const cluster = items.slice(i, j + 1), colEnds = [];
    cluster.forEach(o => {
      let placed = false;
      for (let k = 0; k < colEnds.length; k++) if (o.s >= colEnds[k]) { o.col = k; colEnds[k] = o.e; placed = true; break; }
      if (!placed) { o.col = colEnds.length; colEnds.push(o.e); }
    });
    cluster.forEach(o => o.cols = colEnds.length);
    i = j + 1;
  }
}

function calRenderTimeGrid(vp, events, ndays) {
  const gridH = 24 * _CAL_HOURPX;
  const { start } = calVisibleRange();
  const days = Array.from({ length: ndays }, (_, i) => _cal.addDays(start, i));
  const today = new Date();
  const tg = calEl("div", "cal-tg");

  const headRow = calEl("div", "cal-tg-head");
  headRow.appendChild(calEl("div", "cal-tg-corner"));
  days.forEach(d => {
    const h = calEl("div", "cal-tg-dh" + (_cal.sameDay(d, today) ? " today" : ""));
    h.innerHTML = `<span class="cal-tg-dn">${_CAL_DOW[(d.getDay() + 6) % 7]}</span> <span class="cal-tg-dd">${d.getDate()}</span>`;
    headRow.appendChild(h);
  });
  tg.appendChild(headRow);

  const adRow = calEl("div", "cal-tg-allday");
  adRow.appendChild(calEl("div", "cal-tg-adlabel", "all-day"));
  days.forEach(d => {
    const dc = new Date(d), cell = calEl("div", "cal-tg-adcell");
    events.filter(e => e.all_day && calEventTouchesDay(e, d)).forEach(e => cell.appendChild(calChip(e)));
    cell.onclick = () => calOpenForm(null, { date: _cal.ymd(dc), all_day: true });
    adRow.appendChild(cell);
  });
  tg.appendChild(adRow);

  const scroll = calEl("div", "cal-tg-scroll");
  const bodyGrid = calEl("div", "cal-tg-body");
  const gutter = calEl("div", "cal-tg-gutter");
  for (let h = 0; h < 24; h++) { const cell = calEl("div", "cal-tg-hr", h ? calHourLabel(h) : ""); cell.style.height = _CAL_HOURPX + "px"; gutter.appendChild(cell); }
  bodyGrid.appendChild(gutter);
  const cols = calEl("div", "cal-tg-cols");
  days.forEach(d => {
    const dc = new Date(d), col = calEl("div", "cal-tg-col"); col.style.height = gridH + "px";
    col.style.backgroundSize = `100% ${_CAL_HOURPX}px`;
    const sd = new Date(d.getFullYear(), d.getMonth(), d.getDate()), ed = _cal.addDays(sd, 1);
    const items = events.filter(e => !e.all_day && calEventTouchesDay(e, d)).map(e => {
      const s = _cal.parse(e.start), en = e.end ? _cal.parse(e.end) : new Date(s.getTime() + 30 * 60000);
      const sMin = s < sd ? 0 : _cal.mins(s);
      const eMin = en >= ed ? 1440 : _cal.mins(en);
      return { ref: e, s: sMin, e: eMin < sMin + 15 ? sMin + 15 : eMin };   // s/e = minutes; ref = the event
    });
    calLayoutDay(items);
    items.forEach(o => {
      const e = o.ref;
      const blk = calEl("div", "cal-tg-ev " + (e.editable ? "local" : "feed ro"));
      blk.style.top = (o.s / 1440 * gridH) + "px";
      blk.style.height = Math.max(15, (o.e - o.s) / 1440 * gridH - 2) + "px";
      blk.style.left = `calc(${o.col / o.cols * 100}% + 2px)`;
      blk.style.width = `calc(${100 / o.cols}% - 4px)`;
      blk.innerHTML = `<div class="cal-tg-ev-t">${e.editable ? "" : "🔒 "}${escapeHtml(e.title || "(untitled)")}</div>` +
        `<div class="cal-tg-ev-time">${escapeHtml(calTimeLabel(_cal.parse(e.start)))}</div>`;
      if (e.description || e.location) blk.title = (e.location ? e.location + " — " : "") + (e.description || "");
      if (e.editable) blk.onclick = ev => { ev.stopPropagation(); calOpenForm(e); };
      col.appendChild(blk);
    });
    if (_cal.sameDay(d, today)) { const now = calEl("div", "cal-now"); now.style.top = (_cal.mins(today) / 1440 * gridH) + "px"; col.appendChild(now); }
    col.onclick = ev => {
      const y = ev.clientY - col.getBoundingClientRect().top;
      const hr = Math.max(0, Math.min(23, Math.floor(y / _CAL_HOURPX)));
      calOpenForm(null, { date: _cal.ymd(dc), start: String(hr).padStart(2, "0") + ":00" });
    };
    cols.appendChild(col);
  });
  bodyGrid.appendChild(cols);
  scroll.appendChild(bodyGrid);
  tg.appendChild(scroll);
  vp.appendChild(tg);
  scroll.scrollTop = 7 * _CAL_HOURPX;     // open around the working day
}

function calRenderFeeds(feeds) {
  const box = $("#calFeeds"); if (!box) return;
  box.innerHTML = "";
  if (!feeds.length) { box.innerHTML = `<div class="empty-note" style="padding:10px">No external calendars subscribed.</div>`; return; }
  feeds.forEach(f => {
    const row = calEl("div", "ep");
    const sync = f.last_error ? `⚠ ${f.last_error}` : f.last_sync ? `synced ${f.last_sync.slice(0, 16).replace("T", " ")} UTC` : "never synced";
    row.innerHTML = `<div class="ep-info"><div class="ep-name">${escapeHtml(f.name)}</div><div class="ep-url">${escapeHtml(sync)}</div></div><span class="ep-count ${f.last_error ? "err" : "ok"}">${f.event_count} events</span><button class="ep-del">✕</button>`;
    $(".ep-del", row).onclick = async () => { if (!await confirmAction("Remove feed?", `“${f.name}” and its synced events will be removed. The source calendar is not touched.`, "Remove")) return; await fetch("/api/calendar/feeds/" + f.id, { method: "DELETE" }); calRender(); };
    box.appendChild(row);
  });
}

// Inline create/edit form for a LOCAL event. `ev` → edit; else `prefill` {date,start,end,all_day} for a new one.
function calOpenForm(ev, prefill) {
  const box = $("#calForm"); if (!box) return;
  const isEdit = !!ev; prefill = prefill || {};
  box.hidden = false;
  box.innerHTML = `
    <div class="cal-form-row">
      <input id="cfTitle" placeholder="Event title" style="flex:1;min-width:140px">
      <label class="cal-allday"><input type="checkbox" id="cfAllday"> all day</label>
    </div>
    <div class="cal-form-row">
      <input type="date" id="cfDate">
      <input type="time" id="cfStart" class="cf-time">
      <span class="cf-dash">–</span>
      <input type="time" id="cfEnd" class="cf-time">
      <input id="cfLoc" placeholder="location (optional)" style="flex:1;min-width:110px">
    </div>
    <div class="cal-form-row">
      <button class="primary sm" id="cfSave">${isEdit ? "Save" : "Add event"}</button>
      <button class="exp-btn" id="cfCancel">Cancel</button>
      ${isEdit ? `<button class="exp-btn danger" id="cfDelete" style="margin-left:auto">Delete</button>` : ""}
      <span class="kn-note" id="cfMsg"></span>
    </div>`;
  const startHM = prefill.start || "09:00";
  const endHM = prefill.start ? (String(Math.min(23, +prefill.start.slice(0, 2) + 1)).padStart(2, "0") + ":00") : "";
  $("#cfTitle").value = ev ? (ev.title || "") : "";
  $("#cfDate").value = ev ? ev.start.slice(0, 10) : (prefill.date || _cal.ymd(_calAnchor));
  $("#cfAllday").checked = ev ? !!ev.all_day : !!prefill.all_day;
  $("#cfStart").value = ev ? (ev.all_day ? "" : ev.start.slice(11, 16)) : startHM;
  $("#cfEnd").value = ev ? (ev.end ? ev.end.slice(11, 16) : "") : endHM;
  $("#cfLoc").value = ev ? (ev.location || "") : "";
  const allday = $("#cfAllday");
  const toggleTimes = () => {
    const on = allday.checked;
    $("#cfStart").style.display = on ? "none" : "";
    $("#cfEnd").style.display = on ? "none" : "";
    $(".cf-dash", box).style.display = on ? "none" : "";
  };
  allday.onchange = toggleTimes; toggleTimes();
  const close = () => { box.hidden = true; box.innerHTML = ""; };
  $("#cfCancel").onclick = close;
  $("#cfSave").onclick = async () => {
    const title = $("#cfTitle").value.trim(), date = $("#cfDate").value, on = allday.checked, msg = $("#cfMsg");
    if (!title) { msg.textContent = "title required"; return; }
    if (!date) { msg.textContent = "pick a date"; return; }
    const payload = { title, all_day: on, location: $("#cfLoc").value.trim() };
    if (on) { payload.start = date; payload.end = ""; }
    else {
      const st = $("#cfStart").value, en = $("#cfEnd").value;
      if (!st) { msg.textContent = "pick a start time (or check all-day)"; return; }
      payload.start = date + " " + st; payload.end = en ? date + " " + en : "";
    }
    const r = await api(isEdit ? "/api/calendar/events/" + ev.id : "/api/calendar/events",
      { method: isEdit ? "PUT" : "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    if (!r.ok) { msg.textContent = r.error || "could not save"; return; }
    close(); calRender();
  };
  if (isEdit) $("#cfDelete").onclick = async () => {
    if (!await confirmAction("Delete event?", `“${ev.title}” will be removed.`, "Delete")) return;
    await fetch("/api/calendar/events/" + ev.id, { method: "DELETE" });
    close(); calRender();
  };
  $("#cfTitle").focus();
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

/* ====================================================================
   Memory graph — a force-directed map of the memory store.
   Nodes are memories (colored by category); edges link memories that are
   strongly semantically similar or share a tag. Pure-canvas, no libs.
   ==================================================================== */
const MEM_CAT_COLORS = { identity: "#e0a86b", preference: "#7ec8a9", project: "#6ba3e0", fact: "#9b8fd6", task: "#d67f9b" };
let _mgRaf = null;
function openMemoryGraph() {
  const { body, reused } = createWindow({ id: "win-memgraph", title: "Memory graph", icon: "❄", width: 780, height: 600,
    onClose: () => { if (_mgRaf) { cancelAnimationFrame(_mgRaf); _mgRaf = null; } } });
  if (reused) return;
  body.classList.add("mg-win");
  body.innerHTML = `
    <div class="mg-bar">
      <label class="mg-th">link strength <input type="range" id="mgTh" min="0.45" max="0.9" step="0.01" value="0.62"><span id="mgThVal">0.62</span></label>
      <span class="fe-spacer"></span>
      <span class="mg-legend">${Object.entries(MEM_CAT_COLORS).map(([k, v]) => `<span class="mg-lg"><i style="background:${v}"></i>${k}</span>`).join("")}</span>
      <button class="ed-btn" id="mgReload" title="reload">↻</button>
    </div>
    <div class="mg-stage"><canvas id="mgCanvas"></canvas><div class="mg-inspect" id="mgInspect" style="display:none"></div><div class="mg-empty empty-note" id="mgEmpty" style="display:none"></div></div>`;
  const canvas = $("#mgCanvas", body), th = $("#mgTh", body), thVal = $("#mgThVal", body);
  const inspect = $("#mgInspect", body), empty = $("#mgEmpty", body), ctx = canvas.getContext("2d");
  let nodes = [], edges = [], byId = {}, dragging = null, hover = null;

  function fit() {
    const r = canvas.parentElement.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
    canvas.width = r.width * dpr; canvas.height = r.height * dpr;
    canvas.style.width = r.width + "px"; canvas.style.height = r.height + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { w: r.width, h: r.height };
  }
  async function loadGraph() {
    let g; try { g = await api("/api/memory/graph?threshold=" + th.value); } catch { return; }
    const { w, h } = fit();
    byId = {};
    nodes = (g.nodes || []).map((n, idx) => {
      const ang = (idx / Math.max((g.nodes || []).length, 1)) * Math.PI * 2;
      const o = { ...n, x: w / 2 + Math.cos(ang) * 130 + (idx % 5), y: h / 2 + Math.sin(ang) * 130, vx: 0, vy: 0, r: n.pinned ? 9 : 6, deg: 0 };
      byId[n.id] = o; return o;
    });
    edges = (g.edges || []).filter(e => byId[e.a] && byId[e.b]);
    edges.forEach(e => { byId[e.a].deg++; byId[e.b].deg++; });
    empty.style.display = nodes.length ? "none" : "block";
    if (!nodes.length) empty.textContent = "No memories yet — teach Oceano something in Brain → Memory.";
    inspect.style.display = "none";
    if (!_mgRaf) tick();
  }
  function tick() {
    const w = canvas.clientWidth, h = canvas.clientHeight;
    for (let i = 0; i < nodes.length; i++) {                 // repulsion (Coulomb-ish)
      const a = nodes[i];
      for (let j = i + 1; j < nodes.length; j++) {
        const b = nodes[j];
        let dx = a.x - b.x, dy = a.y - b.y, d2 = dx * dx + dy * dy || 0.01, d = Math.sqrt(d2);
        const f = 950 / d2, ux = dx / d, uy = dy / d;
        a.vx += ux * f; a.vy += uy * f; b.vx -= ux * f; b.vy -= uy * f;
      }
    }
    edges.forEach(e => {                                     // springs along links
      const a = byId[e.a], b = byId[e.b];
      let dx = b.x - a.x, dy = b.y - a.y, d = Math.sqrt(dx * dx + dy * dy) || 0.01;
      const target = e.kind === "semantic" ? 64 : 120, f = (d - target) * 0.01 * e.w, ux = dx / d, uy = dy / d;
      a.vx += ux * f; a.vy += uy * f; b.vx -= ux * f; b.vy -= uy * f;
    });
    nodes.forEach(n => {
      if (n === dragging) { n.vx = n.vy = 0; return; }
      n.vx += (w / 2 - n.x) * 0.002; n.vy += (h / 2 - n.y) * 0.002;   // gentle gravity to center
      n.vx *= 0.86; n.vy *= 0.86; n.x += n.vx; n.y += n.vy;
      n.x = Math.max(12, Math.min(w - 12, n.x)); n.y = Math.max(12, Math.min(h - 12, n.y));
    });
    draw();
    _mgRaf = requestAnimationFrame(tick);
  }
  function draw() {
    const w = canvas.clientWidth, h = canvas.clientHeight;
    ctx.clearRect(0, 0, w, h);
    edges.forEach(e => {
      const a = byId[e.a], b = byId[e.b];
      ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y);
      if (e.kind === "semantic") { ctx.strokeStyle = `rgba(120,170,220,${0.1 + e.w * 0.4})`; ctx.lineWidth = 0.6 + e.w; ctx.setLineDash([]); }
      else { ctx.strokeStyle = "rgba(150,150,175,0.18)"; ctx.lineWidth = 0.8; ctx.setLineDash([3, 3]); }
      ctx.stroke();
    });
    ctx.setLineDash([]);
    nodes.forEach(n => {
      ctx.beginPath(); ctx.arc(n.x, n.y, n.r + (n === hover ? 2 : 0), 0, Math.PI * 2);
      ctx.fillStyle = MEM_CAT_COLORS[n.category] || "#888"; ctx.fill();
      if (n.pinned || n === hover) { ctx.lineWidth = n.pinned ? 2 : 1; ctx.strokeStyle = "#fff"; ctx.stroke(); }
    });
  }
  function nodeAt(mx, my) {
    for (let i = nodes.length - 1; i >= 0; i--) { const n = nodes[i], dx = mx - n.x, dy = my - n.y; if (dx * dx + dy * dy <= (n.r + 4) * (n.r + 4)) return n; }
    return null;
  }
  const relpos = e => { const r = canvas.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
  canvas.onmousedown = e => { const [x, y] = relpos(e); const n = nodeAt(x, y); if (n) { dragging = n; showInspect(n); } };
  canvas.onmousemove = e => { const [x, y] = relpos(e); if (dragging) { dragging.x = x; dragging.y = y; } else { hover = nodeAt(x, y); canvas.style.cursor = hover ? "pointer" : "default"; } };
  window.addEventListener("mouseup", () => { dragging = null; });
  function showInspect(n) {
    inspect.style.display = "block";
    inspect.innerHTML = `<div class="mgi-cat" style="color:${MEM_CAT_COLORS[n.category] || "#888"}">${n.pinned ? "📌 " : ""}${escapeHtml(n.category)}</div><div class="mgi-text">${escapeHtml(n.text)}</div>${n.tags ? `<div class="mgi-tags">${escapeHtml(n.tags)}</div>` : ""}<div class="mgi-deg">${n.deg} link${n.deg === 1 ? "" : "s"}</div>`;
  }
  th.oninput = () => { thVal.textContent = (+th.value).toFixed(2); };
  th.onchange = loadGraph;
  $("#mgReload", body).onclick = loadGraph;
  loadGraph();
}

/* ====================================================================
   System health dashboard — live state of the self-hosted stack.
   ==================================================================== */
let _healthTimer = null;
function openHealth() {
  const { body, reused } = createWindow({ id: "win-health", title: "Health — system", icon: "◉", width: 560, height: 600,
    onClose: () => { if (_healthTimer) { clearInterval(_healthTimer); _healthTimer = null; } } });
  if (reused) return;
  body.classList.add("hd-win");
  body.innerHTML = `<div class="hd-grid" id="hdGrid"></div><div class="hd-foot"><span id="hdUptime">—</span><button class="ed-btn" id="hdReload" title="refresh">↻</button></div>`;
  const grid = $("#hdGrid", body);
  const dot = ok => `<span class="hd-dot ${ok ? "ok" : "bad"}"></span>`;
  const fmtBytes = b => b == null ? "—" : (b >= 1e9 ? (b / 1073741824).toFixed(1) + " GB" : (b / 1048576).toFixed(0) + " MB");
  const fmtDur = s => { if (s == null) return "—"; s = Math.floor(s); const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60); return (d ? d + "d " : "") + (h ? h + "h " : "") + m + "m"; };
  const card = (title, ok, rows) => `<div class="hd-card"><div class="hd-h">${dot(ok)}${escapeHtml(title)}</div>${rows.map(r => `<div class="hd-row"><span>${escapeHtml(r[0])}</span><b>${r[1]}</b></div>`).join("")}</div>`;
  async function load() {
    let d; try { d = await api("/api/health"); } catch { grid.innerHTML = `<div class="empty-note">health unavailable</div>`; return; }
    const ls = d.llamaswap || {}, em = d.embed || {}, sc = d.scheduler || {}, tg = d.telegram || {}, hw = d.hw || {}, rg = d.rag || {};
    grid.innerHTML = [
      card("Inference · llama-swap", ls.ok, [["loaded model", escapeHtml(ls.loaded || d.model || "—")], ["available", (ls.models || []).length || "—"]]),
      card("Embeddings · :8082", em.ok, [["model", escapeHtml(em.model || "—")]]),
      card("GPU", !!(hw.gpu || hw.backend), [["device", escapeHtml(hw.gpu || hw.backend || "—")], ["VRAM free", fmtBytes(hw.vram_free) + (hw.vram_total ? " / " + fmtBytes(hw.vram_total) : "")]]),
      card("Scheduler", sc.beat_ago_s != null && sc.beat_ago_s < 180, [["last beat", sc.beat_ago_s != null ? Math.round(sc.beat_ago_s) + "s ago" : "—"], ["tasks", sc.tasks ?? "—"]]),
      card("Telegram", !!tg.running, [["bot", tg.running ? "@" + escapeHtml(tg.username || "on") : "off"]]),
      card("Knowledge", true, [["memories", d.memory ? d.memory.count : "—"], ["doc chunks", rg.chunks ?? "—"], ["files indexed", rg.files ?? "—"]]),
    ].join("");
    $("#hdUptime", body).textContent = "uptime " + fmtDur(d.uptime_s);
  }
  $("#hdReload", body).onclick = load;
  load(); _healthTimer = setInterval(load, 5000);
}

/* ====================================================================
   Logs — the durable activity log of unattended runs (scheduled tasks,
   workflows, research, evals, upkeep): status, duration, and the result.
   ==================================================================== */
let _logsKind = "";
function openLogs() {
  const { body, reused } = createWindow({ id: "win-logs", title: "Logs — activity", icon: "▤", width: 660, height: 600 });
  if (reused) { loadLogs(); return; }
  body.classList.add("logs-win");
  body.innerHTML = `
    <div class="logs-bar">
      <select id="logKind"><option value="">all activity</option></select>
      <span class="logs-hint">scheduled tasks · workflows · research · evals · upkeep</span>
      <button class="ed-btn" id="logReload" title="refresh">↻</button>
    </div>
    <div class="logs-list" id="logList"><div class="empty-note">loading…</div></div>`;
  $("#logKind", body).onchange = e => { _logsKind = e.target.value; loadLogs(); };
  $("#logReload", body).onclick = () => loadLogs();
  loadLogs();
}
function _relTime(iso) {
  const t = new Date(iso).getTime(); if (!t) return "";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return Math.floor(s) + "s ago";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return new Date(iso).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}
async function loadLogs() {
  const list = $("#logList"); if (!list) return;
  let d; try { d = await api("/api/logs?limit=300" + (_logsKind ? "&kind=" + encodeURIComponent(_logsKind) : "")); }
  catch { list.innerHTML = `<div class="empty-note err">logs unavailable</div>`; return; }
  const sel = $("#logKind");                                   // refresh the filter options, keep the selection
  if (sel) { const cur = sel.value; sel.innerHTML = `<option value="">all activity</option>` + (d.kinds || []).map(k => `<option value="${escapeHtml(k)}">${escapeHtml(k)}</option>`).join(""); sel.value = cur; }
  const runs = d.runs || [];
  if (!runs.length) { list.innerHTML = `<div class="empty-note">No activity logged yet. Scheduled tasks, workflows, research, and upkeep show up here as they run.</div>`; return; }
  list.innerHTML = "";
  runs.forEach(r => {
    const row = document.createElement("div"); row.className = "log-row " + (r.status === "error" ? "err" : "ok");
    const dur = r.duration != null ? (r.duration < 60 ? r.duration + "s" : Math.round(r.duration / 60) + "m") : "";
    row.innerHTML = `<div class="lr-head"><span class="lr-dot"></span><span class="lr-kind">${escapeHtml(r.kind || "run")}</span>`
      + `<span class="lr-title">${escapeHtml(r.title || "")}</span><span class="lr-meta">${dur ? dur + " · " : ""}${escapeHtml(_relTime(r.ts))}</span></div>`;
    if (r.summary) {
      const head = $(".lr-head", row);
      const b = document.createElement("div"); b.className = "lr-body"; b.textContent = r.summary;   // textContent — the result is untrusted text
      row.appendChild(b);
      head.style.cursor = "pointer";
      head.onclick = () => row.classList.toggle("open");
    }
    list.appendChild(row);
  });
}

/* ====================================================================
   Semantic search — ask the corpus (memories + indexed docs), with scores
   and jump-to-source.
   ==================================================================== */
function openSearch() {
  const { body, reused } = createWindow({ id: "win-search", title: "Search — semantic", icon: "⌕", width: 600, height: 560 });
  if (reused) return;
  body.classList.add("ks-win");
  body.innerHTML = `
    <div class="ks-bar">
      <div class="ks-scope"><button data-scope="memory" class="on">Memories</button><button data-scope="docs">Documents</button><button data-scope="chats">Conversations</button></div>
      <input id="ksQ" placeholder="search by meaning, not keywords…" autocomplete="off">
      <button class="primary sm" id="ksGo">Search</button>
    </div>
    <div class="ks-note" id="ksNote"></div>
    <div class="ks-results" id="ksRes"></div>`;
  let scope = "memory";
  $$(".ks-scope button", body).forEach(b => b.onclick = () => { scope = b.dataset.scope; $$(".ks-scope button", body).forEach(x => x.classList.toggle("on", x === b)); });
  const res = $("#ksRes", body), note = $("#ksNote", body);
  async function go() {
    const q = $("#ksQ", body).value.trim(); if (!q) return;
    note.textContent = "searching…"; res.innerHTML = "";
    let r; try { r = await api("/api/brain/search", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ scope, query: q }) }); } catch { note.textContent = "search failed"; return; }
    const items = r.results || [];
    note.textContent = items.length ? `${items.length} result${items.length === 1 ? "" : "s"}` : "nothing close enough — try different words";
    items.forEach(it => {
      const card = document.createElement("div"); card.className = "ks-card";
      const score = typeof it.score === "number" ? it.score : 0;
      if (scope === "memory") {
        card.innerHTML = `<div class="ks-score">${score.toFixed(2)}</div><div class="ks-bd"><div class="ks-txt">${escapeHtml(it.text || "")}</div><div class="ks-meta">${escapeHtml(it.category || "")}${it.tags ? " · " + escapeHtml(it.tags) : ""}</div></div>`;
        card.onclick = () => openBrain("mem");
      } else if (scope === "chats") {
        card.innerHTML = `<div class="ks-score">${score.toFixed(2)}</div><div class="ks-bd"><div class="ks-src">${escapeHtml(it.title || "Untitled")} · ${escapeHtml(it.date || "")}</div><div class="ks-txt">${escapeHtml((it.snippet || "").slice(0, 320))}</div></div>`;
        card.title = "open this conversation";
        card.onclick = () => { openVoyage(it.id); sideShowChats(); };
      } else {
        card.innerHTML = `<div class="ks-score">${score.toFixed(2)}</div><div class="ks-bd"><div class="ks-src">${escapeHtml(it.name || "")}</div><div class="ks-txt">${escapeHtml((it.chunk || "").slice(0, 320))}</div></div>`;
        if (it.path) { card.title = "open " + escapeHtml(it.name || ""); card.onclick = () => openDocSource(it.path); }
      }
      res.appendChild(card);
    });
  }
  $("#ksGo", body).onclick = go;
  $("#ksQ", body).addEventListener("keydown", e => { if (e.key === "Enter") go(); });
  setTimeout(() => $("#ksQ", body).focus(), 50);
}
// indexed docs carry absolute paths; open in the editor when they live under the workspace fence
function openDocSource(absPath) {
  const i = (absPath || "").indexOf("/workspace/");
  if (i >= 0) openFileWindow(absPath.slice(i + "/workspace/".length));
  else toast(absPath, "info");
}

/* ====================================================================
   Notes — a tiny Kanban scratchpad (todo / doing / done), drag to move.
   ==================================================================== */
const NOTE_COLS = [["todo", "To do"], ["doing", "Doing"], ["done", "Done"]];
function openNotes() {
  const { body, reused } = createWindow({ id: "win-notes", title: "Notes — board", icon: "❏", width: 720, height: 540 });
  if (reused) return;
  body.classList.add("kb-win");
  body.innerHTML = `<div class="kb-board" id="kbBoard"></div>`;
  loadNotes();
}
async function loadNotes() {
  const board = $("#kbBoard"); if (!board) return;
  let data; try { data = await api("/api/notes"); } catch { return; }
  board.innerHTML = "";
  NOTE_COLS.forEach(([key, label]) => {
    const col = document.createElement("div"); col.className = "kb-col"; col.dataset.col = key;
    const cards = data[key] || [];
    col.innerHTML = `<div class="kb-col-h">${label}<span class="kb-count">${cards.length}</span></div><button class="kb-add">+ add</button><div class="kb-cards"></div>`;
    const cardsEl = $(".kb-cards", col);
    cards.forEach(c => cardsEl.appendChild(noteCard(c)));
    $(".kb-add", col).onclick = async () => {
      const t = await promptDialog("New card", { placeholder: "what needs doing?", okLabel: "Add" });
      if (!t) return;
      await _postJ("/api/notes", { text: t, col: key }); loadNotes();
    };
    col.addEventListener("dragover", e => { e.preventDefault(); col.classList.add("drop"); });
    col.addEventListener("dragleave", () => col.classList.remove("drop"));
    col.addEventListener("drop", async e => {
      e.preventDefault(); col.classList.remove("drop");
      const id = +e.dataTransfer.getData("text/plain");
      if (id) { await fetch("/api/notes/" + id, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ col: key }) }); loadNotes(); }
    });
    board.appendChild(col);
  });
}
function noteCard(c) {
  const el = document.createElement("div"); el.className = "kb-card"; el.draggable = true;
  el.innerHTML = `<div class="kb-txt">${escapeHtml(c.text)}</div><button class="kb-del" title="delete">✕</button>`;
  el.addEventListener("dragstart", e => { e.dataTransfer.setData("text/plain", c.id); el.classList.add("dragging"); });
  el.addEventListener("dragend", () => el.classList.remove("dragging"));
  $(".kb-txt", el).onclick = async () => {
    const t = await promptDialog("Edit card", { value: c.text, okLabel: "Save" });
    if (t === null) return;
    await fetch("/api/notes/" + c.id, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: t }) }); loadNotes();
  };
  $(".kb-del", el).onclick = async e => { e.stopPropagation(); await fetch("/api/notes/" + c.id, { method: "DELETE" }); loadNotes(); };
  return el;
}

/* ====================================================================
   Voice console — push-to-talk (MediaRecorder → STT) + speak text (TTS).
   Reuses the same faster-whisper / Piper stack as the Telegram channel.
   ==================================================================== */
let _vcRec = null, _vcChunks = [];
async function openVoice() {
  const { body, reused } = createWindow({ id: "win-voice", title: "Voice", icon: "🎙", width: 420, height: 460,
    onClose: () => { try { if (_vcRec && _vcRec.state === "recording") _vcRec.stop(); } catch {} _vcRec = null; } });
  if (reused) return;
  body.classList.add("vc-win");
  body.innerHTML = `
    <div class="vc-status" id="vcStatus">checking voice engines…</div>
    <div class="vc-voice" id="vcVoiceRow" style="display:none">
      <span class="vc-voice-lbl">🔊 voice</span>
      <select id="vcVoice" title="speaking voice"></select>
      <button class="ghost-btn sm" id="vcTest">▶ test</button>
    </div>
    <button class="vc-mic" id="vcMic" disabled><span class="vc-mic-ic">🎙</span><span id="vcMicLabel">…</span></button>
    <div class="vc-transcript" id="vcTx" contenteditable="true" data-ph="your words appear here — editable"></div>
    <div class="vc-actions">
      <button class="ghost-btn sm" id="vcSpeak">🔊 Speak this</button>
      <button class="primary sm" id="vcSend">↪ Send to chat</button>
    </div>
    <audio id="vcAudio" style="display:none"></audio>`;
  const st = $("#vcStatus", body), mic = $("#vcMic", body), micLabel = $("#vcMicLabel", body), tx = $("#vcTx", body), audio = $("#vcAudio", body);
  let s; try { s = await api("/api/voice/status"); } catch { s = {}; }
  st.innerHTML = `${s.stt ? "🎙 STT: " + escapeHtml(s.stt_model || "ready") : "🎙 STT: not installed"} · ${s.tts ? "🔊 TTS: " + escapeHtml(s.tts_voice || "ready") : "🔊 TTS: not installed"}`;
  mic.disabled = !s.stt;
  micLabel.textContent = s.stt ? "click to talk" : "speech-to-text unavailable";
  // voice picker (Kokoro) — change/audition the speaking voice live
  const vrow = $("#vcVoiceRow", body), vsel = $("#vcVoice", body);
  const speakSample = async () => {
    const name = await userGreetName();
    try {
      const r = await fetch("/api/voice/tts", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: `Hi${name ? " " + name : ""}, this is the ${vsel.value} voice.` }) });
      if (r.ok) { audio.src = URL.createObjectURL(await r.blob()); audio.play(); }
    } catch {}
  };
  const saveVoice = () => fetch("/api/voice/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ voice: vsel.value }) });
  try {
    const vv = await api("/api/voice/voices");
    if (vv.voices && vv.voices.length) {
      vsel.innerHTML = vv.voices.map(v => `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`).join("");
      if (vv.settings && vv.settings.voice) vsel.value = vv.settings.voice;
      vrow.style.display = "flex";
      vsel.onchange = async () => {
        await saveVoice();
        st.innerHTML = `${s.stt ? "🎙 STT: " + escapeHtml(s.stt_model || "ready") : "🎙 STT: not installed"} · 🔊 TTS: ${escapeHtml(vsel.value)}`;
        speakSample();
      };
      $("#vcTest", body).onclick = async () => { await saveVoice(); speakSample(); };
    }
  } catch {}
  const startRec = async () => {
    if (!navigator.mediaDevices) { toast("microphone unavailable", "err"); return; }
    let stream; try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); } catch { toast("mic permission denied", "err"); return; }
    _vcChunks = [];
    _vcRec = new MediaRecorder(stream);
    _vcRec.ondataavailable = e => { if (e.data.size) _vcChunks.push(e.data); };
    _vcRec.onstop = async () => {
      stream.getTracks().forEach(t => t.stop());
      mic.classList.remove("rec"); micLabel.textContent = "transcribing…";
      try {
        const r = await fetch("/api/voice/stt", { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: new Blob(_vcChunks, { type: "audio/webm" }) });
        const j = await r.json();
        if (j.text) tx.textContent = (tx.textContent ? tx.textContent.trim() + " " : "") + j.text;
        else toast("nothing heard", "info");
      } catch { toast("transcription failed", "err"); }
      micLabel.textContent = "click to talk";
    };
    _vcRec.start(); mic.classList.add("rec"); micLabel.textContent = "● recording — click to stop";
  };
  mic.onclick = () => { if (_vcRec && _vcRec.state === "recording") _vcRec.stop(); else startRec(); };
  $("#vcSpeak", body).onclick = async () => {
    const text = (tx.textContent || "").trim(); if (!text) return;
    if (!s.tts) { toast("TTS not available", "err"); return; }
    try {
      const r = await fetch("/api/voice/tts", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }) });
      if (!r.ok) { toast("TTS failed", "err"); return; }
      audio.src = URL.createObjectURL(await r.blob()); audio.play();
    } catch { toast("TTS failed", "err"); }
  };
  $("#vcSend", body).onclick = () => {
    const text = (tx.textContent || "").trim(); if (!text) return;
    const input = $("#input"); input.value = text; input.dispatchEvent(new Event("input")); input.focus();
    tx.textContent = ""; toast("dropped into the composer", "info");
  };
}

/* ====================================================================
   Workflows — visual, branching recipes drawn on a Drawflow canvas.
   Nodes: start · tool · instruction · delegate · decision · end.
   Decision nodes route execution down a "yes"/"no" edge (rule | model | delegate).
   The clean graph model {nodes,edges} is the source of truth; we build the canvas
   from it on open and read it back from Drawflow's export on save.
   ==================================================================== */
let _wfTools = null;                          // cached enabled-tool list for tool nodes
async function wfLoadTools() {
  if (_wfTools) return _wfTools;
  try { _wfTools = (await api("/api/tools")).filter(t => t.enabled && !t.name.startsWith("mcp__")); } catch { _wfTools = []; }
  return _wfTools;
}
const WF_PORTS = { start: [0, 1], end: [1, 0], decision: [1, 2], tool: [1, 1], instruction: [1, 1], delegate: [1, 1] };
function wfToolOptions(sel) {
  return (_wfTools || []).map(t => `<option value="${t.name}"${t.name === sel ? " selected" : ""}>${t.name}</option>`).join("");
}
function wfNodeData(n) {
  if (n.type === "tool") return { tool: n.tool || ((_wfTools && _wfTools[0] && _wfTools[0].name) || ""), args: JSON.stringify(n.args || {}) };
  if (n.type === "instruction") return { text: n.text || "" };
  if (n.type === "delegate") return { text: n.text || "", role: n.role || "default" };
  if (n.type === "decision") return { mode: n.mode || "model", question: n.question || "", ruleOp: n.ruleOp || "contains", ruleValue: n.ruleValue || "", role: n.role || "default" };
  return {};
}
function wfNodeHtml(type, data) {
  if (type === "start") return `<div class="wfn wfn-start"><b>▶ Start</b></div>`;
  if (type === "end") return `<div class="wfn wfn-end"><b>■ End</b></div>`;
  if (type === "tool") return `<div class="wfn wfn-tool"><b>🔧 Tool</b><select class="wfn-f wfn-tool-sel">${wfToolOptions(data.tool)}</select><div class="wfn-form"></div></div>`;
  if (type === "instruction") return `<div class="wfn wfn-instruction"><b>✎ Instruction</b><textarea df-text class="wfn-f" placeholder="what should the agent do? (it may use any tool)"></textarea></div>`;
  if (type === "delegate") return `<div class="wfn wfn-delegate"><b>↗ Delegate</b><textarea df-text class="wfn-f" placeholder="task for Claude / cloud"></textarea><select df-role class="wfn-f"><option value="default">default</option><option value="improve">improve</option></select></div>`;
  if (type === "decision") return `<div class="wfn wfn-decision"><b>◆ Decision</b><select df-mode class="wfn-f"><option value="model">model judges</option><option value="rule">rule on prev output</option><option value="delegate">delegate judges</option></select><textarea df-question class="wfn-f" placeholder="yes/no question (model & delegate)"></textarea><div class="wfn-rule"><select df-ruleOp class="wfn-f"><option value="contains">contains</option><option value="equals">equals</option><option value="matches">matches</option><option value="gt">&gt;</option><option value="lt">&lt;</option></select><input df-ruleValue class="wfn-f" placeholder="value"></div><div class="wfn-branches"><span>▸ yes</span><span>▸ no</span></div></div>`;
  return `<div class="wfn"><b>${type}</b></div>`;
}
// ---- tool nodes get a real form (one typed field per parameter), not a JSON box ----
const _WF_LONG_STR = /content|text|body|code|message|prompt|command|instruction/i;
// params that should be a searchable PICKER, keyed "tool.param" → which option source to search
const WF_PICKERS = {
  "load_skill.name": "skills", "run_workflow.name": "workflows",
  "read_file.path": "files", "write_file.path": "files", "edit_file.path": "files",
  "list_files.path": "dirs", "index_docs.folder": "dirs", "make_folder.path": "dirs",
};
// picker params that allow choosing SEVERAL at once (stored comma-joined; the tool accepts a list)
const WF_MULTI = new Set(["load_skill.name", "run_workflow.name"]);
let _wfEnumCache = {}, _wfFilesCache = null;
async function wfFiles() {
  if (!_wfFilesCache) { try { _wfFilesCache = await api("/api/files/all"); } catch { _wfFilesCache = { files: [], dirs: [] }; } }
  return _wfFilesCache;
}
async function wfEnumOptions(src) {                 // src: skills | workflows | files | dirs
  if (_wfEnumCache[src]) return _wfEnumCache[src];
  let opts = [];
  try {
    if (src === "skills") opts = (await api("/api/skills")).map(s => s.name);
    else if (src === "workflows") opts = (await api("/api/workflows")).map(w => w.name);
    else if (src === "files") opts = (await wfFiles()).files || [];
    else if (src === "dirs") opts = (await wfFiles()).dirs || [];
  } catch { opts = []; }
  _wfEnumCache[src] = opts;
  return opts;
}
// turn each .wfn-combo input into a type-to-search autocomplete (still accepts free text)
function wfWireCombos(form) {
  form.querySelectorAll(".wfn-combo").forEach(async inp => {
    const box = inp.parentElement.querySelector(".wfn-acx");
    const opts = await wfEnumOptions(inp.dataset.enum);
    let hi = -1, shown = [];
    const render = () => {
      const q = inp.value.trim().toLowerCase();
      shown = (q ? opts.filter(o => o.toLowerCase().includes(q)) : opts).slice(0, 8);
      if (!shown.length) { box.style.display = "none"; return; }
      box.innerHTML = shown.map((o, i) => `<div class="wfn-ac${i === hi ? " hi" : ""}" data-i="${i}">${escapeHtml(o)}</div>`).join("");
      box.style.display = "block";
    };
    const pick = o => { inp.value = o; box.style.display = "none"; inp.dispatchEvent(new Event("change", { bubbles: true })); };
    inp.addEventListener("focus", () => { hi = -1; render(); });
    inp.addEventListener("input", () => { hi = -1; render(); });
    inp.addEventListener("blur", () => setTimeout(() => { box.style.display = "none"; }, 150));
    inp.addEventListener("keydown", e => {
      if (box.style.display === "none") return;
      if (e.key === "ArrowDown") { e.preventDefault(); hi = Math.min(hi + 1, shown.length - 1); render(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); hi = Math.max(hi - 1, 0); render(); }
      else if (e.key === "Enter" && hi >= 0) { e.preventDefault(); pick(shown[hi]); }
      else if (e.key === "Escape") box.style.display = "none";
    });
    box.addEventListener("mousedown", e => { const it = e.target.closest(".wfn-ac"); if (it) { e.preventDefault(); pick(shown[+it.dataset.i]); } });
  });
}
// multi-select chips combo: choose several skills/workflows; value is kept comma-joined in a hidden field
function wfWireMulti(form) {
  form.querySelectorAll(".wfn-multi").forEach(async wrap => {
    const hidden = wrap.querySelector("input[data-arg]"), chipsEl = wrap.querySelector(".wfn-chips");
    const inp = wrap.querySelector(".wfn-msearch"), box = wrap.querySelector(".wfn-acx");
    const opts = await wfEnumOptions(wrap.dataset.enum);
    const get = () => hidden.value.split(",").map(s => s.trim()).filter(Boolean);
    let hi = -1, shown = [];
    const renderChips = arr => {
      chipsEl.innerHTML = "";
      arr.forEach(name => {
        const c = document.createElement("span"); c.className = "wfn-chip"; c.textContent = name;
        const x = document.createElement("button"); x.type = "button"; x.className = "wfn-chip-x"; x.textContent = "✕";
        x.onclick = () => set(get().filter(n => n !== name));
        c.appendChild(x); chipsEl.appendChild(c);
      });
    };
    const set = arr => {
      hidden.value = [...new Set(arr)].join(", ");
      renderChips(get());
      hidden.dispatchEvent(new Event("change", { bubbles: true }));   // sync into the node's data
    };
    const render = () => {
      const cur = get(), q = inp.value.trim().toLowerCase();
      shown = opts.filter(o => !cur.includes(o) && (!q || o.toLowerCase().includes(q))).slice(0, 8);
      if (!shown.length) { box.style.display = "none"; return; }
      box.innerHTML = shown.map((o, i) => `<div class="wfn-ac${i === hi ? " hi" : ""}" data-i="${i}">${escapeHtml(o)}</div>`).join("");
      box.style.display = "block";
    };
    const add = o => { o = o.trim(); if (o) set([...get(), o]); inp.value = ""; hi = -1; render(); };
    renderChips(get());
    inp.addEventListener("focus", () => { hi = -1; render(); });
    inp.addEventListener("input", () => { hi = -1; render(); });
    inp.addEventListener("blur", () => setTimeout(() => { box.style.display = "none"; }, 150));
    inp.addEventListener("keydown", e => {
      if (e.key === "Enter") { e.preventDefault(); add(hi >= 0 ? shown[hi] : inp.value); }
      else if (e.key === "ArrowDown") { e.preventDefault(); hi = Math.min(hi + 1, shown.length - 1); render(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); hi = Math.max(hi - 1, 0); render(); }
      else if (e.key === "Escape") box.style.display = "none";
      else if (e.key === "Backspace" && !inp.value) { const c = get(); if (c.length) set(c.slice(0, -1)); }
    });
    box.addEventListener("mousedown", e => { const it = e.target.closest(".wfn-ac"); if (it) { e.preventDefault(); add(shown[+it.dataset.i]); } });
  });
}
function wfBuildToolForm(form, toolName, args) {
  const tool = (_wfTools || []).find(t => t.name === toolName);
  const params = (tool && tool.params) || [];
  if (!params.length) { form.innerHTML = `<div class="wfn-noargs">no arguments</div>`; return; }
  form.innerHTML = params.map(p => {
    const v = args[p.name], dt = p.type || "string";
    const lab = `<label class="wfn-lab" title="${escapeHtml(p.description || "")}">${escapeHtml(p.name)}${p.required ? '<i class="wfn-req">*</i>' : ""}</label>`;
    const picker = WF_PICKERS[toolName + "." + p.name];
    let field;
    if (picker && WF_MULTI.has(toolName + "." + p.name))   // pick SEVERAL (chips); stored comma-joined
      field = `<div class="wfn-multi" data-enum="${picker}"><input type="hidden" class="wfn-fld" data-arg="${escapeHtml(p.name)}" data-type="string" value="${v != null ? escapeHtml(String(v)) : ""}"><div class="wfn-chips"></div><div class="wfn-combo-wrap"><input class="wfn-msearch" autocomplete="off" placeholder="add… (choose several)"><div class="wfn-acx" style="display:none"></div></div></div>`;
    else if (picker)              // single searchable combo (free text ok too)
      field = `<div class="wfn-combo-wrap"><input class="wfn-fld wfn-combo" data-arg="${escapeHtml(p.name)}" data-type="string" data-enum="${picker}" autocomplete="off" placeholder="type to search…" value="${v != null ? escapeHtml(String(v)) : ""}"><div class="wfn-acx" style="display:none"></div></div>`;
    else if (dt === "boolean")
      field = `<select class="wfn-fld" data-arg="${escapeHtml(p.name)}" data-type="boolean"><option value="">—</option><option value="true"${v === true ? " selected" : ""}>true</option><option value="false"${v === false ? " selected" : ""}>false</option></select>`;
    else if (dt === "integer" || dt === "number")
      field = `<input class="wfn-fld" type="number" data-arg="${escapeHtml(p.name)}" data-type="${dt}" value="${v != null ? escapeHtml(String(v)) : ""}">`;
    else if (dt === "array" || dt === "object")
      field = `<textarea class="wfn-fld wfn-json" data-arg="${escapeHtml(p.name)}" data-type="${dt}" placeholder="JSON">${v != null ? escapeHtml(JSON.stringify(v)) : ""}</textarea>`;
    else if (_WF_LONG_STR.test(p.name))
      field = `<textarea class="wfn-fld" data-arg="${escapeHtml(p.name)}" data-type="string">${v != null ? escapeHtml(String(v)) : ""}</textarea>`;
    else
      field = `<input class="wfn-fld" data-arg="${escapeHtml(p.name)}" data-type="string" value="${v != null ? escapeHtml(String(v)) : ""}">`;
    return `<div class="wfn-row">${lab}${field}</div>`;
  }).join("");
}
function wfReadToolForm(form) {
  const args = {};
  form.querySelectorAll("[data-arg]").forEach(f => {
    const name = f.dataset.arg, type = f.dataset.type, raw = (f.value != null ? f.value : "").trim();
    f.classList.remove("wfn-bad");
    if (type === "boolean") { if (raw === "true") args[name] = true; else if (raw === "false") args[name] = false; }
    else if (type === "integer") { if (raw !== "") { const n = parseInt(raw, 10); if (!isNaN(n)) args[name] = n; } }
    else if (type === "number") { if (raw !== "") { const n = parseFloat(raw); if (!isNaN(n)) args[name] = n; } }
    else if (type === "array" || type === "object") { if (raw !== "") { try { args[name] = JSON.parse(raw); } catch { f.classList.add("wfn-bad"); } } }
    else if (raw !== "") args[name] = raw;
  });
  return args;
}
// wire a tool node's DOM: populate the form for the chosen tool, sync edits into Drawflow's node data
function wfWireTool(editor, dfId, toolName, argsObj) {
  const el = document.getElementById("node-" + dfId); if (!el) return;
  const sel = el.querySelector(".wfn-tool-sel"), form = el.querySelector(".wfn-form");
  if (!sel || !form) return;
  const state = { tool: sel.value || toolName, args: { ...(argsObj || {}) } };
  const sync = () => editor.updateNodeDataFromId(dfId, { tool: state.tool, args: JSON.stringify(state.args) });
  const bindFields = () => form.querySelectorAll("[data-arg]").forEach(f => {
    const handler = () => { state.args = wfReadToolForm(form); sync(); };
    f.addEventListener("change", handler);                       // selects + combo picks (synthetic) + commit
    if (f.tagName !== "SELECT") f.addEventListener("input", handler);   // live typing
  });
  const build = () => { wfBuildToolForm(form, state.tool, state.args); bindFields(); wfWireCombos(form); wfWireMulti(form); sync(); };
  build();
  sel.onchange = () => { state.tool = sel.value; state.args = {}; build(); };
}
const wfNodeLabel = n => n.type === "tool" ? "🔧 " + (n.tool || "tool")
  : n.type === "instruction" ? (n.text || "instruction").slice(0, 54)
  : n.type === "delegate" ? "↗ " + (n.text || "delegate").slice(0, 48)
  : n.type === "decision" ? "◆ " + (n.question || n.mode || "decision").slice(0, 48)
  : n.type;

function openWorkflows() {
  const { body, reused } = createWindow({ id: "win-workflows", title: "Workflows", icon: "⚙", width: 940, height: 660, restoreKey: "workflows" });
  if (reused) return;
  body.classList.add("wf-win");
  wfRenderList(body);
}
async function wfRenderList(body) {
  body.innerHTML = `
    <div class="wf-head"><h3>Workflows</h3><span class="fe-spacer"></span><button class="primary sm" id="wfNew">+ New workflow</button></div>
    <div class="wf-list" id="wfList"><div class="empty-note">loading…</div></div>`;
  $("#wfNew", body).onclick = () => wfRenderEditor(body, null);
  let wfs; try { wfs = await api("/api/workflows"); } catch { return; }
  const list = $("#wfList", body);
  if (!wfs.length) { list.innerHTML = `<div class="empty-note">No workflows yet. Build one on the canvas — wire up tool, instruction, delegate and decision nodes, then run it on demand or on a schedule.</div>`; return; }
  list.innerHTML = "";
  wfs.forEach(w => {
    const nodes = (w.graph && w.graph.nodes) || [];
    const acts = nodes.filter(n => n.type !== "start" && n.type !== "end");
    const hasDec = nodes.some(n => n.type === "decision");
    const sched = w.schedule ? `<span class="wf-sched" title="scheduled">⏱ ${escapeHtml(w.schedule.cron)}${w.schedule.enabled ? "" : " · off"}</span>` : "";
    const el = document.createElement("div"); el.className = "wf-card"; el.dataset.wid = w.id;
    el.innerHTML = `
      <div class="wf-card-main">
        <div class="wf-card-name">${escapeHtml(w.name)} ${sched}</div>
        ${w.description ? `<div class="wf-card-desc">${escapeHtml(w.description)}</div>` : ""}
        <div class="wf-card-meta">${acts.length} node${acts.length === 1 ? "" : "s"}${hasDec ? " · ◆ branching" : ""}</div>
      </div>
      <div class="wf-card-actions">
        <button class="primary sm wf-run">▶ Run</button>
        <button class="ed-btn wf-edit">Edit</button>
        <button class="ed-btn wf-sched-btn" title="schedule">⏱</button>
        <button class="ed-btn wf-trig-btn" title="triggers">⚡</button>
        <button class="ed-btn wf-runs" title="run history">⟲</button>
        <button class="ed-btn wf-del" title="delete">✕</button>
      </div>`;
    $(".wf-run", el).onclick = () => wfRenderRun(body, w);
    $(".wf-edit", el).onclick = () => wfRenderEditor(body, w);
    $(".wf-sched-btn", el).onclick = () => wfSchedule(body, w);
    $(".wf-trig-btn", el).onclick = () => wfTriggers(body, w);
    $(".wf-runs", el).onclick = () => wfRenderRuns(body, w);
    $(".wf-del", el).onclick = async () => { if (!await confirmAction("Delete workflow?", `“${w.name}” and its schedule will be removed.`)) return; await fetch("/api/workflows/" + w.id, { method: "DELETE" }); wfRenderList(body); };
    list.appendChild(el);
  });
  wfMarkLive(body);
}
async function wfMarkLive(body) {
  let live; try { live = (await api("/api/workflows/live")).running || []; } catch { return; }
  live.filter(r => r.status === "running").forEach(r => {
    const card = body.querySelector(`.wf-card[data-wid="${r.workflow_id}"]`);
    if (!card) return;
    const name = $(".wf-card-name", card);
    if (name && !$(".wf-running", name)) {
      const b = document.createElement("span");
      b.className = "wf-running"; b.title = "running now — open Run to reconnect to its live state";
      b.textContent = " ● running"; name.appendChild(b);
    }
    const btn = $(".wf-run", card); if (btn) btn.textContent = "⊙ View run";
  });
}
async function wfSchedule(body, w) {
  const cron = await promptDialog("Schedule workflow", { value: w.schedule ? w.schedule.cron : "",
    message: "Cron · min hr day mon wkday — leave blank to unschedule", okLabel: "Save" });
  if (cron === null) return;
  await _postJ("/api/workflows/" + w.id + "/schedule", { cron });
  wfRenderList(body);
}
async function wfTriggers(body, w) {
  body.innerHTML = `<div class="wf-head"><button class="ed-btn" id="wfBack">←</button><h3>Triggers · ${escapeHtml(w.name)}</h3></div>
    <div class="wf-trig-wrap"><div class="wf-trig" id="wfTrig"></div>
      <div class="wf-trig-add">
        <select id="wfTrigType" class="wfn-fld"><option value="watch">File / folder watch</option><option value="webhook">Webhook (HTTP)</option><option value="keyword">Chat keyword</option><option value="chain">After another workflow</option></select>
        <button class="ed-btn" id="wfTrigAdd">+ Add</button><span class="fe-spacer"></span>
        <span class="acct-msg" id="wfTrigMsg"></span><button class="primary sm" id="wfTrigSave">Save</button>
      </div></div>`;
  $("#wfBack", body).onclick = () => wfRenderList(body);
  let trg; try { trg = (await api("/api/workflows/" + w.id + "/triggers")).triggers || []; } catch { trg = []; }
  let allwf = []; try { allwf = await api("/api/workflows"); } catch {}
  const host = $("#wfTrig", body), TYPES = { watch: "File / folder watch", webhook: "Webhook", keyword: "Chat keyword", chain: "After another workflow" };
  const render = () => {
    if (!trg.length) { host.innerHTML = `<div class="empty-note">No event triggers yet. Manual ▶ Run and the schedule (⏱) always work — add event triggers below.</div>`; return; }
    host.innerHTML = "";
    trg.forEach((t, i) => {
      const row = document.createElement("div"); row.className = "wf-trig-row";
      let fields = "";
      if (t.type === "watch") fields = `<label class="tf-lab">Folder <input class="wfn-fld tf" data-k="folder" value="${escapeHtml(t.folder || "")}" placeholder="brain/inbox"></label><div class="tf-hint">runs when files are added/changed under workspace/&lt;folder&gt;</div>`;
      else if (t.type === "webhook") { const url = t.token ? `${location.origin}/api/workflows/${w.id}/webhook/${t.token}` : "(Save to generate the URL)"; fields = `<div class="tf-url">POST <code>${escapeHtml(url)}</code>${t.token ? ` <button class="ed-btn tf-copy" type="button">copy</button>` : ""}</div>`; }
      else if (t.type === "keyword") fields = `<label class="tf-lab">Phrase <input class="wfn-fld tf" data-k="pattern" value="${escapeHtml(t.pattern || "")}" placeholder="daily brief"></label><label class="tf-lab">Channel <select class="wfn-fld tf" data-k="channel">${["any", "web", "telegram"].map(c => `<option value="${c}"${t.channel === c ? " selected" : ""}>${c}</option>`).join("")}</select></label>`;
      else if (t.type === "chain") fields = `<label class="tf-lab">After <select class="wfn-fld tf" data-k="after">${allwf.filter(x => x.id !== w.id).map(x => `<option value="${x.id}"${t.after === x.id ? " selected" : ""}>${escapeHtml(x.name)}</option>`).join("") || `<option value="">(no other workflows)</option>`}</select></label><label class="tf-lab">When <select class="wfn-fld tf" data-k="on"><option value="success"${t.on === "success" ? " selected" : ""}>it succeeds</option><option value="any"${t.on === "any" ? " selected" : ""}>it finishes</option></select></label>`;
      row.innerHTML = `<div class="wf-trig-h"><label class="tf-en"><input type="checkbox" class="tf" data-k="enabled"${t.enabled !== false ? " checked" : ""}> <b>${TYPES[t.type]}</b></label><span class="fe-spacer"></span><button class="ed-btn tf-del" type="button">✕</button></div><div class="wf-trig-f">${fields}</div>`;
      $$(".tf", row).forEach(f => {
        const k = f.dataset.k, upd = () => { t[k] = f.type === "checkbox" ? f.checked : (k === "after" ? parseInt(f.value, 10) : f.value); };
        f.addEventListener("change", upd); if (f.tagName === "INPUT" && f.type !== "checkbox") f.addEventListener("input", upd);
      });
      const del = $(".tf-del", row); if (del) del.onclick = () => { trg.splice(i, 1); render(); };
      const cp = $(".tf-copy", row); if (cp) cp.onclick = () => { try { navigator.clipboard.writeText(`${location.origin}/api/workflows/${w.id}/webhook/${t.token}`); cp.textContent = "copied ✓"; } catch {} };
      host.appendChild(row);
    });
  };
  $("#wfTrigAdd", body).onclick = () => {
    const defs = { watch: { type: "watch", enabled: true, folder: "" }, webhook: { type: "webhook", enabled: true },
      keyword: { type: "keyword", enabled: true, pattern: "", channel: "any" },
      chain: { type: "chain", enabled: true, after: (allwf.find(x => x.id !== w.id) || {}).id, on: "success" } };
    trg.push(defs[$("#wfTrigType", body).value]); render();
  };
  $("#wfTrigSave", body).onclick = async () => {
    const msg = $("#wfTrigMsg", body); if (msg) { msg.textContent = "saving…"; msg.className = "acct-msg"; }
    try {
      const r = await (await fetch("/api/workflows/" + w.id + "/triggers", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ triggers: trg }) })).json();
      trg = r.triggers || []; render();
      if (msg) { msg.textContent = "saved ✓"; msg.className = "acct-msg ok"; }
    } catch { if (msg) { msg.textContent = "save failed"; msg.className = "acct-msg err"; } }
  };
  render();
}
async function wfRenderEditor(body, w) {
  await wfLoadTools();
  _wfEnumCache = {}; _wfFilesCache = null;        // refresh skill/workflow/file pickers each time the editor opens
  body.innerHTML = `
    <div class="wf-head"><button class="ed-btn" id="wfBack">←</button>
      <input id="wfName" class="wf-name-in" placeholder="workflow name" value="${w ? escapeHtml(w.name) : ""}">
      <input id="wfDesc" class="wf-desc-in" placeholder="description (optional)" value="${w ? escapeHtml(w.description || "") : ""}">
      <span class="fe-spacer"></span>
      <button class="primary sm" id="wfSave">${w ? "Save" : "Create"}</button></div>
    <div class="wf-palette">
      <span class="wf-pal-lbl">add:</span>
      <button class="ed-btn" data-add="tool">🔧 Tool</button>
      <button class="ed-btn" data-add="instruction">✎ Instruction</button>
      <button class="ed-btn" data-add="delegate">↗ Delegate</button>
      <button class="ed-btn" data-add="decision">◆ Decision</button>
      <button class="ed-btn" data-add="end">■ End</button>
      <span class="fe-spacer"></span>
      <button class="ed-btn" id="wfZoomOut">−</button><button class="ed-btn" id="wfZoomIn">+</button>
      <span class="wf-hint">drag from a node's right dot to another's left dot to connect</span>
    </div>
    <div class="wf-canvas" id="wfCanvas"></div>`;
  $("#wfBack", body).onclick = () => wfRenderList(body);
  const editor = new Drawflow($("#wfCanvas", body));
  editor.reroute = true;
  editor.start();
  let addN = 0;
  const place = n => { addN++; return [80 + (addN % 6) * 46, 70 + (addN % 6) * 40]; };
  const addNode = (type, x, y, n) => {
    const [px, py] = (x != null) ? [x, y] : place();
    const [ins, outs] = WF_PORTS[type];
    const cfg = n || { type };
    const data = wfNodeData(cfg);
    const dfId = editor.addNode(type, ins, outs, px, py, "wf-dfn wf-dfn-" + type, data, wfNodeHtml(type, data));
    if (type === "tool") wfWireTool(editor, dfId, data.tool, cfg.args || {});
    return dfId;
  };
  // build from the saved graph, or seed a fresh start node
  if (w && w.graph && w.graph.nodes && w.graph.nodes.length) {
    const map = {};
    w.graph.nodes.forEach(n => { map[n.id] = addNode(n.type, n.x || 60, n.y || 60, n); });
    (w.graph.edges || []).forEach(e => {
      const from = map[e.from], to = map[e.to]; if (from == null || to == null) return;
      const src = w.graph.nodes.find(x => x.id === e.from) || {};
      const port = src.type === "decision" ? (e.branch === "no" ? "output_2" : "output_1") : "output_1";
      try { editor.addConnection(from, to, port, "input_1"); } catch {}
    });
  } else {
    addNode("start", 40, 80, { type: "start" });
  }
  $$(".wf-palette [data-add]", body).forEach(b => b.onclick = () => addNode(b.dataset.add));
  $("#wfZoomIn", body).onclick = () => editor.zoom_in();
  $("#wfZoomOut", body).onclick = () => editor.zoom_out();
  $("#wfSave", body).onclick = async () => {
    const name = $("#wfName").value.trim(); if (!name) { toast("name is required", "err"); return; }
    const { graph, error } = wfReadCanvas(editor);
    if (error) { toast(error, "err"); return; }
    const payload = { name, description: $("#wfDesc").value.trim(), graph };
    if (w) await fetch("/api/workflows/" + w.id, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    else await _postJ("/api/workflows", payload);
    wfRenderList(body);
  };
}
function wfReadCanvas(editor) {
  const data = editor.export().drawflow.Home.data;
  const nodes = [], edges = [];
  let error = null;
  for (const k in data) {
    const nd = data[k], id = +k, d = nd.data || {};
    const node = { id, type: nd.name, x: Math.round(nd.pos_x), y: Math.round(nd.pos_y) };
    if (nd.name === "tool") {
      node.tool = d.tool || "";
      try { node.args = (d.args || "").trim() ? JSON.parse(d.args) : {}; }
      catch { error = `invalid JSON in tool node “${node.tool || id}”`; node.args = {}; }
    } else if (nd.name === "instruction") node.text = d.text || "";
    else if (nd.name === "delegate") { node.text = d.text || ""; node.role = d.role || "default"; }
    else if (nd.name === "decision") { node.mode = d.mode || "model"; node.question = d.question || ""; node.ruleOp = d.ruleOp || "contains"; node.ruleValue = d.ruleValue || ""; node.role = d.role || "default"; }
    nodes.push(node);
    const outs = nd.outputs || {};
    for (const oname in outs) (outs[oname].connections || []).forEach(c =>
      edges.push({ from: id, to: +c.node, branch: nd.name === "decision" ? (oname === "output_2" ? "no" : "yes") : null }));
  }
  return { graph: { nodes, edges }, error };
}
async function wfRenderRun(body, w) {
  body.innerHTML = `
    <div class="wf-head"><button class="ed-btn" id="wfBack">←</button><h3>Run · ${escapeHtml(w.name)}</h3><span class="fe-spacer"></span><span class="wf-run-status running" id="wfRunStatus">running…</span></div>
    <div class="wf-run-steps" id="wfRunSteps"></div>`;
  $("#wfBack", body).onclick = () => wfRenderList(body);
  const host = $("#wfRunSteps", body), status = $("#wfRunStatus", body), rows = {};
  const addRow = (id, label) => {
    const r = document.createElement("div"); r.className = "wf-run-step running";
    r.innerHTML = `<div class="wf-rs-h"><span class="wf-rs-ic">◌</span><span class="wf-rs-label">${escapeHtml(label || "")}</span><span class="wf-rs-branch"></span></div><div class="wf-rs-tools"></div><div class="wf-rs-out"></div>`;
    host.appendChild(r); host.scrollTop = host.scrollHeight; rows[id] = r; return r;
  };
  // Reconnect to an already-running run (e.g. after a browser refresh) rather than starting a new one.
  let liveState = null;
  try { liveState = ((await api("/api/workflows/live")).running || []).find(x => x.workflow_id === w.id); } catch {}
  if (liveState) return wfReconnectRun(body, w, host, status, rows, addRow, liveState);
  try {
    const resp = await fetch("/api/workflows/" + w.id + "/run", { method: "POST" });
    const reader = resp.body.getReader(), dec = new TextDecoder(); let buf = "";
    while (true) {
      const { value, done } = await reader.read(); if (done) break;
      buf += dec.decode(value, { stream: true }); let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const line = buf.slice(0, i); buf = buf.slice(i + 2);
        if (!line.startsWith("data: ")) continue;
        let ev; try { ev = JSON.parse(line.slice(6)); } catch { continue; }
        if (ev.event === "node_start") addRow(ev.id, ev.label);
        else if (ev.event === "tool" && rows[ev.id] && ev.text) { const t = document.createElement("div"); t.className = "wf-rs-tool"; t.textContent = ev.text; $(".wf-rs-tools", rows[ev.id]).appendChild(t); }
        else if (ev.event === "node_end" && rows[ev.id]) {
          const r = rows[ev.id]; r.className = "wf-run-step " + (ev.ok ? "ok" : "fail");
          $(".wf-rs-ic", r).textContent = ev.ok ? "✓" : "✗";
          if (ev.branch) $(".wf-rs-branch", r).textContent = "→ " + ev.branch;
          $(".wf-rs-out", r).textContent = (ev.output || "").trim();
        } else if (ev.event === "done") { status.textContent = ev.run ? ev.run.summary : "done"; status.className = "wf-run-status " + (ev.status === "ok" ? "ok" : "fail"); }
        else if (ev.event === "error") { status.textContent = "error: " + (ev.message || ""); status.className = "wf-run-status fail"; }
      }
      host.scrollTop = host.scrollHeight;
    }
  } catch { status.textContent = "run failed"; status.className = "wf-run-status fail"; }
}
async function wfReconnectRun(body, w, host, status, rows, addRow, initial) {
  // Re-attach to a run already in progress on the server: render its accumulated steps and poll
  // the live registry until it finishes. Survives browser refreshes and works for scheduled runs.
  status.textContent = "reconnected · running…"; status.className = "wf-run-status running";
  let stop = false;
  const back = $("#wfBack", body), orig = back.onclick;
  back.onclick = () => { stop = true; if (orig) orig(); };
  const paint = (st) => {
    (st.steps || []).forEach(s => {
      const r = rows[s.id] || addRow(s.id, s.label);
      r.className = "wf-run-step " + (s.ok ? "ok" : "fail");
      $(".wf-rs-ic", r).textContent = s.ok ? "✓" : "✗";
      if (s.branch) $(".wf-rs-branch", r).textContent = "→ " + s.branch;
      $(".wf-rs-out", r).textContent = (s.output || "").trim();
    });
    if (st.current && !rows[st.current.id]) addRow(st.current.id, st.current.label);  // node in flight
    host.scrollTop = host.scrollHeight;
  };
  let st = initial;
  while (!stop) {
    paint(st);
    if (st.status !== "running") {
      status.textContent = st.summary || st.status;
      status.className = "wf-run-status " + (st.status === "ok" ? "ok" : "fail");
      return;
    }
    await new Promise(r => setTimeout(r, 1500));
    let arr; try { arr = (await api("/api/workflows/live")).running || []; } catch { return; }
    const next = arr.find(x => x.workflow_id === w.id);
    if (!next) { status.textContent = "finished"; status.className = "wf-run-status ok"; return; }
    st = next;
  }
}
async function wfRenderRuns(body, w) {
  body.innerHTML = `<div class="wf-head"><button class="ed-btn" id="wfBack">←</button><h3>History · ${escapeHtml(w.name)}</h3></div><div class="wf-runs-list" id="wfRunsList"><div class="empty-note">loading…</div></div>`;
  $("#wfBack", body).onclick = () => wfRenderList(body);
  let runs; try { runs = await api("/api/workflows/" + w.id + "/runs"); } catch { return; }
  const list = $("#wfRunsList", body);
  if (!runs.length) { list.innerHTML = `<div class="empty-note">No runs yet — hit ▶ Run.</div>`; return; }
  list.innerHTML = "";
  runs.forEach(r => {
    const el = document.createElement("div"); el.className = "wf-run-rec " + (r.status === "ok" ? "ok" : "fail");
    el.innerHTML = `<div class="wf-rr-h"><span class="wf-rr-status">${r.status === "ok" ? "✓" : "✗"}</span><span class="wf-rr-sum">${escapeHtml(r.summary || "")}</span><span class="fe-spacer"></span><span class="wf-rr-meta">${escapeHtml((r.ts || "").slice(0, 16).replace("T", " "))} · ${escapeHtml(r.trigger || "")}</span></div><div class="wf-rr-steps"></div>`;
    const det = $(".wf-rr-steps", el);
    (r.steps || []).forEach(s => { const d = document.createElement("div"); d.className = "wf-rr-step"; d.innerHTML = `<div class="wf-rr-sl">${s.ok ? "✓" : "✗"} ${escapeHtml(s.label || "")}${s.branch ? ` <span class="wf-rr-br">→ ${escapeHtml(s.branch)}</span>` : ""}</div><div class="wf-rr-out">${escapeHtml((s.output || "").trim().slice(0, 400))}</div>`; det.appendChild(d); });
    el.querySelector(".wf-rr-h").onclick = () => el.classList.toggle("open");
    list.appendChild(el);
  });
}

/* ---------------- background-jobs running indicator (global, polled) ---------------- */
let _jobsLast = [], _jobsTimer = null;
function renderJobsPop(pop) {
  if (!_jobsLast.length) { pop.innerHTML = `<div class="jb-empty">No background jobs running.</div>`; return; }
  pop.innerHTML = `<div class="jb-head">Background jobs</div>` + _jobsLast.map(j =>
    `<div class="jb-item jb-${j.state}"><span class="jb-k">${escapeHtml(j.kind)}</span><span class="jb-l">${escapeHtml(j.label)}</span><span class="jb-m">${j.state === "queued" ? "queued" : Math.round(j.elapsed) + "s"}</span></div>`).join("");
}
async function pollJobs() {
  let d; try { d = await api("/api/jobs"); } catch { return; }
  _jobsLast = d.jobs || [];
  const n = (d.running || 0) + (d.queued || 0), badge = $("#jobsBadge");
  if (badge) {
    badge.style.display = n ? "flex" : "none";
    $("#jbCount").textContent = n;
    badge.classList.toggle("active", (d.running || 0) > 0);
    badge.classList.toggle("only-queued", (d.running || 0) === 0 && (d.queued || 0) > 0);
    badge.title = n ? `${d.running || 0} running${d.queued ? ", " + d.queued + " queued" : ""}` : "";
  }
  const pop = $("#jobsPop"); if (pop && pop.classList.contains("open")) renderJobsPop(pop);
  // highlight the exact workflow cards / scheduler rows whose job is running (match by ref)
  const running = new Set(_jobsLast.filter(j => j.state === "running" && j.ref).map(j => j.ref));
  $$(".wf-card[data-wid]").forEach(c => c.classList.toggle("job-running", running.has("workflow:" + c.dataset.wid)));
  $$(".sched-row[data-tid]").forEach(r => r.classList.toggle("job-running", running.has(r.dataset.src) || running.has("task:" + r.dataset.tid)));
}

/* ---------------- wiring ---------------- */
const autosize = t => { t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, 200) + "px"; };
function wire() {
  const input = $("#input");
  input.addEventListener("input", () => { autosize(input); cmdACUpdate(); });
  input.addEventListener("keydown", e => {
    if (cmdACKey(e)) return;                      // autocomplete consumed the key (nav/accept/dismiss)
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  });
  input.addEventListener("blur", () => setTimeout(cmdACHide, 120));
  $("#send").onclick = () => state.busy ? stopChat() : send();
  { const cb = $("#converseBtn"); if (cb) cb.onclick = toggleConverse; }   // hands-free voice mode
  $("#newVoyage").onclick = newVoyage;
  $("#agentToggle").onchange = e => {
    state.agent = e.target.checked;
    fetch("/api/prefs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ agent_mode: state.agent }) }).catch(() => {});
  };

  $$(".nav-item").forEach(n => n.onclick = () => {
    const v = n.dataset.view;
    if (v === "chat") { setView("chat"); sideShowChats(); }
    else if (v === "files") openExplorer();
    else if (v === "brain") openBrain();
    else if (v === "scheduler") openScheduler();
    else if (v === "calendar") openCalendar();
    else if (v === "researcher") openResearcher();
    else if (v === "workflows") openWorkflows();
    else if (v === "search") openSearch();
    else if (v === "notes") openNotes();
    else if (v === "logs") openLogs();
    else if (v === "health") openHealth();
    else setView(v);
  });

  $("#modelPill").onclick = e => { e.stopPropagation(); $("#modelMenu").classList.toggle("open"); };
  document.addEventListener("click", () => $("#modelMenu").classList.remove("open"));
  $("#modelMenu").onclick = e => e.stopPropagation();

  $("#openSettings").onclick = openSettings;
  $("#toggleSidebar").onclick = () => $("#sidebar").classList.toggle("open");
  $("#liveBtn").onclick = openLiveView;
  { const vb = $("#voiceBtn"); if (vb) vb.onclick = openVoice; }
  { const cb = $("#chatsBack"); if (cb) cb.onclick = sideShowMain; }
  wireAttach();
  { const jb = $("#jobsBadge"); if (jb) jb.onclick = e => { e.stopPropagation(); const p = $("#jobsPop"); p.classList.toggle("open"); if (p.classList.contains("open")) renderJobsPop(p); }; }
  document.addEventListener("click", () => { const p = $("#jobsPop"); if (p) p.classList.remove("open"); });
  pollJobs(); _jobsTimer = setInterval(pollJobs, 2500);

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
      const pw = $("#loginPass").value, codeEl = $("#loginCode");
      try {
        const r = await fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ user: $("#loginUser").value, password: pw, code: codeEl ? codeEl.value.trim() : "" }) });
        const d = await r.json().catch(() => ({}));
        if (r.ok && d.need_code) {                         // password OK — second factor required
          if (codeEl) { codeEl.style.display = ""; codeEl.value = ""; codeEl.focus(); }
          err.textContent = "Enter the 6-digit code from your authenticator app.";
          return;
        }
        if (!r.ok) {
          const inCode = codeEl && codeEl.style.display !== "none";
          err.textContent = inCode ? "Invalid or expired code." : "Invalid username or password.";
          if (inCode) { codeEl.value = ""; codeEl.focus(); } else { $("#loginPass").value = ""; $("#loginPass").focus(); }
          return;
        }
        gate.style.display = "none";
        if (codeEl) { codeEl.style.display = "none"; codeEl.value = ""; }   // reset for next time
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
/* ---------- agent-driven UI control (server pushes ui_open/ui_close/ui_arrange over /api/ui/stream) ---------- */
let _uiES = null;
const UI_OPENERS = {
  files: () => openExplorer(), explorer: () => openExplorer(),
  calendar: () => openCalendar(), brain: () => openBrain(),
  memory: () => openBrain("mem"), knowledge: () => openBrain("kn"), skills: () => openBrain("skills"),
  rivers: () => openBrain("rivers"), evals: () => openBrain("evals"),
  "memory-graph": () => openMemoryGraph(), scheduler: () => openScheduler(),
  researcher: () => openResearcher(), notes: () => openNotes(), health: () => openHealth(),
  search: () => openSearch(), voice: () => openVoice(), workflows: () => openWorkflows(),
  live: () => openLiveView(), settings: () => openSettings(),
};
const UI_WINIDS = {
  files: "win-explorer", explorer: "win-explorer", preview: "win-preview", calendar: "win-cal",
  brain: "win-brain", memory: "win-brain", knowledge: "win-brain", skills: "win-brain",
  rivers: "win-brain", evals: "win-brain", "memory-graph": "win-memgraph", scheduler: "win-sched",
  researcher: "win-research", notes: "win-notes", health: "win-health", search: "win-search",
  voice: "win-voice", workflows: "win-workflows", live: "win-live", settings: "win-settings",
};
function startUiStream() {
  if (_uiES) return;                                   // one stream; survives re-login (idempotent init)
  _uiES = new EventSource("/api/ui/stream");
  _uiES.onmessage = e => { let c; try { c = JSON.parse(e.data); } catch { return; } handleUiCommand(c); };
  _uiES.onerror = () => {};                            // EventSource auto-reconnects
}
function handleUiCommand(c) {
  if (!c || c.type !== "ui") return;
  try {
    if (c.action === "open") uiOpen(c);
    else if (c.action === "close") uiClose(c);
    else if (c.action === "arrange") uiArrange(c);
  } catch { /* a bad command must never break the page */ }
}
function uiOpen(c) {
  if (c.path) {
    const p = String(c.path);
    isPreviewable(p) ? openPreview(p) : openFile(p);
    return;
  }
  const fn = UI_OPENERS[(c.window || "").toLowerCase()];
  if (fn) fn();
}
const _UI_ZONE = { left: "left", right: "right", top: "top", bottom: "bottom", maximize: "full",
  "top-left": "tl", "top-right": "tr", "bottom-left": "bl", "bottom-right": "br" };
function _uiWin(name) {
  if (name) { const id = UI_WINIDS[name.toLowerCase()]; return id ? document.getElementById(id) : null; }
  const vis = $$("#windows .win").filter(w => w.style.display !== "none");   // no name → the front-most window
  return vis.sort((a, b) => (+a.style.zIndex || 0) - (+b.style.zIndex || 0)).pop() || null;
}
function uiClose(c) { const el = _uiWin(c.window), x = el && $(".win-close", el); if (x) x.click(); }
function uiArrange(c) {
  const mode = (c.mode || "").toLowerCase();
  if (mode === "focus" || mode === "center" || mode === "minimize" || _UI_ZONE[mode]) {
    const el = _uiWin(c.window); if (!el) return;             // window optional → the active window
    if (mode === "minimize") return minimizeWindow(el);
    el.style.display = "flex"; el.style.zIndex = ++_winZ;        // surface + un-minimize
    if (el._chip) { el._chip.remove(); el._chip = null; _setWinMin(el.id, false); }
    if (_UI_ZONE[mode]) { _applySnap(el, _UI_ZONE[mode]); return; }   // snap to a half / quarter / full
    if (mode === "center") {
      el.dataset.snapped = ""; el.dataset.maximized = "";
      el.style.left = Math.max(0, (innerWidth - el.offsetWidth) / 2) + "px";
      el.style.top = Math.max(40, (innerHeight - el.offsetHeight) / 2) + "px";
    }
    return;                                                  // focus = surface it (handled above)
  }
  const wins = $$("#windows .win").filter(w => w.style.display !== "none");
  if (!wins.length) return;
  if (mode === "cascade") {
    wins.forEach((w, i) => { w.style.left = (60 + i * 30) + "px"; w.style.top = (70 + i * 30) + "px";
      w.dataset.snapped = ""; w.dataset.maximized = ""; w.style.zIndex = ++_winZ; });
  } else {                                             // tile into a grid
    const n = wins.length, cols = Math.ceil(Math.sqrt(n)), rows = Math.ceil(n / cols), top = 64, gap = 6;
    const cw = Math.floor((innerWidth - gap * (cols + 1)) / cols);
    const ch = Math.floor((innerHeight - top - gap * (rows + 1)) / rows);
    wins.forEach((w, i) => {
      const r = Math.floor(i / cols), col = i % cols;
      w.style.left = (gap + col * (cw + gap)) + "px"; w.style.top = (top + gap + r * (ch + gap)) + "px";
      w.style.width = cw + "px"; w.style.height = ch + "px";
      w.dataset.snapped = ""; w.dataset.maximized = ""; w.style.zIndex = ++_winZ;
    });
  }
}

async function initApp() {
  if (_appStarted) return;        // idempotent — survives a mid-session re-login
  _appStarted = true;
  wire();
  await loadModels();             // resolve the model + mind (Claude vs local) before any chat opens
  loadPrefs();
  setView("chat");
  await loadChats();              // chats now live on Oceano (dated folders), not the browser
  await migrateLocalChats();      // lift any pre-existing browser chats over, once
  const active = localStorage.getItem("oceano.active");
  if (active && _chats.some(s => s.id === active)) await openVoyage(active);
  else if (_chats.length) await openVoyage(_chats[0].id);
  else newVoyage();
  await maybeReconnectChat();       // re-attach to a reply that was still generating before the reload
  restoreWindows();                // re-open the app windows that were open before a reload
  startUiStream();                 // listen for agent-driven window commands (ui_open / ui_arrange…)
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
