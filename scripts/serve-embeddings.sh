#!/usr/bin/env bash
# Dedicated llama.cpp embedding server for Oceano's memory.
# Tiny model, CPU-only (-ngl 0) so it never touches the 8GB GPU or evicts the
# chat model in llama-swap. Listens on :8082, separate from llama-swap (:8081).
#
# Paths come from config.py (so this follows OCEANO_LLAMA_DIR / the install layout
# and survives relocating llama.cpp — no hardcoded home paths).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
eval "$("$ROOT/venv/bin/python" - <<'PY'
import shlex, config
print("LLAMA_BIN=" + shlex.quote(str(config.LLAMA_SERVER_BIN)))
print("MODEL=" + shlex.quote(str(config.EMBED_MODEL)))
PY
)"

if [[ ! -x "$LLAMA_BIN" ]]; then
  echo "llama-server not found/executable: $LLAMA_BIN (build llama.cpp first)"; exit 1
fi
if [[ ! -f "$MODEL" ]]; then
  echo "Embedding model not found: $MODEL"
  echo "Download it (~140 MB) with:"
  echo "  curl -L -o \"$MODEL\" \\"
  echo "    https://huggingface.co/nomic-ai/nomic-embed-text-v1.5-GGUF/resolve/main/nomic-embed-text-v1.5.Q8_0.gguf"
  exit 1
fi

# llama.cpp's shared libs sit next to the binary; point the linker at them so a
# relocated build (its RUNPATH is baked at build time) still resolves them.
export LD_LIBRARY_PATH="$(dirname "$LLAMA_BIN")${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

# -b/-ub 2048: a pooled embedding needs the WHOLE input in one (u)batch, so the default 512 silently
# fails on long memories/docs. 2048 = nomic's native context — covers any single memory or chunk.
exec "$LLAMA_BIN" \
  -m "$MODEL" \
  --embedding --pooling mean \
  -ngl 0 -c 8192 \
  -b 2048 -ub 2048 \
  --host 127.0.0.1 --port 8082
