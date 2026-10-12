#!/usr/bin/env bash
# Opt-in smoke tests against a real Ollama. Pick models you have installed:
#   HOMEMESH_SMOKE_CHAT_MODEL=llama3.2:3b HOMEMESH_SMOKE_EMBED_MODEL=nomic-embed-text scripts/smoke_real_ollama.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
exec .venv/bin/pytest -m smoke tests/smoke -v "$@"
