#!/usr/bin/env bash
# Oceano daemon (re)installer / repair tool.
#
# Renders systemd/oceano.service to THIS user + install path and installs it as the
# system unit /etc/systemd/system/oceano.service, then reloads + restarts + reports.
#
# This is the focused "just fix the service unit" subset of scripts/install.sh — use it
# when the engine itself is fine but the unit got mangled (e.g. a wrong WorkingDirectory
# makes `python -m oceano.engine` fail with "No module named 'oceano'") and you don't
# want to re-run the whole stack installer.
#
# Usage:
#   scripts/install-daemon.sh             # render, install, daemon-reload, enable --now, status
#   scripts/install-daemon.sh --dry-run   # print the rendered unit and the diff vs installed; change NOTHING
#   scripts/install-daemon.sh --no-start  # install + enable but don't (re)start
#   scripts/install-daemon.sh -h|--help
#
# Idempotent: safe to re-run. If the rendered unit already matches what's installed,
# it skips the rewrite and just restarts.
set -euo pipefail

# ---- paths (same knobs/defaults as scripts/install.sh) ---------------------
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if   [ -n "${OCEANO_LLAMA_DIR:-}" ]; then LLAMA_DIR="$OCEANO_LLAMA_DIR"
elif [ ! -d "$ROOT/llama.cpp" ] && [ -d "$HOME/llama.cpp" ]; then LLAMA_DIR="$HOME/llama.cpp"
else LLAMA_DIR="$ROOT/llama.cpp"; fi
SRC="$ROOT/systemd/oceano.service"
DST=/etc/systemd/system/oceano.service
UNIT=oceano.service
SVC_USER="$(id -un)"

# ---- pretty output ---------------------------------------------------------
if [ -t 1 ]; then B=$'\e[1m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; D=$'\e[2m'; X=$'\e[0m'
else B=; G=; Y=; R=; D=; X=; fi
say()  { printf '%s==>%s %s\n'  "$B" "$X" "$*"; }
ok()   { printf '%s  ok%s %s\n' "$G" "$X" "$*"; }
warn() { printf '%swarn%s %s\n' "$Y" "$X" "$*"; }
die()  { printf '%s err%s %s\n' "$R" "$X" "$*" >&2; exit 1; }

# ---- args ------------------------------------------------------------------
DRY=0; START=1
for a in "$@"; do case "$a" in
  --dry-run) DRY=1 ;;
  --no-start) START=0 ;;
  -h|--help) sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) die "unknown arg: $a (try --help)" ;;
esac; done

# ---- preflight: the things that actually make the daemon work --------------
[ -f "$SRC" ] || die "template not found: $SRC"
[ -f "$ROOT/oceano/engine.py" ] || die "$ROOT/oceano/engine.py missing — is ROOT ($ROOT) the repo? WorkingDirectory must hold the oceano package."
[ -x "$ROOT/venv/bin/python" ] || die "$ROOT/venv/bin/python missing — create the venv first (scripts/install.sh)."
"$ROOT/venv/bin/python" -c 'import oceano.engine' 2>/dev/null \
  || die "venv python can't import oceano.engine from $ROOT — fix the package/venv before installing the unit."
ok "preflight: engine imports from $ROOT"

# ---- render the unit (same sed rules as install.sh step 8) -----------------
render() {
  sed -e "s#__OCEANO_ROOT__#$ROOT#g" \
      -e "s#__OCEANO_HOME__#$HOME#g" \
      -e "s#__OCEANO_LLAMA_DIR__#$LLAMA_DIR#g" \
      -e "s#^User=__OCEANO_USER__#User=$SVC_USER#" "$SRC"
}
RENDERED="$(render)"

# guard: no template token may survive into an active line (comments are fine)
if printf '%s\n' "$RENDERED" | grep -vE '^\s*#' | grep -q '__OCEANO_'; then
  printf '%s\n' "$RENDERED" | grep -nE '^\s*[^#].*__OCEANO_' >&2
  die "a template token survived rendering (see above) — fix $SRC"
fi
# guard: WorkingDirectory must be the repo root that holds the package
WD="$(printf '%s\n' "$RENDERED" | sed -n 's/^WorkingDirectory=//p')"
[ "$WD" = "$ROOT" ] || die "rendered WorkingDirectory ($WD) != repo root ($ROOT) — refusing to install a unit that can't import oceano."
ok "rendered unit: WorkingDirectory=$WD, User=$SVC_USER"

if [ "$DRY" -eq 1 ]; then
  say "rendered $UNIT (dry-run, nothing changed):"
  printf '%s\n' "$RENDERED"
  if [ -f "$DST" ]; then
    say "diff vs installed $DST:"
    diff -u "$DST" <(printf '%s\n' "$RENDERED") && ok "installed unit already matches" || true
  else
    warn "no unit installed at $DST yet"
  fi
  exit 0
fi

# ---- install (only if changed) --------------------------------------------
if [ -f "$DST" ] && diff -q "$DST" <(printf '%s\n' "$RENDERED") >/dev/null 2>&1; then
  ok "installed unit already up to date ($DST)"
else
  say "writing $DST (needs sudo)"
  printf '%s\n' "$RENDERED" | sudo tee "$DST" >/dev/null
  sudo systemctl daemon-reload
  ok "installed + daemon-reload"
fi

sudo systemctl enable "$UNIT" >/dev/null 2>&1 || true

if [ "$START" -eq 0 ]; then
  ok "installed + enabled (--no-start: not (re)starting)"; exit 0
fi

# ---- restart + report ------------------------------------------------------
say "restarting $UNIT"
sudo systemctl reset-failed "$UNIT" 2>/dev/null || true   # clear the crash-loop counter
sudo systemctl restart "$UNIT"

# give it a moment to either come up or fall over
for _ in 1 2 3 4 5 6 7 8 9 10; do
  systemctl is-active --quiet "$UNIT" && break
  systemctl is-failed --quiet "$UNIT" && break
  sleep 1
done

if systemctl is-active --quiet "$UNIT"; then
  ok "$UNIT is ${G}active${X}"
  PORT="$(printf '%s\n' "$RENDERED" | sed -n 's/^Environment=OCEANO_WEB_PORT=//p')"; PORT="${PORT:-8800}"
  printf '%s    web UI:%s http://localhost:%s   (logs: journalctl -u %s -f)\n' "$D" "$X" "$PORT" "$UNIT"
else
  warn "$UNIT did NOT come up — last 25 journal lines:"
  journalctl -u "$UNIT" -n 25 --no-pager || true
  die "see the journal above; re-run with --dry-run to inspect the rendered unit"
fi
