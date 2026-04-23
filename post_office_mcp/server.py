from __future__ import annotations

import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastmcp import FastMCP
from pydantic import BaseModel, Field

Status = Literal["queued", "started", "completed", "needs_input", "failed"]

BASE_DIR = Path(os.environ.get("POST_OFFICE_HOME", Path(__file__).resolve().parents[1]))
DB_PATH = Path(os.environ.get("POST_OFFICE_DB_PATH", BASE_DIR / "post_office.db"))
ARTIFACTS_DIR = Path(os.environ.get("POST_OFFICE_ARTIFACTS_DIR", BASE_DIR / "artifacts"))
WORKER_MODE = os.environ.get("POST_OFFICE_WORKER_MODE", "openai").strip().lower()
WORKER_NAME = os.environ.get("POST_OFFICE_WORKER_NAME", "agent-fast").strip() or "agent-fast"
MODEL_NAME = os.environ.get("POST_OFFICE_MODEL", "openai.gpt-5.5.high").strip() or "openai.gpt-5.5.high"
API_BASE = os.environ.get("POST_OFFICE_API_BASE", "").strip()
API_KEY = os.environ.get("POST_OFFICE_API_KEY", "").strip() or os.environ.get("MALAK_API_KEY", "").strip()
WORKER_COMMAND = os.environ.get("POST_OFFICE_WORKER_COMMAND", "").strip()
REASONING_EFFORT = os.environ.get("POST_OFFICE_REASONING_EFFORT", "").strip().lower()
POLL_SECONDS = max(float(os.environ.get("POST_OFFICE_POLL_SECONDS", "1.0")), 0.1)
WORKER_EVENT_PREFIX = "POST_OFFICE_EVENT "


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def gen_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class WorkerResult(BaseModel):
    status: Literal["completed", "needs_input", "failed"] = Field(default="completed")
    summary: str = Field(default="")
    details: str = Field(default="")
    questions: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)


AUTONOMOUS_SYSTEM_PROMPT = """You are a fully autonomous remote worker operating through a post office.

Rules:
- Default to completing the task end-to-end without asking for more input.
- Only return needs_input when the task is genuinely blocked by missing facts, credentials, approvals, or external access.
- Use available tools aggressively when your backend actually has them.
- If you only have tool awareness and not direct tool execution, say that plainly and continue with the best reasoning/reporting you can.
- If the task fails, explain exactly why and what the superior should do next.
- Be concrete, decisive, and action-oriented.
- Return JSON only.

Required JSON schema:
{
  "status": "completed" | "needs_input" | "failed",
  "summary": "short summary",
  "details": "full response",
  "questions": ["only when blocked"],
  "artifacts": ["optional file paths, urls, ids"]
}
"""

SUBAGENT_PROFILES: dict[str, dict[str, Any]] = {
    "default": {
        "label": "General worker",
        "description": "Autonomous general-purpose subagent for research, implementation, debugging, and reporting.",
        "aliases": ["default", "general", "agent-fast", "worker"],
        "system_prompt": "",
    },
    "writer": {
        "label": "Writer",
        "description": "Writing-focused subagent that works ideas-first, then outline, then details.",
        "aliases": ["writer", "writing", "copywriter", "author"],
        "system_prompt": """You are the writer subagent.

Treat good technical writing as a three-layer job:
- Ideas first: find the non-generic points that are actually worth saying.
- Outline next: shape those ideas into a clear structure with strong flow and an obvious reason to care.
- Details last: only after the ideas and outline are solid, add concrete examples, references, anecdotes, data, and precise wording.

Writing rules:
- Prefer authentic, insight-rich writing over generic summaries or filler.
- Do not invent facts, citations, anecdotes, or data points.
- If the source material is thin, state assumptions briefly and keep the prose honest.
- Be concise by default and expand only where extra depth earns its place.
- Keep section-to-section flow smooth so readers can move between big-picture structure and fine-grained details without getting lost.
- Write titles last, after the body is coherent.
- Use polishing only at the end; substance and structure come first.
- If asked to rewrite or edit, preserve the user's actual ideas and voice instead of flattening them into generic LLM prose.
""",
    },
}


def list_subagent_profiles() -> list[dict[str, str]]:
    return [
        {
            "id": profile_id,
            "label": str(profile["label"]),
            "description": str(profile["description"]),
        }
        for profile_id, profile in SUBAGENT_PROFILES.items()
    ]


