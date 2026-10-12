#!/usr/bin/env bash
# Developer loop: coordinator + embedded worker (mock backend enabled) on :8765 and the Vite
# dev server on :5173 with hot reload, both using a throw-away HOMEMESH_HOME.
#   scripts/dev.sh            # mock + real Ollama if running
#   HOMEMESH_MOCK=0 scripts/dev.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export HOMEMESH_HOME="${HOMEMESH_HOME:-$ROOT/.homemesh/dev}"
export HOMEMESH_MOCK="${HOMEMESH_MOCK:-1}"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || { echo "Create the venv first: uv venv && uv pip install -e '.[dev]'"; exit 1; }
if [ ! -f "$HOMEMESH_HOME/homemesh.toml" ]; then
  printf 'devpassword123\n' | "$PY" -m homemesh setup --mode standalone --non-interactive --username dev \
    --password-stdin --local-only --no-service
  echo "Dev login: dev / devpassword123"
fi
"$PY" -m homemesh run &
COORD=$!
trap 'kill $COORD 2>/dev/null' EXIT
cd "$ROOT/apps/web" && npm run dev
