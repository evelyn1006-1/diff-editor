#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"

UNIT_NAME="${1:-diff-editor-terminal-failure-test.service}"
UNIT_PATH="/run/systemd/system/$UNIT_NAME"
PAYLOAD_PATH="$SCRIPT_DIR/failing_service_payload.py"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"
FAIL_DELAY_SECONDS="${FAIL_DELAY_SECONDS:-0.15}"

if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "error: python3 not found" >&2
    exit 1
fi

if [[ ! -f "$PAYLOAD_PATH" ]]; then
    echo "error: missing payload script at $PAYLOAD_PATH" >&2
    exit 1
fi

cat <<EOF | sudo tee "$UNIT_PATH" >/dev/null
[Unit]
Description=Diff Editor Terminal failing service test

[Service]
Type=oneshot
WorkingDirectory=$REPO_ROOT
Environment=FAIL_DELAY_SECONDS=$FAIL_DELAY_SECONDS
Environment=TEST_UNIT_NAME=$UNIT_NAME
ExecStart=$PYTHON_BIN $PAYLOAD_PATH
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl reset-failed "$UNIT_NAME" >/dev/null 2>&1 || true

if sudo systemctl start "$UNIT_NAME"; then
    echo "warning: $UNIT_NAME did not fail as expected" >&2
else
    echo "Seeded failing service: $UNIT_NAME"
fi

ACTIVE_STATE="$(sudo systemctl show "$UNIT_NAME" --property=ActiveState --value 2>/dev/null || true)"
SUB_STATE="$(sudo systemctl show "$UNIT_NAME" --property=SubState --value 2>/dev/null || true)"
RESULT_STATE="$(sudo systemctl show "$UNIT_NAME" --property=Result --value 2>/dev/null || true)"

echo "Current state: ${ACTIVE_STATE:-unknown} / ${SUB_STATE:-unknown} (${RESULT_STATE:-unknown})"
echo
echo "Open the task manager and look for '$UNIT_NAME' in Failed Services."
echo "Restarting it from the UI should fail again and surface the failure popup."
echo
echo "Useful commands:"
echo "  sudo systemctl status $UNIT_NAME --no-pager"
echo "  sudo journalctl -u $UNIT_NAME -n 40 --no-pager -o cat"
echo
echo "Cleanup:"
echo "  bash \"$SCRIPT_DIR/cleanup_failed_service.sh\" \"$UNIT_NAME\""
