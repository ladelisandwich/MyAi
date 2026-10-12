#!/usr/bin/env bash
# Install Ollama (the local model server HomeMesh uses) for the CURRENT USER, without root.
# Works on Bazzite and other immutable systems: nothing is written outside your home folder.
#
#     ./deploy/linux/install-ollama.sh [--version v0.40.3] [--archive file.tar.zst] [--no-service]
#
# What it does:
#   * downloads the official release archive from github.com/ollama/ollama (it bundles the CUDA 12,
#     CUDA 13 and Vulkan runtimes, so only the GPU driver is needed - on Bazzite use a -nvidia image)
#   * verifies its SHA-256 (pinned for the default version; otherwise from the release's sha256sum.txt)
#   * unpacks it into ~/.local/share/ollama/<version> and links ~/.local/bin/ollama
#   * installs a systemd USER service that listens on 127.0.0.1:11434 only, and starts it
# Models are stored in ~/.ollama/models (Ollama's default). Uninstall: see the end of this file.
set -euo pipefail

DEFAULT_VERSION="v0.40.3"
# sha256 of ollama-linux-amd64.tar.zst for DEFAULT_VERSION, from the release's sha256sum.txt
DEFAULT_SHA256="ce6f8b671a699bf1ebafb28d60dbbabf4f37b30b499145fa308a8ba72de021d6"

VERSION="$DEFAULT_VERSION"
ARCHIVE=""
SERVICE=1
while [ $# -gt 0 ]; do
  case "$1" in
    --version) VERSION="$2"; shift ;;
    --archive) ARCHIVE="$2"; shift ;;
    --no-service) SERVICE=0 ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

say() { printf '\033[1m%s\033[0m\n' "$*"; }
fail() { printf '\033[31mError:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(uname -m)" = "x86_64" ] || fail "This script supports x86_64 only (found $(uname -m))."
command -v zstd >/dev/null 2>&1 || fail "zstd is needed to unpack the archive (Bazzite and Fedora include it; Ubuntu: sudo apt install zstd)."
command -v sha256sum >/dev/null 2>&1 || fail "sha256sum is missing (coreutils)."

BASE="${XDG_DATA_HOME:-$HOME/.local/share}/ollama"
DEST="$BASE/$VERSION"
URL="https://github.com/ollama/ollama/releases/download/$VERSION"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

if curl -fsS --max-time 2 http://127.0.0.1:11434/api/version >/dev/null 2>&1 && \
   ! systemctl --user is-active --quiet ollama 2>/dev/null; then
  fail "Something is already serving Ollama on 127.0.0.1:11434 (another Ollama install?). Use that one (HomeMesh just needs its URL) or stop it first."
fi

# ---- download + verify -----------------------------------------------------------------
if [ -z "$ARCHIVE" ]; then
  ARCHIVE="$TMP/ollama-linux-amd64.tar.zst"
  say "Downloading Ollama $VERSION (about 1.4 GB)…"
  curl -fL --retry 5 --retry-delay 3 -C - -o "$ARCHIVE" "$URL/ollama-linux-amd64.tar.zst"
fi
if [ "$VERSION" = "$DEFAULT_VERSION" ]; then
  EXPECTED="$DEFAULT_SHA256"
else
  curl -fsSL --retry 3 -o "$TMP/sha256sum.txt" "$URL/sha256sum.txt"
  EXPECTED="$(awk '$2 ~ /ollama-linux-amd64\.tar\.zst$/ {print $1}' "$TMP/sha256sum.txt")"
  [ -n "$EXPECTED" ] || fail "No checksum for ollama-linux-amd64.tar.zst in $URL/sha256sum.txt"
fi
say "Verifying SHA-256…"
ACTUAL="$(sha256sum "$ARCHIVE" | awk '{print $1}')"
[ "$ACTUAL" = "$EXPECTED" ] || fail "Checksum mismatch for $ARCHIVE (expected $EXPECTED, got $ACTUAL). Nothing was installed."

# ---- unpack ------------------------------------------------------------------------------
say "Unpacking into $DEST…"
rm -rf "$DEST.partial"
mkdir -p "$DEST.partial"
zstd -dc "$ARCHIVE" | tar -x -C "$DEST.partial" --no-same-owner
[ -x "$DEST.partial/bin/ollama" ] || fail "The archive did not contain bin/ollama."
rm -rf "$DEST"
mv "$DEST.partial" "$DEST"
ln -sfn "$DEST" "$BASE/current"
mkdir -p "$HOME/.local/bin"
ln -sf "$BASE/current/bin/ollama" "$HOME/.local/bin/ollama"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "Add ~/.local/bin to your PATH to use the 'ollama' command.";; esac

# ---- user service -----------------------------------------------------------------------------
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
if [ "$SERVICE" = 1 ] && systemctl --user show-environment >/dev/null 2>&1; then
  mkdir -p "$UNIT_DIR"
  cat > "$UNIT_DIR/ollama.service" <<'UNIT'
[Unit]
Description=Ollama local model server (per-user, for HomeMesh)
After=network-online.target

[Service]
ExecStart=%h/.local/share/ollama/current/bin/ollama serve
# Local only: HomeMesh talks to Ollama on this computer. Never expose Ollama to the network.
Environment=OLLAMA_HOST=127.0.0.1:11434
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
UNIT
  if [ -n "${XDG_DATA_HOME:-}" ]; then
    sed -i "s|%h/.local/share/ollama|$BASE|" "$UNIT_DIR/ollama.service"
  fi
  systemctl --user daemon-reload
  systemctl --user enable ollama.service >/dev/null
  systemctl --user restart ollama.service
  for _ in $(seq 1 30); do curl -fsS http://127.0.0.1:11434/api/version >/dev/null 2>&1 && break; sleep 0.5; done
  say "Ollama service: $(systemctl --user is-active ollama.service) ($(curl -fsS http://127.0.0.1:11434/api/version 2>/dev/null || echo 'not answering yet'))"
  echo "Keep it running while logged out (headless worker):  loginctl enable-linger $(id -un)"
else
  echo "systemd user services are not available here (or --no-service): start it yourself with"
  echo "  OLLAMA_HOST=127.0.0.1:11434 $BASE/current/bin/ollama serve"
fi

cat <<EOT

Ollama $VERSION is installed for $(id -un).
  ollama --version
  ollama pull llama3.2:3b          # a small first model
  ollama ps                        # after a chat: shows whether the model runs on the GPU
NVIDIA: 'nvidia-smi' must work on this computer (Bazzite: use a -nvidia image).

Uninstall: systemctl --user disable --now ollama; rm ~/.config/systemd/user/ollama.service ~/.local/bin/ollama;
           rm -r $BASE   (downloaded models are in ~/.ollama - delete that too if you want them gone)
EOT
