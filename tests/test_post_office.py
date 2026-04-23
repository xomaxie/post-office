import json
import time
from pathlib import Path

from starlette.testclient import TestClient

from post_office_mcp import ui_server
from post_office_mcp.server import PostOffice


FAKE_WORKER = Path(__file__).with_name("fake_worker.py")


def write_fake_codex(path: Path, *, sleep_seconds: float = 0.0) -> None:
    path.write_text(
        f"""#!/usr/bin/env python3
import json
import sys
import time
from pathlib import Path

args = sys.argv[1:]
out_path = Path(args[args.index('-o') + 1])
prompt = sys.stdin.read()
sys.stdout.write('booting\\n')
sys.stdout.flush()
sys.stderr.write('warming up\\n')
sys.stderr.flush()
time.sleep({sleep_seconds})
result = {{
    'status': 'completed',
    'summary': 'codex ok',
    'details': json.dumps({{'argv': args, 'prompt': prompt}}),
    'questions': [],
    'artifacts': [],
}}
out_path.write_text(json.dumps(result), encoding='utf-8')
sys.stdout.write(json.dumps(result))
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def make_office(tmp_path: Path) -> PostOffice:
    return PostOffice(
        db_path=tmp_path / "post_office.db",
        worker_mode="command",
        worker_name="agent-fast",
        model_name="openai.gpt-5.5.high",
        worker_command=f"python3 {FAKE_WORKER}",
    )


def test_codex_defaults_to_gpt_55(tmp_path: Path, monkeypatch):
    from post_office_mcp import worker_runner

    monkeypatch.delenv("POST_OFFICE_MODEL", raising=False)
    monkeypatch.setenv("POST_OFFICE_CODEX_BIN", "codex")
    monkeypatch.setenv("POST_OFFICE_CODEX_ENABLE_WEB_SEARCH", "0")

    cmd = worker_runner.build_codex_command("schema.json", "output.json", str(tmp_path))

    assert cmd[cmd.index("-m") + 1] == "gpt-5.5"
    assert worker_runner.resolved_model_name("") == "gpt-5.5"


def test_completed_run_creates_worker_message(tmp_path: Path):
    office = make_office(tmp_path)
    submitted = office.submit_task(recipient="agent-fast", message="Do the thing", subject="test")
    assert office.process_next_run_once() is True

    thread = office.get_thread(submitted["thread_id"])
    assert thread["thread"]["status"] == "completed"
    assert thread["runs"][-1]["state"] == "completed"
    assert any(m["author"] == "agent-fast" and m["status"] == "completed" for m in thread["messages"])


def test_needs_input_run_marks_thread(tmp_path: Path):
    office = make_office(tmp_path)
    submitted = office.submit_task(recipient="agent-fast", message="Need input before proceeding", subject="test")
    office.process_next_run_once()

    thread = office.get_thread(submitted["thread_id"])
    assert thread["thread"]["status"] == "needs_input"
    worker_messages = [m for m in thread["messages"] if m["author"] == "agent-fast"]
    assert worker_messages[-1]["status"] == "needs_input"
    assert "What value should I use?" in worker_messages[-1]["body"]


def test_failure_result_leaves_mail(tmp_path: Path):
    office = make_office(tmp_path)
    submitted = office.submit_task(recipient="agent-fast", message="Fail this run", subject="test")
    office.process_next_run_once()

    thread = office.get_thread(submitted["thread_id"])
    assert thread["thread"]["status"] == "failed"
    assert any(m["author"] == "agent-fast" and m["status"] == "failed" for m in thread["messages"])
    assert any(m["author"] == "postmaster" and m["status"] == "failed" for m in thread["messages"])


def test_invalid_worker_output_becomes_failed_run(tmp_path: Path):
    office = PostOffice(
        db_path=tmp_path / "post_office.db",
        worker_mode="command",
        worker_name="agent-fast",
        model_name="openai.gpt-5.5.high",
        worker_command="python3 -c 'print(\"not json\")'",
    )
    submitted = office.submit_task(recipient="agent-fast", message="Do the thing", subject="test")
    office.process_next_run_once()

    thread = office.get_thread(submitted["thread_id"])
    assert thread["thread"]["status"] == "failed"
    assert thread["thread"]["last_error"] == "Invalid worker response"
    assert any("not json" in m["body"] for m in thread["messages"] if m["author"] == "agent-fast")


def test_followup_keeps_subject_and_can_mark_mailbox_read(tmp_path: Path):
    office = make_office(tmp_path)
    first = office.submit_task(recipient="agent-fast", message="Need input before proceeding", subject="Original subject")
    office.process_next_run_once()

    second = office.submit_task(recipient="agent-fast", message="Use value 42", thread_id=first["thread_id"])
    office.process_next_run_once()

    thread = office.get_thread(first["thread_id"])
    assert thread["thread"]["subject"] == "Original subject"
    assert second["thread_id"] == first["thread_id"]

    unread_mail = office.list_messages(thread_id=first["thread_id"], unread_only=True, mailbox_only=True)
    assert unread_mail
    assert all(message["author"] != "agent-zero" for message in unread_mail)

    marked = office.mark_thread_read(first["thread_id"], mailbox_only=True)
    assert marked["marked_count"] >= 1
    unread_after = office.list_messages(thread_id=first["thread_id"], unread_only=True, mailbox_only=True)
    assert unread_after == []


def test_list_runs_and_get_run(tmp_path: Path):
    office = make_office(tmp_path)
    submitted = office.submit_task(recipient="agent-fast", message="Do the thing", subject="test")
    office.process_next_run_once()

    runs = office.list_runs(thread_id=submitted["thread_id"])
    assert len(runs) == 1
    run_detail = office.get_run(runs[0]["id"])
    assert run_detail["run"]["id"] == submitted["run_id"]
    assert len(run_detail["messages"]) >= 3
    assert any(artifact["name"] == Path(artifact["path"]).name for artifact in run_detail["run"]["artifacts"])


def test_mark_read_endpoint_allows_empty_post_body(tmp_path: Path):
    office = make_office(tmp_path)
    submitted = office.submit_task(recipient="agent-fast", message="Do the thing", subject="test")
    office.process_next_run_once()

    original_office = ui_server.OFFICE
    ui_server.OFFICE = office
    try:
        with TestClient(ui_server.app) as client:
            response = client.post(f"/api/threads/{submitted['thread_id']}/mark-read")
            assert response.status_code == 200
            payload = response.json()
            assert payload["thread_id"] == submitted["thread_id"]
            assert payload["marked_count"] >= 1
    finally:
        ui_server.OFFICE = original_office

def test_manual_fail_unblocks_queue(tmp_path: Path):
    office = make_office(tmp_path)
    first = office.submit_task(recipient="agent-fast", message="Do the thing", subject="first")
    second = office.submit_task(recipient="agent-fast", message="Do the thing", subject="second")

    with office.connect() as conn:
        conn.execute("UPDATE runs SET state = 'started', started_at = ? WHERE id = ?", ("2026-04-15T00:00:00+00:00", first["run_id"]))
        conn.execute("UPDATE threads SET status = 'started', updated_at = ? WHERE id = ?", ("2026-04-15T00:00:00+00:00", first["thread_id"]))

    failed = office.fail_run(first["run_id"], reason="stuck worker")
    assert failed["state"] == "failed"

    processed = office.dispatch_queued(limit=1)
    assert processed["processed_runs"] == 1

    first_thread = office.get_thread(first["thread_id"])
    second_thread = office.get_thread(second["thread_id"])
    assert first_thread["thread"]["status"] == "failed"
    assert any(m["author"] == "postmaster" and m["status"] == "failed" for m in first_thread["messages"])
    assert second_thread["thread"]["status"] == "completed"


def test_late_worker_result_is_ignored_after_manual_fail(tmp_path: Path):
    office = make_office(tmp_path)
    submitted = office.submit_task(recipient="agent-fast", message="Do the thing", subject="late result")
    with office.connect() as conn:
        conn.execute("UPDATE runs SET state = 'started', started_at = ? WHERE id = ?", ("2026-04-15T00:00:00+00:00", submitted["run_id"]))
        conn.execute("UPDATE threads SET status = 'started', updated_at = ? WHERE id = ?", ("2026-04-15T00:00:00+00:00", submitted["thread_id"]))

    office.fail_run(submitted["run_id"], reason="operator canceled")
    from post_office_mcp.server import WorkerResult
    office._record_worker_result(submitted["run_id"], WorkerResult(status="completed", summary="late ok", details="should be ignored"))

    thread = office.get_thread(submitted["thread_id"])
    assert thread["thread"]["status"] == "failed"
    assert thread["thread"]["last_error"] == "operator canceled"
    assert not any(m["author"] == "agent-fast" and "should be ignored" in m["body"] for m in thread["messages"])


def test_fail_run_kills_active_worker_and_unblocks_queue(tmp_path: Path):
    office = make_office(tmp_path)
    office.start()
    try:
        first = office.submit_task(recipient="agent-fast", message="Sleep for a while", subject="slow")
        second = office.submit_task(recipient="agent-fast", message="Do the thing", subject="fast")

        deadline = time.time() + 5
        active_run = None
        while time.time() < deadline:
            active_run = office.get_run(first["run_id"])["run"]
            if active_run["state"] == "started" and active_run["worker_pid"]:
                break
            time.sleep(0.05)
        assert active_run is not None
        assert active_run["state"] == "started"
        assert active_run["worker_pid"]

        failed = office.fail_run(first["run_id"], reason="operator canceled")
        assert failed["killed"] is True
        assert failed["worker_pid"] == active_run["worker_pid"]

        deadline = time.time() + 5
        second_run = None
        while time.time() < deadline:
            second_run = office.get_run(second["run_id"])["run"]
            if second_run["state"] == "completed":
                break
            time.sleep(0.05)
        assert second_run is not None
        assert second_run["state"] == "completed"

        first_thread = office.get_thread(first["thread_id"])
        assert first_thread["thread"]["status"] == "failed"
        assert first_thread["thread"]["last_error"] == "operator canceled"
    finally:
        office.stop()


def test_archive_and_purge_thread(tmp_path: Path):
    office = make_office(tmp_path)
    submitted = office.submit_task(recipient="agent-fast", message="Do the thing", subject="archive me")
    office.process_next_run_once()

    archived = office.archive_thread(submitted["thread_id"], archived=True)
    assert archived["archived"] is True
    assert office.list_threads() == []
    assert office.list_threads(include_archived=True)[0]["id"] == submitted["thread_id"]

    purged = office.purge_thread(submitted["thread_id"])
    assert purged["purged"] is True
    assert office.list_threads(include_archived=True) == []


def test_worker_capabilities_report_shell_and_gh_awareness(tmp_path: Path):
    office = make_office(tmp_path)
    caps = office.worker_capabilities()
    assert caps["aware_tools"] == ["web_search", "shell", "gh"]
    assert caps["shell_access"] is True
    assert "gh" in caps["installed_cli_tools"]


def test_codex_worker_mode_dispatches_via_codex_cli(tmp_path: Path, monkeypatch):
    fake_codex = tmp_path / "fake_codex.py"
    write_fake_codex(fake_codex)

    monkeypatch.setenv('POST_OFFICE_CODEX_BIN', str(fake_codex))
    monkeypatch.setenv('POST_OFFICE_CODEX_ENABLE_WEB_SEARCH', '1')
    monkeypatch.setenv('POST_OFFICE_CODEX_CWD', str(tmp_path))
    office = PostOffice(
        db_path=tmp_path / 'post_office.db',
        worker_mode='codex',
        worker_name='agent-fast',
        model_name='gpt-5.5',
        reasoning_effort='high',
    )

    submitted = office.submit_task(recipient='agent-fast', message='Use tools if useful', subject='codex test')
    office.process_next_run_once()

    thread = office.get_thread(submitted['thread_id'])
    assert thread['thread']['status'] == 'completed'
    worker_messages = [m for m in thread['messages'] if m['author'] == 'agent-fast']
    assert worker_messages[-1]['status'] == 'completed'
    body = worker_messages[-1]['body']
    assert 'codex ok' in body
    run = thread['runs'][-1]
    artifact_names = {artifact['name'] for artifact in run['artifacts']}
    assert 'transcript.md' in artifact_names
    assert 'last-message.json' in artifact_names
    assert 'codex.stdout.txt' in artifact_names
    assert 'codex.stderr.txt' in artifact_names
    assert run['heartbeat_at'] is not None
    assert run['activity_text'].startswith('completed:')
    assert run['workdir'] == str(tmp_path.resolve())
    transcript = next(artifact for artifact in run['artifacts'] if artifact['name'] == 'transcript.md')
    assert transcript['url']
    transcript_path = Path(transcript['path'])
    assert transcript_path.exists()
    transcript_text = transcript_path.read_text(encoding='utf-8')
    assert '--search' in transcript_text
    assert 'model_reasoning_effort' in transcript_text
    assert 'high' in transcript_text


def test_writer_recipient_uses_writer_profile_prompt_and_author(tmp_path: Path, monkeypatch):
    fake_codex = tmp_path / 'fake_codex.py'
    write_fake_codex(fake_codex)

    monkeypatch.setenv('POST_OFFICE_CODEX_BIN', str(fake_codex))
    monkeypatch.setenv('POST_OFFICE_CODEX_CWD', str(tmp_path))
    office = PostOffice(
        db_path=tmp_path / 'post_office.db',
        worker_mode='codex',
        worker_name='agent-fast',
        model_name='gpt-5.5',
        reasoning_effort='high',
    )

    submitted = office.submit_task(recipient='writer', message='Draft a technical blog post', subject='writer test')
    office.process_next_run_once()

    thread = office.get_thread(submitted['thread_id'])
    worker_messages = [m for m in thread['messages'] if m['author'] == 'writer']
    assert worker_messages
    assert worker_messages[-1]['status'] == 'completed'

    run = thread['runs'][-1]
    last_message = next(artifact for artifact in run['artifacts'] if artifact['name'] == 'last-message.json')
    result_payload = json.loads(Path(last_message['path']).read_text(encoding='utf-8'))
    details = json.loads(result_payload['details'])
    prompt = details['prompt']
    assert 'Subagent type: writer' in prompt
    assert 'Ideas first' in prompt
    assert 'Outline next' in prompt
    assert 'Details last' in prompt
    assert 'write titles last' in prompt.lower()


def test_codex_run_reports_live_artifacts_and_heartbeat(tmp_path: Path, monkeypatch):
    fake_codex = tmp_path / 'fake_codex.py'
    write_fake_codex(fake_codex, sleep_seconds=1.5)

    monkeypatch.setenv('POST_OFFICE_CODEX_BIN', str(fake_codex))
    monkeypatch.setenv('POST_OFFICE_CODEX_CWD', str(tmp_path))
    office = PostOffice(
        db_path=tmp_path / 'post_office.db',
        worker_mode='codex',
        worker_name='agent-fast',
        model_name='gpt-5.5',
        reasoning_effort='high',
    )
    office.start()
    try:
        submitted = office.submit_task(recipient='agent-fast', message='Run a longer codex task', subject='codex live')

        deadline = time.time() + 5
        live_run = None
        while time.time() < deadline:
            live_run = office.get_run(submitted['run_id'])['run']
            artifact_names = {artifact['name'] for artifact in live_run['artifacts']}
            if (
                live_run['state'] == 'started'
                and live_run['heartbeat_at'] is not None
                and live_run['activity_text']
                and live_run['workdir'] == str(tmp_path.resolve())
                and {'transcript.md', 'codex.stdout.txt', 'codex.stderr.txt'}.issubset(artifact_names)
            ):
                break
            time.sleep(0.05)

        assert live_run is not None
        assert live_run['state'] == 'started'
        assert live_run['heartbeat_at'] is not None
        assert live_run['activity_text']
        assert live_run['workdir'] == str(tmp_path.resolve())

        deadline = time.time() + 5
        finished_run = None
        while time.time() < deadline:
            finished_run = office.get_run(submitted['run_id'])['run']
            if finished_run['state'] == 'completed':
                break
            time.sleep(0.05)

        assert finished_run is not None
        assert finished_run['state'] == 'completed'
    finally:
        office.stop()


def test_codex_submit_task_workdir_override_wins_per_run(tmp_path: Path, monkeypatch):
    fake_codex = tmp_path / 'fake_codex.py'
    write_fake_codex(fake_codex)
    default_dir = tmp_path / 'default'
    override_dir = tmp_path / 'override'
    default_dir.mkdir()
    override_dir.mkdir()

    monkeypatch.setenv('POST_OFFICE_CODEX_BIN', str(fake_codex))
    monkeypatch.setenv('POST_OFFICE_CODEX_CWD', str(default_dir))
    office = PostOffice(
        db_path=tmp_path / 'post_office.db',
        worker_mode='codex',
        worker_name='agent-fast',
        model_name='gpt-5.5',
        reasoning_effort='high',
    )

    submitted = office.submit_task(
        recipient='agent-fast',
        message='Use the override repo',
        subject='codex override',
        workdir=str(override_dir),
    )
    office.process_next_run_once()

    run = office.get_run(submitted['run_id'])['run']
    assert run['workdir'] == str(override_dir.resolve())
    last_message = next(artifact for artifact in run['artifacts'] if artifact['name'] == 'last-message.json')
    result_payload = json.loads(Path(last_message['path']).read_text(encoding='utf-8'))
    details = json.loads(result_payload['details'])
    argv = details['argv']
    assert argv[argv.index('-C') + 1] == str(override_dir.resolve())


def test_worker_capabilities_report_codex_shell_gh_and_search(tmp_path: Path, monkeypatch):
    monkeypatch.setenv('POST_OFFICE_CODEX_ENABLE_WEB_SEARCH', '1')
    office = PostOffice(
        db_path=tmp_path / 'post_office.db',
        worker_mode='codex',
        worker_name='agent-fast',
        model_name='gpt-5.5',
    )
    caps = office.worker_capabilities()
    assert caps['aware_tools'] == ['web_search', 'shell', 'gh']
    assert caps['shell_access'] is True
    assert caps['gh_cli_access'] is True
    assert caps['web_search_access'] is True
    assert 'codex' in caps['installed_cli_tools']


def test_status_snapshot_lists_subagent_profiles(tmp_path: Path):
    office = make_office(tmp_path)
    snapshot = office.status_snapshot()
    profile_ids = {item['id'] for item in snapshot['subagent_profiles']}
    assert 'default' in profile_ids
    assert 'writer' in profile_ids
