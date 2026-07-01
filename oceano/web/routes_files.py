"""Workspace-file routes: the (fenced) explorer + raw serving, artifact
rendering (markdown / mermaid / chart / slides), sandboxed previews and the
capability tokens that scope them, and file/folder CRUD."""
import base64
import hashlib
import hmac
import os
import shutil
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

import config
from oceano.web.state import _wresolve, load

router = APIRouter()


@router.get("/api/files")
def list_dir(path: str = ""):
    base = _wresolve(path)
    if not base.exists():
        return {"path": "", "entries": []}
    if base.is_file():
        base = base.parent
    entries = [{"name": c.name, "dir": c.is_dir(),
                "path": str(c.relative_to(config.WORKSPACE)),
                "size": (c.stat().st_size if c.is_file() else 0)}
               for c in sorted(base.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))]
    rel = str(base.relative_to(config.WORKSPACE))
    return {"path": "" if rel == "." else rel, "entries": entries}


@router.get("/api/files/all")
def list_all_files():
    """Flat, recursive list of workspace files + dirs (relative posix paths), for the
    searchable file/folder pickers in the Workflows editor. Skips heavy/hidden dirs and
    caps the walk so a huge workspace can't stall the request."""
    base = config.WORKSPACE
    files, dirs, n = [], [], 0
    for root, ds, fs in os.walk(base):
        ds[:] = [d for d in ds if d not in _MTIME_SKIP_DIRS and not d.startswith(".")]
        relp = os.path.relpath(root, base)
        rel = "" if relp == "." else relp.replace(os.sep, "/")
        if rel:
            dirs.append(rel)
        for f in fs:
            files.append(f if not rel else rel + "/" + f)
            n += 1
            if n >= 4000:
                return {"files": sorted(files), "dirs": sorted(dirs), "capped": True}
    return {"files": sorted(files), "dirs": sorted(dirs)}


@router.get("/api/raw")
def raw_file(path: str):
    """Serve a workspace file with its real content-type (for images in chat, downloads)."""
    p = _wresolve(path)
    if not p.is_file():
        raise HTTPException(404, "not a file")
    return FileResponse(str(p))


# Folders never worth statting for app auto-reload — they're what blows a preview
# folder past the walk cap and hides the actual app files behind it.
_MTIME_SKIP_DIRS = {"node_modules", "__pycache__", "venv", "dist", "build"}


@router.get("/api/preview-mtime")
def preview_mtime(path: str):
    """Latest mtime among the files in the previewed app's folder. The Preview window
    polls this to auto-reload when the agent (or you) edits the app. Defined BEFORE the
    /api/preview/{path} catch-all so it isn't swallowed by it."""
    p = _wresolve(path)
    if not p.exists():
        return {"mtime": 0}         # deleted/renamed — never walk the whole workspace for it
    base = p.parent if p.is_file() else p
    latest, n = 0.0, 0
    if p.is_file():
        try:                        # the previewed file itself ALWAYS counts, cap or not —
            latest = p.stat().st_mtime   # edits to it must fire a reload even in a huge folder
        except OSError:
            pass
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in _MTIME_SKIP_DIRS and not d.startswith(".")]
        for name in files:
            try:
                latest = max(latest, (Path(root) / name).stat().st_mtime)
            except OSError:
                pass
            n += 1
            if n >= 1000:           # cap the walk so a huge folder can't stall the poll
                return {"mtime": latest}
    return {"mtime": latest}


# Sandbox flags for previewed content — kept in sync with the iframe's sandbox attribute in
# app.js. Note the deliberate ABSENCE of allow-same-origin: that's what keeps the rendered
# page in an opaque origin so it can't reuse the session cookie against /api/*.
_PREVIEW_SANDBOX = "allow-scripts allow-forms allow-modals allow-popups allow-pointer-lock"


# ---------------- artifact rendering (markdown / mermaid / chart / slides) ----------------
# The Preview iframe can render a handful of *source* artifact types — not just finished
# .html. We wrap the file in a self-contained page that pulls the renderer from /static/vendor
# (loads fine in the opaque sandbox — the CSP sandbox restricts the document's origin, not
# resource fetches) and decodes the file content from base64 (so nothing in it can break out
# of the HTML/JS context). Same security headers as a plain preview apply.
def _artifact_kind(name):
    n = (name or "").lower()
    if n.endswith(".slides.md") or n.endswith(".slides"):
        return "slides"
    if n.endswith(".chart.json"):
        return "chart"
    if n.endswith((".mmd", ".mermaid")):
        return "mermaid"
    if n.endswith((".md", ".markdown")):
        return "markdown"
    return None


