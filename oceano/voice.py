"""Voice for the Telegram channel: speech-in (faster-whisper) and speech-out (Piper).

Self-contained: both engines run in Oceano's own venv and their models live under
Oceano/assets, so packaging is just `pip install -r requirements.txt` + the bundled
assets. ffmpeg converts Piper's WAV to the OGG/Opus that Telegram voice notes use.
If a voice can't be loaded, TTS falls back to espeak-ng; if neither works, callers
get None/'' and simply stay text-only.
"""
import os
import subprocess
import sys
import tempfile
from shutil import which

import config

_whisper = None        # lazily-loaded faster-whisper model (load once, reuse)


# ---------------- availability ----------------
def stt_available():
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


def tts_available():
    return config.TTS_VOICE.exists() or which("espeak-ng") is not None


def status():
    return {"stt": stt_available(), "stt_model": config.STT_MODEL,
            "tts": tts_available(), "tts_voice": config.TTS_VOICE.stem if config.TTS_VOICE.exists() else "espeak-ng"}


# ---------------- speech → text ----------------
def _model():
    global _whisper
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
def synthesize(text):
    """Render `text` to an OGG/Opus voice-note file and return its path (the CALLER
    deletes it), or None if speech is unavailable. Piper if a voice is bundled, else
    espeak-ng as a robotic fallback."""
    text = (text or "").strip()[:config.TTS_MAX_CHARS]
    if not text:
        return None
    wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
    ogg = tempfile.NamedTemporaryFile(suffix=".ogg", delete=False).name
    ok = False
    try:
        made = False
        if config.TTS_VOICE.exists():
            r = subprocess.run([sys.executable, "-m", "piper", "--model", str(config.TTS_VOICE),
                                "--output_file", wav], input=text, text=True,
                               capture_output=True, timeout=120)
            made = r.returncode == 0 and os.path.exists(wav) and os.path.getsize(wav) > 0
        if not made and which("espeak-ng"):     # robotic fallback, always available
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
