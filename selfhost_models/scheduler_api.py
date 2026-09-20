"""Authenticated durable resources, independent of model readiness."""
import asyncio
import base64
import json
from urllib.parse import parse_qs

from pydantic import ValidationError

from .scheduler_schema import SchedulerError, SubmitJob, Upload


async def body_json(gateway, receive, headers, limit):
    if headers.get(b"content-type", b"").split(b";", 1)[0] != b"application/json":
        raise SchedulerError("json_required", 415)
    body = bytearray()
    try:
        async with asyncio.timeout(10):
            while True:
                event = await receive()
                if event["type"] == "http.disconnect":
                    raise SchedulerError("client_disconnected", 408)
                chunk = event.get("body", b"")
                if len(body) + len(chunk) > limit:
                    raise SchedulerError("body_too_large", 413)
                if gateway.receiving_bytes + len(chunk) > gateway.receive_budget:
                    raise SchedulerError("receive_budget_exceeded", 429)
                body.extend(chunk)
                gateway.receiving_bytes += len(chunk)
                if not event.get("more_body"):
                    break
        return json.loads(body)
    finally:
        gateway.receiving_bytes -= len(body)


async def route(gateway, scope, receive, send, headers, rid):
    path, method = scope["path"], scope["method"]
    if path not in ("/v1/catalog", "/v1/jobs", "/v1/artifacts", "/v1/scheduler") and not path.startswith(("/v1/jobs/", "/v1/artifacts/")):
        return False
    store = gateway.scheduler
    status = 200
    try:
        if path == "/v1/catalog" and method == "GET":
            result = {"object": "list", "data": store.catalog()}
        elif path == "/v1/scheduler" and method == "GET":
            state = store.state()
            result = {k: state[k] for k in ("phase", "deployment", "worker_epoch", "heartbeat")}
            result["inflight"] = store.lease_count()
        elif path == "/v1/jobs" and method == "POST":
            spec = SubmitJob.model_validate(await body_json(gateway, receive, headers, store.config.input_bytes))
            result, created = store.submit(spec, headers.get(b"idempotency-key", b"").decode("ascii"))
            status = 201 if created else 200
        elif path == "/v1/jobs" and method == "GET":
            qs = parse_qs(scope.get("query_string", b"").decode(), strict_parsing=True)
            if set(qs) - {"after", "limit"} or any(len(v) != 1 for v in qs.values()):
                raise SchedulerError("invalid_pagination")
            result = {"data": store.jobs(int(qs.get("after", [0])[0]), int(qs.get("limit", [100])[0]))}
        elif path == "/v1/artifacts" and method == "POST":
            upload = Upload.model_validate(await body_json(gateway, receive, headers, 1500000))
            data = base64.b64decode(upload.data_base64, validate=True)
            result = store.upload(data, upload.media_type, upload.description)
            status = 201
        elif path.startswith("/v1/artifacts/") and method == "GET":
            data, media = store.artifact(path.removeprefix("/v1/artifacts/"))
            result = {"media_type": media, "data_base64": base64.b64encode(data).decode()}
        elif path.startswith("/v1/jobs/"):
            parts = path.split("/")
            jid = parts[3]
            if len(parts) == 4 and method == "GET":
                result = store.job(jid)
            elif len(parts) == 5 and parts[4] == "result" and method == "GET":
                result = store.result(jid)
            elif len(parts) == 5 and parts[4] == "cancel" and method == "POST":
                if await body_json(gateway, receive, headers, 1024) != {}:
                    raise SchedulerError("invalid_request")
                result = store.cancel(jid)
            else:
                raise SchedulerError("not_found", 404)
        else:
            raise SchedulerError("not_found", 404)
        await gateway.json_response(send, status, result, rid)
    except (ValidationError, ValueError, UnicodeError, RecursionError):
        raise SchedulerError("invalid_request") from None
    return True
