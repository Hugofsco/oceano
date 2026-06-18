"""Voice: speech-in (faster-whisper) and speech-out (Kokoro → Piper → espeak-ng).

Self-contained: every engine runs in Oceano's own venv and its model lives under Oceano/assets,
so packaging is just `pip install -r requirements.txt` + the bundled assets. ffmpeg converts the
synthesized WAV to the OGG/Opus that voice notes / the web player use.

TTS engine order (config.TTS_ENGINE): KOKORO — a natural neural voice, local & CPU-friendly, the
default when its model is present — then PIPER, then espeak-ng as a robotic last resort. If none
work, callers get None/'' and simply stay text-only.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from shutil import which

import config
from oceano import atomicio

_VOICE_SETTINGS = config.WORKSPACE.parent / "data" / "voice.json"   # runtime TTS prefs (picker)

_whisper = None        # lazily-loaded faster-whisper model (load once, reuse)
_kokoro = None         # lazily-loaded Kokoro TTS model (load once, reuse)
_tts_lock = threading.Lock()   # guards the lazy model loads against concurrent first-use
_synth_lock = threading.Lock() # serializes Kokoro inference (kokoro-onnx isn't guaranteed re-entrant)

# Piper voices live next to the bundled one (assets/voice/), so a downloaded voice packages the
# same way. The rhasspy/piper-voices catalog (one small JSON) lists every voice + its files; each
# voice is an <name>.onnx (the model) plus a sibling <name>.onnx.json (its config), fetched from the
# resolve/ URL. We cache the catalog on disk so Browse works offline after the first fetch.
PIPER_DIR = config.TTS_VOICE.parent
_PIPER_HF = "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
_PIPER_CATALOG_URL = _PIPER_HF + "voices.json"
_PIPER_CATALOG_CACHE = PIPER_DIR / ".piper-catalog.json"
_piper_catalog_mem = None      # in-process cache of the parsed catalog


# ---------------- availability ----------------
def stt_available():
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


def kokoro_available():
    """True if the Kokoro model is on disk and its libs import — the natural primary voice."""
    if not (config.KOKORO_MODEL.exists() and config.KOKORO_VOICES.exists()):
        return False
    try:
        import kokoro_onnx, soundfile  # noqa: F401
        return True
    except Exception:
        return False


def get_settings():
    """Runtime TTS prefs (data/voice.json) layered over the config/env defaults — so the Voice
    window can change engine/voice/speed live, no restart (voice is passed per-call to Kokoro)."""
    try:
        d = json.loads(_VOICE_SETTINGS.read_text())
    except (OSError, ValueError):
        d = {}
    if not isinstance(d, dict):
        d = {}
    try:
        speed = float(d.get("speed"))
    except (TypeError, ValueError):
        speed = config.KOKORO_SPEED
    eng = d.get("engine")
    return {"engine": eng if eng in ("auto", "kokoro", "piper") else config.TTS_ENGINE,
            "voice": d.get("voice") or config.KOKORO_VOICE, "speed": speed,
            "piper_voice": d.get("piper_voice") or config.TTS_VOICE.name,  # active Piper .onnx (under PIPER_DIR)
            "wake": bool(d.get("wake", False)),                       # require the wake word in conversation mode
            "wake_word": (d.get("wake_word") or config.WAKE_WORD)}    # the phrase to listen for


def list_voices():
    """The Kokoro voice names available to pick from (empty if Kokoro isn't set up)."""
    if not kokoro_available():
        return []
    try:
        return sorted(_kokoro_model().get_voices())
    except Exception:
        return []


def set_settings(engine=None, voice=None, speed=None, wake=None, wake_word=None, piper_voice=None):
    """Update + persist TTS prefs; the voice is validated against list_voices(). Returns the new settings."""
    cur = get_settings()
    if engine in ("auto", "kokoro", "piper"):
        cur["engine"] = engine
    if voice:
        vs = list_voices()
        if not vs or voice in vs:                 # accept a valid voice (or any if we can't enumerate)
            cur["voice"] = str(voice)
    if speed is not None:
        try:
            cur["speed"] = max(0.5, min(float(speed), 2.0))
        except (TypeError, ValueError):
            pass
    if piper_voice:
        name = os.path.basename(str(piper_voice))                     # accept only a bare filename we host locally
        if name.endswith(".onnx") and (PIPER_DIR / name).exists():
            cur["piper_voice"] = name
    if wake is not None:
        cur["wake"] = bool(wake)
    if wake_word is not None:
        w = str(wake_word).strip()
        if w:                                     # never let the wake phrase become empty
            cur["wake_word"] = w
    try:
        atomicio.write_text(_VOICE_SETTINGS, json.dumps(cur))
    except OSError:
        pass
    return cur


# ---------------- Piper voices: browse + download ----------------
def _piper_voice_path():
    """The active Piper voice file: the picked one if present, else the bundled default, else None."""
    name = get_settings().get("piper_voice")
    if name:
        p = PIPER_DIR / os.path.basename(name)
        if p.exists():
            return p
    return config.TTS_VOICE if config.TTS_VOICE.exists() else None


def piper_installed():
    """Local Piper voices: every <name>.onnx under PIPER_DIR that also has its <name>.onnx.json config
    sibling — so a half-downloaded voice never shows as usable. Returns [{file, name, active}]."""
    active = _piper_voice_path()
    active = active.name if active else None
    out = []
    try:
        for p in sorted(PIPER_DIR.glob("*.onnx")):
            if not p.with_name(p.name + ".json").exists():   # no config → Piper can't use it; skip
                continue
            out.append({"file": p.name, "name": p.stem, "active": p.name == active})
    except OSError:
        pass
    return out


def piper_catalog(force=False):
    """Fetch + cache the rhasspy/piper-voices catalog (dict keyed by voice key). Cached in memory and
    on disk so Browse keeps working offline after the first successful fetch. Returns {} on failure."""
    global _piper_catalog_mem
    if _piper_catalog_mem is not None and not force:
        return _piper_catalog_mem
    data = None
    try:
        import httpx
        r = httpx.get(_PIPER_CATALOG_URL, timeout=25, follow_redirects=True)
        r.raise_for_status()
        data = r.json()
        try:
            PIPER_DIR.mkdir(parents=True, exist_ok=True)
            atomicio.write_text(_PIPER_CATALOG_CACHE, json.dumps(data))
        except OSError:
            pass
    except Exception:
        try:
            data = json.loads(_PIPER_CATALOG_CACHE.read_text())   # offline fallback
        except (OSError, ValueError):
            data = None
    if not isinstance(data, dict):
        data = {}
    _piper_catalog_mem = data
    return data


def piper_languages():
    """[{code, name, count}] from the catalog, English first then alphabetical — for the Browse filter."""
    langs = {}
    for v in piper_catalog().values():
        lg = v.get("language") or {}
        code = lg.get("code")
        if not code:
            continue
        langs.setdefault(code, {"code": code, "name": lg.get("name_english") or code, "count": 0})
        langs[code]["count"] += 1
    return sorted(langs.values(), key=lambda x: (not x["code"].startswith("en"), x["name"]))


def piper_list(lang=None):
    """Catalog voices, optionally filtered to one language code. Sorted by quality (high→low) then name."""
    installed = {i["file"] for i in piper_installed()}
    qrank = {"x_low": 0, "low": 1, "medium": 2, "high": 3}
    out = []
    for key, v in piper_catalog().items():
        lg = v.get("language") or {}
        if lang and lg.get("code") != lang:
            continue
        size = sum(m.get("size_bytes", 0) for p, m in (v.get("files") or {}).items() if p.endswith(".onnx"))
        out.append({"key": key, "name": v.get("name") or key, "quality": v.get("quality") or "",
                    "speakers": v.get("num_speakers") or 1, "size_mb": round(size / 1e6, 1),
                    "lang_code": lg.get("code"), "lang_name": lg.get("name_english"),
                    "installed": (key + ".onnx") in installed})
    out.sort(key=lambda x: (-qrank.get(x["quality"], 0), x["name"]))
    return out


def piper_download(key):
    """Download a catalog voice's .onnx + .onnx.json into PIPER_DIR (md5-verified, confined to that
    dir). Every file is staged to a .part and verified BEFORE anything is committed, so a flaky
    network can't leave a half-installed voice; leftover .part files are always cleaned up. Returns
    {'ok': True, 'file': '<key>.onnx'} or {'ok': False, 'error': '...'}."""
    import re
    v = piper_catalog().get(key)
    if not v:
        return {"ok": False, "error": "unknown voice"}
    want = [(p, m) for p, m in (v.get("files") or {}).items()
            if p.endswith(".onnx") or p.endswith(".onnx.json")]
    if not any(p.endswith(".onnx") for p, _ in want):
        return {"ok": False, "error": "no model file in catalog entry"}
    staged = []                                          # (final_path, part_path) downloaded but not yet committed
    try:
        import httpx
        PIPER_DIR.mkdir(parents=True, exist_ok=True)
        saved = None
        # 1) fetch EVERY file to a .part and md5-verify it before committing anything
        for path, meta in want:
            name = os.path.basename(path)
            if not re.fullmatch(r"[A-Za-z0-9._-]+\.onnx(\.json)?", name):   # no traversal / junk names
                return {"ok": False, "error": "bad filename in catalog"}
            tmp = PIPER_DIR / (name + ".part")
            staged.append((PIPER_DIR / name, tmp))
            h = hashlib.md5()
            with httpx.stream("GET", _PIPER_HF + path, timeout=300, follow_redirects=True) as r:
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in r.iter_bytes(65536):
                        f.write(chunk)
                        h.update(chunk)
            digest = meta.get("md5_digest")
            if digest and h.hexdigest() != digest:
                return {"ok": False, "error": "checksum mismatch — download corrupt, try again"}
            if name.endswith(".onnx"):
                saved = name
        # 2) all files present + verified → commit them together
        for dst, tmp in staged:
            os.replace(tmp, dst)
        staged = []                                      # committed; nothing left to clean up
        return {"ok": True, "file": saved}
    except Exception as e:
        return {"ok": False, "error": f"download failed: {type(e).__name__}"}
    finally:
        for _dst, tmp in staged:                         # any .part not committed (error/mismatch) → remove
            try:
                os.remove(tmp)
            except OSError:
                pass


def _engine():
    """Resolve the active engine from settings: 'kokoro' | 'piper' | 'auto' (Kokoro when present)."""
    e = get_settings()["engine"].lower()
    if e in ("kokoro", "piper"):
        return e
    return "kokoro" if kokoro_available() else "piper"


def tts_available():
    return kokoro_available() or _piper_voice_path() is not None or which("espeak-ng") is not None


def status():
    s = get_settings()
    eng = _engine() if tts_available() else None
    if eng == "kokoro":
        voice = s["voice"]
    elif eng == "piper":
        pv = _piper_voice_path()
        voice = pv.stem if pv else "espeak-ng"
    else:
        voice = None
    return {"stt": stt_available(), "stt_model": config.STT_MODEL,
            "tts": tts_available(), "tts_engine": eng, "tts_voice": voice,
            "wake": s["wake"], "wake_word": s["wake_word"]}


def reload():
    """Drop the loaded STT/TTS models so the next call reloads them — e.g. after changing the voice or
    swapping the model files on disk. Models are lazy-loaded, so this just clears the in-process cache."""
    global _whisper, _kokoro
    with _tts_lock:
        _whisper = _kokoro = None
    return True


# ---------------- speech → text ----------------
def _model():
    global _whisper
    if _whisper is None:
        with _tts_lock:                          # don't load the model twice under concurrent first-use
            if _whisper is None:
                from faster_whisper import WhisperModel
                _whisper = WhisperModel(config.STT_MODEL, device=config.STT_DEVICE,
                                        compute_type=config.STT_COMPUTE, download_root=str(config.STT_DIR))
    return _whisper


def transcribe(audio_path, language="en"):
    """Transcribe an audio file (Telegram voice notes are OGG/Opus; faster-whisper
    reads them directly). Returns the transcript text, or '' on failure/empty."""
    if not stt_available():
        return ""
    try:
        segments, _info = _model().transcribe(str(audio_path), language=language)
        return " ".join(s.text for s in segments).strip()
    except Exception:
        return ""


# ---------------- text → speech ----------------
def _kokoro_model():
    global _kokoro
    if _kokoro is None:
        with _tts_lock:
            if _kokoro is None:
                from kokoro_onnx import Kokoro
                _kokoro = Kokoro(str(config.KOKORO_MODEL), str(config.KOKORO_VOICES))
    return _kokoro


def _kokoro_wav(text, wav_path):
    """Synthesize `text` to a WAV with Kokoro (the natural voice). Returns True on success."""
    try:
        import soundfile as sf
        s = get_settings()
        with _synth_lock:                            # one Kokoro inference at a time (web + Telegram can overlap)
            samples, sr = _kokoro_model().create(text, voice=s["voice"], speed=s["speed"], lang="en-us")
        if samples is None or len(samples) == 0:
            return False
        sf.write(wav_path, samples, sr)
        return os.path.exists(wav_path) and os.path.getsize(wav_path) > 0
    except Exception:
        return False


# Emoji / pictographs / symbol blocks that TTS would otherwise mispronounce or stumble over.
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"   # emoji, pictographs, transport, supplemental, flags
    "\U00002600-\U000027BF"   # misc symbols + dingbats (✓ ✗ ☀ ✉ …)
    "\U00002B00-\U00002BFF"   # misc symbols & arrows (⭐ ⬆ …)
    "\U00002300-\U000023FF"   # misc technical (⏰ ⌚ ⏳ …)
    "\U00002190-\U000021FF"   # arrows (← → ↕ …)
    "\U0000FE00-\U0000FE0F"   # emoji variation selectors
    "\U0000200D\U000020E3"    # zero-width joiner, combining keycap
    "•·▪◦‣⁃●○■□"             # bullet / list glyphs
    "]+",
    flags=re.UNICODE,
)


