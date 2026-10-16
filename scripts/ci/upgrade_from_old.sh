#!/usr/bin/env bash
# CI: upgrade a RUNNING HomeMesh installed from an older revision (default: the last build before the
# instance lock, which owners installed on 2026-10-12/13) with this tree's install.sh --upgrade, the way
# the guide says. The old process must be stopped by the installer, exactly one new process must run
# afterwards (with the new PID record), the database must be migrated with a backup, and chat must work.
set -euo pipefail
OLD_REV="${1:-3c901e4}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WORK="${RUNNER_TEMP:-/tmp}/hm-upgrade-from-old"
rm -rf "$WORK"; mkdir -p "$WORK/old" "$WORK/home"
git -C "$ROOT" archive "$OLD_REV" | tar -x -C "$WORK/old"
export HOME="$WORK/home" HOMEMESH_HOME="$WORK/home/hm" PATH="$WORK/home/.local/bin:$PATH"
VENV="$HOME/.local/share/homemesh/venv"
running() { pgrep -f "^$VENV/bin/(python[0-9.]*|homemesh) .*homemesh run|^$VENV/bin/homemesh run" || true; }

echo "== install $OLD_REV and start it"
(cd "$WORK/old" && bash deploy/linux/install.sh --no-web)
echo "correct-horse-battery" | homemesh setup --mode standalone --non-interactive --username owner \
  --password-stdin --mock --no-service --port 18947
homemesh start
sleep 2
old_pid="$(cat "$HOMEMESH_HOME/data/homemesh.pid")"
case "$old_pid" in ''|*[!0-9]*) echo "expected the old bare-PID format, got: $old_pid"; exit 1 ;; esac
"$VENV/bin/python" "$ROOT/scripts/bench.py" --runs 1 --model mock-chat

echo "== upgrade with this tree's installer, as the guide says"
(cd "$ROOT" && bash deploy/linux/install.sh --no-web --upgrade)
if kill -0 "$old_pid" 2>/dev/null; then echo "the old process $old_pid is still running"; exit 1; fi
rc=0; homemesh status || rc=$?
[ "$rc" = 0 ] || [ "$rc" = 2 ] || { echo "not running after the upgrade (status exit $rc)"; exit 1; }
homemesh version | grep -F "$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/src/homemesh/version.py")"
# Since 0.3.0 the venv path is a link to venvs/<id>; the old real folder is kept as venvs/legacy-<time>.
[ -L "$VENV" ] || { echo "the old venv folder was not converted to a link"; exit 1; }
case "$(readlink "$VENV")" in venvs/*) ;; *) echo "unexpected link target: $(readlink "$VENV")"; exit 1 ;; esac
ls "$(dirname "$VENV")/venvs" | grep -q '^legacy-[0-9]\{14\}$' || { echo "the old program folder was not kept"; exit 1; }
[ -f "$VENV/homemesh-install.json" ] || { echo "no install record in the new venv"; exit 1; }
grep -q '"ready": true' "$HOMEMESH_HOME/data/homemesh.pid" || { echo "no new PID record"; exit 1; }
n="$(running | grep -c . || true)"
[ "$n" = 1 ] || { echo "expected one HomeMesh process, found $n"; ps -o pid,args -p "$(running | paste -sd, -)"; exit 1; }
ls "$HOMEMESH_HOME/data/backups/" | grep -q '^pre-migration-' || { echo "no pre-migration backup"; exit 1; }
"$VENV/bin/python" "$ROOT/scripts/bench.py" --runs 1 --model mock-chat
homemesh stop
[ -z "$(running)" ] || { echo "still running after stop"; exit 1; }
echo "OK: upgrade from $OLD_REV passed"
