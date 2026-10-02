"""Isolated loopback ASGI application, separate from the inference gateway."""
from __future__ import annotations

import asyncio
import base64
import hmac
import json
from pathlib import Path, PurePosixPath
import re
import secrets
import sqlite3
from urllib.parse import parse_qs, quote

from .batch import (BatchError, BatchStore, FILE_BYTES, FILE_COUNT, PARTITIONS, RATES,
                    TOTAL_BYTES, decode_file, make_zip, read_archive, safe_path, templates)
from .batch_projects import ProjectStore, exclusion

MAX_BODY = 23 * 1024 * 1024
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8")}
ID_RE = r"(?:draft|batch)_[a-f0-9]{32}"


def exact_body(value, keys, optional=()):
    if not isinstance(value, dict) or set(value) - set(keys) - set(optional) or set(keys) - set(value):
        raise BatchError("invalid_body", "要求格式不正確，缺少或多出欄位。", 400)
    return value


def _json_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON key")
        value[key] = item
    return value


class BatchApp:
    def __init__(self, state: Path, port: int = 18771):
        self.store = ProjectStore(state)
        self.port = port
        self.csrf_token = secrets.token_urlsafe(32)
        self.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        try:
            headers = {k.decode("latin1").lower(): v.decode("latin1") for k, v in scope.get("headers", [])}
            host = headers.get("host", "")
            if host not in self.allowed_hosts:
                raise BatchError("host_denied", "僅允許本機 loopback 位址。", 403)
            if headers.get("sec-fetch-site") == "cross-site":
                raise BatchError("origin_denied", "拒絕其他網站對本機工具的要求。", 403)
            origin = headers.get("origin")
            if origin is not None and origin != "http://" + host:
                raise BatchError("origin_denied", "拒絕其他網站對本機工具的要求。", 403)
            method, path = scope["method"], scope["path"]
            body = None
            if method in ("POST", "PUT", "DELETE"):
                if not hmac.compare_digest(headers.get("x-batch-csrf", ""), self.csrf_token):
                    raise BatchError("csrf_denied", "頁面已過期，請重新整理後再操作。", 403)
                if headers.get("content-type", "").split(";")[0].strip() != "application/json":
                    raise BatchError("content_type", "請使用 JSON 格式。", 415)
                try:
                    if int(headers.get("content-length", "0")) > MAX_BODY:
                        raise BatchError("body_limit", "上傳要求超過本機 23 MiB 上限。", 413)
                except ValueError:
                    raise BatchError("invalid_length", "要求大小無效。", 400) from None
                raw = bytearray()
                async with asyncio.timeout(15):
                    while True:
                        event = await receive()
                        if event["type"] == "http.disconnect":
                            return
                        raw.extend(event.get("body", b""))
                        if len(raw) > MAX_BODY:
                            raise BatchError("body_limit", "上傳要求超過本機 23 MiB 上限。", 413)
                        if not event.get("more_body", False):
                            break
                try:
                    body = json.loads(raw, object_pairs_hook=_json_pairs, parse_constant=lambda v: (_ for _ in ()).throw(ValueError(v)))
                except (ValueError, UnicodeError, RecursionError):
                    raise BatchError("invalid_json", "JSON 無效，請重新整理後再試。", 400) from None
            elif method != "GET":
                raise BatchError("method", "不支援此操作。", 405)
            status, content, media, filename = self.route(method, path, body, scope.get("query_string", b""))
        except BatchError as exc:
            status, content, media, filename = exc.status, {"error": {"code": exc.code, "message": exc.message, "fields": exc.fields or {}}}, "application/json; charset=utf-8", None
        except TimeoutError:
            status, content, media, filename = 408, {"error": {"code": "body_timeout", "message": "上傳逾時，請縮小檔案後重試。"}}, "application/json; charset=utf-8", None
        except (OSError, sqlite3.Error):
            status, content, media, filename = 503, {"error": {"code": "storage_error", "message": "本機儲存失敗；請保留 state 並檢查可用空間，勿重新提交未知工作。"}}, "application/json; charset=utf-8", None
        await self.respond(send, status, content, media, filename)

    async def respond(self, send, status, content, media, filename):
        payload = json.dumps(content, ensure_ascii=False, allow_nan=False).encode() if isinstance(content, (dict, list)) else content
        headers = [(b"content-type", media.encode()), (b"content-length", str(len(payload)).encode()),
                   (b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"),
                   (b"referrer-policy", b"no-referrer"), (b"x-frame-options", b"DENY"),
                   (b"content-security-policy", b"default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; form-action 'self'; base-uri 'none'; frame-ancestors 'none'; object-src 'none'")]
        if filename:
            # RFC 5987 Unicode filename, with a safe ASCII compatibility fallback.
            basename = PurePosixPath(filename).name
            fallback = re.sub(r"[^A-Za-z0-9._-]", "_", basename) or "result-file"
            disposition = f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{quote(basename, safe="")}'
            headers.append((b"content-disposition", disposition.encode("ascii")))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": payload})

    def route(self, method, path, body, query):
        media = "application/json; charset=utf-8"
        if method == "GET" and path in STATIC:
            name, content_type = STATIC[path]
            return 200, (Path(__file__).parent / "batch_static" / name).read_bytes(), content_type, None
        if method == "GET" and path == "/api/bootstrap":
            return 200, {"csrf_token": self.csrf_token, "live_enabled": False, "schema_version": 2, "templates": templates(), "partitions": PARTITIONS,
                         "rates": RATES, "source_date": "2026-10-02", "limits": {"file_bytes": FILE_BYTES, "total_bytes": TOTAL_BYTES, "file_count": FILE_COUNT}}, media, None
        if path in ("/api/environments", "/api/locations"):
            table = path.removeprefix("/api/")
            if method == "GET":
                return 200, {table: self.store.named(table)}, media, None
            if method == "POST":
                if table == "environments":
                    exact_body(body, ("name", "sif_path"))
                    content = self.store.register_environment(**body)
                else:
                    exact_body(body, ("name", "kind", "path"))
                    content = self.store.register_location(**body)
                return 201, content, media, None
        if path == "/api/projects":
            if method == "GET":
                return 200, {"projects": self.store.projects()}, media, None
            if method == "POST":
                exact_body(body, ("name",))
                return 201, self.store.create_project(body["name"]), media, None
        project_match = re.fullmatch(r"/api/projects/(project_[a-f0-9]{32})(/files|/example)?", path)
        if project_match:
            ident, action = project_match.groups()
            if not action and method == "GET":
                content = self.store.project(ident)
            elif not action and method == "PUT":
                exact_body(body, ("revision", "config"))
                content = self.store.update_project(ident, **body)
            elif action == "/files" and method == "POST":
                exact_body(body, ("revision", "files"))
                if not isinstance(body["files"], list) or not 1 <= len(body["files"]) <= 2000:
                    raise BatchError("files", "最多選取 2000 個項目，匯入最多 100 個檔案。")
                files = {}
                for item in body["files"]:
                    exact_body(item, ("path", "data_base64"))
                    reason = exclusion(item["path"])
                    if item["path"] in files:
                        raise BatchError("path_conflict", "匯入清單包含重複路徑。")
                    files[item["path"]] = b"" if reason else decode_file(item["data_base64"])
                content = self.store.import_project(ident, body["revision"], files)
            elif action == "/example" and method == "POST":
                exact_body(body, ("revision",))
                content = self.store.project_example(ident, body["revision"])
            else:
                raise BatchError("method", "不支援這個專案操作。", 405)
            return 200, content, media, None
        if path == "/api/work-drafts":
            if method == "GET":
                return 200, {"drafts": self.store.work_drafts()}, media, None
            if method == "POST":
                exact_body(body, ("project_id",))
                return 201, self.store.create_work_draft(body["project_id"]), media, None
        work_match = re.fullmatch(r"/api/work-drafts/(draft_[a-f0-9]{32})(/prepare)?", path)
        if work_match:
            ident, action = work_match.groups()
            if not action and method == "GET":
                content = self.store.work_draft(ident)
            elif not action and method == "PUT":
                exact_body(body, ("revision", "spec", "location_ids"))
                content = self.store.update_work_draft(ident, **body)
            elif action == "/prepare" and method == "POST":
                exact_body(body, ("revision",))
                self.store.work_draft(ident)
                content = self.store.prepare(ident, body["revision"])
            else:
                raise BatchError("method", "不支援這個工作草稿操作。", 405)
            return 200, content, media, None
        if path == "/api/drafts":
            if method == "GET":
                with self.store.connection() as db:
                    project_ids = {r[0] for r in db.execute("SELECT draft_id FROM work_drafts")}
                drafts = [d | {"origin": "project" if d["id"] in project_ids else "legacy"} for d in self.store.drafts()]
                return 200, {"drafts": drafts}, media, None
            if method == "POST":
                exact_body(body, ("template",))
                return 201, self.store.create(body["template"]), media, None
        if method == "GET" and path == "/api/jobs":
            return 200, {"jobs": self.store.jobs()}, media, None
        match = re.fullmatch(r"/api/(drafts|jobs)/(" + ID_RE + r")(.*)", path)
        if not match:
            raise BatchError("not_found", "找不到此操作。", 404)
        group, ident, action = match.groups()
        if group == "drafts" and ident.startswith("draft_"):
            if method != "GET":
                with self.store.connection() as db:
                    if db.execute("SELECT 1 FROM work_drafts WHERE draft_id=?", (ident,)).fetchone():
                        raise BatchError("project_draft", "專案工作請使用新版工作草稿操作。", 409)
            if action == "" and method == "GET":
                content = self.store.draft(ident)
            elif action == "" and method == "PUT":
                exact_body(body, ("revision", "spec"))
                content = self.store.update(ident, body["revision"], body["spec"])
            elif action == "/example" and method == "POST":
                exact_body(body, ())
                content = self.store.example(ident)
            elif action == "/files" and method == "POST":
                exact_body(body, ("files",))
                if not isinstance(body["files"], list) or not 1 <= len(body["files"]) <= FILE_COUNT:
                    raise BatchError("input_limit", "請選擇 1 至 100 個檔案。")
                files = {}
                for item in body["files"]:
                    exact_body(item, ("path", "data_base64"))
                    safe_path(item["path"])
                    if item["path"] in files:
                        raise BatchError("path_conflict", "上傳清單含重複路徑。")
                    files[item["path"]] = decode_file(item["data_base64"])
                content = self.store.upload(ident, files)
            elif action == "/inputs/archive" and method == "POST":
                exact_body(body, ("prefix", "data_base64"))
                if body["prefix"] not in ("code", "data"):
                    raise BatchError("prefix", "ZIP 請匯入 code 或 data 目錄。")
                files = read_archive(decode_file(body["data_base64"]))
                content = self.store.upload(ident, {body["prefix"] + "/" + p: v for p, v in files.items()})
            elif action == "/files" and method == "DELETE":
                exact_body(body, ("path", "revision", "sha256"))
                content = self.store.remove_file(ident, body["path"], body["revision"], body["sha256"])
            elif action == "/prepare" and method == "POST":
                exact_body(body, ("revision",))
                content = self.store.prepare(ident, body["revision"])
            else:
                raise BatchError("not_found", "找不到草稿操作。", 404)
        elif group == "jobs" and ident.startswith("batch_"):
            if action == "" and method == "GET":
                content = self.store.job(ident)
            elif action == "/clone" and method == "POST":
                exact_body(body, ())
                content = self.store.clone_job(ident)
            elif action == "/package" and method == "GET":
                return 200, self.store.package(ident), "application/zip", ident + "-package.zip"
            elif action == "/submit" and method == "POST":
                exact_body(body, ())
                content = self.store.submit(ident)
            elif action == "/demo" and method == "POST":
                exact_body(body, ("scenario",))
                content = self.store.demo(ident, body["scenario"])
            elif action == "/demo-confirm" and method == "POST":
                exact_body(body, ("outcome",))
                content = self.store.demo_confirm(ident, body["outcome"])
            elif action == "/cancel" and method == "POST":
                exact_body(body, ())
                content = self.store.cancel(ident)
            elif action == "/results/collect" and method == "POST":
                exact_body(body, ())
                content = self.store.collect(ident)
            elif action == "/results/import" and method == "POST":
                exact_body(body, ("data_base64",))
                content = self.store.import_results(ident, decode_file(body["data_base64"]))
            elif action == "/results/download" and method == "GET":
                return 200, make_zip(self.store.result_files(ident)), "application/zip", ident + "-results.zip"
            elif action in ("/results/file", "/results/preview") and method == "GET":
                try:
                    args = parse_qs(query.decode("utf-8"), strict_parsing=True)
                except (ValueError, UnicodeError):
                    raise BatchError("invalid_query", "檔案路徑參數無效。", 400) from None
                if set(args) != {"path"} or len(args["path"]) != 1:
                    raise BatchError("invalid_query", "請指定單一成果檔案。", 400)
                name = args["path"][0]
                data = self.store.result_file(ident, name)
                if action == "/results/file":
                    return 200, data, "application/octet-stream", PurePosixPath(name).name
                try:
                    text = data[:65536].decode("utf-8")
                except UnicodeError:
                    raise BatchError("binary_preview", "此檔案為二進位或非 UTF-8，請下載檢視。", 415) from None
                content = {"path": name, "text": text, "truncated": len(data) > 65536}
            else:
                raise BatchError("not_found", "找不到工作操作。", 404)
        else:
            raise BatchError("not_found", "識別碼格式無效。", 404)
        return 200, content, media, None