_ARTIFACT_BASE_CSS = """
  :root{color-scheme:dark}*{box-sizing:border-box}
  body{margin:0;background:#0b1620;color:#e6edf3;font:15px/1.65 'Hanken Grotesk',-apple-system,system-ui,sans-serif}
  ::selection{background:#1f6feb55}a{color:#58a6ff}
  .wrap{max-width:860px;margin:0 auto;padding:30px 28px 80px}
  pre{background:#0d1117;border:1px solid #1c2733;border-radius:10px;padding:14px 16px;overflow:auto}
  code{font-family:'JetBrains Mono',ui-monospace,monospace;font-size:.92em}
  :not(pre)>code{background:#1c2733;padding:.1em .4em;border-radius:5px}
  table{border-collapse:collapse;width:100%;margin:1em 0}
  th,td{border:1px solid #1c2733;padding:7px 11px;text-align:left}
  th{background:#101c27}
  blockquote{border-left:3px solid #2b7a78;margin:1em 0;padding:.2em 1em;color:#9fb3c8}
  img{max-width:100%;border-radius:8px}
  h1,h2,h3{font-family:'Fraunces',Georgia,serif;line-height:1.2}
  h1{font-size:2em}h2{font-size:1.5em;border-bottom:1px solid #1c2733;padding-bottom:.2em}
  hr{border:none;border-top:1px solid #1c2733;margin:2em 0}
  .art-err{color:#ff7b72;padding:22px;font-family:'JetBrains Mono',monospace;white-space:pre-wrap}
"""

_TPL_MARKDOWN = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/static/vendor/atom-one-dark.min.css">
<style>__CSS__</style>
<script src="/static/vendor/marked.min.js"></script>
<script src="/static/vendor/purify.min.js"></script>
<script src="/static/vendor/highlight.min.js"></script></head>
<body><article class="wrap" id="doc"></article><script>
const RAW=new TextDecoder().decode(Uint8Array.from(atob("__B64__"),c=>c.charCodeAt(0)));
try{marked.setOptions({gfm:true,breaks:false});
  document.getElementById('doc').innerHTML=DOMPurify.sanitize(marked.parse(RAW));
  document.querySelectorAll('pre code').forEach(b=>{try{hljs.highlightElement(b)}catch(e){}});
}catch(e){document.getElementById('doc').innerHTML='<div class="art-err">'+e+'</div>';}
</script></body></html>"""

_TPL_MERMAID = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>__CSS__ .wrap{text-align:center}.mermaid{visibility:hidden}</style>
<script src="/static/vendor/mermaid.min.js"></script></head>
<body><div class="wrap"><pre class="mermaid" id="m"></pre></div><script>
const RAW=new TextDecoder().decode(Uint8Array.from(atob("__B64__"),c=>c.charCodeAt(0)));
const el=document.getElementById('m');el.textContent=RAW;
try{mermaid.initialize({startOnLoad:false,theme:'dark',securityLevel:'strict'});
  mermaid.run({nodes:[el]}).then(()=>{el.style.visibility='visible'})
   .catch(e=>{el.outerHTML='<div class="art-err">'+e+'</div>';});
}catch(e){el.outerHTML='<div class="art-err">'+e+'</div>';}
</script></body></html>"""