def _speakable(text):
    """Turn an assistant reply (markdown + emoji) into clean text for TTS, so it doesn't read '*',
    backticks, or emoji aloud. Only the SPOKEN text is affected — the on-screen reply keeps its
    formatting. Used for both the web voice conversation and Telegram voice notes."""
    t = text or ""
    t = re.sub(r"```.*?```", " ", t, flags=re.DOTALL)        # fenced code blocks → drop (you read those on screen)
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", t)              # images → drop
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)           # links → keep just the visible text
    t = re.sub(r"https?://\S+", " link ", t)                # bare URLs → 'link' (don't spell the whole thing out)
    t = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", t)             # # headings
    t = re.sub(r"(?m)^\s{0,3}>\s?", "", t)                  # > blockquotes
    t = re.sub(r"(?m)^\s*[-*+•·]\s+", "", t)                # bullet list markers
    t = re.sub(r"(?m)^\s*\d+[.)]\s+", "", t)                # numbered list markers
    t = re.sub(r"(?m)^\s*[-*_]{3,}\s*$", " ", t)            # --- horizontal rules
    t = t.replace("`", "")                                   # inline code → keep the word, drop the backticks
    t = re.sub(r"[*~]+", "", t)                              # ** * ~~ markers (never part of a word)
    t = re.sub(r"(?<![A-Za-z0-9])_+|_+(?![A-Za-z0-9])", "", t)  # _emphasis_ underscores, but keep snake_case intact
    t = t.replace("|", " ")                                  # table pipes
    t = _EMOJI_RE.sub(" ", t)                                # emoji / pictographs / bullet glyphs
    t = re.sub(r"[ \t]+", " ", t)                            # collapse runs of spaces
    t = re.sub(r" *\n *", "\n", t)                           # tidy around newlines
    t = re.sub(r"\n{2,}", "\n", t)                           # collapse blank lines
    return t.strip()


