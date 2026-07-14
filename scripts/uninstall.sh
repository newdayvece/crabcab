#!/usr/bin/env bash

echo "crabcab uninstaller"
echo

pkill -f "crabcab/proxy.py" 2>/dev/null && echo "stopped the proxy" || true

PLIST="$HOME/Library/LaunchAgents/com.crabcab.proxy.plist"
if [ -f "$PLIST" ]; then
  launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "removed the launchd auto-start agent"
fi

rm -rf "$HOME/crabcab" && echo "removed ~/crabcab (scripts, logs, state)"

echo
echo "left untouched on purpose:"
echo "  - your codex login (~/.codex) - crabcab only ever borrowed it"
echo "  - the 'claudex' block in your shell rc (~/.zshrc etc) - open it and delete that block yourself"
echo
echo "done. and thanks for riding the crab cab."
