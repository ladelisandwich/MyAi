#!/usr/bin/env bash
# Set up SearXNG (a private meta search engine) for HomeMesh's web_search tool, for the CURRENT
# USER, without root: a rootless Podman container run by a systemd user service (Quadlet), reachable
# only from this computer at http://127.0.0.1:8888.
#
#     ./deploy/bazzite/install-searxng.sh [--port 8888] [--configure] [--no-start] [--remove]
#
#   --port N      publish on 127.0.0.1:N (default: the port of an existing install, else 8888)
#   --configure   also point this computer's HomeMesh at it and allow web_search + fetch_url here
#                 (homemesh web set-searxng / homemesh worker allow-tool), then restart HomeMesh
#   --no-start    only write the files (settings + Quadlet unit)
#   --remove      stop it and remove the Quadlet unit (keeps settings, the image and the cache volume)
#
# Files: ~/.config/homemesh-searxng/settings.yml (owner-only; has a random secret key) and
# ~/.config/containers/systemd/homemesh-searxng.container. Nothing outside your home folder is
# changed; no firewall or network settings are touched. Details: docs/WEB_ACCESS.md.
set -euo pipefail

PORT=""
CONFIGURE=0
START=1
REMOVE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="$2"; shift ;;
    --configure) CONFIGURE=1 ;;
    --no-start) START=0 ;;
    --remove) REMOVE=1 ;;
    -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

say() { printf '\033[1m%s\033[0m\n' "$*"; }
[ "$CONFIGURE" -eq 1 ] && [ "$START" -eq 0 ] && { echo "--configure needs SearXNG running; leave out --no-start." >&2; exit 2; }
fail() { printf '\033[31mError:\033[0m %s\n' "$*" >&2; exit 1; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="$HERE/homemesh-searxng.container"
UNIT=homemesh-searxng
CONF_DIR="${HOMEMESH_SEARXNG_CONFIG_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/homemesh-searxng}"
QUADLET_DIR="${HOMEMESH_QUADLET_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/containers/systemd}"
INSTALLED_PORT=""
if [ -f "$QUADLET_DIR/$UNIT.container" ]; then
  INSTALLED_PORT="$(sed -n 's/^PublishPort=127\.0\.0\.1:\([0-9]*\):8080$/\1/p' "$QUADLET_DIR/$UNIT.container")"
fi
# Re-running (e.g. after an update) keeps the port of the existing install unless --port is given.
PORT="${PORT:-${INSTALLED_PORT:-8888}}"
case "$PORT" in ''|*[!0-9]*) fail "--port needs a number" ;; esac
[ "$PORT" -ge 1024 ] && [ "$PORT" -le 65535 ] || fail "--port must be between 1024 and 65535"
[ "$(id -u)" -ne 0 ] || fail "Run this as your normal user, not root (it sets up a per-user service)."

URL="http://127.0.0.1:$PORT"

if [ "$REMOVE" -eq 1 ]; then
  OLD_IMAGE=""
  [ -f "$QUADLET_DIR/$UNIT.container" ] && OLD_IMAGE="$(sed -n 's/^Image=//p' "$QUADLET_DIR/$UNIT.container")"
  systemctl --user stop "$UNIT.service" 2>/dev/null || true
  rm -f "$QUADLET_DIR/$UNIT.container"
  systemctl --user daemon-reload 2>/dev/null || true
  say "SearXNG stopped and its service removed."
  echo "Kept: $CONF_DIR (settings), the container image and the volume '$UNIT-cache'."
  echo "Remove them too, if you like:  rm -r '$CONF_DIR'; podman volume rm $UNIT-cache${OLD_IMAGE:+; podman rmi $OLD_IMAGE}"
  echo "And on this computer's HomeMesh:  homemesh web set-searxng none && homemesh worker disallow-tool web_search"
  exit 0
fi

