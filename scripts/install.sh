#!/usr/bin/env bash
# Oceano installer / stack bootstrapper.
#
# Brings up everything Oceano needs ON THE HOST (no containers), idempotently:
#   1. detect the GPU/driver and pick the matching llama.cpp build backend
#      (NVIDIA→CUDA, AMD/Intel→Vulkan, ROCm, or CPU; installs the NVIDIA driver if absent)
#   2. apt deps (build tools incl. libcurl, the backend's dev/runtime libs, python)
#   3. build llama.cpp with that backend (skips if already built to match)
#   4. fetch the embedding model if missing
#   5. python venv + requirements + playwright chromium (+ system libs via install-deps)
#   6. ensure oceano.env exists
#   7. SearXNG (:8080): install Docker + bring up the bundled compose if down
#   8. llama-swap (:8081): download the binary, write a starter config, install a unit
#   9. install + enable the single oceano.service (templated to this user/path)
#  10. health summary
#
# Usage:
#   scripts/install.sh            # full BAREMETAL install (default; idempotent, safe to re-run)
#   scripts/install.sh --docker   # CONTAINERIZED stack instead (docker compose; GPU auto-detected)
#   scripts/install.sh --check    # detect + probe only, change NOTHING (either mode)
#   scripts/install.sh --rebuild-llama   # force a clean llama.cpp rebuild (baremetal)
#   scripts/install.sh --with-models     # also download the chat model (several GB)
#   scripts/install.sh --yes      # don't prompt before the heavy steps
#
# Docker mode builds ONE image (oceano:local) with the GPU backend detect_gpu picks
# (CUDA/Vulkan/ROCm/CPU) and brings up 4 services via deploy/docker/ — oceano (:8800),
# embeddings (:8082, CPU), llama-swap (:8081, GPU), searxng. Models live in ./models.
#
# Override paths via env (same names config.py uses): OCEANO_LLAMA_DIR,
# OCEANO_MODELS_DIR, OCEANO_LLAMA_SWAP_BIN, OCEANO_LLAMA_SWAP_CFG, EMBED_MODEL, SEARXNG_COMPOSE.
# llama.cpp defaults to <oceano>/llama.cpp (a pre-existing ~/llama.cpp is reused).
set -euo pipefail

# ---- paths (override via env) ----------------------------------------------
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# llama.cpp location — same knob + same default logic as config.py: keep it under
# the Oceano dir for fresh installs, but reuse a pre-existing ~/llama.cpp if present.
if   [ -n "${OCEANO_LLAMA_DIR:-}" ]; then LLAMA_DIR="$OCEANO_LLAMA_DIR"
elif [ ! -d "$ROOT/llama.cpp" ] && [ -d "$HOME/llama.cpp" ]; then LLAMA_DIR="$HOME/llama.cpp"
else LLAMA_DIR="$ROOT/llama.cpp"; fi
LLAMA_BUILD="$LLAMA_DIR/build"
MODELS_DIR="${OCEANO_MODELS_DIR:-$LLAMA_DIR/models}"
EMBED_MODEL="${EMBED_MODEL:-$MODELS_DIR/nomic-embed-text-v1.5.Q8_0.gguf}"
EMBED_MODEL_URL="https://huggingface.co/nomic-ai/nomic-embed-text-v1.5-GGUF/resolve/main/nomic-embed-text-v1.5.Q8_0.gguf"
# Kokoro neural TTS voice (~120 MB total; the natural default voice). Falls back to Piper if absent.
KOKORO_DIR="${OCEANO_KOKORO_DIR:-$ROOT/assets/kokoro}"
KOKORO_REL="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
LLAMA_SWAP_BIN="${OCEANO_LLAMA_SWAP_BIN:-/usr/local/bin/llama-swap}"
LLAMA_SWAP_CFG="${OCEANO_LLAMA_SWAP_CFG:-$LLAMA_DIR/llama-swap.yaml}"
SEARXNG_COMPOSE="${SEARXNG_COMPOSE:-$ROOT/deploy/searxng/docker-compose.yml}"
VENV="$ROOT/venv"
DOCKER_DIR="$ROOT/deploy/docker"        # containerized stack (--docker)
DOCKER_MODELS="$ROOT/models"            # host-mounted models dir for the containers

