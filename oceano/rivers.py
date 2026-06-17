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
import struct
import subprocess
import threading
from pathlib import Path

import requests
import yaml

import config
from oceano import atomicio

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


# ============================ VRAM estimate (GGUF-aware) ============================
# Per-element KV-cache cost in bytes for each dtype the GUI offers (q8_0 = 34 B / 32 elems,
# q4_0 = 18 B / 32 elems, including the block scale overhead).
_KV_BYTES = {"f16": 2.0, "q8_0": 1.0625, "q4_0": 0.5625}
_VRAM_OVERHEAD = 350 * 1024 * 1024        # rough compute-buffer / driver allowance

_GGUF_SCALAR_SIZE = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}
_GGUF_SCALAR_FMT = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f",
                    7: "<?", 10: "<Q", 11: "<q", 12: "<d"}
_GGUF_STRING, _GGUF_ARRAY = 8, 9


def _gguf_meta(path, window=32 * 1024 * 1024):
    """Read the architecture fields needed for a KV-cache estimate straight from a GGUF file's
    metadata header — no `gguf` dependency. Array values (e.g. the tokenizer) are skipped by
    size, so only the small header region is touched. Returns a dict, or None if the file is
    unreadable / the metadata exceeds `window` / the layout is unexpected."""
    try:
        with open(path, "rb") as fh:
            buf = fh.read(window)
    except OSError:
        return None
    if len(buf) < 24 or buf[:4] != b"GGUF":
        return None
    off = 4
    try:
        if struct.unpack_from("<I", buf, off)[0] < 2:      # version
            return None
        off += 4 + 8                                        # version + tensor_count (u64)
        kv_count = struct.unpack_from("<Q", buf, off)[0]; off += 8
    except struct.error:
        return None

    def take(n):                                            # bounds-checked cursor advance
        nonlocal off
        if off + n > len(buf):
            raise IndexError
        off += n
        return off - n

    def rd_str():
        ln = struct.unpack_from("<Q", buf, take(8))[0]
        return buf[take(ln):off].decode("utf-8", "replace")

    def skip(vtype):
        if vtype in _GGUF_SCALAR_SIZE:
            take(_GGUF_SCALAR_SIZE[vtype])
        elif vtype == _GGUF_STRING:
            rd_str()
        elif vtype == _GGUF_ARRAY:
            at = struct.unpack_from("<I", buf, take(4))[0]
            n = struct.unpack_from("<Q", buf, take(8))[0]
            if at in _GGUF_SCALAR_SIZE:
                take(_GGUF_SCALAR_SIZE[at] * n)
            elif at == _GGUF_STRING:
                for _ in range(n):
                    rd_str()
            else:
                raise IndexError                            # nested/unknown array → bail
        else:
            raise IndexError

    meta = {}
    try:
        for _ in range(kv_count):
            key = rd_str()
            vtype = struct.unpack_from("<I", buf, take(4))[0]
            if vtype == _GGUF_STRING:
                meta[key] = rd_str()
            elif vtype in _GGUF_SCALAR_FMT:
                meta[key] = struct.unpack_from(_GGUF_SCALAR_FMT[vtype], buf,
                                               take(_GGUF_SCALAR_SIZE[vtype]))[0]
            else:
                skip(vtype)                                 # arrays — never needed for the estimate
    except (IndexError, struct.error):
        return None

    arch = meta.get("general.architecture")
    if not arch:
        return None
    g = lambda s: meta.get(f"{arch}.{s}")
    n_layers, n_head = g("block_count"), g("attention.head_count")
    n_head_kv = g("attention.head_count_kv") or n_head
    n_embd = g("embedding_length")
    head_dim = g("attention.key_length") or ((n_embd // n_head) if (n_embd and n_head) else None)
    head_dim_v = g("attention.value_length") or head_dim       # V dim can differ from K
    if not (n_layers and n_head_kv and head_dim):
        return None
    return {"arch": arch, "n_layers": int(n_layers), "n_head_kv": int(n_head_kv),
            "head_dim": int(head_dim), "head_dim_v": int(head_dim_v), "n_embd": int(n_embd or 0),
            "sliding_window": int(g("attention.sliding_window") or 0),
            "n_ctx_train": int(g("context_length") or 0)}


def estimate(filename, ctx=8192, kv="f16", ngl=99, kv_v=None):
    """Estimate VRAM to serve `filename` at the given context / KV dtype / GPU-offload, against
    this box's VRAM. Accurate when the GGUF metadata parses (KV from the real n_layers/n_head_kv/
    head_dim — an UPPER bound, since sliding-window layers use less); otherwise a clearly-flagged
    heuristic (approx=True, KV omitted). All sizes are bytes."""
    base = Path(filename or "").name
    if not _safe_gguf(base):
        return {"ok": False, "error": "invalid filename"}
    path = config.MODELS_DIR / base
    if not path.exists():
        return {"ok": False, "error": "model file not found on disk"}
    try:
        ctx = max(256, min(int(ctx), 1_048_576)); ngl = max(0, min(int(ngl), 999))
    except (TypeError, ValueError):
        ctx, ngl = 8192, 99
    k = kv if kv in _KV_BYTES else "f16"
    v = kv_v if kv_v in _KV_BYTES else k
    size = path.stat().st_size
    total_vram, free_vram = _vram_bytes()
    meta = _gguf_meta(path)
    note = ""
    if meta:
        frac = min(1.0, ngl / meta["n_layers"]) if meta["n_layers"] else 1.0
        weights = size * frac
        kv_bytes = (meta["n_layers"] * ctx * meta["n_head_kv"]
                    * (meta["head_dim"] * _KV_BYTES[k] + meta["head_dim_v"] * _KV_BYTES[v]))
        approx = False
        sw = meta.get("sliding_window") or 0
        if 0 < sw < ctx:                          # real sliding window → full-attn KV is a loose ceiling
            note = f"sliding-window attention (window {sw:,}) — real KV is likely well below this estimate"
        elif meta["n_ctx_train"] and ctx > meta["n_ctx_train"]:
            note = f"context exceeds the model's trained max ({meta['n_ctx_train']:,}) — needs rope-scaling"
    else:
        weights, kv_bytes, approx = size, None, True
        note = "couldn't read model metadata — weights only (KV cache not included)"
    total = weights + (kv_bytes or 0) + _VRAM_OVERHEAD
    return {"ok": True, "filename": base, "ctx": ctx, "kv": k, "kv_v": v, "ngl": ngl,
            "weights_gpu": int(weights),
            "kv_bytes": (int(kv_bytes) if kv_bytes is not None else None),
            "overhead": _VRAM_OVERHEAD, "total": int(total),
            "vram_total": total_vram, "vram_free": free_vram,
            "fits": (total <= total_vram) if total_vram else None,
            "approx": approx, "note": note,
            "n_layers": meta["n_layers"] if meta else None,
            "n_ctx_train": meta["n_ctx_train"] if meta else None}


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
# Extra-flags escape hatch: this string is spliced into a shell-executed `cmd`, so it is strictly
# allowlisted (no shell metacharacters, no newlines) and length-capped — same posture as the
# validated name/filename. Anything outside this set is refused with a clear message.
_EXTRA_RE = re.compile(r"^[A-Za-z0-9 ._:=+/-]*$")
# Known llama-server flags Rivers manages as structured fields; these map flag -> integer param.
_FLAG_INT = {"-ngl": "ngl", "-c": "ctx", "-b": "batch", "-ub": "ubatch", "-t": "threads",
             "--n-cpu-moe": "n_cpu_moe", "--parallel": "parallel"}


def _norm_params(ngl=99, ctx=8192, fa=True, kv="f16", kv_v=None, ttl=600, threads=None,
                 batch=None, ubatch=None, n_cpu_moe=None, parallel=1, extra=""):
    """Clamp/allowlist every serving param to a shell-safe value. Returns (params, error)."""
    def _opt(x, lo, hi):                       # optional positive int: None/''/0 → omit the flag
        if x in (None, "", 0, "0"):
            return None
        return max(lo, min(int(x), hi))
    try:
        ngl = max(0, min(int(ngl), 999)); ctx = max(256, min(int(ctx), 1_048_576))
        ttl = max(0, min(int(ttl), 86400)); parallel = max(1, min(int(parallel or 1), 64))
        threads, batch = _opt(threads, 1, 1024), _opt(batch, 1, 1_048_576)
        ubatch, n_cpu_moe = _opt(ubatch, 1, 1_048_576), _opt(n_cpu_moe, 1, 999)
    except (TypeError, ValueError):
        return None, "numeric parameters must be integers"
    kv = kv if kv in _KV_TYPES else "f16"
    kv_v = kv_v if kv_v in _KV_TYPES else kv
    extra = (extra or "").strip()
    if len(extra) > 400 or not _EXTRA_RE.fullmatch(extra):
        return None, ("extra flags contain unsupported characters "
                      "(allowed: letters, digits, space and . _ : = + / -)")
    return {"ngl": ngl, "ctx": ctx, "fa": bool(fa), "kv": kv, "kv_v": kv_v, "ttl": ttl,
            "threads": threads, "batch": batch, "ubatch": ubatch, "n_cpu_moe": n_cpu_moe,
            "parallel": parallel, "extra": extra}, None


def _build_flags(p):
    """The llama-server flag string for a normalized params dict (from _norm_params)."""
    parts = [f"-ngl {p['ngl']}", f"-fa {'1' if p['fa'] else '0'}"]
    if p["n_cpu_moe"]:
        parts.append(f"--n-cpu-moe {p['n_cpu_moe']}")
    parts.append(f"--parallel {p['parallel']}")
    parts.append(f"-c {p['ctx']}")
    if p["batch"]:
        parts.append(f"-b {p['batch']}")
    if p["ubatch"]:
        parts.append(f"-ub {p['ubatch']}")
    if p["kv"] != "f16" or p["kv_v"] != "f16":        # f16 is the default; emit only when quantized
        parts.append(f"-ctk {p['kv']} -ctv {p['kv_v']}")
    if p["threads"]:
        parts.append(f"-t {p['threads']} --threads-batch {p['threads']}")
    if p["extra"]:
        parts.append(p["extra"])
    parts.append("--jinja")
    return " ".join(parts)


def _block_text(name, model_path, flags, ttl):
    """One llama-swap model block (the same format serve() has always written)."""
    return (
        f'  "{name}":\n'
        f'    cmd: |\n'
        f'      {config.LLAMA_SERVER_BIN}\n'
        f'      -m {model_path}\n'
        f'      {flags}\n'
        f'      --host 127.0.0.1 --port ${{PORT}}\n'
        f'    ttl: {ttl}\n'
    )


def _validate_has(text, name):
    """The new YAML must still parse AND contain `name` (catches an indentation slip)."""
    try:
        check = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        return False, str(e)
    if name not in (check.get("models") or {}):
        return False, "the edit did not register the model (indentation?)"
    return True, ""


def _block_span(lines, name):
    """(start, end) line indices for model `name`'s block: its `  "name":` key line through the
    last non-blank line before the next sibling (any line indented <= 2 spaces) or EOF. The block's
    own leading comments sit before `start`, so they survive an edit. None if the key isn't found."""
    keyre = re.compile(r'^  "?' + re.escape(name) + r'"?\s*:\s*$')
    start = next((i for i, ln in enumerate(lines) if keyre.match(ln)), None)
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        ln = lines[j]
        if not ln.strip():
            continue
        if len(ln) - len(ln.lstrip(" ")) <= 2:        # a sibling key OR the next block's comment
            end = j
            break
    while end - 1 > start and not lines[end - 1].strip():   # don't swallow trailing blank separators
        end -= 1
    return start, end


def _model_path_in(cmd):
    """The `-m <path>` argument from a llama-swap cmd string, or ''."""
    toks = (cmd or "").split()
    for i, t in enumerate(toks):
        if t == "-m" and i + 1 < len(toks):
            return toks[i + 1]
    return ""


def _parse_flags(cmd):
    """Decompose a llama-server cmd into structured params; UNKNOWN flags are preserved verbatim
    in `extra`, so editing a hand-tuned model never drops a flag (known ones like --n-cpu-moe
    round-trip into their field; anything without a field lands in `extra`)."""
    toks = (cmd or "").split()
    p = {"ngl": 99, "ctx": 8192, "fa": True, "kv": "f16", "kv_v": "f16", "threads": None,
         "batch": None, "ubatch": None, "n_cpu_moe": None, "parallel": 1}
    extra, i = [], 0
    while i < len(toks):
        t = toks[i]
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        if t.endswith("llama-server") or t == "--jinja":
            i += 1; continue
        if t in ("-m", "--host", "--port"):
            i += 2; continue                            # path/host/port are re-emitted by _block_text
        if t in _FLAG_INT:
            try: p[_FLAG_INT[t]] = int(nxt)
            except (TypeError, ValueError): pass
            i += 2; continue
        if t == "-fa":
            p["fa"] = (nxt != "0"); i += 2; continue
        if t == "-ctk":
            p["kv"] = nxt if nxt in _KV_TYPES else p["kv"]; i += 2; continue
        if t == "-ctv":
            p["kv_v"] = nxt if nxt in _KV_TYPES else p["kv_v"]; i += 2; continue
        if t == "--threads-batch":
            i += 2; continue                            # mirrors -t; rebuilt from `threads`
        extra.append(t); i += 1                          # unknown flag → keep verbatim
    p["extra"] = " ".join(extra).strip()
    return p


def serve(filename, name=None, ngl=99, ctx=8192, fa=True, kv="f16", ttl=600, kv_v=None,
          threads=None, batch=None, ubatch=None, n_cpu_moe=None, parallel=1, extra=""):
    """Append a model block to llama-swap.yaml (comment-preserving: we append text rather than
    re-dump). llama-swap's -watch-config hot-reloads it.

    Serving params (all settable from the GUI):
      ngl  GPU layers (0 = CPU)        ctx  context window (tokens)
      fa   flash attention on/off      kv/kv_v  K/V cache dtype: f16 | q8_0 | q4_0
      ttl  seconds resident after use  threads  CPU threads (-t / --threads-batch)
      batch (-b) · ubatch (-ub) · n_cpu_moe (MoE expert offload) · parallel · extra (extra flags)

    `filename`/`name`/`extra` end up inside a shell-executed `cmd`, so all are strictly validated
    (allowlist) and the numeric params clamped — none can break YAML or inject shell."""
    base = Path(filename or "").name
    if not _safe_gguf(base):
        return {"ok": False, "error": "invalid filename — must be a plain .gguf name"}
    name = _sanitize_name(name or Path(base).stem)
    if not name:
        return {"ok": False, "error": "invalid model name (use letters, digits, . _ -)"}
    path = config.MODELS_DIR / base
    if not path.exists():
        return {"ok": False, "error": "model file not found on disk"}
    p, err = _norm_params(ngl=ngl, ctx=ctx, fa=fa, kv=kv, kv_v=kv_v, ttl=ttl, threads=threads,
                          batch=batch, ubatch=ubatch, n_cpu_moe=n_cpu_moe, parallel=parallel, extra=extra)
    if err:
        return {"ok": False, "error": err}

    cfg = config.LLAMA_SWAP_CFG
    try:
        data = yaml.safe_load(cfg.read_text()) or {}
    except (OSError, yaml.YAMLError) as e:
        return {"ok": False, "error": f"cannot read llama-swap.yaml: {e}"}
    if name in set((data.get("models") or {}).keys()):
        return {"ok": False, "error": f"a model named {name!r} is already in llama-swap.yaml"}
    if _cmd_uses(base):
        return {"ok": False, "error": "this file is already served by llama-swap"}

    original = cfg.read_text()
    if "models:" not in original:
        return {"ok": False, "error": "llama-swap.yaml has no 'models:' section"}
    updated = original.rstrip("\n") + "\n\n" + _block_text(name, path, _build_flags(p), p["ttl"])
    ok, verr = _validate_has(updated, name)
    if not ok:
        return {"ok": False, "error": f"refused to write — would corrupt config: {verr}"}
    atomicio.write_text(cfg, updated)          # atomic: a crash can't truncate llama-swap.yaml
    return {"ok": True, "name": name, "ngl": p["ngl"], "ctx": p["ctx"], "fa": p["fa"],
            "kv": p["kv"], "kv_v": p["kv_v"], "ttl": p["ttl"]}


def served():
    """Current llama-swap model blocks as structured params (for the UI's edit/unserve list)."""
    try:
        data = yaml.safe_load(config.LLAMA_SWAP_CFG.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return {"models": []}
    out = []
    for name, spec in (data.get("models") or {}).items():
        cmd = (spec or {}).get("cmd", "") or ""
        out.append({"name": name, "filename": Path(_model_path_in(cmd)).name,
                    "ttl": (spec or {}).get("ttl", 600), **_parse_flags(cmd)})
    return {"models": out}


def update_served(name, ngl=99, ctx=8192, fa=True, kv="f16", ttl=600, kv_v=None, threads=None,
                  batch=None, ubatch=None, n_cpu_moe=None, parallel=1, extra=""):
    """Re-tune an already-served model in place. Surgically replaces ONLY that model's block (its
    leading comments and the rest of the file are untouched), keeping the existing model file
    path; re-validates before an atomic write."""
    name = _sanitize_name(name)
    if not name:
        return {"ok": False, "error": "invalid model name"}
    cfg = config.LLAMA_SWAP_CFG
    try:
        text = cfg.read_text()
        data = yaml.safe_load(text) or {}
    except (OSError, yaml.YAMLError) as e:
        return {"ok": False, "error": f"cannot read llama-swap.yaml: {e}"}
    spec = (data.get("models") or {}).get(name)
    if spec is None:
        return {"ok": False, "error": f"no served model named {name!r}"}
    model_path = _model_path_in(spec.get("cmd", ""))
    if not model_path:
        return {"ok": False, "error": "could not find the model path in the current config"}
    p, err = _norm_params(ngl=ngl, ctx=ctx, fa=fa, kv=kv, kv_v=kv_v, ttl=ttl, threads=threads,
                          batch=batch, ubatch=ubatch, n_cpu_moe=n_cpu_moe, parallel=parallel, extra=extra)
    if err:
        return {"ok": False, "error": err}
    lines = text.split("\n")
    span = _block_span(lines, name)
    if span is None:
        return {"ok": False, "error": "could not locate the model block to edit"}
    start, end = span
    block_lines = _block_text(name, model_path, _build_flags(p), p["ttl"]).rstrip("\n").split("\n")
    updated = "\n".join(lines[:start] + block_lines + lines[end:])
    if not updated.endswith("\n"):
        updated += "\n"
    ok, verr = _validate_has(updated, name)
    if not ok:
        return {"ok": False, "error": f"refused to write — would corrupt config: {verr}"}
    atomicio.write_text(cfg, updated)
    return {"ok": True, "name": name, "ngl": p["ngl"], "ctx": p["ctx"], "fa": p["fa"],
            "kv": p["kv"], "kv_v": p["kv_v"], "ttl": p["ttl"]}


def unserve(name):
    """Remove a model block from llama-swap.yaml (surgical delete; other blocks + comments stay).
    Re-validates that the model is gone and the file still parses before the atomic write."""
    name = _sanitize_name(name)
    if not name:
        return {"ok": False, "error": "invalid model name"}
    cfg = config.LLAMA_SWAP_CFG
    try:
        text = cfg.read_text()
        data = yaml.safe_load(text) or {}
    except (OSError, yaml.YAMLError) as e:
        return {"ok": False, "error": f"cannot read llama-swap.yaml: {e}"}
    if name not in (data.get("models") or {}):
        return {"ok": False, "error": f"no served model named {name!r}"}
    lines = text.split("\n")
    span = _block_span(lines, name)
    if span is None:
        return {"ok": False, "error": "could not locate the model block to remove"}
    start, end = span
    updated = re.sub(r"\n{3,}", "\n\n", "\n".join(lines[:start] + lines[end:]))
    if not updated.endswith("\n"):
        updated += "\n"
    try:
        check = yaml.safe_load(updated) or {}
    except yaml.YAMLError as e:
        return {"ok": False, "error": f"refused to write — would corrupt config: {e}"}
    if name in (check.get("models") or {}) or "models:" not in updated:
        return {"ok": False, "error": "refused — removal left the config inconsistent"}
    atomicio.write_text(cfg, updated)
    return {"ok": True, "name": name}


def delete_model(filename):
    """Delete a model's .gguf from disk to reclaim space. Guarded: refuses a model still wired
    into llama-swap (unserve it first), the configured embedding model (memory/RAG need it), and
    anything outside the models dir. Returns {ok, filename, freed} or {ok:False, error}."""
    base = Path(filename or "").name
    if not _safe_gguf(base):
        return {"ok": False, "error": "invalid filename — must be a plain .gguf name"}
    path = (config.MODELS_DIR / base).resolve()
    if not path.is_relative_to(config.MODELS_DIR.resolve()):     # defense-in-depth (basename already safe)
        return {"ok": False, "error": "path escapes the models directory"}
    if not path.is_file():
        return {"ok": False, "error": "model file not found on disk"}
    if path == Path(config.EMBED_MODEL).resolve():
        return {"ok": False, "error": "that's the embedding model — memory & document search need it"}
    served_as = _cmd_uses(base)
    if served_as:
        return {"ok": False, "error": f"still served as {served_as!r} — unserve it first, then delete"}
    try:
        freed = path.stat().st_size
        path.unlink()
    except OSError as e:
        return {"ok": False, "error": f"could not delete: {e}"}
    return {"ok": True, "filename": base, "freed": freed}