_TPL_CHART = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>__CSS__ .wrap{max-width:780px;padding-top:40px}</style>
<script src="/static/vendor/chart.umd.min.js"></script></head>
<body><div class="wrap"><canvas id="c"></canvas><div class="art-err" id="err"></div></div><script>
const RAW=new TextDecoder().decode(Uint8Array.from(atob("__B64__"),c=>c.charCodeAt(0)));
try{const cfg=JSON.parse(RAW);
  Chart.defaults.color='#9fb3c8';Chart.defaults.borderColor='#1c2733';
  Chart.defaults.font.family="'Hanken Grotesk',sans-serif";
  new Chart(document.getElementById('c'),cfg);
}catch(e){document.getElementById('err').textContent='Invalid chart spec (expects a Chart.js config JSON): '+e;}
</script></body></html>"""

_TPL_SLIDES = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>__CSS__
 html,body{height:100%;overflow:hidden;background:#070f17}
 #deck{height:100vh;display:flex;align-items:center;justify-content:center;padding:6vh 8vw;cursor:pointer}
 .slide{max-width:920px;width:100%;animation:fade .25s ease}
 .slide h1{font-size:2.7em;margin-top:0}.slide h2{border:none}
 #hud{position:fixed;bottom:14px;right:18px;font:13px/1 'JetBrains Mono',monospace;color:#5b7287}
 #hint{position:fixed;bottom:14px;left:18px;font:12px 'JetBrains Mono',monospace;color:#3d4f5e}
 @keyframes fade{from{opacity:0;transform:translateY(7px)}to{opacity:1}}</style>
<script src="/static/vendor/marked.min.js"></script>
<script src="/static/vendor/purify.min.js"></script></head>
<body><div id="deck"></div><div id="hud"></div><div id="hint">← → / space · click to advance</div><script>
const RAW=new TextDecoder().decode(Uint8Array.from(atob("__B64__"),c=>c.charCodeAt(0)));
const slides=RAW.split(/\\n-{3,}\\s*\\n/).map(s=>s.trim()).filter(Boolean);
let i=0;const deck=document.getElementById('deck'),hud=document.getElementById('hud');
function render(){deck.innerHTML='<section class="slide">'+DOMPurify.sanitize(marked.parse(slides[i]||'*empty deck*'))+'</section>';hud.textContent=(i+1)+' / '+Math.max(slides.length,1);}
function go(d){const n=Math.min(Math.max(i+d,0),slides.length-1);if(n!==i){i=n;render();}}
addEventListener('keydown',e=>{if(e.key==='ArrowRight'||e.key===' '||e.key==='PageDown'){e.preventDefault();go(1);}else if(e.key==='ArrowLeft'||e.key==='PageUp'){go(-1);}else if(e.key==='Home'){i=0;render();}else if(e.key==='End'){i=slides.length-1;render();}});
deck.addEventListener('click',e=>go(e.clientX<innerWidth*0.25?-1:1));
render();</script></body></html>"""

_ARTIFACT_TEMPLATES = {"markdown": _TPL_MARKDOWN, "mermaid": _TPL_MERMAID,
                       "chart": _TPL_CHART, "slides": _TPL_SLIDES}


def _artifact_html(kind, raw):
    b64 = base64.b64encode(raw.encode("utf-8")).decode()
    return _ARTIFACT_TEMPLATES[kind].replace("__CSS__", _ARTIFACT_BASE_CSS).replace("__B64__", b64)


# ---------------- preview capability tokens ----------------
# The Preview iframe is sandboxed WITHOUT allow-same-origin (see _PREVIEW_SANDBOX), so the rendered
# page sits in an opaque origin and can't send the SameSite=Lax session cookie — that's what stops a
# previewed page from calling /api/* with your session. The flip side: the page's OWN relative
# assets (./style.css, ./app.js), if served from the cookie-gated /api/preview/* path, would 401 —
# the opaque-origin sub-requests carry no cookie. So multi-file previews load through
# /preview/<token>/… instead: an unguessable, time-boxed, read-only capability minted by a logged-in
# user (via /api/preview-token) and confined to the previewed file's directory subtree. No cookie is
# involved, so the /api/* containment is fully preserved.
_PREVIEW_TOKEN_TTL = 12 * 3600        # long enough to outlast an editing session; re-minted on reload


