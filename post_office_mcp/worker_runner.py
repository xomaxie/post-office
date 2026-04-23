from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openai import OpenAI

from post_office_mcp.server import render_worker_system_prompt


WORKER_EVENT_PREFIX = "POST_OFFICE_EVENT "
HEARTBEAT_SECONDS = max(float(os.environ.get("POST_OFFICE_HEARTBEAT_SECONDS", "2.0")), 0.5)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def codex_bin() -> str:
    return os.environ.get("POST_OFFICE_CODEX_BIN", "codex").strip() or "codex"


def codex_default_workdir() -> str:
    configured = os.environ.get("POST_OFFICE_CODEX_CWD", "").strip()
    if configured:
        return configured
    home = os.environ.get("POST_OFFICE_HOME", "").strip()
    if home:
        return home
    return os.getcwd()


def requested_workdir(payload: dict[str, Any] | None = None) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("requested_workdir", "workdir"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    metadata = payload.get("request_metadata")
    if isinstance(metadata, dict):
        for key in ("workdir", "cwd", "repo_path"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def codex_workdir(payload: dict[str, Any] | None = None) -> str:
    base = Path(codex_default_workdir()).expanduser()
    requested = requested_workdir(payload)
    candidate = Path(requested).expanduser() if requested else base
    if requested and not candidate.is_absolute():
        candidate = base / candidate
    candidate = candidate.resolve()
    if not candidate.exists():
        raise ValueError(f"Codex workdir does not exist: {candidate}")
    if not candidate.is_dir():
        raise ValueError(f"Codex workdir is not a directory: {candidate}")
    return str(candidate)


def codex_web_search_enabled() -> bool:
    return env_flag("POST_OFFICE_CODEX_ENABLE_WEB_SEARCH", default=True)


def codex_extra_args() -> list[str]:
    raw = os.environ.get("POST_OFFICE_CODEX_ARGS", "").strip()
    return shlex.split(raw) if raw else []


def artifacts_root() -> Path:
    configured = os.environ.get("POST_OFFICE_ARTIFACTS_DIR", "").strip()
    if configured:
        return Path(configured)
    home = os.environ.get("POST_OFFICE_HOME", "").strip()
    if home:
        return Path(home) / "artifacts"
    return Path.cwd() / "artifacts"


def codex_output_schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "status": {"type": "string", "enum": ["completed", "needs_input", "failed"]},
            "summary": {"type": "string"},
            "details": {"type": "string"},
            "questions": {"type": "array", "items": {"type": "string"}},
            "artifacts": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["status", "summary", "details", "questions", "artifacts"],
    }


def build_codex_command(schema_path: str, output_path: str, workdir: str) -> list[str]:
    model_name = os.environ.get("POST_OFFICE_MODEL", "gpt-5.5").strip() or "gpt-5.5"
    reasoning_effort = os.environ.get("POST_OFFICE_REASONING_EFFORT", "").strip().lower()

    cmd = [codex_bin()]
    if codex_web_search_enabled():
        cmd.append("--search")
    cmd.append("exec")
    cmd.extend(["--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox"])
    cmd.extend(["-C", workdir])
    cmd.extend(["-m", resolved_model_name(model_name)])
    if reasoning_effort:
        cmd.extend(["-c", f'model_reasoning_effort="{reasoning_effort}"'])
    cmd.extend(codex_extra_args())
    cmd.extend(["--output-schema", schema_path, "-o", output_path])
    return cmd


def run_command(payload: dict[str, Any], worker_command: str) -> str:
    if not worker_command:
        raise ValueError("POST_OFFICE_WORKER_COMMAND is required when POST_OFFICE_WORKER_MODE=command")
    proc = subprocess.run(
        shlex.split(worker_command),
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
    )
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0:
        raise RuntimeError(stderr or stdout or f"worker command exited with code {proc.returncode}")
    return stdout


def run_codex(payload: dict[str, Any]) -> str:
    prompt = render_codex_prompt(payload)
    run_dir = artifacts_root() / payload["thread_id"] / payload["run_id"]
    run_dir.mkdir(parents=True, exist_ok=True)

    prompt_path = run_dir / "prompt.txt"
    schema_path = run_dir / "worker-result.schema.json"
    output_path = run_dir / "last-message.json"
    stdout_path = run_dir / "codex.stdout.txt"
    stderr_path = run_dir / "codex.stderr.txt"
    transcript_path = run_dir / "transcript.md"

    prompt_path.write_text(prompt, encoding="utf-8")
    schema_path.write_text(json.dumps(codex_output_schema(), indent=2), encoding="utf-8")
    output_path.write_text("", encoding="utf-8")
    stdout_path.write_bytes(b"")
    stderr_path.write_bytes(b"")

    try:
        workdir = codex_workdir(payload)
    except Exception as exc:
        workdir = requested_workdir(payload) or codex_default_workdir()
        result = {
            "status": "failed",
            "summary": "Invalid Codex workdir",
            "details": str(exc),
            "questions": [],
            "artifacts": [str(transcript_path), str(output_path), str(prompt_path), str(schema_path), str(stdout_path), str(stderr_path)],
        }
        output_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        write_transcript(
            transcript_path=transcript_path,
            payload=payload,
            workdir=workdir,
            command=[codex_bin()],
            prompt_path=prompt_path,
            schema_path=schema_path,
            output_path=output_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            returncode=None,
        )
        return json.dumps(result)

    artifact_paths = [str(transcript_path), str(output_path), str(prompt_path), str(schema_path), str(stdout_path), str(stderr_path)]
    progress = ProgressTracker(workdir=workdir, artifacts=artifact_paths)
    progress.set_activity("prepared prompt and artifacts")
    write_transcript(
        transcript_path=transcript_path,
        payload=payload,
        workdir=workdir,
        command=[codex_bin()],
        prompt_path=prompt_path,
        schema_path=schema_path,
        output_path=output_path,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        returncode=None,
    )
    emit_worker_event(progress.snapshot(include_artifacts=True))

    if shutil.which(codex_bin()) is None:
        result = {
            "status": "failed",
            "summary": "Codex CLI not found",
            "details": f"Codex CLI not found: {codex_bin()}",
            "questions": [],
            "artifacts": artifact_paths,
        }
        output_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        write_transcript(
            transcript_path=transcript_path,
            payload=payload,
            workdir=workdir,
            command=[codex_bin()],
            prompt_path=prompt_path,
            schema_path=schema_path,
            output_path=output_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            returncode=None,
        )
        return json.dumps(result)

    cmd = build_codex_command(str(schema_path), str(output_path), workdir)
    progress.set_activity("launching codex")
    emit_worker_event(progress.snapshot())
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        bufsize=0,
    )
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    stdout_thread = threading.Thread(
        target=pump_stream,
        args=(proc.stdout, stdout_path, stdout_chunks),
        kwargs={"channel": "stdout", "progress": progress},
        name=f"codex-stdout-{payload['run_id']}",
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=pump_stream,
        args=(proc.stderr, stderr_path, stderr_chunks),
        kwargs={"channel": "stderr", "progress": progress},
        name=f"codex-stderr-{payload['run_id']}",
        daemon=True,
    )
    heartbeat_stop = threading.Event()

    def heartbeat_loop() -> None:
        while not heartbeat_stop.wait(HEARTBEAT_SECONDS):
            emit_worker_event(progress.snapshot())

    heartbeat_thread = threading.Thread(
        target=heartbeat_loop,
        name=f"codex-heartbeat-{payload['run_id']}",
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    heartbeat_thread.start()
    try:
        if proc.stdin is not None:
            proc.stdin.write(prompt.encode("utf-8"))
            proc.stdin.close()
        proc.wait()
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=1)
    stdout_thread.join(timeout=1)
    stderr_thread.join(timeout=1)
    stdout = b"".join(stdout_chunks).decode("utf-8", errors="replace").strip()
    stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace").strip()
    progress.set_activity("parsing codex result")
    emit_worker_event(progress.snapshot())
    file_output = output_path.read_text(encoding="utf-8").strip() if output_path.exists() else ""

    try:
        result = json.loads(file_output or stdout)
    except Exception:
        result = {
            "status": "failed",
            "summary": "Invalid Codex response" if proc.returncode == 0 else "Codex execution failed",
            "details": stderr or stdout or file_output or f"codex exited with code {proc.returncode}",
            "questions": [],
            "artifacts": [],
        }

    result["artifacts"] = dedupe_artifacts([*artifact_paths, *result.get("artifacts", [])])
    output_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    write_transcript(
        transcript_path=transcript_path,
        payload=payload,
        workdir=workdir,
        command=cmd,
        prompt_path=prompt_path,
        schema_path=schema_path,
        output_path=output_path,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        returncode=proc.returncode,
    )
    return json.dumps(result)


