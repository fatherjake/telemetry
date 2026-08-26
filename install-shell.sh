#!/usr/bin/env bash
# OPTIONAL: permanently add the telemetry settings to your shell profile.
#
# Nothing else in this project modifies your profile. This script only acts
# when you run it directly, and it asks before writing.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENVFILE="$HERE/.env.telemetry"

[ -f "$ENVFILE" ] || { echo "run ./setup.sh first"; exit 1; }

case "${SHELL##*/}" in
  zsh)  PROFILE="$HOME/.zshrc" ;;
  bash) PROFILE="$HOME/.bash_profile"; [ -f "$HOME/.bashrc" ] && PROFILE="$HOME/.bashrc" ;;
  *)    PROFILE="${1:-}" ;;
esac
[ -n "${PROFILE:-}" ] || { echo "could not detect your shell profile; pass it as an argument"; exit 1; }

MARKER="# >>> claude-telemetry >>>"
if grep -qF "$MARKER" "$PROFILE" 2>/dev/null; then
  echo "already installed in $PROFILE"; exit 0
fi

cat <<MSG

This will append the following to $PROFILE:

$MARKER
[ -f "$ENVFILE" ] && source "$ENVFILE"
# <<< claude-telemetry <<<

Every new shell will then send Claude Code telemetry to your local collector.
If the collector is not running, Claude Code simply fails to export and
continues working normally.

MSG
read -r -p "Proceed? [y/N] " reply
case "$reply" in
  y|Y|yes|YES) ;;
  *) echo "aborted"; exit 1 ;;
esac

cp "$PROFILE" "$PROFILE.bak-$(date +%s)" 2>/dev/null || true
{
  echo ""
  echo "$MARKER"
  echo "[ -f \"$ENVFILE\" ] && source \"$ENVFILE\""
  echo "# <<< claude-telemetry <<<"
} >> "$PROFILE"

echo "installed. Open a new shell, or run: source $PROFILE"
echo "to undo, delete the marked block from $PROFILE"