def resolve_subagent_type(recipient: str = "", metadata: dict[str, Any] | None = None) -> str:
    candidates: list[str] = []
    if isinstance(metadata, dict):
        for key in ("subagent_type", "agent_type", "profile", "recipient_profile"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append(value.strip().lower())
    if recipient.strip():
        candidates.append(recipient.strip().lower())
    for candidate in candidates:
        for profile_id, profile in SUBAGENT_PROFILES.items():
            aliases = {profile_id, *(alias.lower() for alias in profile.get("aliases", []))}
            if candidate in aliases:
                return profile_id
    return "default"


def get_subagent_profile(recipient: str = "", metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    profile_id = resolve_subagent_type(recipient=recipient, metadata=metadata)
    profile = dict(SUBAGENT_PROFILES[profile_id])
    profile["id"] = profile_id
    return profile


def render_worker_system_prompt(recipient: str = "", metadata: dict[str, Any] | None = None) -> str:
    profile = get_subagent_profile(recipient=recipient, metadata=metadata)
    prompt_parts = [AUTONOMOUS_SYSTEM_PROMPT]
    extra_prompt = str(profile.get("system_prompt", "") or "").strip()
    if extra_prompt:
        prompt_parts.append(extra_prompt)
    return "\n\n".join(prompt_parts)


@dataclass
class PostOffice:
    db_path: Path
    worker_mode: str
    worker_name: str
    model_name: str
    api_base: str = ""
    api_key: str = ""
    worker_command: str = ""
    reasoning_effort: str = ""

    def __post_init__(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
        self._worker_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._change_counter = 0
        self._change_lock = threading.Lock()
        self._active_processes: dict[str, subprocess.Popen[str]] = {}
        self._active_processes_lock = threading.Lock()
        self._init_db()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS threads (
                    id TEXT PRIMARY KEY,
                    recipient TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_run_id TEXT,
                    last_error TEXT,
                    archived_at TEXT
                );

                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    heartbeat_at TEXT,
                    finished_at TEXT,
                    worker_pid INTEGER,
                    cancel_requested_at TEXT,
                    activity_text TEXT,
                    workdir TEXT,
                    artifacts_json TEXT NOT NULL DEFAULT '[]',
                    error_text TEXT,
                    FOREIGN KEY(thread_id) REFERENCES threads(id)
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    run_id TEXT,
                    author TEXT NOT NULL,
                    role TEXT NOT NULL,
                    status TEXT NOT NULL,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    read_at TEXT,
                    metadata_json TEXT NOT NULL,
                    FOREIGN KEY(thread_id) REFERENCES threads(id),
                    FOREIGN KEY(run_id) REFERENCES runs(id)
                );

                CREATE INDEX IF NOT EXISTS idx_messages_thread_created ON messages(thread_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_messages_unread ON messages(read_at, created_at);
                CREATE INDEX IF NOT EXISTS idx_runs_state_created ON runs(state, created_at);
                """
            )
            thread_columns = {row["name"] for row in conn.execute("PRAGMA table_info(threads)").fetchall()}
            if "archived_at" not in thread_columns:
                conn.execute("ALTER TABLE threads ADD COLUMN archived_at TEXT")
            run_columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
            if "worker_pid" not in run_columns:
                conn.execute("ALTER TABLE runs ADD COLUMN worker_pid INTEGER")
            if "cancel_requested_at" not in run_columns:
                conn.execute("ALTER TABLE runs ADD COLUMN cancel_requested_at TEXT")
            if "heartbeat_at" not in run_columns:
                conn.execute("ALTER TABLE runs ADD COLUMN heartbeat_at TEXT")
            if "activity_text" not in run_columns:
                conn.execute("ALTER TABLE runs ADD COLUMN activity_text TEXT")
            if "workdir" not in run_columns:
                conn.execute("ALTER TABLE runs ADD COLUMN workdir TEXT")
            if "artifacts_json" not in run_columns:
                conn.execute("ALTER TABLE runs ADD COLUMN artifacts_json TEXT NOT NULL DEFAULT '[]'")

    def start(self) -> None:
        if self._worker_thread and self._worker_thread.is_alive():
            return
        self._stop_event.clear()
        self._wake_event.set()
        self._worker_thread = threading.Thread(target=self._worker_loop, name="post-office-worker", daemon=True)
        self._worker_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        self._kill_all_active_processes("service stopping")
        if self._worker_thread and self._worker_thread.is_alive() and self._worker_thread is not threading.current_thread():
            self._worker_thread.join(timeout=max(POLL_SECONDS, 1.0))

    @property
    def change_counter(self) -> int:
        return self._change_counter

    def _bump_change(self) -> None:
        with self._change_lock:
            self._change_counter += 1

    def worker_capabilities(self) -> dict[str, Any]:
        hinted = [item.strip() for item in os.environ.get("POST_OFFICE_TOOL_HINTS", "web_search,shell,gh").split(",") if item.strip()]
        detected = [name for name in ("bash", "sh", "gh", "git", "curl", "python3", "node", "npm", "codex") if shutil.which(name)]
        shell_access = self.worker_mode in {"command", "codex"}
        gh_access = shell_access and shutil.which("gh") is not None
        web_search_access = (
            (self.worker_mode == "command" and bool(os.environ.get("POST_OFFICE_WEB_SEARCH_COMMAND", "").strip()))
            or (self.worker_mode == "codex" and os.environ.get("POST_OFFICE_CODEX_ENABLE_WEB_SEARCH", "1").strip().lower() not in {"0", "false", "no", "off"})
        )
        notes = {
            "command": "Command-mode workers can invoke host CLI tools directly through POST_OFFICE_WORKER_COMMAND.",
            "codex": "Codex-mode workers run through Codex CLI with real shell and gh access, plus native web search when POST_OFFICE_CODEX_ENABLE_WEB_SEARCH is enabled.",
            "openai": "OpenAI-mode workers only receive tool awareness and host capability hints unless the remote backend itself exposes tools.",
        }.get(self.worker_mode, "Worker capabilities depend on the configured backend.")
        return {
            "aware_tools": ["web_search", "shell", "gh"],
            "installed_cli_tools": sorted({*hinted, *detected}),
            "shell_access": shell_access,
            "gh_cli_access": gh_access,
            "web_search_access": web_search_access,
            "notes": notes,
        }

    def status_snapshot(self, thread_limit: int = 20, run_limit: int = 20) -> dict[str, Any]:
        unread = self.list_messages(unread_only=True, limit=100, mailbox_only=True)
        threads = self.list_threads(limit=thread_limit)
        runs = self.list_runs(limit=run_limit)
        return {
            "worker_mode": self.worker_mode,
            "worker_name": self.worker_name,
            "model_name": self.model_name,
            "reasoning_effort": self._resolved_reasoning_effort(),
            "db_path": str(self.db_path),
            "unread_messages": len(unread),
            "recent_threads": threads,
            "recent_runs": runs,
            "change_counter": self.change_counter,
            "worker_capabilities": self.worker_capabilities(),
            "subagent_profiles": list_subagent_profiles(),
        }

    def submit_task(
        self,
        recipient: str,
        message: str,
        subject: str = "",
        thread_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        workdir: str = "",
    ) -> dict[str, Any]:
        recipient = (recipient or self.worker_name).strip() or self.worker_name
        subject = (subject or "").strip()
        if not message.strip():
            raise ValueError("message is required")
        created_at = now_iso()
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, dict):
            raise ValueError("metadata must be a JSON object")
        metadata = dict(metadata)
        requested_workdir = (workdir or "").strip()
        if requested_workdir:
            metadata["workdir"] = requested_workdir
        with self.connect() as conn:
            if thread_id:
                existing = conn.execute("SELECT id, recipient, subject FROM threads WHERE id = ?", (thread_id,)).fetchone()
                if not existing:
                    raise ValueError(f"thread not found: {thread_id}")
                recipient = existing["recipient"]
                subject = subject or existing["subject"]
                conn.execute(
                    "UPDATE threads SET updated_at = ?, status = ?, last_error = NULL, archived_at = NULL WHERE id = ?",
                    (created_at, "queued", thread_id),
                )
            else:
                subject = subject or "Task dispatch"
                thread_id = gen_id("thread")
                conn.execute(
                    "INSERT INTO threads(id, recipient, subject, status, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?)",
                    (thread_id, recipient, subject, "queued", created_at, created_at),
                )

            run_id = gen_id("run")
            request_id = gen_id("msg")
            queue_notice_id = gen_id("msg")
            conn.execute(
                "INSERT INTO runs(id, thread_id, recipient, state, created_at) VALUES(?, ?, ?, ?, ?)",
                (run_id, thread_id, recipient, "queued", created_at),
            )
            conn.execute(
                "UPDATE threads SET last_run_id = ?, updated_at = ?, status = ? WHERE id = ?",
                (run_id, created_at, "queued", thread_id),
            )
            conn.execute(
                "INSERT INTO messages(id, thread_id, run_id, author, role, status, body, created_at, metadata_json) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    request_id,
                    thread_id,
                    run_id,
                    "agent-zero",
                    "request",
                    "queued",
                    message,
                    created_at,
                    json.dumps(metadata),
                ),
            )
            conn.execute(
                "INSERT INTO messages(id, thread_id, run_id, author, role, status, body, created_at, metadata_json) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    queue_notice_id,
                    thread_id,
                    run_id,
                    "postmaster",
                    "system",
                    "queued",
                    f"Queued for {recipient}.",
                    created_at,
                    json.dumps({"event": "queued"}),
                ),
            )
        self._wake_event.set()
        self._bump_change()
        return {
            "thread_id": thread_id,
            "run_id": run_id,
            "request_message_id": request_id,
            "postmaster_message_id": queue_notice_id,
            "status": "queued",
        }

    def list_threads(self, status: str | None = None, limit: int = 20, include_archived: bool = False) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 200))
        sql = "SELECT * FROM threads"
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if not include_archived:
            clauses.append("archived_at IS NULL")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def list_runs(self, thread_id: str | None = None, state: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        sql = "SELECT * FROM runs"
        clauses: list[str] = []
        params: list[Any] = []
        if thread_id:
            clauses.append("thread_id = ?")
            params.append(thread_id)
        if state:
            clauses.append("state = ?")
            params.append(state)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._run_row_to_dict(row) for row in rows]

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if not run:
                raise ValueError(f"run not found: {run_id}")
            thread = conn.execute("SELECT * FROM threads WHERE id = ?", (run["thread_id"],)).fetchone()
            messages = conn.execute("SELECT * FROM messages WHERE run_id = ? ORDER BY created_at ASC", (run_id,)).fetchall()
        return {
            "run": self._run_row_to_dict(run),
            "thread": dict(thread) if thread else None,
            "messages": [self._message_row_to_dict(row) for row in messages],
        }

    def list_messages(
        self,
        thread_id: str | None = None,
        unread_only: bool = False,
        limit: int = 20,
        mailbox_only: bool = False,
        author: str | None = None,
        role: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        sql = "SELECT * FROM messages"
        params: list[Any] = []
        clauses: list[str] = []
        if thread_id:
            clauses.append("thread_id = ?")
            params.append(thread_id)
        if unread_only:
            clauses.append("read_at IS NULL")
        if mailbox_only:
            clauses.append("author != 'agent-zero'")
        if author:
            clauses.append("author = ?")
            params.append(author)
        if role:
            clauses.append("role = ?")
            params.append(role)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._message_row_to_dict(row) for row in rows]

    def get_thread(self, thread_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            thread = conn.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
            if not thread:
                raise ValueError(f"thread not found: {thread_id}")
            messages = conn.execute("SELECT * FROM messages WHERE thread_id = ? ORDER BY created_at ASC", (thread_id,)).fetchall()
            runs = conn.execute("SELECT * FROM runs WHERE thread_id = ? ORDER BY created_at ASC", (thread_id,)).fetchall()
        return {
            "thread": dict(thread),
            "runs": [self._run_row_to_dict(row) for row in runs],
            "messages": [self._message_row_to_dict(row) for row in messages],
        }

    def _message_row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
        return item

    def _run_row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        raw_paths = json.loads(item.pop("artifacts_json") or "[]")
        item["artifact_paths"] = raw_paths
        item["artifacts"] = [self._artifact_entry(path) for path in raw_paths]
        return item

    def _merge_artifact_paths(self, *path_groups: list[str]) -> list[str]:
        seen: set[str] = set()
        merged: list[str] = []
        for paths in path_groups:
            for path in paths:
                if not path or path in seen:
                    continue
                seen.add(path)
                merged.append(path)
        return merged

    def _requested_workdir_from_metadata(self, metadata: dict[str, Any] | None) -> str:
        if not isinstance(metadata, dict):
            return ""
        for key in ("workdir", "cwd", "repo_path"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def _artifact_entry(self, raw_path: str) -> dict[str, Any]:
        if "://" in raw_path:
            return {
                "name": raw_path.rstrip("/").rsplit("/", 1)[-1] or raw_path,
                "path": raw_path,
                "url": raw_path,
            }
        path = Path(raw_path).expanduser()
        entry = {
            "name": path.name or raw_path,
            "path": str(path),
            "url": None,
        }
        try:
            resolved = path.resolve()
            relative = resolved.relative_to(ARTIFACTS_DIR.resolve())
        except Exception:
            return entry
        entry["url"] = "/artifacts/" + "/".join(relative.parts)
        return entry

    def mark_read(self, message_id: str) -> dict[str, Any]:
        read_at = now_iso()
        with self.connect() as conn:
            updated = conn.execute("UPDATE messages SET read_at = COALESCE(read_at, ?) WHERE id = ?", (read_at, message_id))
            if updated.rowcount == 0:
                raise ValueError(f"message not found: {message_id}")
            row = conn.execute("SELECT read_at FROM messages WHERE id = ?", (message_id,)).fetchone()
        self._bump_change()
        return {"message_id": message_id, "read_at": row["read_at"] if row else read_at}

    def archive_thread(self, thread_id: str, archived: bool = True) -> dict[str, Any]:
        changed_at = now_iso()
        archived_at = changed_at if archived else None
        with self.connect() as conn:
            exists = conn.execute("SELECT id FROM threads WHERE id = ?", (thread_id,)).fetchone()
            if not exists:
                raise ValueError(f"thread not found: {thread_id}")
            conn.execute("UPDATE threads SET archived_at = ?, updated_at = ? WHERE id = ?", (archived_at, changed_at, thread_id))
        self._bump_change()
        return {"thread_id": thread_id, "archived": archived, "archived_at": archived_at}

    def purge_thread(self, thread_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            thread = conn.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
            if not thread:
                raise ValueError(f"thread not found: {thread_id}")
            active = conn.execute(
                "SELECT id FROM runs WHERE thread_id = ? AND state IN ('queued', 'started') LIMIT 1",
                (thread_id,),
            ).fetchone()
            if active:
                raise ValueError("cannot purge thread with queued or started runs")
            run_count = conn.execute("SELECT COUNT(*) AS n FROM runs WHERE thread_id = ?", (thread_id,)).fetchone()["n"]
            message_count = conn.execute("SELECT COUNT(*) AS n FROM messages WHERE thread_id = ?", (thread_id,)).fetchone()["n"]
            conn.execute("DELETE FROM messages WHERE thread_id = ?", (thread_id,))
            conn.execute("DELETE FROM runs WHERE thread_id = ?", (thread_id,))
            conn.execute("DELETE FROM threads WHERE id = ?", (thread_id,))
        self._bump_change()
        return {"thread_id": thread_id, "purged": True, "deleted_runs": run_count, "deleted_messages": message_count}

    def mark_thread_read(self, thread_id: str, mailbox_only: bool = True) -> dict[str, Any]:
        read_at = now_iso()
        sql = "UPDATE messages SET read_at = COALESCE(read_at, ?) WHERE thread_id = ?"
        params: list[Any] = [read_at, thread_id]
        if mailbox_only:
            sql += " AND author != 'agent-zero'"
        with self.connect() as conn:
            exists = conn.execute("SELECT 1 FROM threads WHERE id = ?", (thread_id,)).fetchone()
            if not exists:
                raise ValueError(f"thread not found: {thread_id}")
            updated = conn.execute(sql, params)
        self._bump_change()
        return {"thread_id": thread_id, "read_at": read_at, "marked_count": updated.rowcount}

    def dispatch_queued(self, limit: int = 1) -> dict[str, Any]:
        processed = 0
        limit = max(1, min(limit, 100))
        while processed < limit:
            if not self.process_next_run_once():
                break
            processed += 1
        return {"processed_runs": processed}

    def fail_run(self, run_id: str, reason: str = "Manually failed by operator") -> dict[str, Any]:
        reason = (reason or "Manually failed by operator").strip() or "Manually failed by operator"
        finished_at = now_iso()
        active_kill = self._kill_active_process(run_id, reason=reason)
        with self.connect() as conn:
            run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if not run:
                raise ValueError(f"run not found: {run_id}")
            if run["state"] in {"completed", "failed", "needs_input"}:
                raise ValueError(f"run is already terminal: {run['state']}")
            conn.execute(
                "UPDATE runs SET state = ?, finished_at = COALESCE(finished_at, ?), heartbeat_at = ?, activity_text = ?, worker_pid = NULL, cancel_requested_at = ?, error_text = ? WHERE id = ?",
                (
                    "failed",
                    finished_at,
                    finished_at,
                    f"failed: {reason}",
                    finished_at if active_kill["killed"] else run["cancel_requested_at"],
                    reason,
                    run_id,
                ),
            )
            thread = conn.execute("SELECT * FROM threads WHERE id = ?", (run["thread_id"],)).fetchone()
            thread_status = "failed" if thread and thread["last_run_id"] == run_id else (thread["status"] if thread else "failed")
            last_error = reason if thread and thread["last_run_id"] == run_id else (thread["last_error"] if thread else reason)
            conn.execute(
                "UPDATE threads SET status = ?, updated_at = ?, last_error = ? WHERE id = ?",
                (thread_status, finished_at, last_error, run["thread_id"]),
            )
            conn.execute(
                "INSERT INTO messages(id, thread_id, run_id, author, role, status, body, created_at, metadata_json) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    gen_id("msg"),
                    run["thread_id"],
                    run_id,
                    "postmaster",
                    "system",
                    "failed",
                    f"Run failed: {reason}",
                    finished_at,
                    json.dumps({"event": "failed", "error": reason, "source": "manual_fail"}),
                ),
            )
        self._bump_change()
        self._wake_event.set()
        return {"run_id": run_id, "state": "failed", "reason": reason, **active_kill}

    def process_next_run_once(self) -> bool:
        with self.connect() as conn:
            run = conn.execute("SELECT * FROM runs WHERE state = 'queued' ORDER BY created_at ASC LIMIT 1").fetchone()
            if not run:
                return False
            run_id = run["id"]
            thread_id = run["thread_id"]
            started_at = now_iso()
            conn.execute(
                "UPDATE runs SET state = ?, started_at = ?, heartbeat_at = ?, activity_text = ? WHERE id = ? AND state = 'queued'",
                ("started", started_at, started_at, "worker dispatched", run_id),
            )
            conn.execute("UPDATE threads SET status = ?, updated_at = ? WHERE id = ?", ("started", started_at, thread_id))
            conn.execute(
                "INSERT INTO messages(id, thread_id, run_id, author, role, status, body, created_at, metadata_json) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    gen_id("msg"),
                    thread_id,
                    run_id,
                    "postmaster",
                    "system",
                    "started",
                    f"Dispatched to {run['recipient']}.",
                    started_at,
                    json.dumps({"event": "started"}),
                ),
            )
        self._bump_change()
        try:
            payload = self._build_worker_payload(run_id)
            result = self._dispatch_to_worker(payload)
            self._record_worker_result(run_id, result)
        except Exception as exc:
            self._record_failure(run_id, str(exc))
        return True

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            worked = False
            try:
                worked = self.process_next_run_once()
            except Exception:
                worked = False
            if worked:
                continue
            self._wake_event.wait(POLL_SECONDS)
            self._wake_event.clear()

    def _build_worker_payload(self, run_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if not run:
                raise ValueError(f"run not found: {run_id}")
            thread = conn.execute("SELECT * FROM threads WHERE id = ?", (run["thread_id"],)).fetchone()
            messages = conn.execute("SELECT * FROM messages WHERE thread_id = ? ORDER BY created_at ASC", (run["thread_id"],)).fetchall()
        history = []
        latest_request = ""
        latest_request_metadata: dict[str, Any] = {}
        for row in messages:
            item = dict(row)
            metadata = json.loads(item["metadata_json"] or "{}")
            history.append(
                {
                    "id": item["id"],
                    "author": item["author"],
                    "role": item["role"],
                    "status": item["status"],
                    "body": item["body"],
                    "created_at": item["created_at"],
                    "metadata": metadata,
                }
            )
            if item["run_id"] == run_id and item["role"] == "request" and item["author"] == "agent-zero":
                latest_request = item["body"]
                latest_request_metadata = metadata if isinstance(metadata, dict) else {}
        return {
            "run_id": run_id,
            "thread_id": run["thread_id"],
            "recipient": run["recipient"],
            "subject": thread["subject"] if thread else "Task dispatch",
            "latest_request": latest_request,
            "request_metadata": latest_request_metadata,
            "requested_workdir": self._requested_workdir_from_metadata(latest_request_metadata),
            "subagent_type": resolve_subagent_type(run["recipient"], latest_request_metadata),
            "subagent_profile": get_subagent_profile(run["recipient"], latest_request_metadata),
            "system_prompt": render_worker_system_prompt(run["recipient"], latest_request_metadata),
            "history": history,
            "capabilities": self.worker_capabilities(),
        }

    def _dispatch_to_worker(self, payload: dict[str, Any]) -> WorkerResult:
        return self._dispatch_in_subprocess(payload["run_id"], payload)

    def _dispatch_in_subprocess(self, run_id: str, payload: dict[str, Any]) -> WorkerResult:
        env = os.environ.copy()
        env["POST_OFFICE_WORKER_MODE"] = self.worker_mode
        env["POST_OFFICE_WORKER_NAME"] = self.worker_name
        env["POST_OFFICE_MODEL"] = self.model_name
        env["POST_OFFICE_API_BASE"] = self.api_base
        env["POST_OFFICE_API_KEY"] = self.api_key
        env["MALAK_API_KEY"] = self.api_key
        env["POST_OFFICE_WORKER_COMMAND"] = self.worker_command
        env["POST_OFFICE_REASONING_EFFORT"] = self.reasoning_effort
        env["POST_OFFICE_ARTIFACTS_DIR"] = str(ARTIFACTS_DIR)
        proc = subprocess.Popen(
            [sys.executable, "-m", "post_office_mcp.worker_runner"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            bufsize=1,
            start_new_session=True,
        )
        self._register_active_process(run_id, proc)
        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        stdout_lock = threading.Lock()
        stderr_lock = threading.Lock()

        def read_stdout() -> None:
            if not proc.stdout:
                return
            for chunk in iter(lambda: proc.stdout.read(4096), ""):
                if not chunk:
                    break
                with stdout_lock:
                    stdout_chunks.append(chunk)

        def read_stderr() -> None:
            if not proc.stderr:
                return
            for line in iter(proc.stderr.readline, ""):
                if not line:
                    break
                stripped = line.rstrip("\n")
                if stripped.startswith(WORKER_EVENT_PREFIX):
                    raw_event = stripped[len(WORKER_EVENT_PREFIX) :].strip()
                    try:
                        event = json.loads(raw_event)
                    except json.JSONDecodeError:
                        with stderr_lock:
                            stderr_chunks.append(line)
                        continue
                    if isinstance(event, dict):
                        self._handle_worker_event(run_id, event)
                        continue
                with stderr_lock:
                    stderr_chunks.append(line)

        stdout_thread = threading.Thread(target=read_stdout, name=f"post-office-stdout-{run_id}", daemon=True)
        stderr_thread = threading.Thread(target=read_stderr, name=f"post-office-stderr-{run_id}", daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        try:
            if proc.stdin:
                proc.stdin.write(json.dumps(payload))
                proc.stdin.close()
            proc.wait()
        finally:
            self._clear_active_process(run_id)
        stdout_thread.join(timeout=1)
        stderr_thread.join(timeout=1)
        with stdout_lock:
            stdout = "".join(stdout_chunks).strip()
        with stderr_lock:
            stderr = "".join(stderr_chunks).strip()
        if proc.returncode != 0:
            raise RuntimeError(stderr or stdout or f"worker subprocess exited with code {proc.returncode}")
        return self._parse_worker_result(stdout)

    def _handle_worker_event(self, run_id: str, event: dict[str, Any]) -> None:
        heartbeat_at = event.get("heartbeat_at")
        if not isinstance(heartbeat_at, str) or not heartbeat_at.strip():
            heartbeat_at = now_iso()
        activity_text = event.get("activity_text")
        if not isinstance(activity_text, str) or not activity_text.strip():
            activity_text = None
        workdir = event.get("workdir")
        if not isinstance(workdir, str) or not workdir.strip():
            workdir = None
        artifacts = event.get("artifacts")
        artifact_paths = [path for path in artifacts if isinstance(path, str) and path.strip()] if isinstance(artifacts, list) else []
        with self.connect() as conn:
            run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if not run or run["state"] not in {"queued", "started"}:
                return
            merged_artifacts = self._merge_artifact_paths(json.loads(run["artifacts_json"] or "[]"), artifact_paths)
            conn.execute(
                "UPDATE runs SET heartbeat_at = ?, activity_text = COALESCE(?, activity_text), workdir = COALESCE(?, workdir), artifacts_json = ? WHERE id = ?",
                (heartbeat_at, activity_text, workdir, json.dumps(merged_artifacts), run_id),
            )
            thread = conn.execute("SELECT * FROM threads WHERE id = ?", (run["thread_id"],)).fetchone()
            if thread and thread["last_run_id"] == run_id:
                conn.execute("UPDATE threads SET updated_at = ? WHERE id = ?", (heartbeat_at, run["thread_id"]))
        self._bump_change()

    def _resolved_reasoning_effort(self) -> str:
        if self.reasoning_effort:
            return self.reasoning_effort
        for suffix in (".minimal", ".low", ".medium", ".high"):
            if self.model_name.endswith(suffix):
                return suffix.rsplit(".", 1)[1]
        return ""

    def _parse_worker_result(self, raw_text: str) -> WorkerResult:
        text = raw_text.strip()
        try:
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1 and end >= start:
                return WorkerResult.model_validate_json(text[start : end + 1])
        except Exception:
            pass
        return WorkerResult(status="failed", summary="Invalid worker response", details=text or "Worker returned no JSON output.")

    def _record_worker_result(self, run_id: str, result: WorkerResult) -> None:
        finished_at = now_iso()
        status: Status = result.status
        body_parts = []
        if result.summary:
            body_parts.append(result.summary)
        if result.details:
            body_parts.append(result.details)
        if result.questions:
            body_parts.append("Questions:\n- " + "\n- ".join(result.questions))
        if result.artifacts:
            body_parts.append("Artifacts:\n- " + "\n- ".join(result.artifacts))
        body = "\n\n".join(part for part in body_parts if part).strip() or result.status
        failure_notice = (result.summary.strip() or result.details.strip() or "Worker reported failure.") if status == "failed" else ""
        with self.connect() as conn:
            run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if not run:
                raise ValueError(f"run not found: {run_id}")
            if run["state"] not in {"queued", "started"}:
                return
            conn.execute(
                "UPDATE runs SET state = ?, finished_at = ?, heartbeat_at = ?, activity_text = ?, worker_pid = NULL, artifacts_json = ?, error_text = ? WHERE id = ?",
                (
                    status,
                    finished_at,
                    finished_at,
                    f"{status}: {result.summary.strip() or status}",
                    json.dumps(self._merge_artifact_paths(json.loads(run['artifacts_json'] or '[]'), result.artifacts)),
                    failure_notice or None,
                    run_id,
                ),
            )
            conn.execute(
                "UPDATE threads SET status = ?, updated_at = ?, last_error = ? WHERE id = ?",
                (status, finished_at, failure_notice or None, run["thread_id"]),
            )
            conn.execute(
                "INSERT INTO messages(id, thread_id, run_id, author, role, status, body, created_at, metadata_json) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    gen_id("msg"),
                    run["thread_id"],
                    run_id,
                    run["recipient"] or self.worker_name,
                    "response",
                    status,
                    body,
                    finished_at,
                    result.model_dump_json(),
                ),
            )
            if status == "failed":
                conn.execute(
                    "INSERT INTO messages(id, thread_id, run_id, author, role, status, body, created_at, metadata_json) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        gen_id("msg"),
                        run["thread_id"],
                        run_id,
                        "postmaster",
                        "system",
                        "failed",
                        f"Run failed: {failure_notice}",
                        finished_at,
                        json.dumps({"event": "failed", "error": failure_notice, "source": "worker_result"}),
                    ),
                )
        self._bump_change()

    def _record_failure(self, run_id: str, error_text: str) -> None:
        finished_at = now_iso()
        with self.connect() as conn:
            run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if not run or run["state"] not in {"queued", "started"}:
                return
            conn.execute(
                "UPDATE runs SET state = ?, finished_at = ?, heartbeat_at = ?, activity_text = ?, worker_pid = NULL, error_text = ? WHERE id = ?",
                ("failed", finished_at, finished_at, f"failed: {error_text}", error_text, run_id),
            )
            conn.execute(
                "UPDATE threads SET status = ?, updated_at = ?, last_error = ? WHERE id = ?",
                ("failed", finished_at, error_text, run["thread_id"]),
            )
            conn.execute(
                "INSERT INTO messages(id, thread_id, run_id, author, role, status, body, created_at, metadata_json) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    gen_id("msg"),
                    run["thread_id"],
                    run_id,
                    "postmaster",
                    "system",
                    "failed",
                    f"Run failed: {error_text}",
                    finished_at,
                    json.dumps({"event": "failed", "error": error_text}),
                ),
            )
        self._bump_change()

    def _register_active_process(self, run_id: str, proc: subprocess.Popen[str]) -> None:
        with self._active_processes_lock:
            self._active_processes[run_id] = proc
        with self.connect() as conn:
            conn.execute("UPDATE runs SET worker_pid = ? WHERE id = ?", (proc.pid, run_id))
        self._bump_change()

    def _clear_active_process(self, run_id: str) -> None:
        with self._active_processes_lock:
            self._active_processes.pop(run_id, None)
        with self.connect() as conn:
            conn.execute("UPDATE runs SET worker_pid = NULL WHERE id = ?", (run_id,))
        self._bump_change()

    def _kill_active_process(self, run_id: str, reason: str) -> dict[str, Any]:
        with self._active_processes_lock:
            proc = self._active_processes.get(run_id)
        if not proc:
            return {"killed": False, "worker_pid": None}
        pid = proc.pid
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        return {"killed": True, "worker_pid": pid}

    def _kill_all_active_processes(self, reason: str) -> None:
        with self._active_processes_lock:
            items = list(self._active_processes.items())
        for _run_id, proc in items:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


OFFICE = PostOffice(
    db_path=DB_PATH,
    worker_mode=WORKER_MODE,
    worker_name=WORKER_NAME,
    model_name=MODEL_NAME,
    api_base=API_BASE,
    api_key=API_KEY,
    worker_command=WORKER_COMMAND,
    reasoning_effort=REASONING_EFFORT,
)

mcp = FastMCP(
    name="Post Office Subagent",
    instructions=(
        "Leave work in the post office, then check the mailbox for replies. "
        "The postmaster routes queued runs to a single autonomous worker and always leaves mail when a run starts, completes, needs input, or fails."
    ),
)


@mcp.tool()
def submit_task(
    message: str,
    recipient: str = WORKER_NAME,
    subject: str = "",
    thread_id: str | None = None,
    metadata_json: str = "{}",
    workdir: str = "",
) -> dict[str, Any]:
    """Leave a message in the post office for a subagent task."""
    metadata = json.loads(metadata_json or "{}")
    return OFFICE.submit_task(
        recipient=recipient,
        message=message,
        subject=subject,
        thread_id=thread_id,
        metadata=metadata,
        workdir=workdir,
    )


@mcp.tool()
def list_threads(status: str = "", limit: int = 20, include_archived: bool = False) -> list[dict[str, Any]]:
    """List conversation threads in the post office."""
    return OFFICE.list_threads(status=status or None, limit=limit, include_archived=include_archived)


@mcp.tool()
def list_runs(thread_id: str = "", state: str = "", limit: int = 20) -> list[dict[str, Any]]:
    """List worker runs globally or for one thread."""
    return OFFICE.list_runs(thread_id=thread_id or None, state=state or None, limit=limit)


@mcp.tool()
def get_run(run_id: str) -> dict[str, Any]:
    """Read one run with its run-scoped messages."""
    return OFFICE.get_run(run_id)


@mcp.tool()
def list_messages(
    thread_id: str = "",
    unread_only: bool = False,
    limit: int = 20,
    mailbox_only: bool = False,
    author: str = "",
    role: str = "",
    status: str = "",
) -> list[dict[str, Any]]:
    """List mailbox messages globally or for one thread."""
    return OFFICE.list_messages(
        thread_id=thread_id or None,
        unread_only=unread_only,
        limit=limit,
        mailbox_only=mailbox_only,
        author=author or None,
        role=role or None,
        status=status or None,
    )


@mcp.tool()
def get_thread(thread_id: str) -> dict[str, Any]:
    """Read one full thread with messages and runs."""
    return OFFICE.get_thread(thread_id)


@mcp.tool()
def mark_message_read(message_id: str) -> dict[str, Any]:
    """Mark a mailbox message as read."""
    return OFFICE.mark_read(message_id)


@mcp.tool()
def mark_thread_read(thread_id: str, mailbox_only: bool = True) -> dict[str, Any]:
    """Mark all messages in a thread as read, optionally skipping Agent Zero request mail."""
    return OFFICE.mark_thread_read(thread_id=thread_id, mailbox_only=mailbox_only)


@mcp.tool()
def archive_thread(thread_id: str, archived: bool = True) -> dict[str, Any]:
    """Archive or unarchive a thread."""
    return OFFICE.archive_thread(thread_id=thread_id, archived=archived)


@mcp.tool()
def purge_thread(thread_id: str) -> dict[str, Any]:
    """Permanently delete a thread and all related runs/messages."""
    return OFFICE.purge_thread(thread_id=thread_id)


@mcp.tool()
def dispatch_queued(limit: int = 1) -> dict[str, Any]:
    """Process queued runs immediately. Mostly useful for manual testing and debugging."""
    return OFFICE.dispatch_queued(limit=limit)


@mcp.tool()
def fail_run(run_id: str, reason: str = "Manually failed by operator") -> dict[str, Any]:
    """Manually fail a queued or started run so the queue can move again."""
    return OFFICE.fail_run(run_id=run_id, reason=reason)


@mcp.resource("postoffice://status")
def status_resource() -> str:
    return json.dumps(OFFICE.status_snapshot(), indent=2)


@mcp.resource("postoffice://thread/{thread_id}")
def thread_resource(thread_id: str) -> str:
    return json.dumps(OFFICE.get_thread(thread_id), indent=2)


@mcp.resource("postoffice://run/{run_id}")
def run_resource(run_id: str) -> str:
    return json.dumps(OFFICE.get_run(run_id), indent=2)


@mcp.resource("postoffice://worker-capabilities")
def worker_capabilities_resource() -> str:
    return json.dumps(OFFICE.worker_capabilities(), indent=2)


@mcp.resource("postoffice://subagent-profiles")
def subagent_profiles_resource() -> str:
    return json.dumps(list_subagent_profiles(), indent=2)


def main() -> None:
    OFFICE.start()
    try:
        transport = os.environ.get("POST_OFFICE_TRANSPORT", "stdio").strip().lower() or "stdio"
        if transport in {"http", "streamable-http", "streamable_http"}:
            host = os.environ.get("POST_OFFICE_HOST", "127.0.0.1")
            port = int(os.environ.get("POST_OFFICE_PORT", "8123"))
            mcp.run(transport="streamable-http", host=host, port=port)
        elif transport == "sse":
            host = os.environ.get("POST_OFFICE_HOST", "127.0.0.1")
            port = int(os.environ.get("POST_OFFICE_PORT", "8123"))
            mcp.run(transport="sse", host=host, port=port)
        else:
            mcp.run(transport="stdio")
    finally:
        OFFICE.stop()


if __name__ == "__main__":
    main()
