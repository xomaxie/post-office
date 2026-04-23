from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from post_office_mcp.server import ARTIFACTS_DIR, BASE_DIR, OFFICE, now_iso

UI_DIST = BASE_DIR / "ui" / "dist"
INDEX_FILE = UI_DIST / "index.html"


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def error_response(message: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


async def read_json_body(request: Request, *, required: bool = False) -> dict[str, Any]:
    body = await request.body()
    if not body or not body.strip():
        if required:
            raise ValueError("request body must be a JSON object")
        return {}
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError("request body must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    return payload


@asynccontextmanager
async def lifespan(_app):
    OFFICE.start()
    try:
        yield
    finally:
        OFFICE.stop()


async def api_status(_: Request) -> JSONResponse:
    return JSONResponse(OFFICE.status_snapshot())


async def api_threads(request: Request) -> JSONResponse:
    status = request.query_params.get("status") or None
    limit = int(request.query_params.get("limit", "24"))
    include_archived = parse_bool(request.query_params.get("include_archived"))
    return JSONResponse(OFFICE.list_threads(status=status, limit=limit, include_archived=include_archived))


async def api_thread_detail(request: Request) -> JSONResponse:
    thread_id = request.path_params["thread_id"]
    try:
        return JSONResponse(OFFICE.get_thread(thread_id))
    except ValueError as exc:
        return error_response(str(exc), status_code=404)


async def api_run_detail(request: Request) -> JSONResponse:
    run_id = request.path_params["run_id"]
    try:
        return JSONResponse(OFFICE.get_run(run_id))
    except ValueError as exc:
        return error_response(str(exc), status_code=404)


async def api_messages(request: Request) -> JSONResponse:
    params = request.query_params
    return JSONResponse(
        OFFICE.list_messages(
            thread_id=params.get("thread_id") or None,
            unread_only=parse_bool(params.get("unread_only")),
            limit=int(params.get("limit", "50")),
            mailbox_only=parse_bool(params.get("mailbox_only")),
            author=params.get("author") or None,
            role=params.get("role") or None,
            status=params.get("status") or None,
        )
    )


async def api_submit_task(request: Request) -> JSONResponse:
    try:
        payload = await read_json_body(request, required=True)
        result = OFFICE.submit_task(
            recipient=(payload.get("recipient") or OFFICE.worker_name),
            message=payload.get("message", ""),
            subject=payload.get("subject", ""),
            thread_id=payload.get("thread_id") or None,
            metadata=payload.get("metadata") or {},
            workdir=payload.get("workdir", ""),
        )
        return JSONResponse(result)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)


async def api_dispatch(request: Request) -> JSONResponse:
    try:
        payload = await read_json_body(request)
        limit = int(payload.get("limit", 1))
        return JSONResponse(OFFICE.dispatch_queued(limit=limit))
    except ValueError as exc:
        return error_response(str(exc), status_code=400)


async def api_fail_run(request: Request) -> JSONResponse:
    run_id = request.path_params["run_id"]
    try:
        payload = await read_json_body(request)
        return JSONResponse(OFFICE.fail_run(run_id=run_id, reason=(payload.get("reason") or "Stopped from UI")))
    except ValueError as exc:
        return error_response(str(exc), status_code=400)




async def api_archive_thread(request: Request) -> JSONResponse:
    thread_id = request.path_params["thread_id"]
    try:
        payload = await read_json_body(request)
        return JSONResponse(OFFICE.archive_thread(thread_id=thread_id, archived=bool(payload.get("archived", True))))
    except ValueError as exc:
        status_code = 404 if "thread not found" in str(exc) else 400
        return error_response(str(exc), status_code=status_code)


async def api_purge_thread(request: Request) -> JSONResponse:
    thread_id = request.path_params["thread_id"]
    try:
        return JSONResponse(OFFICE.purge_thread(thread_id=thread_id))
    except ValueError as exc:
        return error_response(str(exc), status_code=400)

async def api_mark_thread_read(request: Request) -> JSONResponse:
    thread_id = request.path_params["thread_id"]
    try:
        payload = await read_json_body(request)
        return JSONResponse(
            OFFICE.mark_thread_read(thread_id=thread_id, mailbox_only=bool(payload.get("mailbox_only", True)))
        )
    except ValueError as exc:
        status_code = 404 if "thread not found" in str(exc) else 400
        return error_response(str(exc), status_code=status_code)


def sse_frame(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode("utf-8")


async def api_events(request: Request) -> StreamingResponse:
    async def event_stream():
        last_seen = -1
        yield sse_frame(
            "ready",
            {
                "seq": OFFICE.change_counter,
                "timestamp": now_iso(),
            },
        )
        while True:
            if await request.is_disconnected():
                break
            current = OFFICE.change_counter
            if current != last_seen:
                last_seen = current
                yield sse_frame("change", {"seq": current, "timestamp": now_iso()})
            else:
                yield b": keep-alive\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def spa_index(_: Request):
    if not INDEX_FILE.exists():
        return error_response(
            f"UI build not found at {INDEX_FILE}. Run npm install && npm run build in {BASE_DIR / 'ui'}.",
            status_code=503,
        )
    return FileResponse(INDEX_FILE)


routes = [
    Route("/api/status", api_status),
    Route("/api/events", api_events),
    Route("/api/threads", api_threads),
    Route("/api/threads/{thread_id:str}", api_thread_detail),
    Route("/api/threads/{thread_id:str}/archive", api_archive_thread, methods=["POST"]),
    Route("/api/threads/{thread_id:str}/purge", api_purge_thread, methods=["POST"]),
    Route("/api/threads/{thread_id:str}/mark-read", api_mark_thread_read, methods=["POST"]),
    Route("/api/runs/{run_id:str}", api_run_detail),
    Route("/api/runs/{run_id:str}/fail", api_fail_run, methods=["POST"]),
    Route("/api/messages", api_messages),
    Route("/api/submit-task", api_submit_task, methods=["POST"]),
    Route("/api/dispatch", api_dispatch, methods=["POST"]),
]

if UI_DIST.exists():
    routes.append(Mount("/assets", app=StaticFiles(directory=UI_DIST / "assets"), name="assets"))
if ARTIFACTS_DIR.exists():
    routes.append(Mount("/artifacts", app=StaticFiles(directory=ARTIFACTS_DIR), name="artifacts"))

routes.append(Route("/{path:path}", spa_index))

app = Starlette(debug=False, routes=routes, lifespan=lifespan)
