"""Central config for Oceano. Override anything via environment variables."""
import os
from pathlib import Path

# --- LLM endpoint (your llama-swap, OpenAI-compatible) ---
LLM_BASE_URL = os.environ.get("OCEANO_LLM_URL", "http://127.0.0.1:8081/v1")
LLM_API_KEY  = os.environ.get("OCEANO_LLM_KEY", "sk-no-key-needed")  # llama.cpp ignores it

# Model. There is deliberately NO hardcoded default — Oceano resolves the model it uses from
# what you've actually set up (delegate.resolve_primary): your chosen primary (Settings →
# Delegation), else a model you've served via Brain → Rivers. OCEANO_MODEL only force-pins one
# (handy for headless/dev); left unset, an empty string means "ask Rivers what's available".
MODEL = os.environ.get("OCEANO_MODEL", "")

# How long to wait on the LLM endpoint. CONNECT is short so a down llama-swap fails fast
# instead of wedging a turn (esp. Telegram's single event loop); READ is the max idle gap
# while a response is in flight — per-chunk when streaming — so it trips on a half-open
# connection but never cuts off a slow-but-live generation or a cold model swap.
LLM_CONNECT_TIMEOUT = float(os.environ.get("OCEANO_LLM_CONNECT_TIMEOUT", "10"))
LLM_TIMEOUT = float(os.environ.get("OCEANO_LLM_TIMEOUT", "300"))

# --- Workspace: the folder the agent actually works in ---
WORKSPACE = Path(os.environ.get("OCEANO_WORKSPACE", Path(__file__).parent / "workspace")).resolve()

# --- Web search (your running SearXNG) ---
SEARXNG_URL = os.environ.get("OCEANO_SEARXNG", "http://127.0.0.1:8080")

# --- http_request tool: hosts it may reach even though they're internal/local (e.g. a Home
# Assistant box at 192.168.x.x or homeassistant.local). The SSRF guard blocks ALL other internal
# addresses; this is the deliberate, opt-in exception. Comma-separated hostnames/IPs.
HTTP_ALLOW = {h.strip().lower() for h in os.environ.get("OCEANO_HTTP_ALLOW", "").split(",") if h.strip()}

# --- Local model serving (llama.cpp + llama-swap), used by the Rivers ---
# Knob: OCEANO_LLAMA_DIR (single, canonical — install.sh reads the same name).
# Default: keep the stack tidy under the Oceano dir (fresh installs build there),
# but honour a pre-existing ~/llama.cpp so older setups aren't relocated/broken.
_OCEANO_ROOT = Path(__file__).resolve().parent
def _default_llama_dir():
    env = os.environ.get("OCEANO_LLAMA_DIR")
    if env:
        return Path(env)
    local, legacy = _OCEANO_ROOT / "llama.cpp", Path.home() / "llama.cpp"
    return legacy if (not local.exists() and legacy.exists()) else local
LLAMA_DIR = _default_llama_dir()
MODELS_DIR = Path(os.environ.get("OCEANO_MODELS_DIR", LLAMA_DIR / "models"))
EMBED_MODEL = Path(os.environ.get("OCEANO_EMBED_MODEL", MODELS_DIR / "nomic-embed-text-v1.5.Q8_0.gguf"))
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
