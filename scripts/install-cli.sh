#!/usr/bin/env bash
# Install the `oceano` terminal command — a tiny launcher that runs cli.py inside the
# project's venv, from anywhere. Idempotent; safe to re-run.
#
#   scripts/install-cli.sh             # → ~/.local/bin/oceano   (no sudo)
#   scripts/install-cli.sh --system    # → /usr/local/bin/oceano (sudo; system-wide PATH)
#   scripts/install-cli.sh --uninstall # remove it
#
# Paths are baked into the launcher — re-run this if you move the repo.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAME=oceano
SYSTEM=0; UNINSTALL=0
for a in "$@"; do case "$a" in
  --system) SYSTEM=1 ;;
  --uninstall) UNINSTALL=1 ;;
  -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
  *) echo "unknown flag: $a" >&2; exit 2 ;;
esac; done

c() { printf '\033[%sm' "$1"; }; NC=$(c 0); B=$(c '1;36'); G=$(c 32); Y=$(c 33)
ok()   { printf '  %s✓%s %s\n' "$G" "$NC" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$NC" "$*"; }

if [ "$SYSTEM" = 1 ]; then BIN=/usr/local/bin; SUDO=sudo; else BIN="$HOME/.local/bin"; SUDO=""; fi
DST="$BIN/$NAME"

if [ "$UNINSTALL" = 1 ]; then
  $SUDO rm -f "$DST" && ok "removed $DST" || warn "nothing at $DST"
  exit 0
fi

[ -f "$ROOT/cli.py" ] || { echo "cli.py not found at $ROOT — run this from the Oceano repo" >&2; exit 1; }
[ -x "$ROOT/venv/bin/python" ] || warn "venv missing at $ROOT/venv — run scripts/install.sh first (the command will fail until the venv exists)"

$SUDO mkdir -p "$BIN"
# The launcher: load the project's env (so the CLI honours the same OCEANO_* knobs as the
# service — model, endpoints, …), then run cli.py with the venv python so deps + imports
# resolve no matter the current directory.
$SUDO tee "$DST" >/dev/null <<EOF
#!/usr/bin/env bash
# Oceano terminal client — installed by scripts/install-cli.sh. Paths are baked in;
# re-run that installer if you move the repo.
[ -f "$ROOT/oceano.env" ] && { set -a; . "$ROOT/oceano.env"; set +a; }
exec "$ROOT/venv/bin/python" "$ROOT/cli.py" "\$@"
EOF
$SUDO chmod +x "$DST"
printf '%s\n' "${B}≈ installed the 'oceano' command → $DST${NC}"

case ":$PATH:" in
  *":$BIN:"*) ok "$BIN is on your PATH — just run:  oceano" ;;
  *) warn "$BIN is NOT on your PATH yet. Add it, then re-open your shell:"
     printf "       echo 'export PATH=\"%s:\$PATH\"' >> ~/.bashrc && source ~/.bashrc\n" "$BIN" ;;
esac
