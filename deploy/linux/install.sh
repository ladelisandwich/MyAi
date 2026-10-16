#!/usr/bin/env bash
# HomeMesh AI - user-level installer for Linux (Fedora, Ubuntu, Arch, Bazzite and other
# immutable distributions). Run it from a checkout or an unpacked release:
#
#     ./deploy/linux/install.sh [--upgrade] [--no-web] [--python /usr/bin/python3.13]
#
# What it does (nothing system-wide, no root, no rpm-ostree layering):
#   * builds the web UI if Node.js is available and the built UI is missing or older than its sources
#   * installs HomeMesh from this source tree into a NEW virtual environment,
#     ~/.local/share/homemesh/venvs/<commit>-<time>, while an installed HomeMesh keeps running
#   * points ~/.local/share/homemesh/venv (a link; the command and the systemd unit use it) at the
#     new one, restarting HomeMesh if it ran; the previous version's folder is kept, older ones removed
#   * links ~/.local/bin/homemesh
# Your data (~/.local/share/homemesh data files or $HOMEMESH_HOME) is never touched.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DATA_BASE="${XDG_DATA_HOME:-$HOME/.local/share}/homemesh"
VENV="${HOMEMESH_VENV:-$DATA_BASE/venv}"
VENV="${VENV%/}"
case "$VENV" in /*) ;; *) VENV="$PWD/$VENV" ;; esac
PREFIX="$(dirname "$VENV")"          # holds venv (the link), venvs/<id>/ and the updater's b/
VENVS="$PREFIX/venvs"
BIN_DIR="$HOME/.local/bin"
UNIT="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/homemesh.service"
ID_RE='^([0-9a-f]{12}|nogit|legacy)-[0-9]{14}$'   # the only folder names under venvs/ that are ever removed
PYTHON=""
BUILD_WEB=1
UPGRADE=0
WAS_RUNNING=0
SWITCHED=0
NEW=""
LEGACY=""
PREV=""   # the venv the link pointed at before this install (kept for going back)

while [ $# -gt 0 ]; do
  case "$1" in
    --upgrade) UPGRADE=1 ;;
    --no-web) BUILD_WEB=0 ;;
    --python) PYTHON="$2"; shift ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

say() { printf '\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33mWarning:\033[0m %s\n' "$*" >&2; }
fail() { printf '\033[31mError:\033[0m %s\n' "$*" >&2; exit 1; }

# On any failure before the switch: remove the half-built venv, put a moved old folder back, and say
# how to get HomeMesh running again if this installer stopped it.
on_exit() {
  local rc=$?
  if [ "$SWITCHED" = 0 ]; then
    if [ -n "$LEGACY" ] && [ ! -e "$VENV" ] && [ ! -L "$VENV" ] && [ -d "$LEGACY" ]; then
      mv -- "$LEGACY" "$VENV" || true
    fi
    if [ -n "$NEW" ] && [ -d "$NEW" ] && [ ! -L "$NEW" ]; then rm -rf -- "$NEW" || true; fi
    if [ "$rc" != 0 ] && [ "$WAS_RUNNING" = 1 ]; then
      warn "HomeMesh was stopped for the upgrade, which did not finish; the installed version is unchanged. Start it again with 'homemesh start'."
    fi
  fi
}
trap on_exit EXIT

# ---- Python -------------------------------------------------------------------------
if [ -z "$PYTHON" ]; then
  for c in python3.13 python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1; then PYTHON="$(command -v "$c")"; break; fi
  done
fi
[ -n "$PYTHON" ] || fail "Python 3.11+ not found. On Bazzite/Fedora it is preinstalled as python3; on Ubuntu: sudo apt install python3 python3-venv"
"$PYTHON" - <<'PY' || fail "HomeMesh needs Python 3.11 or newer (found $("$PYTHON" --version 2>&1))."
import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)
PY
"$PYTHON" -c "import venv, sqlite3; sqlite3.connect(':memory:').execute('create virtual table t using fts5(x)')" 2>/dev/null \
  || fail "Python is missing venv or SQLite FTS5 support. Ubuntu/Debian: sudo apt install python3-venv"
say "Using $("$PYTHON" --version) at $PYTHON"

# ---- Web UI -------------------------------------------------------------------------
# Build it when it is missing or older than its sources (after 'git pull'), so an upgrade never
# pairs a new backend with an old web interface.
WEB_INDEX="$ROOT/src/homemesh/web_dist/index.html"
WEB_STALE=0
if [ ! -f "$WEB_INDEX" ]; then
  WEB_STALE=1
elif [ -n "$(cd "$ROOT/apps/web" && find src public index.html package.json package-lock.json vite.config.ts \
             -newer "$WEB_INDEX" -type f -print -quit 2>/dev/null)" ]; then
  WEB_STALE=1
fi
if [ "$WEB_STALE" = 1 ]; then
  if [ "$BUILD_WEB" = 1 ] && command -v npm >/dev/null 2>&1; then
    say "Building the web interface…"
    "$ROOT/scripts/build_web.sh"
  elif [ -f "$WEB_INDEX" ]; then
    echo "Note: the web interface sources changed but Node.js/npm is not available (or --no-web);"
    echo "      the previously built web UI is used. Install Node.js 20.19+ and run scripts/build_web.sh."
  else
    echo "Note: no prebuilt web UI found and Node.js/npm is not available (or --no-web)."
    echo "      The API and CLI will work; install Node.js 20.19+ and run scripts/build_web.sh for the UI."
    echo "      On Bazzite you can use 'brew install node' or a distrobox for this one-time build."
  fi
fi

# ---- New virtual environment (built while the installed version keeps running) -----------------
if [ -d "$VENV" ] && [ "$UPGRADE" = 0 ] && [ -x "$VENV/bin/homemesh" ]; then
  say "HomeMesh is already installed in $VENV - re-run with --upgrade to update it."
else
  TS="$(date -u +%Y%m%d%H%M%S)"
  GIT="$(command -v git 2>/dev/null || true)"
  SHA=""
  if [ -n "$GIT" ] && [ -e "$ROOT/.git" ]; then
    SHA="$("$GIT" -C "$ROOT" rev-parse --verify -q 'HEAD^{commit}' 2>/dev/null || true)"
  fi
  if [[ "$SHA" =~ ^[0-9a-f]{40}([0-9a-f]{24})?$ ]]; then ID="${SHA:0:12}-$TS"; else ID="nogit-$TS"; fi
  [ ! -e "$VENVS/$ID" ] || fail "$VENVS/$ID already exists; run the installer again."
  NEW="$VENVS/$ID"
  say "Installing into $NEW…"
  mkdir -p "$VENVS"
  "$PYTHON" -m venv "$NEW"
  "$NEW/bin/python" -m pip install --quiet --upgrade pip
  "$NEW/bin/python" -m pip install --quiet --upgrade "$ROOT"
  # The install record (commit, branch, where git and Node.js are) lets HomeMesh update itself later;
  # it is written into the new folder before anything uses it. It refuses while an update is running.
  REC=(--source "$ROOT" --stable-venv "$VENV" --built-by installer)
  if [ -n "$GIT" ]; then REC+=(--git "$GIT"); fi
  NODE="$(command -v node 2>/dev/null || true)"
  if [ -n "$NODE" ]; then REC+=(--node-dir "$(dirname "$NODE")"); fi
  "$NEW/bin/python" -m homemesh update record-install "${REC[@]}" \
    || fail "The new version could not record its install (message above); nothing was changed."

  if [ "$UPGRADE" = 1 ] && [ -x "$VENV/bin/homemesh" ]; then
    # Ask the version that is installed NOW: it started the running process and knows how to find it
    # (builds from before the instance lock are invisible to newer ones). Same order as install.ps1
    # -Upgrade. 'homemesh status' exits 0 when running, 2 when running but degraded (e.g. Ollama
    # down), 3 when stopped.
    rc=0; "$VENV/bin/homemesh" status >/dev/null 2>&1 || rc=$?
    if [ "$rc" = 0 ] || [ "$rc" = 2 ]; then
      WAS_RUNNING=1
      say "Stopping HomeMesh for the upgrade…"
      if ! "$VENV/bin/homemesh" stop; then
        WAS_RUNNING=0  # it still runs
        fail "Could not stop HomeMesh; run 'homemesh stop --force' and re-run the upgrade."
      fi
      rc=0; "$VENV/bin/homemesh" status >/dev/null 2>&1 || rc=$?
      if [ "$rc" = 0 ] || [ "$rc" = 2 ]; then
        WAS_RUNNING=0
        fail "HomeMesh still answers although 'homemesh stop' found nothing to stop (a process the installed version can't see). Log out and back in (or restart the computer), then run the upgrade again."
      fi
    fi
  fi

  # ---- Switch: <prefix>/venv becomes a link to venvs/<id> (atomic rename) ----------------------
  if [ -L "$VENV" ]; then
    PREV="$(readlink -- "$VENV")"; PREV="${PREV##*/}"  # kept below, to go back to
  elif [ -d "$VENV" ]; then
    PREV="legacy-$TS"
    LEGACY="$VENVS/legacy-$TS"
    say "Moving the previous program folder to $LEGACY (kept in case you need to go back)…"
    mv -- "$VENV" "$LEGACY"
  elif [ -e "$VENV" ]; then
    fail "$VENV exists but is not a folder; move it away and run the installer again."
  fi
  rm -f -- "$VENV.next"
  ln -s "venvs/$ID" "$VENV.next"
  mv -T -- "$VENV.next" "$VENV"
  SWITCHED=1
