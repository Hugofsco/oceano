"""Rivers — Oceano's model cookbook. Browse Hugging Face GGUF models, check
whether they fit your GPU, download them, and wire them into llama-swap so they
serve on :8081.

Inspired by PewDiePie's Odysseus "cookbook" + hwfit. Everything runs on the host
against the local llama.cpp/llama-swap stack.

  hw()                  -> GPU/VRAM/backend snapshot (for the hardware-fit badges)
  recommended()         -> a curated list of models, auto-scored against this machine
  search(q)             -> Hugging Face GGUF repos
  files(repo)           -> the repo's .gguf files, sizes, parsed quant, fit verdict
  start_download(...)   -> background download into the models dir (poll jobs())
  serve(...)            -> append a model block to llama-swap.yaml (it hot-reloads)
  installed()           -> .gguf already on disk + whether each is wired into llama-swap
"""
import re
import subprocess
import threading
from pathlib import Path

import requests
import yaml

import config

HF_API = "https://huggingface.co/api"
HF_RESOLVE = "https://huggingface.co"
_UA = {"User-Agent": "Oceano-Rivers/1.0"}

_JOBS = {}            # job_id -> progress dict
_JOB_SEQ = [0]
_JOBS_LOCK = threading.Lock()

# --- input validation (these values reach upstream URLs AND a shell-executed
# llama-swap `cmd`, so they must be strictly allowlisted, not just trusted from
# the UI) ----------------------------------------------------------------------
_RE_REPO = re.compile(r"[A-Za-z0-9][\w.-]*/[A-Za-z0-9][\w.-]*")     # owner/name
_RE_GGUF = re.compile(r"[A-Za-z0-9][\w.-]*\.gguf", re.IGNORECASE)   # a basename
_RE_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,39}")                 # llama-swap model id


def _safe_repo(repo):
    return bool(repo) and _RE_REPO.fullmatch(repo) is not None


def _safe_gguf(basename):
    return _RE_GGUF.fullmatch(basename or "") is not None


def _sanitize_name(s):
    """Reduce to a safe llama-swap model id ([a-z0-9._-], <=40), or '' if nothing left."""
    s = re.sub(r"[^a-z0-9._-]+", "-", (s or "").lower()).strip("-._")[:40]
    return s if _RE_NAME.fullmatch(s) else ""


# ---- HTTP session (ignore netrc/proxy; carry an HF token only if configured) ----
def _session():
    s = requests.Session()
    s.trust_env = False
    s.headers.update(_UA)
    if config.HF_TOKEN:
        s.headers["Authorization"] = f"Bearer {config.HF_TOKEN}"
    return s


# ============================ hardware ============================
def _detect_backend():
    def has(cmd):
        from shutil import which
        return which(cmd) is not None
    if has("nvidia-smi"):
        return "cuda"
    if has("vulkaninfo"):
        return "vulkan"
    if has("rocminfo"):
        return "rocm"
    return "cpu"


def _vram_bytes():
    """(total, free) VRAM in bytes — best effort. AMD via amdgpu sysfs; else None."""
    for card in sorted(Path("/sys/class/drm").glob("card*/device/mem_info_vram_total")):
        try:
            total = int(card.read_text())
            used_f = card.with_name("mem_info_vram_used")
            used = int(used_f.read_text()) if used_f.exists() else 0
            return total, max(0, total - used)
        except (OSError, ValueError):
            continue
    # NVIDIA fallback
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.total,memory.free",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5)
        t, f = (int(x) for x in out.stdout.splitlines()[0].split(","))
        return t * 1024 * 1024, f * 1024 * 1024
    except Exception:
        return None, None


def _gpu_name():
    try:
        out = subprocess.run(["vulkaninfo", "--summary"], capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"deviceName\s*=\s*(.+)", out)
        if m:
            return m.group(1).strip()
    except Exception:
        pass
    return None


def hw():
    total, free = _vram_bytes()
    return {"backend": _detect_backend(), "gpu": _gpu_name(),
            "vram_total": total, "vram_free": free}


