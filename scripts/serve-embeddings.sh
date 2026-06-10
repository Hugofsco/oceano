#!/usr/bin/env bash
# Dedicated llama.cpp embedding server for Oceano's memory.
# Tiny model, CPU-only (-ngl 0) so it never touches the 8GB GPU or evicts the
# chat model in llama-swap. Listens on :8082, separate from llama-swap (:8081).
set -euo pipefail

LLAMA_BIN=/home/user/llama.cpp/build/bin/llama-server
MODEL=/home/user/llama.cpp/models/nomic-embed-text-v1.5.Q8_0.gguf

if [[ ! -f "$MODEL" ]]; then
  echo "Embedding model not found: $MODEL"
  echo "Download it (~140 MB) with:"
  echo "  curl -L -o \"$MODEL\" \\"
  echo "    https://huggingface.co/nomic-ai/nomic-embed-text-v1.5-GGUF/resolve/main/nomic-embed-text-v1.5.Q8_0.gguf"
  exit 1
fi

exec "$LLAMA_BIN" \
  -m "$MODEL" \
  --embedding --pooling mean \
  -ngl 0 -c 8192 \
  --host 127.0.0.1 --port 8082