fi

mkdir -p "$BIN_DIR"
ln -sf "$VENV/bin/homemesh" "$BIN_DIR/homemesh"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) echo "Add $BIN_DIR to your PATH (e.g. in ~/.bashrc): export PATH=\"$BIN_DIR:\$PATH\"";; esac

if [ "$SWITCHED" = 1 ] && [ -f "$UNIT" ]; then
  # Refresh the systemd unit from the new code, for the data folder the unit already uses (not this
  # terminal's HOMEMESH_HOME).
  UNIT_HOME="$(sed -n 's/^Environment=HOMEMESH_HOME=//p' "$UNIT" | tail -n 1)"
  if [ -n "$UNIT_HOME" ]; then
    refresh=(env "HOMEMESH_HOME=$UNIT_HOME" "$VENV/bin/python" -m homemesh service install)
  else
    refresh=(env -u HOMEMESH_HOME "$VENV/bin/python" -m homemesh service install)
  fi
  if "${refresh[@]}" >/dev/null 2>&1; then
    say "Refreshed the systemd unit $UNIT."
  else
    warn "could not refresh $UNIT; run 'homemesh service install' once."
  fi
fi

if [ "$WAS_RUNNING" = 1 ]; then
  say "Starting HomeMesh again (database migrations run automatically, with a backup first)…"
  "$BIN_DIR/homemesh" start || warn \
    "the upgrade is installed, but HomeMesh did not come back up (message above). Check 'homemesh status' and 'homemesh logs'."
