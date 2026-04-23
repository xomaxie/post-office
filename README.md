# Post Office

Post Office is a mailbox-based dispatcher for Agent Zero subagents. Agent Zero leaves a task as mail, the postmaster routes it to a worker backend, and the worker leaves mail back when it starts, completes, needs input, or fails.

It includes:

- a FastMCP server
- a subprocess-backed worker runner
- a React + Tailwind dashboard
- Codex CLI, OpenAI-compatible, and custom command worker modes
- SQLite storage for threads, runs, messages, and worker state
- per-run artifact folders for transcripts and raw worker output

## Dependencies

The repo keeps both dependency graphs pinned:

| Area | Files | Installer command |
| --- | --- | --- |
| Python | `pyproject.toml`, `uv.lock` | `uv sync --frozen` |
| UI | `ui/package.json`, `ui/package-lock.json` | `npm ci` |

Runtime tools:

- Python 3.11+
- `uv`
- Node.js 22+ and `npm`
- optional: `codex` for Codex worker mode
- optional: `gh` so Codex workers can use GitHub CLI access

## Install

Clone the repo, then run:

```bash
./install.sh
```

That installs Python dependencies, installs UI dependencies, and builds the frontend.

To also install and start the local UI systemd service:

```bash
sudo ./install.sh --systemd
```

The systemd path uses the directory where you run the installer, so clone it where you want it to live first. A common layout is:

```bash
sudo git clone https://github.com/xomaxie/post-office.git /opt/post-office
cd /opt/post-office
sudo ./install.sh --systemd
```

## Configuration

Common environment variables:

| Variable | Default | Notes |
| --- | --- | --- |
| `POST_OFFICE_HOME` | repo root | base directory for the database, artifacts, and UI build |
| `POST_OFFICE_DB_PATH` | `$POST_OFFICE_HOME/post_office.db` | SQLite database path |
| `POST_OFFICE_ARTIFACTS_DIR` | `$POST_OFFICE_HOME/artifacts` | per-run artifact storage |
| `POST_OFFICE_WORKER_MODE` | `openai` | `codex`, `openai`, or `command` |
| `POST_OFFICE_WORKER_NAME` | `agent-fast` | author name for worker replies |
| `POST_OFFICE_MODEL` | `openai.gpt-5.5.high` | worker model; Codex mode resolves this to `gpt-5.5` |
| `POST_OFFICE_REASONING_EFFORT` | unset | use values like `high` when your backend supports them |
| `POST_OFFICE_CODEX_ENABLE_WEB_SEARCH` | `1` in Codex mode | enables native Codex web search |
| `POST_OFFICE_CODEX_CWD` | current dir / home | default Codex workspace |

`deploy/post-office-ui.env.example` has a ready-to-edit env file.

## Worker backends

### Codex CLI mode

```bash
POST_OFFICE_WORKER_MODE=codex
POST_OFFICE_WORKER_NAME=agent-fast
POST_OFFICE_MODEL=gpt-5.5
POST_OFFICE_REASONING_EFFORT=high
POST_OFFICE_CODEX_ENABLE_WEB_SEARCH=1
POST_OFFICE_CODEX_CWD=/opt/post-office
```

Codex mode gives workers shell access, `gh` access when installed, web search when enabled, live stdout/stderr artifacts, and stop support for active subprocess runs.

### OpenAI-compatible mode

```bash
POST_OFFICE_WORKER_MODE=openai
POST_OFFICE_API_BASE=<openai-compatible-base-url>
POST_OFFICE_API_KEY=<key>
POST_OFFICE_MODEL=gpt-5.5
POST_OFFICE_REASONING_EFFORT=high
```

The direct API path uses the OpenAI Responses API client with no application-level timeout.

### External command mode

```bash
POST_OFFICE_WORKER_MODE=command
POST_OFFICE_WORKER_COMMAND='python3 /abs/path/to/worker.py'
```

The command receives one JSON payload on stdin and must print one JSON result:

```json
{
  "status": "completed",
  "summary": "short summary",
  "details": "full response",
  "questions": [],
  "artifacts": []
}
```

## MCP tools

- `submit_task`
- `list_threads`
- `list_runs`
- `get_run`
- `list_messages`
- `get_thread`
- `mark_message_read`
- `mark_thread_read`
- `archive_thread`
- `purge_thread`
- `dispatch_queued`
- `fail_run`

## MCP resources

- `postoffice://status`
- `postoffice://thread/{thread_id}`
- `postoffice://run/{run_id}`
- `postoffice://worker-capabilities`
- `postoffice://subagent-profiles`

## Run locally

MCP stdio server:

```bash
POST_OFFICE_HOME=$PWD PYTHONPATH=$PWD uv run python -m post_office_mcp.server
```

HTTP MCP server:

```bash
POST_OFFICE_HOME=$PWD \
PYTHONPATH=$PWD \
POST_OFFICE_TRANSPORT=streamable-http \
POST_OFFICE_HOST=127.0.0.1 \
POST_OFFICE_PORT=8123 \
uv run python -m post_office_mcp.server
```

UI server:

```bash
POST_OFFICE_HOME=$PWD PYTHONPATH=$PWD uv run python -m uvicorn post_office_mcp.ui_server:app --host 127.0.0.1 --port 8765
```

The UI serves JSON endpoints under `/api/*`, streams live updates at `/api/events`, and serves the built frontend from `ui/dist`.

## Agent Zero MCP config example

Local stdio:

```json
{
  "mcpServers": {
    "post_office": {
      "command": "/opt/post-office/.venv/bin/python",
      "args": ["-m", "post_office_mcp.server"],
      "env": {
        "PYTHONPATH": "/opt/post-office",
        "POST_OFFICE_HOME": "/opt/post-office",
        "POST_OFFICE_WORKER_MODE": "codex",
        "POST_OFFICE_WORKER_NAME": "agent-fast",
        "POST_OFFICE_MODEL": "gpt-5.5",
        "POST_OFFICE_REASONING_EFFORT": "high",
        "POST_OFFICE_CODEX_ENABLE_WEB_SEARCH": "1",
        "POST_OFFICE_CODEX_CWD": "/opt/post-office"
      }
    }
  }
}
```

HTTP:

```json
{
  "mcpServers": {
    "post_office": {
      "type": "streamable-http",
      "url": "http://127.0.0.1:8123/mcp",
      "init_timeout": 0,
      "tool_timeout": 0
    }
  }
}
```

## Dashboard

The dashboard supports:

- live thread, run, and mailbox views
- new tasks and follow-ups in the same thread
- mark-thread-read
- archive and purge
- manual queued dispatch
- stop active runs
- per-run transcript and artifact links

## Tests

```bash
uv sync --frozen --extra dev
uv run pytest -q
cd ui && npm ci && npm run build
```