# ============================ hardware-fit ============================
def fit(size_bytes, vram_total=None):
    """Will a model of this weight size run on the GPU? Heuristic traffic-light +
    a 0-100 score (higher = runs better on this box).
    Returns {verdict: fits|partial|cpu|unknown, ngl, score, note}."""
    if not vram_total or not size_bytes:
        return {"verdict": "unknown", "ngl": 99, "score": None, "note": "no GPU detected — CPU only"}
    usable = vram_total * 0.92            # headroom for compute buffers + driver
    r = usable / size_bytes               # how many model-sizes of VRAM we have
    if r >= 1.25:                         # weights + room for a decent KV cache
        score = min(100, round(75 + (r - 1.25) * 30))
        return {"verdict": "fits", "ngl": 99, "score": score, "note": "fits on GPU with room for context"}
    if r >= 0.667:                        # spill some layers to CPU
        ngl = max(1, int(99 * r))
        score = round(45 + (r - 0.667) * (30 / (1.25 - 0.667)))
        return {"verdict": "partial", "ngl": ngl, "score": score,
                "note": f"partial offload (~{ngl} layers on GPU) — slower"}
    return {"verdict": "cpu", "ngl": 0, "score": max(8, round(r * 60)),
            "note": "too large for VRAM — CPU/heavy offload, slow"}


# A curated catalog (ungated bartowski GGUFs, Q4_K_M) scored against the box —
# the "list a bunch of models and see what your machine can run" view. Sizes are
# baked in so scoring is instant + offline; the download fetches the real file.
RECOMMENDED = [
    ("bartowski/Llama-3.2-1B-Instruct-GGUF", "Llama-3.2-1B-Instruct-Q4_K_M.gguf", 807694464, "Llama 3.2 1B", "1B"),
    ("bartowski/Llama-3.2-3B-Instruct-GGUF", "Llama-3.2-3B-Instruct-Q4_K_M.gguf", 2019377696, "Llama 3.2 3B", "3B"),
    ("bartowski/Qwen2.5-3B-Instruct-GGUF", "Qwen2.5-3B-Instruct-Q4_K_M.gguf", 1929903264, "Qwen2.5 3B", "3B"),
    ("bartowski/Phi-3.5-mini-instruct-GGUF", "Phi-3.5-mini-instruct-Q4_K_M.gguf", 2393232672, "Phi-3.5 mini", "3.8B"),
    ("bartowski/Mistral-7B-Instruct-v0.3-GGUF", "Mistral-7B-Instruct-v0.3-Q4_K_M.gguf", 4372812000, "Mistral 7B v0.3", "7B"),
    ("bartowski/Qwen2.5-7B-Instruct-GGUF", "Qwen2.5-7B-Instruct-Q4_K_M.gguf", 4683074240, "Qwen2.5 7B", "7B"),
    ("bartowski/Meta-Llama-3.1-8B-Instruct-GGUF", "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf", 4920739232, "Llama 3.1 8B", "8B"),
    ("bartowski/gemma-2-9b-it-GGUF", "gemma-2-9b-it-Q4_K_M-fp16.gguf", 6843425696, "Gemma 2 9B", "9B"),
    ("bartowski/Qwen2.5-14B-Instruct-GGUF", "Qwen2.5-14B-Instruct-Q4_K_M.gguf", 8988110976, "Qwen2.5 14B", "14B"),
    ("bartowski/Qwen2.5-32B-Instruct-GGUF", "Qwen2.5-32B-Instruct-Q4_K_M.gguf", 19851336576, "Qwen2.5 32B", "32B"),
]
_VERDICT_RANK = {"fits": 0, "partial": 1, "cpu": 2, "unknown": 3}


def recommended():
    """The curated catalog, each scored against this machine. Ordered most-capable-
    that-still-runs first (fits before partial before cpu; bigger first within a tier)."""
    vram = _vram_bytes()[0]
    on_disk = {p.name for p in config.MODELS_DIR.glob("*.gguf")} if config.MODELS_DIR.exists() else set()
    out = []
    for repo, fn, size, label, params in RECOMMENDED:
        f = fit(size, vram)
        out.append({"repo": repo, "filename": fn, "size": size, "label": label,
                    "params": params, "quant": _parse_quant(fn), "fit": f,
                    "downloaded": fn in on_disk})
    out.sort(key=lambda m: (_VERDICT_RANK.get(m["fit"]["verdict"], 3), -m["size"]))
    return {"vram": vram, "models": out}


