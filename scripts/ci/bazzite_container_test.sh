#!/usr/bin/env bash
# Runs as root inside the Bazzite OCI image in CI: checks what the image provides, validates the
# Quadlet files with Bazzite's own Podman, then runs the install lifecycle as a normal user.
set -euo pipefail
echo "== image"; grep -E '^(PRETTY_NAME|VERSION_ID)=' /etc/os-release
python3 -VV
ls /usr/bin/python3* | tr '\n' ' '; echo
python3 -c "import venv, sqlite3; sqlite3.connect(':memory:').execute('create virtual table t using fts5(x)'); print('venv + sqlite FTS5 OK, sqlite', sqlite3.sqlite_version)"
podman --version
command -v brew node npm nvidia-smi firewall-cmd || true

useradd -m tester
cp -r /src /home/tester/homemesh
chown -R tester:tester /home/tester/homemesh

echo "== Quadlet dry-run with this Podman"
su - tester -c 'mkdir -p ~/.config/containers/systemd && cp ~/homemesh/deploy/bazzite/*.container ~/homemesh/deploy/bazzite/*.network ~/.config/containers/systemd/'
QUADLET=""
for q in /usr/libexec/podman/quadlet /usr/lib/podman/quadlet; do [ -x "$q" ] && QUADLET="$q" && break; done
[ -n "$QUADLET" ] || { echo "quadlet generator not found"; exit 1; }
su - tester -c "$QUADLET -dryrun -user" > /tmp/quadlet.out 2>&1 || { cat /tmp/quadlet.out; echo "Quadlet rejected the unit files"; exit 1; }
grep -E '^(---|ExecStart=)' /tmp/quadlet.out | cut -c1-240
if grep -iE 'unsupported|unknown key|error' /tmp/quadlet.out; then echo "Quadlet reported problems"; exit 1; fi

echo "== Ollama via deploy/linux/install-ollama.sh (per-user, checksum-verified), then a tiny real model"
su - tester -c 'cd ~/homemesh && ./deploy/linux/install-ollama.sh --no-service'
su - tester -c 'nohup ~/.local/bin/ollama serve > ~/ollama.log 2>&1 &'
for _ in $(seq 1 60); do curl -fsS http://127.0.0.1:11434/api/version && break; sleep 1; done
su - tester -c '~/.local/bin/ollama pull smollm2:135m'

echo "== install lifecycle as an unprivileged user (mock checks + a real chat through Ollama)"
su - tester -c 'HM_REAL_MODEL=smollm2:135m bash ~/homemesh/scripts/ci/linux_install_lifecycle.sh'
