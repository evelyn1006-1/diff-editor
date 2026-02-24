#!/usr/bin/env bash
set -euo pipefail

sudo systemctl reload nginx
sudo systemctl restart diff-editor
