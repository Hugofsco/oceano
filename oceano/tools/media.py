"""Media tools: transcribe, speak, fetch (yt-dlp), and convert files."""
import os
import subprocess
import tempfile
from pathlib import Path

import config
from oceano import safety
from oceano.tools.core import _resolve, _ws, tool

# ============================ media: transcribe · speak · fetch · convert ============================
@tool({
    "type": "function",
    "function": {
        "name": "transcribe_media",
        "description": "Transcribe an audio OR video file in the workspace to text (local "
                       "faster-whisper) — e.g. a meeting recording, podcast, or a clip you fetched "
                       "with fetch_media. Returns the transcript.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "workspace path to the audio/video file"},
            "language": {"type": "string", "description": "language code like 'en' or 'es'; empty = auto-detect"},
        }, "required": ["path"]},
    },
})
def transcribe_media(path, language=""):
    from oceano import voice
    if not voice.stt_available():
        return "ERROR: speech-to-text unavailable (faster-whisper not installed)"
    p = _resolve(path)
    if not p.is_file():
        return f"(no such file: {path})"
    text = voice.transcribe(str(p), language=(language or None))
    if not text:                                  # video container PyAV can't open → extract audio first
        from shutil import which
        if which("ffmpeg"):
            wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
            try:
                r = subprocess.run(["ffmpeg", "-y", "-i", str(p), "-ac", "1", "-ar", "16000", wav],
                                   capture_output=True, timeout=config.SHELL_TIMEOUT)
                if r.returncode == 0:
                    text = voice.transcribe(wav, language=(language or None))
            except Exception:
                pass
            finally:
                try: os.remove(wav)
                except OSError: pass
    return text[:12000] if text else "(no speech detected, or the file isn't decodable audio/video)"


@tool({
    "type": "function",
    "function": {
        "name": "speak_to_file",
        "description": "Turn text into a spoken audio file (.ogg) saved in the workspace (local Piper "
                       "voice, espeak-ng fallback) — for a narrated summary or a spoken reply. Returns "
                       "a markdown audio reference.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"},
            "name": {"type": "string", "description": "output file name, default narration.ogg"},
        }, "required": ["text"]},
    },
})
def speak_to_file(text, name="narration.ogg"):
    import shutil
    from oceano import voice
    if not voice.tts_available():
        return "ERROR: text-to-speech unavailable (no Piper voice and espeak-ng not installed)"
    if not name.lower().endswith(".ogg"):
        name += ".ogg"
    tmp = voice.synthesize(text)
    if not tmp:
        return "ERROR: could not synthesize speech"
    dest = _resolve(name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(tmp, dest)
    except OSError as e:
        try: os.remove(tmp)
        except OSError: pass
        return f"ERROR saving audio: {e}"
    rel = dest.relative_to(_ws())
    return f"wrote spoken audio to {rel}\n\n![spoken audio]({rel})"


@tool({
    "type": "function",
    "function": {
        "name": "fetch_media",
        "description": "Download audio/video from a URL (YouTube and many other sites, via yt-dlp) "
                       "into the workspace — then you can transcribe_media it. Set audio_only for a "
                       "smaller MP3 when you just need the words.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"},
            "audio_only": {"type": "boolean", "description": "extract audio only (MP3) — best for transcription"},
            "name": {"type": "string", "description": "optional base filename (no extension)"},
        }, "required": ["url"]},
    },
})
def fetch_media(url, audio_only=False, name=""):
    refusal = safety.check_url(url)
    if refusal:
        return refusal
    try:
        import yt_dlp
    except ImportError:
        return "ERROR: yt-dlp not installed — `pip install yt-dlp`"
    outdir = _ws() / "downloads"
    outdir.mkdir(parents=True, exist_ok=True)
    base = "".join(c if (c.isalnum() or c in "._- ") else "_" for c in (name or "")).strip() or "%(title).80s"
    opts = {"outtmpl": str(outdir / (base + ".%(ext)s")), "noplaylist": True, "quiet": True,
            "no_warnings": True, "restrictfilenames": True, "max_filesize": 1024 * 1024 * 1024}
    if audio_only:
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}]
    else:
        opts["format"] = "bv*+ba/b"
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            fn = ydl.prepare_filename(info)
            if audio_only:
                fn = os.path.splitext(fn)[0] + ".mp3"
    except Exception as e:
        return f"ERROR downloading: {type(e).__name__}: {e}"
    p = Path(fn)
    if not p.exists():                            # postprocessing renamed it — grab the newest
        files = sorted(outdir.glob("*"), key=lambda x: x.stat().st_mtime, reverse=True)
        p = files[0] if files else p
    try:
        rel = p.relative_to(_ws())
    except ValueError:
        rel = p.name
    if not p.exists():
        return "ERROR: download produced no file"
    return (f"downloaded to {rel} ({p.stat().st_size // 1024} KB). "
            "Use transcribe_media on it to get a transcript.")


@tool({
    "type": "function",
    "function": {
        "name": "convert",
        "description": "Convert a workspace file to another format: media via ffmpeg (mp4→mp3, wav→ogg, "
                       "…), documents via pandoc (docx→md, md→pdf, …), images via ImageMagick "
                       "(png→jpg, …). Returns the new file's path.",
        "parameters": {"type": "object", "properties": {
            "source": {"type": "string", "description": "workspace path of the file to convert"},
            "to": {"type": "string", "description": "target format / extension, e.g. 'mp3', 'md', 'jpg'"},
        }, "required": ["source", "to"]},
    },
})
def convert(source, to):
    from shutil import which
    p = _resolve(source)
    if not p.is_file():
        return f"(no such file: {source})"
    to = (to or "").lstrip(".").lower()
    if not to:
        return "ERROR: specify a target format, e.g. to='mp3' or to='md'"
    src_ext = p.suffix.lower().lstrip(".")
    dest = p.with_suffix("." + to)
    IMG = {"png", "jpg", "jpeg", "webp", "gif", "bmp", "tiff"}
    DOCS = {"md", "markdown", "html", "pdf", "docx", "txt", "rst", "epub", "tex", "odt"}
    if to in IMG and src_ext in IMG:
        bin_ = which("magick") or which("convert")
        if not bin_:
            return "ERROR: image conversion needs ImageMagick — `apt install imagemagick`"
        cmd = [bin_, str(p), str(dest)]
    elif to in DOCS or src_ext in DOCS:
        if not which("pandoc"):
            return "ERROR: document conversion needs pandoc — `apt install pandoc`"
        cmd = ["pandoc", str(p), "-o", str(dest)]
    else:
        if not which("ffmpeg"):
            return "ERROR: media conversion needs ffmpeg"
        cmd = ["ffmpeg", "-y", "-i", str(p), str(dest)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=max(config.SHELL_TIMEOUT, 300))
    except subprocess.TimeoutExpired:
        return "ERROR: conversion timed out"
    if r.returncode != 0 or not dest.exists():
        return f"ERROR converting: {((r.stderr or r.stdout) or '').strip()[:500]}"
    return f"converted to {dest.relative_to(_ws())} ({dest.stat().st_size // 1024} KB)"