def _preview_root(path):
    """The directory subtree a token authorizes: the previewed file's parent as a
    workspace-relative POSIX string ('' = workspace root)."""
    rel = (path or "").replace("\\", "/").strip("/")
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def _make_preview_token(root, secret):
    # "prev:" domain tag (see _make_token) — keeps this capability token distinct from a session
    # cookie even though both are HMAC'd with the same auth secret in the same base64 envelope.
    msg = f"{root}:{int(time.time())}"
    sig = hmac.new(secret.encode(), f"prev:{msg}".encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{msg}:{sig}".encode()).decode()


def _preview_token_root(token, secret):
    """Authorized root for a valid, unexpired token, else None. HMAC-signed, so root can't be
    tampered without the secret."""
    try:
        root, ts, sig = base64.urlsafe_b64decode(token.encode()).decode().rsplit(":", 2)
        good = hmac.new(secret.encode(), f"prev:{root}:{ts}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, good):
            return None
        if time.time() - int(ts) > _PREVIEW_TOKEN_TTL:
            return None
        return root
    except Exception:
        return None


def _serve_preview(p):
    """Build the sandboxed response for an already-resolved workspace path. Renders artifact source
    (.md/.mmd/.chart.json/.slides) to HTML; serves everything else raw. The CSP `sandbox` (no
    allow-same-origin) forces an opaque origin HOWEVER the response is loaded — so even opened
    directly (new tab, window.open, a crafted link) it can't act with the session. nosniff stops
    MIME confusion; no-store keeps auto-reload fetching fresh."""
    if p.is_dir():
        p = p / "index.html"
    if not p.is_file():
        raise HTTPException(404, "not found")
    headers = {
        "Cache-Control": "no-store",
        "Content-Security-Policy": f"sandbox {_PREVIEW_SANDBOX}",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",       # keep the capability token out of the Referer header
    }
    kind = _artifact_kind(p.name)               # .md/.mmd/.chart.json/.slides → render, not raw
    if kind:
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            raise HTTPException(500, str(e))
        return HTMLResponse(_artifact_html(kind, raw), headers=headers)
    return FileResponse(str(p), headers=headers)


@router.get("/api/preview-token")
def preview_token(path: str):
    """Mint a capability token scoped to the previewed file's folder. Cookie-gated by the
    middleware, so only a logged-in user can mint one; the token then rides in the
    /preview/<token>/… URL the sandboxed iframe loads."""
    _wresolve(path)                              # 400s if the path escapes the workspace
    root = _preview_root(path)
    secret = load().get("auth", {}).get("secret", "")
    return {"token": _make_preview_token(root, secret), "root": root}


@router.get("/api/preview/{path:path}")
def preview_file(path: str):
    """Cookie-gated single-file preview — the iframe's top-level navigation carries the Lax cookie,
    so this still works for a lone .html/.md/etc. A multi-file app whose relative assets must load
    goes through /preview/<token>/… instead (see the preview-capability note above)."""
    return _serve_preview(_wresolve(path))


@router.get("/preview/{token}/{path:path}")
def preview_capability(token: str, path: str):
    """Token-authed preview — deliberately NOT under /api/, so the middleware doesn't demand the
    cookie the sandboxed (opaque-origin) iframe can't send. Validates the capability token and
    confines the request to its authorized directory subtree (read-only). Same sandbox/nosniff/
    no-store headers as the cookie-gated route. NOTE: a token minted for a workspace-root file
    authorizes the whole workspace-root subtree — keep multi-file apps in their own folder for a
    tighter scope."""
    secret = load().get("auth", {}).get("secret", "")
    root = _preview_token_root(token, secret)
    if root is None:
        raise HTTPException(403, "invalid or expired preview token")
    p = _wresolve(path)
    if not p.is_relative_to(_wresolve(root)):    # confine to the token's subtree
        raise HTTPException(403, "path outside preview scope")
    return _serve_preview(p)


@router.get("/api/file")
def read_file_api(path: str):
    p = _wresolve(path)
    if not p.is_file():
        raise HTTPException(404, "not a file")
    try:
        return {"path": path, "content": p.read_text(encoding="utf-8")}
    except (UnicodeDecodeError, ValueError):
        return {"path": path, "content": None, "binary": True}


@router.post("/api/file")
async def write_file_api(req: Request):
    b = await req.json()
    p = _wresolve(b["path"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(b.get("content", ""), encoding="utf-8")
    return {"ok": True}


@router.delete("/api/file")
def delete_file_api(path: str):
    p = _wresolve(path)
    if p == config.WORKSPACE:        # an empty/'.' path resolves to the root — don't rmtree everything
        raise HTTPException(400, "refusing to delete the workspace root")
    if p.is_dir():
        shutil.rmtree(p)
    elif p.is_file():
        p.unlink()
    return {"ok": True}


@router.post("/api/folder")
async def make_folder_api(req: Request):
    p = _wresolve((await req.json())["path"])
    p.mkdir(parents=True, exist_ok=True)
    return {"ok": True}


@router.post("/api/rename")
async def rename_api(req: Request):
    b = await req.json()
    src, dst = _wresolve(b["path"]), _wresolve(b["to"])
    if config.WORKSPACE in (src, dst):               # never move/clobber the workspace root itself
        raise HTTPException(400, "refusing to move the workspace root")
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)
    return {"ok": True}
