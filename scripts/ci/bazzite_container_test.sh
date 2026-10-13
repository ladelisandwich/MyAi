#!/usr/bin/env bash
# Runs as root inside the Bazzite OCI image in CI: checks what the image provides, validates the
# Quadlet files with Bazzite's own Podman, then runs the install lifecycle as a normal user.
set -euo pipefail
echo "== image"; grep -E '^(PRETTY_NAME|VERSION_ID)=' /etc/os-release
python3 -VV
ls /usr/bin/python3* | tr '\n' ' '; echo
python3 -c "import venv, sqlite3; sqlite3.connect(':memory:').execute('create virtual table t using fts5(x)'); print('venv + sqlite FTS5 OK, sqlite', sqlite3.sqlite_version)"
podman --version
command -v brew node npm nvidia-smi firewall-cmd zstd git curl || true
python3 -c 'import compression.zstd' 2>/dev/null && echo 'python3 has compression.zstd' || true

useradd -m tester 2>/dev/null
cp -r /src /home/tester/homemesh
chown -R tester:tester /home/tester/homemesh
# Run as the unprivileged user. setpriv, not su: containers have no PAM login sessions.
as_tester() {
  setpriv --reuid=tester --regid=tester --init-groups env -i HOME=/home/tester USER=tester LOGNAME=tester \
    PATH=/home/tester/.local/bin:/usr/local/bin:/usr/bin:/bin bash -lc "$1"
}

echo "== Quadlet dry-run with this Podman"
as_tester 'mkdir -p ~/.config/containers/systemd && cp ~/homemesh/deploy/bazzite/*.container ~/homemesh/deploy/bazzite/*.network ~/.config/containers/systemd/'
QUADLET=""
for q in /usr/libexec/podman/quadlet /usr/lib/podman/quadlet; do [ -x "$q" ] && QUADLET="$q" && break; done
[ -n "$QUADLET" ] || { echo "quadlet generator not found"; exit 1; }
as_tester "$QUADLET -dryrun -user" > /tmp/quadlet.out 2>&1 || { cat /tmp/quadlet.out; echo "Quadlet rejected the unit files"; exit 1; }
grep -E '^(---|ExecStart=)' /tmp/quadlet.out | cut -c1-240
if grep -iE 'unsupported|unknown key|error' /tmp/quadlet.out; then echo "Quadlet reported problems"; exit 1; fi
echo "== SearXNG unit as install-searxng.sh writes it (custom port), Quadlet dry-run again"
as_tester 'rm -f ~/.config/containers/systemd/homemesh-searxng.container && cd ~/homemesh && bash deploy/bazzite/install-searxng.sh --no-start --port 18888'
as_tester "$QUADLET -dryrun -user" > /tmp/quadlet2.out 2>&1 || { cat /tmp/quadlet2.out; echo "Quadlet rejected the SearXNG unit"; exit 1; }
grep -q -- '127.0.0.1:18888:8080' /tmp/quadlet2.out || { cat /tmp/quadlet2.out; echo "port not applied"; exit 1; }
if grep -iE 'unsupported|unknown key|error' /tmp/quadlet2.out; then echo "Quadlet reported problems"; exit 1; fi
as_tester 'test "$(stat -c %a ~/.config/homemesh-searxng/settings.yml)" = 600'

echo "== Ollama via deploy/linux/install-ollama.sh (per-user, checksum-verified), then a tiny real model"
as_tester 'cd ~/homemesh && bash deploy/linux/install-ollama.sh --no-service'
as_tester 'nohup ~/.local/bin/ollama serve > ~/ollama.log 2>&1 &'
for _ in $(seq 1 60); do curl -fsS http://127.0.0.1:11434/api/version && break; sleep 1; done
as_tester '~/.local/bin/ollama pull smollm2:135m'

echo "== install lifecycle as an unprivileged user (mock checks + a real chat through Ollama)"
as_tester 'HM_REAL_MODEL=smollm2:135m bash ~/homemesh/scripts/ci/linux_install_lifecycle.sh'