_QUANT_RE = re.compile(r"(IQ\d[A-Z_]*|Q\d+(?:_[A-Z0-9]+)*|BF16|F16|F32)", re.IGNORECASE)


def _parse_quant(filename):
    m = _QUANT_RE.search(filename)
    return m.group(1).upper() if m else "?"


# ============================ Hugging Face ============================
def search(query, limit=24):
    """GGUF repos matching the query, most-downloaded first."""
    if not query.strip():
        return []
    r = _session().get(f"{HF_API}/models", timeout=15, params={
        "search": query, "filter": "gguf", "limit": limit,
        "sort": "downloads", "direction": -1})
    r.raise_for_status()
    return [{"repo": m["id"], "downloads": m.get("downloads", 0), "likes": m.get("likes", 0)}
            for m in r.json()]


def files(repo):
    """The .gguf files in a repo with size + quant + fit verdict. Sharded/dir
    quants are skipped (single-file GGUF only for now)."""
    if not _safe_repo(repo):
        return {"gated": False, "files": [], "error": "invalid repo id"}
    vram = _vram_bytes()[0]
    try:
        r = _session().get(f"{HF_API}/models/{repo}/tree/main", timeout=15, params={"recursive": "true"})
    except requests.RequestException as e:
        return {"gated": False, "error": str(e), "files": []}
    if r.status_code in (401, 403):
        return {"gated": True, "files": [],
                "error": "gated repo — set HF_TOKEN (and accept its terms on huggingface.co)"}
    r.raise_for_status()
    out = []
    for e in r.json():
        path = e.get("path", "")
        if not path.endswith(".gguf") or "/" in path:        # skip sharded/in-folder parts
            continue
        size = e.get("size") or 0
        out.append({"filename": path, "size": size, "quant": _parse_quant(path),
                    "fit": fit(size, vram)})
    out.sort(key=lambda f: f["size"])
    return {"gated": False, "files": out}


# ============================ downloads ============================
def _download_worker(job_id, repo, filename):
    job = _JOBS[job_id]
    dest = config.MODELS_DIR / filename
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        tmp.parent.mkdir(parents=True, exist_ok=True)
        with _session().get(f"{HF_RESOLVE}/{repo}/resolve/main/{filename}",
                            stream=True, timeout=60, allow_redirects=True) as r:
            r.raise_for_status()
            job["total"] = int(r.headers.get("Content-Length", 0))
            done = 0
            with open(tmp, "wb") as fh:
                for chunk in r.iter_content(chunk_size=1 << 20):   # 1 MiB
                    if job.get("cancel"):
                        raise RuntimeError("cancelled")
                    fh.write(chunk)
                    done += len(chunk)
                    job["downloaded"] = done
        tmp.rename(dest)
        job["status"], job["path"] = "done", str(dest)
    except Exception as e:
        job["status"], job["error"] = "error", str(e)
        try:
            tmp.unlink()
        except OSError:
            pass


def start_download(repo, filename):
    base = Path(filename or "").name
    if not _safe_repo(repo):
        raise ValueError("invalid repo id")
    if not _safe_gguf(base):                  # never let a metacharacter-laden name hit disk/URL
        raise ValueError("invalid filename — must be a plain .gguf name")
    dest = config.MODELS_DIR / base
    if dest.exists():
        return {"already": True, "path": str(dest)}
    with _JOBS_LOCK:
        _JOB_SEQ[0] += 1
        job_id = f"dl{_JOB_SEQ[0]}"
        _JOBS[job_id] = {"id": job_id, "repo": repo, "filename": base,
                         "total": 0, "downloaded": 0, "status": "downloading", "error": None}
    threading.Thread(target=_download_worker, args=(job_id, repo, base), daemon=True).start()
    return {"already": False, "job": job_id}


def jobs():
    with _JOBS_LOCK:
        return list(_JOBS.values())


def cancel(job_id):
    j = _JOBS.get(job_id)
    if j and j["status"] == "downloading":
        j["cancel"] = True
        return True
    return False


