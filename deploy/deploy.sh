#!/usr/bin/env bash
set -euo pipefail

if ! sudo -n true 2>/dev/null; then
    echo "Error: This script requires passwordless sudo" >&2
    exit 1
fi

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_DIR="$APP_DIR/deploy"
SYSTEMD_DIR="/etc/systemd/system"
SYSTEMD_WANTS_DIR="$SYSTEMD_DIR/multi-user.target.wants"
NGINX_SOURCE="${NGINX_SOURCE:-/home/evelyn/AUTHORIZATION/editor.princessevelyn.com.nginx}"
NGINX_AVAILABLE="/etc/nginx/sites-available/editor.princessevelyn.com"
NGINX_ENABLED="/etc/nginx/sites-enabled/editor.princessevelyn.com"
PYTHON="${PYTHON:-/usr/local/bin/python3.14}"
NGINX_BACKUP_DIR=""

cleanup_nginx_backup() {
    if [[ -n "$NGINX_BACKUP_DIR" && -d "$NGINX_BACKUP_DIR" ]]; then
        rm -rf "$NGINX_BACKUP_DIR"
    fi
}

trap cleanup_nginx_backup EXIT

link_path() {
    local source="$1"
    local target="$2"

    if [[ ! -e "$source" ]]; then
        echo "Missing source path: $source" >&2
        exit 1
    fi

    sudo mkdir -p "$(dirname "$target")"
    sudo ln -sfn "$source" "$target"
}

link_unit() {
    local name="$1"
    link_path "$DEPLOY_DIR/$name.service" "$SYSTEMD_DIR/$name.service"
}

enable_unit() {
    local name="$1"
    link_path "$SYSTEMD_DIR/$name.service" "$SYSTEMD_WANTS_DIR/$name.service"
}

backup_path() {
    local source="$1"
    local backup_name="$2"

    if sudo test -e "$source" || sudo test -L "$source"; then
        sudo cp -a "$source" "$NGINX_BACKUP_DIR/$backup_name"
    fi
}

restore_path() {
    local target="$1"
    local backup_name="$2"

    sudo rm -rf "$target"
    if sudo test -e "$NGINX_BACKUP_DIR/$backup_name" || sudo test -L "$NGINX_BACKUP_DIR/$backup_name"; then
        sudo cp -a "$NGINX_BACKUP_DIR/$backup_name" "$target"
    fi
}

validate_nginx_candidate() {
    NGINX_BACKUP_DIR="$(mktemp -d /tmp/diff-editor-nginx.XXXXXX)"

    backup_path "$NGINX_AVAILABLE" "available"
    backup_path "$NGINX_ENABLED" "enabled"

    link_path "$NGINX_SOURCE" "$NGINX_AVAILABLE"
    link_path "$NGINX_AVAILABLE" "$NGINX_ENABLED"

    if ! sudo nginx -t >/dev/null 2>&1; then
        restore_path "$NGINX_AVAILABLE" "available"
        restore_path "$NGINX_ENABLED" "enabled"
        echo "Replacement nginx config failed validation; restored previous nginx links" >&2
        exit 1
    fi
}

cd "$APP_DIR"

# Only install dependencies if requirements have changed
HASH_FILE="$DEPLOY_DIR/.requirements.hash"
if [[ -f requirements.txt ]]; then
    REQ_HASH=$(md5sum requirements.txt | cut -d' ' -f1)
    if [[ ! -f "$HASH_FILE" ]] || [[ "$(cat "$HASH_FILE")" != "$REQ_HASH" ]]; then
        echo "Requirements changed, installing dependencies..."
        sudo -H "$PYTHON" -m pip install --upgrade pip wheel --quiet --root-user-action=ignore --break-system-packages
        sudo -H "$PYTHON" -m pip install -r requirements.txt --quiet --root-user-action=ignore --break-system-packages
        echo "$REQ_HASH" > "$HASH_FILE"
    fi
elif [[ -f pyproject.toml ]]; then
    REQ_HASH=$(md5sum pyproject.toml | cut -d' ' -f1)
    if [[ ! -f "$HASH_FILE" ]] || [[ "$(cat "$HASH_FILE")" != "$REQ_HASH" ]]; then
        echo "pyproject.toml changed, installing dependencies..."
        sudo -H "$PYTHON" -m pip install --upgrade pip wheel --quiet --root-user-action=ignore --break-system-packages
        sudo -H "$PYTHON" -m pip install . --quiet --root-user-action=ignore --break-system-packages
        echo "$REQ_HASH" > "$HASH_FILE"
    fi
fi

link_unit diff-editor
link_unit terminal
enable_unit diff-editor
enable_unit terminal
validate_nginx_candidate

sudo systemctl reload nginx
sudo systemctl daemon-reload
sudo systemctl restart diff-editor terminal