def dedupe_artifacts(paths: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for path in paths:
        if not path or path in seen:
            continue
        seen.add(path)
        ordered.append(path)
    return ordered


def emit_worker_event(event: dict[str, Any]) -> None:
    sys.stderr.write(WORKER_EVENT_PREFIX + json.dumps(event, separators=(",", ":")) + "\n")
    sys.stderr.flush()


class ProgressTracker:
    def __init__(self, *, workdir: str, artifacts: list[str]) -> None:
        self.workdir = workdir
        self.artifacts = list(artifacts)
        self.activity_text = "preparing codex run"
        self.stdout_bytes = 0
        self.stderr_bytes = 0
        self._lock = threading.Lock()

    def set_activity(self, activity_text: str) -> None:
        with self._lock:
            self.activity_text = activity_text

    def note_output(self, channel: str, byte_count: int) -> None:
        with self._lock:
            if channel == "stdout":
                self.stdout_bytes += byte_count
            else:
                self.stderr_bytes += byte_count
            self.activity_text = (
                f"capturing codex {channel} "
                f"(stdout={self.stdout_bytes}B stderr={self.stderr_bytes}B)"
            )

    def snapshot(self, *, include_artifacts: bool = False) -> dict[str, Any]:
        with self._lock:
            payload: dict[str, Any] = {
                "heartbeat_at": now_iso(),
                "activity_text": self.activity_text,
                "workdir": self.workdir,
            }
            if include_artifacts:
                payload["artifacts"] = list(self.artifacts)
            return payload


def pump_stream(
    stream,
    target_path: Path,
    collector: list[bytes],
    *,
    channel: str,
    progress: ProgressTracker,
) -> None:
    with target_path.open("ab") as sink:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                break
            collector.append(chunk)
            sink.write(chunk)
            sink.flush()
            progress.note_output(channel, len(chunk))


def write_transcript(
    *,
    transcript_path: Path,
    payload: dict[str, Any],
    workdir: str,
    command: list[str],
    prompt_path: Path,
    schema_path: Path,
    output_path: Path,
    stdout_path: Path | None,
    stderr_path: Path | None,
    returncode: int | None,
) -> None:
    lines = [
        f"# Codex run {payload['run_id']}",
        "",
        f"- Thread: `{payload['thread_id']}`",
        f"- Recipient: `{payload['recipient']}`",
        f"- Subject: `{payload['subject']}`",
        f"- Workdir: `{workdir}`",
        f"- Model: `{resolved_model_name(os.environ.get('POST_OFFICE_MODEL', 'gpt-5.5'))}`",
        f"- Reasoning: `{os.environ.get('POST_OFFICE_REASONING_EFFORT', '').strip().lower() or 'default'}`",
        f"- Web search: `{'enabled' if codex_web_search_enabled() else 'disabled'}`",
        f"- Return code: `{returncode if returncode is not None else 'not started'}`",
        "",
        "## Command",
        "",
        "```bash",
        shlex.join(command),
        "```",
        "",
        "## Files",
        "",
        f"- Prompt: `{prompt_path}`",
        f"- Schema: `{schema_path}`",
        f"- Output: `{output_path}`",
    ]
    if stdout_path is not None:
        lines.append(f"- Stdout: `{stdout_path}`")
    if stderr_path is not None:
        lines.append(f"- Stderr: `{stderr_path}`")
    transcript_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_openai(payload: dict[str, Any]) -> str:
    api_key = os.environ.get("POST_OFFICE_API_KEY", "").strip() or os.environ.get("MALAK_API_KEY", "").strip()
    api_base = os.environ.get("POST_OFFICE_API_BASE", "").strip()
    model_name = os.environ.get("POST_OFFICE_MODEL", "openai.gpt-5.5.high").strip() or "openai.gpt-5.5.high"
    reasoning_effort = os.environ.get("POST_OFFICE_REASONING_EFFORT", "").strip().lower()
    if not api_key:
        raise ValueError("POST_OFFICE_API_KEY or MALAK_API_KEY is required when POST_OFFICE_WORKER_MODE=openai")
    client_kwargs: dict[str, Any] = {"api_key": api_key, "timeout": None}
    if api_base:
        client_kwargs["base_url"] = api_base
    client = OpenAI(**client_kwargs)
    prompt = render_prompt(payload)
    request_kwargs: dict[str, Any] = {
        "model": resolved_model_name(model_name),
        "input": [
            {
                "role": "system",
                "content": payload.get("system_prompt")
                or render_worker_system_prompt(payload.get("recipient", ""), payload.get("request_metadata")),
            },
            {"role": "user", "content": prompt},
        ],
    }
    effort = resolved_reasoning_effort(model_name, reasoning_effort)
    if effort:
        request_kwargs["reasoning"] = {"effort": effort}
    response = client.responses.create(**request_kwargs)
    text = getattr(response, "output_text", "") or ""
    if not text:
        text = json.dumps(response.model_dump(), indent=2)
    return text


def resolved_reasoning_effort(model_name: str, explicit: str) -> str:
    if explicit:
        return explicit
    for suffix in (".minimal", ".low", ".medium", ".high"):
        if model_name.endswith(suffix):
            return suffix.rsplit(".", 1)[1]
    return ""


def resolved_model_name(model_name: str) -> str:
    for prefix in ("openai.", "responses."):
        if model_name.startswith(prefix):
            model_name = model_name[len(prefix) :]
            break
    for suffix in (".minimal", ".low", ".medium", ".high"):
        if model_name.endswith(suffix):
            model_name = model_name[: -len(suffix)]
            break
    return model_name or "gpt-5.5"


def render_prompt(payload: dict[str, Any]) -> str:
    history_lines = []
    for item in payload["history"]:
        history_lines.append(
            f"[{item['created_at']}] {item['author']} ({item['role']}/{item['status']}):\n{item['body']}"
        )
    capabilities = payload.get("capabilities") or {}
    requested = requested_workdir(payload) or "default"
    subagent_type = payload.get("subagent_type") or "default"
    subagent_profile = payload.get("subagent_profile") or {}
    subagent_label = subagent_profile.get("label") or subagent_type
    subagent_description = subagent_profile.get("description") or ""
    return (
        f"Recipient: {payload['recipient']}\n"
        f"Subagent type: {subagent_type}\n"
        f"Subagent label: {subagent_label}\n"
        f"Subagent description: {subagent_description}\n"
        f"Thread: {payload['thread_id']}\n"
        f"Run: {payload['run_id']}\n"
        f"Subject: {payload['subject']}\n"
        f"Requested workdir: {requested}\n\n"
        "Tool awareness:\n"
        f"- Aware of tools: {', '.join(capabilities.get('aware_tools', [])) or 'none'}\n"
        f"- Host CLI access: {'yes' if capabilities.get('shell_access') else 'no'}\n"
        f"- gh CLI access: {'yes' if capabilities.get('gh_cli_access') else 'no'}\n"
        f"- Web search access: {'yes' if capabilities.get('web_search_access') else 'no'}\n"
        f"- Installed CLI tools seen on host: {', '.join(capabilities.get('installed_cli_tools', [])) or 'none'}\n"
        f"- Notes: {capabilities.get('notes', '')}\n\n"
        f"Latest request:\n{payload['latest_request']}\n\n"
        f"Thread history:\n" + "\n\n".join(history_lines)
    )


def render_codex_prompt(payload: dict[str, Any]) -> str:
    return (
        f"{payload.get('system_prompt') or render_worker_system_prompt(payload.get('recipient', ''), payload.get('request_metadata'))}\n\n"
        "You are running inside Codex CLI. Prefer using real shell and gh commands when useful. "
        "If web search is available, use it when the task depends on fresh external information.\n\n"
        "Return JSON matching the provided output schema exactly.\n\n"
        f"{render_prompt(payload)}"
    )


def main() -> int:
    payload = json.load(sys.stdin)
    mode = os.environ.get("POST_OFFICE_WORKER_MODE", "openai").strip().lower() or "openai"
    if mode == "command":
        output = run_command(payload, os.environ.get("POST_OFFICE_WORKER_COMMAND", "").strip())
    elif mode == "openai":
        output = run_openai(payload)
    elif mode == "codex":
        output = run_codex(payload)
    else:
        raise ValueError(f"unsupported POST_OFFICE_WORKER_MODE: {mode}")
    sys.stdout.write(output)
    if not output.endswith("\n"):
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