# ============================ llama-swap wiring ============================
def installed():
    """Local .gguf files + which are already wired into llama-swap."""
    out = []
    for f in sorted(config.MODELS_DIR.glob("*.gguf")):
        if "vocab" in f.name:                                # skip tokenizer-only vocab files
            continue
        out.append({"filename": f.name, "size": f.stat().st_size,
                    "quant": _parse_quant(f.name), "served": _cmd_uses(f.name)})
    return out


def _cmd_uses(filename):
    """The llama-swap model name whose cmd references this file, or None."""
    try:
        data = yaml.safe_load(config.LLAMA_SWAP_CFG.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return None
    for name, spec in (data.get("models") or {}).items():
        if filename in (spec or {}).get("cmd", ""):
            return name
    return None


_KV_TYPES = ("f16", "q8_0", "q4_0")     # KV-cache dtype the GUI may pick


def serve(filename, name=None, ngl=99, ctx=8192, fa=True, kv="f16", ttl=600):
    """Append a model block to llama-swap.yaml (comment-preserving: we append text
    rather than re-dump). llama-swap's -watch-config hot-reloads it.

    Serving params (all settable from the GUI):
      ngl  GPU layers (0 = CPU)         ctx  context window (tokens)
      fa   flash attention on/off       kv   KV-cache dtype: f16 | q8_0 | q4_0
      ttl  seconds llama-swap keeps the model resident after last use

    `filename` and `name` end up inside a shell-executed `cmd`, so both are strictly
    validated — only [A-Za-z0-9._-] survives, which can't break YAML or inject shell.
    The numeric/enum params are clamped/allowlisted, so they're shell-safe too."""
    base = Path(filename or "").name
    if not _safe_gguf(base):
        return {"ok": False, "error": "invalid filename — must be a plain .gguf name"}
    name = _sanitize_name(name or Path(base).stem)
    if not name:
        return {"ok": False, "error": "invalid model name (use letters, digits, . _ -)"}

    path = config.MODELS_DIR / base
    if not path.exists():
        return {"ok": False, "error": "model file not found on disk"}

    cfg = config.LLAMA_SWAP_CFG
    try:
        data = yaml.safe_load(cfg.read_text()) or {}
    except (OSError, yaml.YAMLError) as e:
        return {"ok": False, "error": f"cannot read llama-swap.yaml: {e}"}

    existing = set((data.get("models") or {}).keys())
    if name in existing:
        return {"ok": False, "error": f"a model named {name!r} is already in llama-swap.yaml"}
    if _cmd_uses(base):
        return {"ok": False, "error": "this file is already served by llama-swap"}

    try:
        ngl = int(ngl); ctx = int(ctx); ttl = int(ttl)
    except (TypeError, ValueError):
        ngl, ctx, ttl = 99, 8192, 600
    ngl = max(0, min(ngl, 999)); ctx = max(256, min(ctx, 1_048_576)); ttl = max(0, min(ttl, 86400))
    kv = kv if kv in _KV_TYPES else "f16"          # allowlist → shell-safe
    # build the flag string from the chosen params (only allowlisted/clamped values)
    flags = f"-ngl {ngl} -fa {'1' if fa else '0'} --parallel 1 -c {ctx}"
    if kv != "f16":                                 # f16 is llama.cpp's default; only emit when quantized
        flags += f" -ctk {kv} -ctv {kv}"
    flags += " --jinja"

    block = (
        f'\n  "{name}":\n'
        f'    cmd: |\n'
        f'      {config.LLAMA_SERVER_BIN}\n'
        f'      -m {path}\n'
        f'      {flags}\n'
        f'      --host 127.0.0.1 --port ${{PORT}}\n'
        f'    ttl: {ttl}\n'
    )
    original = cfg.read_text()
    if "models:" not in original:
        return {"ok": False, "error": "llama-swap.yaml has no 'models:' section"}
    updated = original.rstrip("\n") + "\n" + block
    # validate it still parses AND gained exactly this model before writing
    try:
        check = yaml.safe_load(updated) or {}
        if name not in (check.get("models") or {}):
            raise ValueError("append did not register the model (indentation?)")
    except (yaml.YAMLError, ValueError) as e:
        return {"ok": False, "error": f"refused to write — would corrupt config: {e}"}
    cfg.write_text(updated)
    return {"ok": True, "name": name, "ngl": ngl, "ctx": ctx, "fa": fa, "kv": kv, "ttl": ttl}
