#!/usr/bin/env bash
# Dedicated llama.cpp reranker (cross-encoder) for Oceano's RAG: re-orders the dense
# retrieval candidates by joint query-doc relevance. CPU-only (-ngl 0) so it never
# touches the GPU or evicts the chat model in llama-swap. Listens on :8084.
#
# OPTIONAL: if the reranker model isn't present, the engine just skips this server and
# RAG stays dense (see oceano/engine.py rerank_supervisor). Paths come from config.py.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
eval "$("$ROOT/venv/bin/python" - <<'PY'
import shlex, config
print("LLAMA_BIN=" + shlex.quote(str(config.LLAMA_SERVER_BIN)))
print("MODEL=" + shlex.quote(str(config.RERANK_MODEL)))
PY
)"

if [[ ! -x "$LLAMA_BIN" ]]; then
  echo "llama-server not found/executable: $LLAMA_BIN (build llama.cpp first)"; exit 1
fi
if [[ ! -f "$MODEL" ]]; then
  echo "Reranker model not found: $MODEL"
  echo "Download it (~640 MB) with:"
  echo "  curl -L -o \"$MODEL\" \\"
  echo "    https://huggingface.co/gpustack/bge-reranker-v2-m3-GGUF/resolve/main/bge-reranker-v2-m3-Q8_0.gguf"
  exit 1
fi

# llama.cpp's shared libs sit next to the binary; point the linker at them so a
# relocated build (its RUNPATH is baked at build time) still resolves them.
export LD_LIBRARY_PATH="$(dirname "$LLAMA_BIN")${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

# --reranking enables the /rerank endpoint; -b/-ub 2048 so a long query+chunk pair fits one batch.
exec "$LLAMA_BIN" \
  -m "$MODEL" \
  --reranking \
  -ngl 0 -c 2048 \
  -b 2048 -ub 2048 \
  --host 127.0.0.1 --port 8084
