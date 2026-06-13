# Oceano — one image, GPU backend chosen at BUILD time (cuda | vulkan | rocm | cpu).
#
# Built by `scripts/install.sh --docker`, which detects the GPU and passes the matching
# base images + cmake flag as build-args (so this single Dockerfile covers every backend,
# mirroring the baremetal installer). All four compute roles in
# deploy/docker/docker-compose.yml run FROM this image with different commands:
#   oceano (engine :8800) · embeddings (llama-server :8082, CPU) · llama-swap (:8081, GPU)
#
# Default build-args = a CPU build on plain Ubuntu, so `docker build .` works with no args.
ARG BUILDER_IMAGE=ubuntu:22.04
ARG RUNTIME_IMAGE=ubuntu:22.04

# ───────────────────────── stage 1: build llama.cpp + grab llama-swap ─────────────────────────
FROM ${BUILDER_IMAGE} AS builder
ARG OCEANO_BACKEND=cpu
ARG CMAKE_GPU=""
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake git curl ca-certificates libcurl4-openssl-dev \
 && if [ "$OCEANO_BACKEND" = "vulkan" ]; then \
        apt-get install -y --no-install-recommends libvulkan-dev glslc glslang-tools spirv-tools; fi \
 && rm -rf /var/lib/apt/lists/*
# llama.cpp with the selected backend (CMAKE_GPU e.g. -DGGML_CUDA=ON / -DGGML_VULKAN=ON / -DGGML_HIP=ON)
RUN git clone --depth 1 https://github.com/ggml-org/llama.cpp /opt/llama.cpp \
 && cmake -S /opt/llama.cpp -B /opt/llama.cpp/build -DCMAKE_BUILD_TYPE=Release ${CMAKE_GPU} \
 && cmake --build /opt/llama.cpp/build --config Release -j"$(nproc)" --target llama-server
# llama-swap (latest linux_amd64 release)
RUN url=$(curl -s https://api.github.com/repos/mostlygeek/llama-swap/releases/latest \
            | grep browser_download_url | grep -i linux_amd64 | grep -ivE 'sha|\.txt|\.md' | head -1 | cut -d'"' -f4) \
 && curl -L --fail -o /tmp/ls.tgz "$url" && tar -xzf /tmp/ls.tgz -C /tmp \
 && install -m755 "$(find /tmp -name llama-swap -type f | head -1)" /usr/local/bin/llama-swap

# ───────────────────────── stage 2: runtime (engine + servers) ─────────────────────────
FROM ${RUNTIME_IMAGE} AS runtime
ARG OCEANO_BACKEND=cpu
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv python3-pip curl ca-certificates libcurl4 \
        ffmpeg espeak-ng git \
 && if [ "$OCEANO_BACKEND" = "vulkan" ]; then \
        apt-get install -y --no-install-recommends libvulkan1 mesa-vulkan-drivers; fi \
 && rm -rf /var/lib/apt/lists/*

# the llama.cpp server (+ its shared libs) and llama-swap from the build stage
COPY --from=builder /opt/llama.cpp/build/bin /opt/llama.cpp/build/bin
COPY --from=builder /usr/local/bin/llama-swap /usr/local/bin/llama-swap
# llama.cpp bakes a RUNPATH at build time; point the linker at the .so's next to the binary
ENV LD_LIBRARY_PATH=/opt/llama.cpp/build/bin \
    OCEANO_LLAMA_DIR=/opt/llama.cpp

WORKDIR /app
# deps first (better layer caching), then Playwright/Chromium for the browser tools
COPY requirements.txt /app/requirements.txt
RUN python3 -m venv /app/venv \
 && /app/venv/bin/pip install --no-cache-dir --upgrade pip \
 && /app/venv/bin/pip install --no-cache-dir -r /app/requirements.txt playwright \
 && /app/venv/bin/python -m playwright install --with-deps chromium
COPY . /app

# OCEANO_EMBED_MANAGED=0 — the embedding server is its own compose service, not a child here.
ENV PATH="/app/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    OCEANO_EMBED_MANAGED=0 \
    OCEANO_WEB_HOST=0.0.0.0
EXPOSE 8800 8081 8082
# default role = the engine (web + telegram + scheduler); other services override `command`.
CMD ["/app/venv/bin/python", "-m", "oceano.engine"]
