from __future__ import annotations

import asyncio
import hmac
import logging
import re
import time

from aiohttp import web
from aiogram import Bot

from .settings import Settings
from .store import RelayStore

log = logging.getLogger(__name__)
MAX_BODY_BYTES = 256 * 1024
ALLOWED_ERRORS = {
    "agent_unavailable",
    "opencode_api_unsupported",
    "processing_interrupted",
    "opencode_timeout",
    "opencode_error",
}
OPERATION_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def bearer_authorized(request: web.Request, settings: Settings) -> bool:
    header = request.headers.get("Authorization", "")
    prefix = "Bearer "
    if not header.startswith(prefix):
        return False
    return hmac.compare_digest(header[len(prefix):], settings.relay_token)


@web.middleware
async def auth_middleware(request: web.Request, handler):
    if request.path.startswith("/internal/") and not bearer_authorized(request, request.app["settings"]):
        raise web.HTTPUnauthorized(text="Unauthorized")
    return await handler(request)


async def read_json(request: web.Request) -> dict:
    if request.content_length and request.content_length > MAX_BODY_BYTES:
        raise web.HTTPRequestEntityTooLarge(max_size=MAX_BODY_BYTES, actual_size=request.content_length)
    try:
        payload = await request.json()
    except Exception as exc:
        raise web.HTTPBadRequest(text="Expected JSON body") from exc
    if not isinstance(payload, dict):
        raise web.HTTPBadRequest(text="Expected a JSON object")
    return payload


async def health(request: web.Request) -> web.Response:
    store: RelayStore = request.app["store"]
    stats = store.queue_stats()
    return web.json_response({"status": "ok", "queue": stats})


async def worker_heartbeat(request: web.Request) -> web.Response:
    payload = await read_json(request)
    worker_id = payload.get("worker_id")
    version = payload.get("opencode_version")
    healthy = payload.get("healthy") is True
    if not isinstance(worker_id, str) or not worker_id or len(worker_id) > 128:
        raise web.HTTPBadRequest(text="Invalid worker_id")
    if version is not None and (not isinstance(version, str) or len(version) > 64):
        raise web.HTTPBadRequest(text="Invalid opencode_version")
    store: RelayStore = request.app["store"]
    store.worker_heartbeat(worker_id, healthy, version)
    return web.json_response({"accepted": True})


async def claim_job(request: web.Request) -> web.Response:
    payload = await read_json(request)
    worker_id = payload.get("worker_id")
    wait_seconds = payload.get("wait_seconds", 15)
    if not isinstance(worker_id, str) or not worker_id or len(worker_id) > 128:
        raise web.HTTPBadRequest(text="Invalid worker_id")
    if not isinstance(wait_seconds, int) or not 0 <= wait_seconds <= 25:
        raise web.HTTPBadRequest(text="wait_seconds must be between 0 and 25")

    store: RelayStore = request.app["store"]
    settings: Settings = request.app["settings"]
    deadline = time.monotonic() + wait_seconds
    while True:
        job = store.claim_job(worker_id, settings.job_lease_seconds, settings.pending_job_ttl_seconds)
        if job:
            return web.json_response({
                "id": job["id"],
                "prompt": job["prompt_text"],
                "telegram_user_id": job["user_id"],
                "telegram_chat_id": job["chat_id"],
                "created_at": job["created_at"],
            })
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return web.Response(status=204)
        await asyncio.sleep(min(1.0, remaining))


async def heartbeat_job(request: web.Request) -> web.Response:
    payload = await read_json(request)
    worker_id = payload.get("worker_id")
    if not isinstance(worker_id, str) or not worker_id:
        raise web.HTTPBadRequest(text="Invalid worker_id")
    store: RelayStore = request.app["store"]
    settings: Settings = request.app["settings"]
    accepted = store.heartbeat_job(request.match_info["job_id"], worker_id, settings.job_lease_seconds)
    if not accepted:
        raise web.HTTPConflict(text="Job lease is no longer valid")
    return web.json_response({"accepted": True})


async def complete_job(request: web.Request) -> web.Response:
    payload = await read_json(request)
    worker_id, result = payload.get("worker_id"), payload.get("result")
    if not isinstance(worker_id, str) or not worker_id:
        raise web.HTTPBadRequest(text="Invalid worker_id")
    if not isinstance(result, str) or not result.strip() or len(result) > 200_000:
        raise web.HTTPBadRequest(text="Invalid result")
    store: RelayStore = request.app["store"]
    if not store.complete_job(request.match_info["job_id"], worker_id, result.strip()):
        raise web.HTTPConflict(text="Job lease is no longer valid")
    return web.json_response({"accepted": True})


async def fail_job(request: web.Request) -> web.Response:
    payload = await read_json(request)
    worker_id, error_code = payload.get("worker_id"), payload.get("error_code")
    if not isinstance(worker_id, str) or not worker_id:
        raise web.HTTPBadRequest(text="Invalid worker_id")
    if error_code not in ALLOWED_ERRORS:
        raise web.HTTPBadRequest(text="Unsupported error_code")
    store: RelayStore = request.app["store"]
    if not store.fail_job(request.match_info["job_id"], worker_id, error_code):
        raise web.HTTPConflict(text="Job lease is no longer valid")
    return web.json_response({"accepted": True})


async def enqueue_notification(request: web.Request) -> web.Response:
    payload = await read_json(request)
    operation_id, chat_id, text = payload.get("operation_id"), payload.get("chat_id"), payload.get("text")
    settings: Settings = request.app["settings"]
    if not isinstance(operation_id, str) or not OPERATION_ID.fullmatch(operation_id):
        raise web.HTTPBadRequest(text="Invalid operation_id")
    if not isinstance(chat_id, int) or chat_id != settings.authorized_user_id:
        raise web.HTTPForbidden(text="Chat is not authorized")
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        raise web.HTTPBadRequest(text="Invalid notification text")
    store: RelayStore = request.app["store"]
    try:
        item, created = store.enqueue_notification(operation_id, chat_id, text.strip())
    except ValueError as exc:
        raise web.HTTPConflict(text=str(exc)) from exc
    return web.json_response({"accepted": True, "created": created, "status": item["status"]}, status=202)


async def notification_status(request: web.Request) -> web.Response:
    store: RelayStore = request.app["store"]
    item = store.notification_status(request.match_info["operation_id"])
    if item is None:
        raise web.HTTPNotFound(text="Notification not found")
    return web.json_response(item)


def create_app(settings: Settings, store: RelayStore, bot: Bot) -> web.Application:
    app = web.Application(middlewares=[auth_middleware], client_max_size=MAX_BODY_BYTES)
    app["settings"] = settings
    app["store"] = store
    app["bot"] = bot
    app.router.add_get("/healthz", health)
    app.router.add_post("/internal/v1/agent/heartbeat", worker_heartbeat)
    app.router.add_post("/internal/v1/jobs/claim", claim_job)
    app.router.add_post("/internal/v1/jobs/{job_id}/heartbeat", heartbeat_job)
    app.router.add_post("/internal/v1/jobs/{job_id}/complete", complete_job)
    app.router.add_post("/internal/v1/jobs/{job_id}/fail", fail_job)
    app.router.add_post("/internal/v1/notifications", enqueue_notification)
    app.router.add_get("/internal/v1/notifications/{operation_id}", notification_status)
    return app
