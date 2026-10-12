#!/usr/bin/env bash
# Install-path test run by CI as an UNPRIVILEGED user (e.g. inside the Bazzite image):
# install.sh -> setup -> background start -> chat via the API (labelled mock backend) ->
# upgrade while running -> stop -> uninstall (data must be kept).
set -euo pipefail
cd "$(dirname "$0")/../.."
export PATH="$HOME/.local/bin:$PATH"
export HOMEMESH_HOME="$HOME/hm-ci"

echo "== $(. /etc/os-release && echo "$PRETTY_NAME") / $(python3 --version)"
./deploy/linux/install.sh --no-web
homemesh version

echo "correct-horse-battery" | homemesh setup --mode standalone --non-interactive --username owner \
  --password-stdin --mock --no-service --port 18941
homemesh start
rc=0; homemesh status || rc=$?
[ "$rc" = 0 ] || [ "$rc" = 2 ] || { echo "status exit $rc after start"; exit 1; }
"$HOME/.local/share/homemesh/venv/bin/python" scripts/bench.py --runs 2 --model mock-chat

echo "== upgrade while running (must restart it)"
./deploy/linux/install.sh --no-web --upgrade
rc=0; homemesh status || rc=$?
[ "$rc" = 0 ] || [ "$rc" = 2 ] || { echo "not running after upgrade (exit $rc)"; exit 1; }
"$HOME/.local/share/homemesh/venv/bin/python" scripts/bench.py --runs 1 --model mock-chat

homemesh stop
if homemesh status; then echo "still running after stop"; exit 1; fi

echo "== uninstall keeps data"
./deploy/linux/uninstall.sh
[ ! -e "$HOME/.local/bin/homemesh" ] || { echo "command still installed"; exit 1; }
[ -f "$HOMEMESH_HOME/data/homemesh.sqlite3" ] || { echo "data was removed"; exit 1; }
echo "OK: Linux install lifecycle passed"
