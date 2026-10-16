#!/usr/bin/env bash
# Remove the HomeMesh program. Your data (conversations, memories, settings, backups) is KEPT
# unless you pass --purge-data and confirm.
set -euo pipefail
DATA_BASE="${XDG_DATA_HOME:-$HOME/.local/share}/homemesh"
VENV="${HOMEMESH_VENV:-$DATA_BASE/venv}"
VENV="${VENV%/}"
PREFIX="$(dirname "$VENV")"
ID_RE='^([0-9a-f]{12}|nogit|legacy)-[0-9]{14}$'   # program folders under venvs/ (installer / updater)
BUILD_RE='^[0-9a-f]{8}$'                           # the updater's build folders under b/
PURGE=0
[ "${1:-}" = "--purge-data" ] && PURGE=1

if [ -x "$VENV/bin/homemesh" ]; then
  "$VENV/bin/homemesh" stop || true
  "$VENV/bin/homemesh" service uninstall || true
fi
rm -f "$HOME/.local/bin/homemesh"
# The stable path first (a link to venvs/<id> since 0.3.0, a real folder before), then the program
# folders and build folders by their generated names only: <prefix> may also hold your data.
if [ -L "$VENV" ]; then rm -f -- "$VENV"; else rm -rf -- "$VENV"; fi
for leftover in "$VENV.next" "$VENV.old"; do
  if [ -L "$leftover" ]; then rm -f -- "$leftover"; fi
done
for dir in "$PREFIX/venvs" "$PREFIX/b"; do
  [ -d "$dir" ] && [ ! -L "$dir" ] || continue
  re="$ID_RE"; [ "${dir##*/}" = b ] && re="$BUILD_RE"
  for d in "$dir"/*; do
    name="${d##*/}"
    if [ -d "$d" ] && [ ! -L "$d" ] && [[ "$name" =~ $re ]]; then rm -rf -- "$d"; fi
  done
  rmdir -- "$dir" 2>/dev/null || true
done
echo "HomeMesh program removed."

if [ "$PURGE" = 1 ]; then
  CONF="${XDG_CONFIG_HOME:-$HOME/.config}/homemesh"
  echo "This will permanently delete: $DATA_BASE and $CONF (conversations, memories, keys, backups)."
  read -r -p "Type DELETE to confirm: " ans
  if [ "$ans" = "DELETE" ]; then rm -rf "$DATA_BASE" "$CONF"; echo "Data deleted."; else echo "Kept your data."; fi
else
  echo "Your data is kept in ${HOMEMESH_HOME:-$DATA_BASE} (delete it yourself or re-run with --purge-data)."
fi
