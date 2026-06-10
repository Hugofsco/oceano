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

# --- Agent safety knobs ---
MAX_STEPS   = int(os.environ.get("OCEANO_MAX_STEPS", "12"))   # tool-call loop cap per turn
SHELL_TIMEOUT = int(os.environ.get("OCEANO_SHELL_TIMEOUT", "60"))
CONFINE_TO_WORKSPACE = os.environ.get("OCEANO_CONFINE", "1") == "1"  # file ops stay inside workspace

# --- Telegram frontend ---
# Token from @BotFather. ALLOWED = comma-separated Telegram user IDs that may use
# the bot. EMPTY ALLOWED = nobody (the bot refuses everyone) — this is on purpose,
# since the agent can run shell commands.
TELEGRAM_TOKEN = os.environ.get("OCEANO_TELEGRAM_TOKEN", "")
TELEGRAM_ALLOWED = {
    int(x) for x in os.environ.get("OCEANO_TELEGRAM_ALLOWED", "").replace(" ", "").split(",") if x
}

WORKSPACE.mkdir(parents=True, exist_ok=True)
