"""Central config for Oceano. Override anything via environment variables."""
import os
from pathlib import Path

# --- LLM endpoint (your llama-swap, OpenAI-compatible) ---
LLM_BASE_URL = os.environ.get("OCEANO_LLM_URL", "http://127.0.0.1:8081/v1")
LLM_API_KEY  = os.environ.get("OCEANO_LLM_KEY", "sk-no-key-needed")  # llama.cpp ignores it

# Default model. qwen3-4b is already loaded + fast = snappy dev.
# For harder multi-step jobs: OCEANO_MODEL=gpt-oss-20b (stronger, but triggers a ~10-15s swap).
MODEL = os.environ.get("OCEANO_MODEL", "qwen3-4b")

# --- Workspace: the folder the agent actually works in ---
WORKSPACE = Path(os.environ.get("OCEANO_WORKSPACE", Path(__file__).parent / "workspace")).resolve()

# --- Web search (your running SearXNG) ---
SEARXNG_URL = os.environ.get("OCEANO_SEARXNG", "http://127.0.0.1:8080")

# --- Local model serving (llama.cpp + llama-swap), used by the Rivers ---
LLAMA_DIR = Path(os.environ.get("OCEANO_LLAMA_DIR", Path.home() / "llama.cpp"))
MODELS_DIR = Path(os.environ.get("OCEANO_MODELS_DIR", LLAMA_DIR / "models"))
LLAMA_SERVER_BIN = os.environ.get("OCEANO_LLAMA_SERVER_BIN", str(LLAMA_DIR / "build/bin/llama-server"))
LLAMA_SWAP_CFG = Path(os.environ.get("OCEANO_LLAMA_SWAP_CFG", LLAMA_DIR / "llama-swap.yaml"))
# Optional Hugging Face token — only needed to list/download gated repos.
HF_TOKEN = os.environ.get("HF_TOKEN", "") or os.environ.get("OCEANO_HF_TOKEN", "")

# --- Agent safety knobs ---
MAX_STEPS   = int(os.environ.get("OCEANO_MAX_STEPS", "25"))   # tool-call loop cap per turn (multi-file builds need headroom)
SHELL_TIMEOUT = int(os.environ.get("OCEANO_SHELL_TIMEOUT", "60"))
CONFINE_TO_WORKSPACE = os.environ.get("OCEANO_CONFINE", "1") == "1"  # file ops stay inside workspace

# After each turn, a background LLM pass extracts durable facts and saves new ones
# (self-learning memory). Set OCEANO_AUTO_LEARN=0 to disable.
AUTO_LEARN = os.environ.get("OCEANO_AUTO_LEARN", "1") == "1"

# --- Telegram frontend ---
# Token from @BotFather. ALLOWED = comma-separated Telegram user IDs that may use
# the bot. EMPTY ALLOWED = nobody (the bot refuses everyone) — this is on purpose,
# since the agent can run shell commands.
TELEGRAM_TOKEN = os.environ.get("OCEANO_TELEGRAM_TOKEN", "")
TELEGRAM_ALLOWED = {
    int(x) for x in os.environ.get("OCEANO_TELEGRAM_ALLOWED", "").replace(" ", "").split(",") if x
}

# --- Voice (Telegram speech in/out) ---
# Everything lives under Oceano/assets so the whole thing packages cleanly.
#   STT: faster-whisper (model cached in assets/whisper)
#   TTS: Piper (voice .onnx in assets/voice) → ffmpeg → OGG/Opus
ASSETS = WORKSPACE.parent / "assets"
STT_MODEL = os.environ.get("OCEANO_STT_MODEL", "base.en")          # faster-whisper model id
STT_DIR = Path(os.environ.get("OCEANO_STT_DIR", ASSETS / "whisper"))
STT_DEVICE = os.environ.get("OCEANO_STT_DEVICE", "cpu")
STT_COMPUTE = os.environ.get("OCEANO_STT_COMPUTE", "int8")         # int8 = fast on CPU
TTS_VOICE = Path(os.environ.get("OCEANO_TTS_VOICE", ASSETS / "voice" / "alan.onnx"))
TTS_MAX_CHARS = int(os.environ.get("OCEANO_TTS_MAX_CHARS", "900")) # cap spoken length

WORKSPACE.mkdir(parents=True, exist_ok=True)