# chat models llama-swap serves (only fetched with --with-models)
declare -A CHAT_MODELS=(
  ["$MODELS_DIR/Qwen3-4B-Instruct-2507-Q4_K_M.gguf"]="https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507-GGUF/resolve/main/Qwen3-4B-Instruct-2507-Q4_K_M.gguf"
)

# ---- flags -----------------------------------------------------------------
CHECK=0; REBUILD=0; WITH_MODELS=0; ASSUME_YES=0; MODE=baremetal
for a in "$@"; do case "$a" in
  --check) CHECK=1 ;;
  --docker|--container) MODE=docker ;;
  --baremetal|--host) MODE=baremetal ;;
  --rebuild-llama) REBUILD=1 ;;
  --with-models) WITH_MODELS=1 ;;
  --yes|-y) ASSUME_YES=1 ;;
  -h|--help) sed -n '2,34p' "$0"; exit 0 ;;
  *) echo "unknown flag: $a" >&2; exit 2 ;;
esac; done

# ---- pretty output ---------------------------------------------------------
c() { printf '\033[%sm' "$1"; }; NC=$(c 0); B=$(c '1;36'); G=$(c 32); Y=$(c 33); R=$(c 31)
say()  { printf '%s\n' "${B}≈ $*${NC}"; }
ok()   { printf '  %s✓%s %s\n' "$G" "$NC" "$*"; }
skip() { printf '  %s•%s %s\n' "$Y" "$NC" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$NC" "$*"; }
die()  { printf '%s✗ %s%s\n' "$R" "$*" "$NC" >&2; exit 1; }
port_up() { curl -sf -o /dev/null --max-time 2 "http://127.0.0.1:$1${2:-/}" 2>/dev/null; }
confirm() { [ "$ASSUME_YES" = 1 ] && return 0; read -rp "  proceed? [y/N] " r; [[ "$r" =~ ^[Yy]$ ]]; }

# ============================================================================
# 1. detect distro + GPU/driver -> backend
# ============================================================================
BACKEND="cpu"; CMAKE_GPU=""; declare -a APT_GPU=(); NEED_NVIDIA_DRIVER=0
_VULKAN_PKGS=(libvulkan-dev mesa-vulkan-drivers vulkan-tools glslc glslang-tools spirv-tools)
detect_gpu() {
  say "Detecting GPU / driver"
  if command -v nvidia-smi >/dev/null && nvidia-smi -L >/dev/null 2>&1; then
    BACKEND="cuda"; CMAKE_GPU="-DGGML_CUDA=ON"; APT_GPU=(nvidia-cuda-toolkit)
    ok "NVIDIA detected: $(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)"
  elif lspci 2>/dev/null | grep -qi 'nvidia'; then
    BACKEND="cuda"; CMAKE_GPU="-DGGML_CUDA=ON"; APT_GPU=(nvidia-cuda-toolkit); NEED_NVIDIA_DRIVER=1
    warn "NVIDIA GPU present but no driver (nvidia-smi missing) — will offer to install it."
  elif command -v vulkaninfo >/dev/null && vulkaninfo --summary >/dev/null 2>&1; then
    BACKEND="vulkan"; CMAKE_GPU="-DGGML_VULKAN=ON"; APT_GPU=("${_VULKAN_PKGS[@]}")
    local gpu; gpu=$(vulkaninfo --summary 2>/dev/null | grep -m1 'deviceName' | sed 's/.*= //')
    ok "Vulkan detected: ${gpu:-unknown device}  (RADV/Mesa or vendor ICD)"
  elif lspci 2>/dev/null | grep -qiE 'amd/ati|advanced micro devices.*\[amd/ati\]'; then
    BACKEND="vulkan"; CMAKE_GPU="-DGGML_VULKAN=ON"; APT_GPU=("${_VULKAN_PKGS[@]}")
    warn "AMD GPU present but no Vulkan userspace yet — will install mesa to enable it."
  elif command -v rocminfo >/dev/null && rocminfo >/dev/null 2>&1; then
    BACKEND="rocm"; CMAKE_GPU="-DGGML_HIP=ON"
    ok "ROCm/HIP detected"
    warn "ROCm path is heavier; ensure the rocm/hip dev packages are installed."
  else
    warn "No GPU acceleration found — building CPU-only (slow for chat models)."
  fi
  printf '  → backend: %s%s%s   cmake: %s\n' "$B" "$BACKEND" "$NC" "${CMAKE_GPU:-<none>}"
}

# NVIDIA proprietary driver (only when an NVIDIA GPU exists but the driver doesn't)
gpu_driver() {
  [ "$NEED_NVIDIA_DRIVER" = 1 ] || return 0
  say "NVIDIA driver"
  warn "installing the recommended driver via ubuntu-drivers — a REBOOT is needed afterwards"
  confirm || { skip "skipped — install the NVIDIA driver yourself, then re-run for the CUDA build"; return; }
  sudo apt-get install -y ubuntu-drivers-common && sudo ubuntu-drivers install \
    && warn "driver installed — REBOOT, then re-run this script to build with CUDA" \
    || warn "driver install failed — see 'ubuntu-drivers devices'"
}

# ============================================================================
# 2. apt dependencies
# ============================================================================
apt_deps() {
  say "System packages (apt)"
  command -v apt-get >/dev/null || { warn "not an apt system — install build deps manually: cmake, git, python3-venv, + your GPU's dev libs"; return; }
  # libcurl4-openssl-dev: llama.cpp builds with LLAMA_CURL=ON by default and won't
  # configure without it. ccache speeds up rebuilds.
  # ffmpeg + espeak-ng power the voice stack (Kokoro/Piper → ogg conversion, espeak phonemizer/fallback).
  local base=(build-essential cmake git curl ca-certificates python3-venv python3-pip libcurl4-openssl-dev ffmpeg espeak-ng)
  local missing=()
  for p in "${base[@]}" "${APT_GPU[@]}"; do
    dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p")
  done
  if [ ${#missing[@]} -eq 0 ]; then ok "all base + ${BACKEND} packages already present"; return; fi
  warn "will apt-install: ${missing[*]}"
  confirm || { skip "skipped apt install"; return; }
  sudo apt-get update -qq
  sudo apt-get install -y "${missing[@]}"
  ok "installed ${#missing[@]} packages"
}

# ============================================================================
# 3. build llama.cpp with the detected backend
# ============================================================================
build_llama() {
  say "llama.cpp ($BACKEND)"
  if [ ! -d "$LLAMA_DIR/.git" ]; then
    warn "cloning llama.cpp into $LLAMA_DIR"
    confirm || { skip "skipped clone"; return; }
    git clone https://github.com/ggml-org/llama.cpp "$LLAMA_DIR"
  fi
  # already built to match? (CMakeCache has GGML_<BACKEND>=ON and the binary exists)
  local want="GGML_$( [ "$BACKEND" = rocm ] && echo HIP || echo "${BACKEND^^}" )=ON"
  if [ "$REBUILD" = 0 ] && [ -x "$LLAMA_BUILD/bin/llama-server" ] \
     && { [ "$BACKEND" = cpu ] || grep -q "$want" "$LLAMA_BUILD/CMakeCache.txt" 2>/dev/null; }; then
    ok "llama-server already built with $BACKEND — skipping (use --rebuild-llama to force)"
    return
  fi
  warn "configuring + building llama.cpp ($BACKEND) — this can take several minutes"
  confirm || { skip "skipped build"; return; }
  [ "$REBUILD" = 1 ] && rm -rf "$LLAMA_BUILD"
  cmake -S "$LLAMA_DIR" -B "$LLAMA_BUILD" -DCMAKE_BUILD_TYPE=Release $CMAKE_GPU
  cmake --build "$LLAMA_BUILD" --config Release -j"$(nproc)"
  ok "built $LLAMA_BUILD/bin/llama-server"
}

# ============================================================================
# 4. embedding model
# ============================================================================
fetch_model() {  # $1=path $2=url
  if [ -f "$1" ]; then ok "model present: $(basename "$1")"; return; fi
  warn "downloading $(basename "$1")"
  confirm || { skip "skipped — Oceano memory/RAG need this"; return; }
  mkdir -p "$(dirname "$1")"; curl -L --fail -o "$1" "$2" || { rm -f "$1"; die "download failed"; }
  ok "downloaded $(basename "$1")"
}
models() {
  say "Models"
  fetch_model "$EMBED_MODEL" "$EMBED_MODEL_URL"
  fetch_model "$KOKORO_DIR/kokoro-v1.0.int8.onnx" "$KOKORO_REL/kokoro-v1.0.int8.onnx"
  fetch_model "$KOKORO_DIR/voices-v1.0.bin" "$KOKORO_REL/voices-v1.0.bin"
  if [ "$WITH_MODELS" = 1 ]; then for m in "${!CHAT_MODELS[@]}"; do fetch_model "$m" "${CHAT_MODELS[$m]}"; done
  else skip "chat models: pass --with-models to fetch (several GB); $(ls "$MODELS_DIR"/*.gguf 2>/dev/null | grep -ivc nomic) chat gguf already present"; fi
}

# ============================================================================
# 5. python venv + deps
# ============================================================================
python_env() {
  say "Python environment"
  [ -d "$VENV" ] || { warn "creating venv at $VENV"; python3 -m venv "$VENV"; }
  "$VENV/bin/pip" install -q --upgrade pip >/dev/null
  "$VENV/bin/pip" install -q -r "$ROOT/requirements.txt"
  ok "requirements installed"
  if ! "$VENV/bin/python" -c "import playwright" 2>/dev/null; then
    skip "playwright not in requirements — skipping browser install"
  else
    # install-deps pulls the system libs Chromium needs (libnss3, libatk, …) — needs
    # root, and without them headless Chromium can't launch on a fresh box.
    sudo "$VENV/bin/python" -m playwright install-deps chromium >/dev/null 2>&1 \
      && ok "playwright system libs installed" || warn "playwright install-deps failed (browser may not launch)"
    "$VENV/bin/python" -m playwright install chromium >/dev/null 2>&1 && ok "playwright chromium ready" \
      || warn "playwright chromium download failed (browser tools won't work until fixed)"
  fi
  # Pre-fetch the speech-to-text model so voice works offline immediately (faster-whisper would
  # otherwise download it on first use). Best-effort — voice degrades gracefully if this fails.
  "$VENV/bin/python" -c "from faster_whisper import WhisperModel; WhisperModel('${OCEANO_STT_MODEL:-base.en}', download_root='$ROOT/assets/whisper')" >/dev/null 2>&1 \
    && ok "speech-to-text model ready" || skip "STT model prefetch skipped (will download on first use)"
}

# ============================================================================
# 6. oceano.env
# ============================================================================
oceano_env() {
  say "Secrets file"
  if [ -f "$ROOT/oceano.env" ]; then ok "oceano.env present"; return; fi
  cp "$ROOT/oceano.env.example" "$ROOT/oceano.env"; chmod 600 "$ROOT/oceano.env"
  ok "created oceano.env from example (fill in Telegram token etc. when ready)"
}

# ============================================================================
# 7. support services: SearXNG + llama-swap
# ============================================================================
ensure_docker() {
  command -v docker >/dev/null && return 0
  warn "Docker not installed — installing docker.io + compose plugin (apt)"
  confirm || return 1
  sudo apt-get install -y docker.io docker-compose-v2 && sudo systemctl enable --now docker \
    && ok "Docker installed" || { warn "docker install failed"; return 1; }
}
ensure_searxng() {
  say "SearXNG (:8080, web search)"
  if port_up 8080 "/healthz" || port_up 8080; then ok "SearXNG responding on :8080"; return; fi
  ensure_docker || { warn "skipping SearXNG (no Docker)"; return; }
  [ -f "$SEARXNG_COMPOSE" ] || { warn "no compose at $SEARXNG_COMPOSE"; return; }
  local settings; settings="$(dirname "$SEARXNG_COMPOSE")/settings.yml"
  if [ -f "$settings" ] && grep -q REPLACE_ME "$settings"; then   # bake a real secret_key once
    sed -i "s/REPLACE_ME/$(LC_ALL=C tr -dc 'a-f0-9' </dev/urandom | head -c 48)/" "$settings"
  fi
  warn "bringing SearXNG up via $SEARXNG_COMPOSE"
  confirm || { skip "skipped"; return; }
  sudo docker compose -f "$SEARXNG_COMPOSE" up -d \
    && { sleep 3; port_up 8080 && ok "SearXNG started" || warn "started — may take a few seconds to answer"; } \
    || warn "compose up failed"
}

write_llama_swap_starter() {  # a starter llama-swap.yaml so :8081 + Rivers' "Serve" work from scratch
  local q="$MODELS_DIR/Qwen3-4B-Instruct-2507-Q4_K_M.gguf" entry=""
  [ -f "$q" ] && entry=$(printf '  "qwen3-4b":\n    cmd: |\n      %s/bin/llama-server\n      -m %s\n      -ngl 99 -fa 1 --parallel 1 -c 65536 -ctk q8_0 -ctv q4_0 --jinja\n      --host 127.0.0.1 --port ${PORT}\n    ttl: 600\n' "$LLAMA_BUILD" "$q")
  { printf '# llama-swap — chat models on :8081 (one resident at a time).\n'
    printf '# Oceano Rivers appends models here; -watch-config hot-reloads.\n'
    printf 'healthCheckTimeout: 300\nlogLevel: info\nmodels:\n%s' "$entry"; } > "$LLAMA_SWAP_CFG"
  ok "wrote starter $(basename "$LLAMA_SWAP_CFG")"
}
install_llama_swap() {
  say "llama-swap (:8081, chat models)"
  # 1. binary (download the latest linux_amd64 release if missing)
  if [ ! -x "$LLAMA_SWAP_BIN" ]; then
    warn "llama-swap not installed — fetching the latest release binary"
    if confirm; then
      local url tmp
      url=$(curl -s https://api.github.com/repos/mostlygeek/llama-swap/releases/latest \
            | grep browser_download_url | grep -i 'linux_amd64' | grep -ivE 'sha|\.txt|\.md' | head -1 | cut -d'"' -f4)
      if [ -n "$url" ]; then
        tmp=$(mktemp -d)
        curl -L --fail -o "$tmp/ls.tgz" "$url" && tar -xzf "$tmp/ls.tgz" -C "$tmp" \
          && sudo install -m755 "$(find "$tmp" -name llama-swap -type f | head -1)" "$LLAMA_SWAP_BIN" \
          && ok "installed llama-swap → $LLAMA_SWAP_BIN" || warn "llama-swap install failed"
        rm -rf "$tmp"
      else warn "no linux_amd64 release asset found — install llama-swap manually"; fi
    else skip "skipped llama-swap install"; fi
  else ok "llama-swap binary present"; fi
  # 2. starter config
  [ -f "$LLAMA_SWAP_CFG" ] && ok "config present: $(basename "$LLAMA_SWAP_CFG")" || write_llama_swap_starter
  # 3. durable systemd unit (templated). Start only if :8081 is free.
  local src="$ROOT/systemd/oceano-llama-swap.service" dst=/etc/systemd/system/oceano-llama-swap.service
  if [ -x "$LLAMA_SWAP_BIN" ] && [ -f "$src" ]; then
    sed -e "s#__LLAMA_DIR__#$LLAMA_DIR#g" -e "s#/usr/local/bin/llama-swap#$LLAMA_SWAP_BIN#g" \
        -e "s#^User=__OCEANO_USER__#User=$(id -un)#" "$src" | sudo tee "$dst" >/dev/null
    sudo systemctl daemon-reload
    if port_up 8081 "/v1/models"; then
      sudo systemctl enable oceano-llama-swap >/dev/null 2>&1 || true
      ok "already running on :8081 (unit installed; takes over on next boot)"
    else
      confirm && { sudo systemctl enable --now oceano-llama-swap >/dev/null 2>&1 || true
        sleep 2; port_up 8081 "/v1/models" && ok "llama-swap started via systemd" || warn "started; :8081 not answering yet"; } \
        || skip "unit installed, not started"
    fi
  fi
}

# ============================================================================
# 8. the single oceano.service
# ============================================================================
install_service() {
  say "oceano.service (engine: web + telegram + scheduler + embeddings)"
  local src="$ROOT/systemd/oceano.service" dst=/etc/systemd/system/oceano.service
  [ -f "$src" ] || die "missing $src"
  # Template the unit to THIS install: the __OCEANO_USER__ / __OCEANO_ROOT__ /
  # __OCEANO_HOME__ / __OCEANO_LLAMA_DIR__ tokens (venv, ExecStart, EnvironmentFile, PATH,
  # ReadWritePaths) → the real user / $ROOT / $HOME / $LLAMA_DIR. (%h is unusable here — on
  # a system unit it = /root.)
  mkdir -p "$HOME/.claude"        # the ReadWritePaths entry below; Claude Code writes its state here
  mkdir -p "$ROOT/assets/voice"   # ditto — the daemon downloads Piper voices here (must exist + be writable)
  local rendered; rendered=$(sed -e "s#__OCEANO_ROOT__#$ROOT#g" -e "s#__OCEANO_HOME__#$HOME#g" \
                                 -e "s#__OCEANO_LLAMA_DIR__#$LLAMA_DIR#g" \
                                 -e "s#^User=__OCEANO_USER__#User=$(id -un)#" "$src")
  if [ -f "$dst" ] && [ "$rendered" = "$(cat "$dst" 2>/dev/null)" ]; then ok "unit already installed + current"
  else
    warn "installing $dst (user=$(id -un), root=$ROOT)"
    confirm || { skip "skipped"; return; }
    printf '%s\n' "$rendered" | sudo tee "$dst" >/dev/null; sudo systemctl daemon-reload
  fi
  sudo systemctl enable --now oceano.service >/dev/null 2>&1 || true
  systemctl is-active --quiet oceano.service && ok "oceano.service active" || warn "oceano.service not active — check: journalctl -u oceano"
}

# ============================================================================
# 8b. polkit rule — restart the llama-swap model server from the web UI (no password,
#     without weakening the daemon's NoNewPrivileges). Optional; degrades gracefully.
# ============================================================================
install_polkit() {
  say "polkit rule (restart the model server from the web UI)"
  local src="$ROOT/systemd/oceano-polkit.rules" dst=/etc/polkit-1/rules.d/49-oceano.rules
  [ -f "$src" ] || { warn "missing $src — skipping (llama-swap won't be restartable from the UI)"; return; }
  if [ ! -d /etc/polkit-1/rules.d ]; then
    warn "no /etc/polkit-1/rules.d (older polkit?) — skipping; restart llama-swap with: sudo systemctl restart oceano-llama-swap"
    return
  fi
  local rendered; rendered=$(sed -e "s#__OCEANO_USER__#$(id -un)#g" "$src")
  if [ -f "$dst" ] && [ "$rendered" = "$(cat "$dst" 2>/dev/null)" ]; then ok "polkit rule already installed + current"; return; fi
  warn "installing $dst (lets $(id -un) restart oceano-llama-swap without a password)"
  confirm || { skip "skipped"; return; }
  printf '%s\n' "$rendered" | sudo tee "$dst" >/dev/null
  sudo systemctl reload polkit 2>/dev/null || sudo systemctl restart polkit 2>/dev/null || true   # polkitd also auto-reloads rules.d
  ok "polkit rule installed"
}

# ============================================================================
# 9. the `oceano` terminal command (the rich cli.py launcher)
# ============================================================================
install_cli() {
  say "oceano CLI command"
  bash "$ROOT/scripts/install-cli.sh" 2>/dev/null || warn "CLI launcher install skipped (scripts/install-cli.sh)"
}

# ============================================================================
# health summary
# ============================================================================
summary() {
  say "Health"
  port_up 8800        && ok "web UI        :8800  up" || warn "web UI        :8800  DOWN"
  port_up 8082 "/v1/models" && ok "embeddings    :8082  up" || warn "embeddings    :8082  DOWN"
  port_up 8081 "/v1/models" && ok "llama-swap    :8081  up" || warn "llama-swap    :8081  DOWN"
  port_up 8080        && ok "SearXNG       :8080  up" || warn "SearXNG       :8080  DOWN"
  echo
  say "Open the web UI at http://127.0.0.1:8800  (default login: admin / admin)"
  command -v oceano >/dev/null && say "Or chat from the terminal:  oceano" \
    || say "Terminal client: scripts/install-cli.sh  →  then run 'oceano'"
}

# ============================================================================
# docker mode: containerized stack (compose) — GPU via a per-vendor override
# ============================================================================
DOCKER="docker"
ensure_nvidia_toolkit() {   # GPU-in-Docker for CUDA needs NVIDIA's Container Toolkit on the host
  [ "$BACKEND" = cuda ] || return 0
  command -v nvidia-ctk >/dev/null && { ok "NVIDIA Container Toolkit present"; return 0; }
  say "NVIDIA Container Toolkit (GPU passthrough for Docker)"
  warn "installing nvidia-container-toolkit (adds NVIDIA's apt repo) — required for GPU in containers"
  confirm || { skip "skipped — containers will run CPU-only"; BACKEND=cpu; CMAKE_GPU=""; return 0; }
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
  sudo apt-get update -qq && sudo apt-get install -y nvidia-container-toolkit \
    && sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker \
    && ok "toolkit installed + Docker configured for GPUs" \
    || { warn "toolkit install failed — falling back to CPU containers"; BACKEND=cpu; CMAKE_GPU=""; }
}
docker_searxng_secret() {   # bake a real SearXNG secret_key once (same as the baremetal path)
  local s="$ROOT/deploy/searxng/settings.yml"
  [ -f "$s" ] && grep -q REPLACE_ME "$s" \
    && sed -i "s/REPLACE_ME/$(LC_ALL=C tr -dc 'a-f0-9' </dev/urandom | head -c 48)/" "$s" || true
}
docker_models() {           # models live OUTSIDE the image, in a host-mounted ./models
  say "Models (host-mounted ./models)"
  mkdir -p "$DOCKER_MODELS"
  fetch_model "$DOCKER_MODELS/nomic-embed-text-v1.5.Q8_0.gguf" "$EMBED_MODEL_URL"
  if [ "$WITH_MODELS" = 1 ]; then
    fetch_model "$DOCKER_MODELS/Qwen3-4B-Instruct-2507-Q4_K_M.gguf" \
      "https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507-GGUF/resolve/main/Qwen3-4B-Instruct-2507-Q4_K_M.gguf"
  else skip "chat models: pass --with-models to fetch (several GB), or add one via Rivers later"; fi
}
docker_main() {
  printf '%s\n' "${B}╔══ Oceano installer · docker ══╗${NC}  root=$ROOT"
  detect_gpu
  ensure_docker || die "Docker is required for --docker (install it, or run the baremetal default)"
  docker info >/dev/null 2>&1 || DOCKER="sudo docker"   # use sudo only if the daemon needs it
  ensure_nvidia_toolkit
  oceano_env
  docker_searxng_secret
  docker_models
  local BUILDER RUNTIME OVERRIDE=""
  case "$BACKEND" in
    cuda)   BUILDER=nvidia/cuda:12.4.1-devel-ubuntu22.04; RUNTIME=nvidia/cuda:12.4.1-runtime-ubuntu22.04; OVERRIDE=docker-compose.nvidia.yml ;;
    vulkan) BUILDER=ubuntu:22.04;                          RUNTIME=ubuntu:22.04;                          OVERRIDE=docker-compose.vulkan.yml ;;
    rocm)   BUILDER=rocm/dev-ubuntu-22.04:6.1.2;           RUNTIME=rocm/dev-ubuntu-22.04:6.1.2;           OVERRIDE=docker-compose.rocm.yml ;;
    *)      BUILDER=ubuntu:22.04;                          RUNTIME=ubuntu:22.04 ;;
  esac
  say "Build image oceano:local ($BACKEND) — llama.cpp + python + chromium (several minutes, GBs)"
  if confirm; then
    $DOCKER build -f "$ROOT/Dockerfile" \
      --build-arg BUILDER_IMAGE="$BUILDER" --build-arg RUNTIME_IMAGE="$RUNTIME" \
      --build-arg OCEANO_BACKEND="$BACKEND" --build-arg CMAKE_GPU="$CMAKE_GPU" \
      -t oceano:local "$ROOT" || die "image build failed"
    ok "built oceano:local"
  else skip "skipped build (compose needs the image — build it before 'up')"; fi
  local files=(-f "$DOCKER_DIR/docker-compose.yml"); [ -n "$OVERRIDE" ] && files+=(-f "$DOCKER_DIR/$OVERRIDE")
  say "Start the stack: docker compose ${files[*]} up -d"
  confirm || { skip "skipped 'compose up' — run it yourself when ready"; return 0; }
  $DOCKER compose "${files[@]}" up -d || die "compose up failed"
  sleep 4
  say "Health"
  port_up 8800 && ok "web UI :8800 up" \
    || warn ":8800 not answering yet — give it a moment ($DOCKER compose ${files[*]} logs -f oceano)"
  say "Open the web UI at http://127.0.0.1:8800  (default login: admin / admin)"
}

# ============================================================================
baremetal_main() {
  printf '%s\n' "${B}╔══ Oceano installer ══╗${NC}  root=$ROOT  llama=$LLAMA_DIR"
  detect_gpu
  apt_deps
  gpu_driver
  build_llama
  models
  python_env
  oceano_env
  ensure_searxng
  install_llama_swap
  install_service
  install_polkit
  install_cli
  summary
}

main() {
  if [ "$CHECK" = 1 ]; then
    detect_gpu
    say "Probing services (--check: no changes will be made; mode=$MODE)"
    for pp in "8800 web" "8082 embeddings" "8081 llama-swap" "8080 searxng"; do
      set -- $pp; port_up "$1" "/v1/models" || port_up "$1" && ok "$2 (:$1) up" || warn "$2 (:$1) down"
    done
    say "Would build llama.cpp with: ${CMAKE_GPU:-CPU-only}; apt: ${APT_GPU[*]:-none extra}"
    exit 0
  fi
  if [ "$MODE" = docker ]; then docker_main; else baremetal_main; fi
}
main
