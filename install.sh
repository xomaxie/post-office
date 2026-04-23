#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Post Office installer

Usage:
  ./install.sh [--systemd] [--help]

What it does:
  - installs Python dependencies with uv using uv.lock
  - installs UI dependencies with npm using ui/package-lock.json
  - builds the React UI
  - optionally installs/restarts the post-office-ui systemd service

Environment knobs:
  POST_OFFICE_MODEL                 default: gpt-5.5
  POST_OFFICE_REASONING_EFFORT      default: high
  POST_OFFICE_WORKER_MODE           default: codex
  POST_OFFICE_WORKER_NAME           default: agent-fast
  POST_OFFICE_CODEX_ENABLE_WEB_SEARCH default: 1
USAGE
}

WITH_SYSTEMD=0
for arg in "$@"; do
  case "$arg" in
    --systemd) WITH_SYSTEMD=1 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown argument: $arg" >&2; usage; exit 2 ;;
  esac
done

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

need_cmd python3
need_cmd uv
need_cmd node
need_cmd npm

if ! command -v codex >/dev/null 2>&1; then
  echo "Warning: codex CLI not found. Codex worker mode will not run until it is installed." >&2
fi

if ! command -v gh >/dev/null 2>&1; then
  echo "Warning: gh CLI not found. Workers will not have GitHub CLI access until it is installed." >&2
fi

echo "Installing Python dependencies..."
uv sync --frozen

echo "Installing and building UI..."
(
  cd ui
  npm ci
  npm run build
)

if [[ "$WITH_SYSTEMD" == "1" ]]; then
  if [[ "${EUID}" -ne 0 ]]; then
    echo "--systemd must be run as root so it can write /etc and systemd units." >&2
    exit 1
  fi

  MODEL="${POST_OFFICE_MODEL:-gpt-5.5}"
  REASONING="${POST_OFFICE_REASONING_EFFORT:-high}"
  MODE="${POST_OFFICE_WORKER_MODE:-codex}"
  WORKER_NAME="${POST_OFFICE_WORKER_NAME:-agent-fast}"
  WEB_SEARCH="${POST_OFFICE_CODEX_ENABLE_WEB_SEARCH:-1}"

  cat > /etc/post-office-ui.env <<ENV
POST_OFFICE_HOME=${ROOT_DIR}
PYTHONPATH=${ROOT_DIR}
POST_OFFICE_WORKER_MODE=${MODE}
POST_OFFICE_WORKER_NAME=${WORKER_NAME}
POST_OFFICE_MODEL=${MODEL}
POST_OFFICE_REASONING_EFFORT=${REASONING}
POST_OFFICE_CODEX_ENABLE_WEB_SEARCH=${WEB_SEARCH}
POST_OFFICE_CODEX_CWD=${ROOT_DIR}
ENV

  cat > /etc/systemd/system/post-office-ui.service <<UNIT
[Unit]
Description=Post Office UI server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=/etc/post-office-ui.env
WorkingDirectory=${ROOT_DIR}
ExecStart=${ROOT_DIR}/.venv/bin/python -m uvicorn post_office_mcp.ui_server:app --host 127.0.0.1 --port 8765
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
UNIT

  systemctl daemon-reload
  systemctl enable --now post-office-ui.service
  systemctl restart post-office-ui.service
  systemctl --no-pager --full status post-office-ui.service || true
fi

cat <<DONE

Post Office install complete.

Run MCP stdio:
  POST_OFFICE_HOME=${ROOT_DIR} PYTHONPATH=${ROOT_DIR} uv run python -m post_office_mcp.server

Run UI server:
  POST_OFFICE_HOME=${ROOT_DIR} PYTHONPATH=${ROOT_DIR} uv run python -m uvicorn post_office_mcp.ui_server:app --host 127.0.0.1 --port 8765
DONE
