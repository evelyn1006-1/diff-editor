#!/usr/bin/env bash
set -euo pipefail

UNIT_NAME="${1:-diff-editor-terminal-failure-test.service}"
UNIT_PATH="/run/systemd/system/$UNIT_NAME"

sudo systemctl stop "$UNIT_NAME" >/dev/null 2>&1 || true
sudo systemctl reset-failed "$UNIT_NAME" >/dev/null 2>&1 || true
sudo rm -f "$UNIT_PATH"
sudo systemctl daemon-reload
sudo systemctl reset-failed "$UNIT_NAME" >/dev/null 2>&1 || true

echo "Removed runtime unit: $UNIT_NAME"
echo "If it still appears in the UI, use the task manager refresh button once."