fi

# Keep the folder the link points at and the one it pointed at before (to go back to; else the newest
# other one); remove older ones. Only real folders with generated names are touched.
if [ "$SWITCHED" = 1 ] && [ -d "$VENVS" ]; then
  TARGET="$(readlink -- "$VENV")"; TARGET="${TARGET##*/}"
  KEEP=""; KEEP_TS=""
  if [ -n "$PREV" ] && [ "$PREV" != "$TARGET" ] && [ -d "$VENVS/$PREV" ] && [[ "$PREV" =~ $ID_RE ]]; then
    KEEP="$PREV"
  else
    for d in "$VENVS"/*; do
      name="${d##*/}"
      if [ -L "$d" ] || [ ! -d "$d" ] || ! [[ "$name" =~ $ID_RE ]] || [ "$name" = "$TARGET" ]; then continue; fi
      ts="${name##*-}"
      if [ -z "$KEEP" ] || [[ "$ts" > "$KEEP_TS" ]]; then KEEP="$name"; KEEP_TS="$ts"; fi
    done
  fi
  for d in "$VENVS"/*; do
    name="${d##*/}"
    if [ -L "$d" ] || [ ! -d "$d" ] || ! [[ "$name" =~ $ID_RE ]] || [ "$name" = "$TARGET" ] || [ "$name" = "$KEEP" ]; then
      continue
    fi
    rm -rf -- "$d" || warn "could not remove the old program folder $d"
  done
fi

say "Installed: $("$VENV/bin/homemesh" version)"
cat <<EOT

Next steps:
  homemesh setup            # choose standalone / coordinator / worker / client
  homemesh service install  # optional: start automatically at login (systemd user service)
  homemesh start            # start in the background
  homemesh doctor           # check GPU, Ollama, ports, database
  homemesh update allow     # optional: let the hub's web app update this computer

Local AI models: install Ollama (https://ollama.com/download) and pull a model, e.g.
  ollama pull llama3.2:3b && ollama pull nomic-embed-text
EOT
