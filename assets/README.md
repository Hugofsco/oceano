# assets/ — bundled runtime models (not committed to git)

Large model binaries live here so Oceano is self-contained at runtime, but they are
**not in git** (GitHub caps files at 100 MB; the Whisper model alone is ~139 MB).
Populate them on install / first run:

- **TTS voice** — `assets/voice/<voice>.onnx` (+ matching `.onnx.json`). A Piper voice,
  e.g. from https://huggingface.co/rhasspy/piper-voices . Default expected: `alan.onnx`,
  or point `OCEANO_TTS_VOICE` elsewhere. (espeak-ng is the automatic fallback.)
- **STT model** — `assets/whisper/` is filled **automatically** by faster-whisper on the
  first Telegram voice note (model id `OCEANO_STT_MODEL`, default `base.en`).
