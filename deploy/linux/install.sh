#!/usr/bin/env bash
# HomeMesh AI - user-level installer for Linux (Fedora, Ubuntu, Arch, Bazzite and other
# immutable distributions). Run it from a checkout or an unpacked release:
#
#     ./deploy/linux/install.sh [--upgrade] [--no-web] [--python /usr/bin/python3.13]
#
# What it does (nothing system-wide, no root, no rpm-ostree layering):
#   * creates a virtual environment in ~/.local/share/homemesh/venv
#   * installs HomeMesh from this source tree into it
#   * builds the web UI if Node.js is available and no prebuilt UI is present
#   * links ~/.local/bin/homemesh
# Your data (~/.local/share/homemesh/data or $HOMEMESH_HOME) is never touched.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DATA_BASE="${XDG_DATA_HOME:-$HOME/.local/share}/homemesh"
VENV="${HOMEMESH_VENV:-$DATA_BASE/venv}"
BIN_DIR="$HOME/.local/bin"
PYTHON=""
BUILD_WEB=1
UPGRADE=0

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
fail() { printf '\033[31mError:\033[0m %s\n' "$*" >&2; exit 1; }

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
if [ ! -f "$ROOT/src/homemesh/web_dist/index.html" ]; then
  if [ "$BUILD_WEB" = 1 ] && command -v npm >/dev/null 2>&1; then
    say "Building the web interface…"
    "$ROOT/scripts/build_web.sh"
  else
    echo "Note: no prebuilt web UI found and Node.js/npm is not available (or --no-web)."
    echo "      The API and CLI will work; install Node.js 20.19+ and run scripts/build_web.sh for the UI."
    echo "      On Bazzite you can use 'brew install node' or a distrobox for this one-time build."
  fi
fi

# ---- Virtual environment + package ----------------------------------------------------
if [ -d "$VENV" ] && [ "$UPGRADE" = 0 ] && [ -x "$VENV/bin/homemesh" ]; then
  say "HomeMesh is already installed in $VENV - re-run with --upgrade to update it."
else
  say "Installing into $VENV…"
  mkdir -p "$DATA_BASE"
  "$PYTHON" -m venv "$VENV"
  "$VENV/bin/python" -m pip install --quiet --upgrade pip
  "$VENV/bin/python" -m pip install --quiet --upgrade "$ROOT"
fi

mkdir -p "$BIN_DIR"
ln -sf "$VENV/bin/homemesh" "$BIN_DIR/homemesh"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) echo "Add $BIN_DIR to your PATH (e.g. in ~/.bashrc): export PATH=\"$BIN_DIR:\$PATH\"";; esac

if [ "$UPGRADE" = 1 ] && "$BIN_DIR/homemesh" status >/dev/null 2>&1; then
  say "Restarting HomeMesh to apply the upgrade (database migrations run automatically, with a backup first)…"
  "$BIN_DIR/homemesh" restart || true
fi

say "Installed: $("$VENV/bin/homemesh" version)"
cat <<EOT

Next steps:
  homemesh setup            # choose standalone / coordinator / worker / client
  homemesh service install  # optional: start automatically at login (systemd user service)
  homemesh start            # start in the background
  homemesh doctor           # check GPU, Ollama, ports, database

Local AI models: install Ollama (https://ollama.com/download) and pull a model, e.g.
  ollama pull llama3.2:3b && ollama pull nomic-embed-text
EOT