[ -f "$TEMPLATE" ] || fail "Missing $TEMPLATE (run this script from the HomeMesh source folder)."
# HOMEMESH_SEARXNG_IMAGE picks another SearXNG build without editing the file from the repository
# (which would block 'git pull'); it applies to this run, so set it again on later runs.
IMAGE="${HOMEMESH_SEARXNG_IMAGE:-$(sed -n 's/^Image=//p' "$TEMPLATE")}"
[ -n "$IMAGE" ] || fail "No Image= line in $TEMPLATE"

port_busy() {  # does anything (HTTP or not) listen on 127.0.0.1:$1?
  if command -v ss >/dev/null 2>&1; then
    [ -n "$(ss -Htln "sport = :$1" 2>/dev/null)" ]
  else
    [ "$(curl --noproxy "*" -s -o /dev/null -w '%{http_code}' --max-time 2 "http://127.0.0.1:$1/" || true)" != "000" ]
  fi
}

# ---- checks before anything is written -------------------------------------------------------
if [ "$START" -eq 1 ]; then
  command -v podman >/dev/null 2>&1 || fail "Podman is not installed. Bazzite includes it; elsewhere install the 'podman' package."
  systemctl --user show-environment >/dev/null 2>&1 || \
    fail "systemd user services are not available in this session (log in to the desktop, or use 'machinectl shell $(id -un)@')."
  ours_here=0
  if systemctl --user is-active --quiet "$UNIT.service" 2>/dev/null && [ "$INSTALLED_PORT" = "$PORT" ]; then ours_here=1; fi
  if [ "$ours_here" -eq 0 ] && port_busy "$PORT"; then
    fail "Port $PORT on this computer is already in use by another program. Choose another one with --port."
  fi
fi
if [ "$CONFIGURE" -eq 1 ]; then
  command -v homemesh >/dev/null 2>&1 || fail "The homemesh command was not found; install HomeMesh first (deploy/linux/install.sh)."
  [ -f "$(homemesh config path)" ] || \
    fail "HomeMesh is not set up on this computer yet. Join it first ('homemesh worker join …' from 'homemesh node pair' on the hub), then run this again."
fi

# ---- settings.yml ---------------------------------------------------------------------------
mkdir -p "$CONF_DIR"
chmod 700 "$CONF_DIR"
if [ -f "$CONF_DIR/settings.yml" ]; then
  say "Keeping your existing $CONF_DIR/settings.yml"
  grep -q 'json' "$CONF_DIR/settings.yml" || \
    echo "  Note: it does not mention 'json' (search.formats); HomeMesh needs JSON output."
else
  SECRET="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  [ "${#SECRET}" -eq 64 ] || fail "Could not generate a secret key."
  umask 077
  cat > "$CONF_DIR/settings.yml" <<EOF
# SearXNG settings for HomeMesh (written by install-searxng.sh). Everything not listed here uses
# SearXNG's defaults: https://docs.searxng.org/admin/settings/
use_default_settings: true

general:
  instance_name: "HomeMesh search"

server:
  # Random per-install key (SearXNG refuses to start with its placeholder key).
  secret_key: "$SECRET"
  # Private instance used by one household: no rate limiter (it would block HomeMesh's JSON
  # requests and needs a Valkey database), not advertised as public.
  limiter: false
  public_instance: false
  image_proxy: false

search:
  # HomeMesh asks for JSON (format=json); html keeps the normal web page working too.
  formats:
    - html
    - json
  safe_search: 1

valkey:
  url: false
EOF
  umask 022
  chmod 600 "$CONF_DIR/settings.yml"
  say "Wrote $CONF_DIR/settings.yml"
fi

# ---- Quadlet unit ----------------------------------------------------------------------------
mkdir -p "$QUADLET_DIR"
NEW_UNIT="$(sed -e "s#^PublishPort=127.0.0.1:8888:8080#PublishPort=127.0.0.1:$PORT:8080#" \
                -e "s#^Image=.*#Image=$IMAGE#" \
                -e "s#^Volume=%h/.config/homemesh-searxng:#Volume=$CONF_DIR:#" "$TEMPLATE")"
if [ -f "$QUADLET_DIR/$UNIT.container" ] && [ "$(cat "$QUADLET_DIR/$UNIT.container")" != "$NEW_UNIT" ]; then
  cp "$QUADLET_DIR/$UNIT.container" "$QUADLET_DIR/$UNIT.container.bak-$(date +%Y%m%d-%H%M%S)"
  echo "  (previous unit kept as $UNIT.container.bak-*)"
fi
printf '%s\n' "$NEW_UNIT" > "$QUADLET_DIR/$UNIT.container"
say "Wrote $QUADLET_DIR/$UNIT.container (127.0.0.1:$PORT only)"

if [ "$START" -eq 0 ]; then
  echo "Not started (--no-start). Start it with: systemctl --user daemon-reload && systemctl --user start $UNIT"
  exit 0
fi

systemctl --user daemon-reload
if ! systemctl --user cat "$UNIT.service" >/dev/null 2>&1; then
  for q in /usr/libexec/podman/quadlet /usr/lib/podman/quadlet; do
    [ -x "$q" ] && QUADLET_UNIT_DIRS="$QUADLET_DIR" "$q" -dryrun -user 2>&1 | tail -n 20 >&2 || true
  done
  fail "systemd did not create $UNIT.service from the Quadlet file (Podman 4.4 or newer is needed; see the messages above)."
fi

say "Downloading the SearXNG image (pinned build, about 100 MB; only the first time)…"
podman pull "$IMAGE"

say "Starting SearXNG…"
if ! systemctl --user restart "$UNIT.service"; then
  journalctl --user -u "$UNIT.service" -n 30 --no-pager >&2 || true
  if [ -z "$INSTALLED_PORT" ]; then
    rm -f "$QUADLET_DIR/$UNIT.container"  # a first install that can't start should not retry at every login
    systemctl --user daemon-reload || true
  fi
  fail "SearXNG could not start (log above)."
fi
for _ in $(seq 1 60); do
  curl --noproxy "*" -fsS --max-time 2 "$URL/healthz" >/dev/null 2>&1 && break
  sleep 1
done
if ! curl --noproxy "*" -fsS --max-time 2 "$URL/healthz" >/dev/null 2>&1; then
  journalctl --user -u "$UNIT.service" -n 30 --no-pager >&2 || true
  fail "SearXNG did not become ready on $URL (log above). Status: systemctl --user status $UNIT"
fi
# An empty query asks nothing from the internet: 400 means JSON output is on, 403 means it is off.
CODE="$(curl --noproxy "*" -s -o /dev/null -w '%{http_code}' --max-time 5 "$URL/search?format=json" || true)"
case "$CODE" in
  400) say "SearXNG is running on $URL with JSON output enabled." ;;
  403) fail "SearXNG is running but JSON output is off. Add '- json' under search.formats in $CONF_DIR/settings.yml, then: systemctl --user restart $UNIT" ;;
  *) fail "Unexpected answer from $URL/search (HTTP $CODE). Status: systemctl --user status $UNIT" ;;
esac
echo "It starts automatically when you log in. Stop: systemctl --user stop $UNIT   Remove: $0 --remove"
if [ -n "$INSTALLED_PORT" ] && [ "$INSTALLED_PORT" != "$PORT" ]; then
  say "The address changed from port $INSTALLED_PORT to $PORT: run 'homemesh web set-searxng $URL' and 'homemesh restart'."
fi

if [ "$CONFIGURE" -eq 1 ]; then
  command -v homemesh >/dev/null 2>&1 || fail "The homemesh command was not found; configure it by hand (below)."
  homemesh web set-searxng "$URL"
  homemesh worker allow-tool web_search
  homemesh worker allow-tool fetch_url
  homemesh restart || true
  homemesh web test "weather" || true
else
  echo
  echo "Next, let HomeMesh on this computer use it:"
  echo "  homemesh web set-searxng $URL"
  echo "  homemesh worker allow-tool web_search"
  echo "  homemesh worker allow-tool fetch_url"
  echo "  homemesh restart && homemesh web test"
fi
