#!/usr/bin/env bash
# Remove the HomeMesh program. Your data (conversations, memories, settings, backups) is KEPT
# unless you pass --purge-data and confirm.
set -euo pipefail
DATA_BASE="${XDG_DATA_HOME:-$HOME/.local/share}/homemesh"
VENV="${HOMEMESH_VENV:-$DATA_BASE/venv}"
PURGE=0
[ "${1:-}" = "--purge-data" ] && PURGE=1

if [ -x "$VENV/bin/homemesh" ]; then
  "$VENV/bin/homemesh" stop || true
  "$VENV/bin/homemesh" service uninstall || true
fi
rm -f "$HOME/.local/bin/homemesh"
rm -rf "$VENV"
echo "HomeMesh program removed."

if [ "$PURGE" = 1 ]; then
  CONF="${XDG_CONFIG_HOME:-$HOME/.config}/homemesh"
  echo "This will permanently delete: $DATA_BASE and $CONF (conversations, memories, keys, backups)."
  read -r -p "Type DELETE to confirm: " ans
  if [ "$ans" = "DELETE" ]; then rm -rf "$DATA_BASE" "$CONF"; echo "Data deleted."; else echo "Kept your data."; fi
else
  echo "Your data is kept in $DATA_BASE (delete it yourself or re-run with --purge-data)."
fi
