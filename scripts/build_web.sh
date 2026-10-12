#!/usr/bin/env bash
# Build the web UI and copy it into the Python package (src/homemesh/web_dist) so that
# 'pip install .' / wheels ship it. Requires Node.js >= 20.19 and npm.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/apps/web"
if ! command -v npm >/dev/null 2>&1; then
  echo "npm not found. Install Node.js 20.19+ (https://nodejs.org) or use a release that already contains the UI." >&2
  exit 1
fi
npm ci --no-audit --no-fund
npm run build
rm -rf "$ROOT/src/homemesh/web_dist"
cp -r "$ROOT/apps/web/dist" "$ROOT/src/homemesh/web_dist"
echo "Web UI built into src/homemesh/web_dist"