def synthesize(text):
    """Render `text` to an OGG/Opus voice-note file and return its path (the CALLER deletes it), or
    None if speech is unavailable. Engine order: Kokoro (natural) → Piper → espeak-ng (robotic)."""
    text = _speakable(text)[:config.TTS_MAX_CHARS]           # strip markdown + emoji before speaking
    if not text:
        return None
    wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
    ogg = tempfile.NamedTemporaryFile(suffix=".ogg", delete=False).name
    ok = False
    try:
        made = False
        if _engine() == "kokoro" and kokoro_available():
            made = _kokoro_wav(text, wav)
        pv = _piper_voice_path()
        if not made and pv is not None:                    # Piper (the selected voice, else the bundled one)
            r = subprocess.run([sys.executable, "-m", "piper", "--model", str(pv),
                                "--output_file", wav], input=text, text=True,
                               capture_output=True, timeout=120)
            made = r.returncode == 0 and os.path.exists(wav) and os.path.getsize(wav) > 0
        if not made and which("espeak-ng"):               # robotic last resort
            subprocess.run(["espeak-ng", "-w", wav, text], capture_output=True, timeout=60)
            made = os.path.exists(wav) and os.path.getsize(wav) > 0
        if made:
            conv = subprocess.run(["ffmpeg", "-y", "-i", wav, "-c:a", "libopus", "-b:a", "32k", ogg],
                                  capture_output=True, timeout=60)
            ok = conv.returncode == 0 and os.path.exists(ogg) and os.path.getsize(ogg) > 0
        return ogg if ok else None               # caller deletes the returned file
    except Exception:
        return None
    finally:
        try:
            os.remove(wav)
        except OSError:
            pass
        if not ok:
            try:
                os.remove(ogg)
            except OSError:
                pass
