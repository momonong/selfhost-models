"""Behavior and safety contracts for the isolated offline Nano5 tool."""
import asyncio
import base64
import io
import json
from pathlib import Path
import socket
import stat
import subprocess
import sys
import zipfile

import httpx
import pytest

from selfhost_models.batch import (BatchError, BatchStore, FILE_BYTES, Spec, decode_file,
                                   make_zip, parse_spec, read_archive, safe_path)
from selfhost_models.batch_app import BatchApp, MAX_BODY


def ready(store, template="python"):
    draft = store.create(template)
    draft = store.example(draft["id"])
    return draft, store.prepare(draft["id"], draft["revision"])


def error(code, fn):
    with pytest.raises(BatchError) as exc:
        fn()
    assert exc.value.code == code


def test_preparation_immutable_package_and_restart(tmp_path):
    store = BatchStore(tmp_path / "state")
    for template in ("python", "training"):
        draft, job = ready(store, template)
        assert job["work_state"] == "prepared" and job["attempt_count"] == 0
        assert store.prepare(draft["id"], draft["revision"])["id"] == job["id"]
        assert store.update(draft["id"], draft["revision"], draft["spec"])["revision"] == draft["revision"]
        package = store.package(job["id"])
        with zipfile.ZipFile(io.BytesIO(package)) as z:
            assert "job.slurm" in z.namelist()
            assert z.read("job.slurm").decode() == job["script"]
            assert z.read("inputs/" + draft["spec"]["entrypoint"]) != b""
            assert json.loads(z.read("manifest.json"))["mode"] == "prepared"
        reopened = BatchStore(tmp_path / "state")
        assert reopened.job(job["id"]) == job
        assert reopened.package(job["id"]) == package
        assert reopened.draft(draft["id"]) == draft
        store.upload(draft["id"], {"data/new.csv": b"new"})
        assert store.package(job["id"]) == package


@pytest.mark.parametrize("field,value", [
    ("minutes", 121), ("gpus", 9), ("gpus", 0), ("gpus", True), ("cpus", 17),
    ("memory_gib", 129), ("account", "X\n#SBATCH --nodes=4"),
    ("email", "a@b.co\n#SBATCH --gres=gpu:8"), ("sif_path", "docker://image"),
    ("sif_path", "/tmp/../image.sif"), ("max_cost_twd", float("nan")),
    ("max_cost_twd", 0.1), ("entrypoint", "../secret.py"), ("entrypoint", "file.txt"),
    ("arguments", ["\0"]), ("arguments", ["a" * 8193]), ("partition", "4nodes"),
    ("template", "shell"), ("title", "\n"),
])
def test_resource_and_field_validation(field, value):
    spec = Spec().model_dump()
    spec[field] = value
    with pytest.raises(BatchError):
        parse_spec(spec)


def test_cost_known_and_unknown_and_partition_time(tmp_path):
    store = BatchStore(tmp_path)
    draft = store.example(store.create("training")["id"])
    spec = draft["spec"] | {"project_type": "academic", "partition": "normal2", "gpus": 2, "minutes": 120, "max_cost_twd": 240.0}
    draft = store.update(draft["id"], draft["revision"], spec)
    job = store.prepare(draft["id"], draft["revision"])
    assert job["cost"]["gpu_hours"] == 4
    assert job["cost"]["estimated_max_twd"] == 240
    assert not job["cost"]["storage_included"]
    error("invalid_fields", lambda: parse_spec(spec | {"project_type": "unknown"}))
    assert parse_spec(spec | {"minutes": 2880, "gpus": 1, "max_cost_twd": 4000.0})
    error("invalid_fields", lambda: parse_spec(spec | {"minutes": 2881, "max_cost_twd": 4000.0}))


def test_chinese_long_paths_shell_arguments_are_data(tmp_path):
    store = BatchStore(tmp_path / ("很長的目錄" * 8))
    draft = store.create("python")
    path = "/".join(["中文資料夾" * 6] * 8) + "/危險 ' $(touch marker).py"
    assert len(path.encode()) > 700
    draft = store.upload(draft["id"], {path: b"raise RuntimeError('NEVER RUN')"})
    args = ["; touch marker", "$(touch marker)", "a b", "'single'", '<script>alert(1)</script>']
    spec = draft["spec"] | {"entrypoint": path, "arguments": args, "account": "TEST123", "sif_path": "/work/checked.sif"}
    draft = store.update(draft["id"], draft["revision"], spec)
    job = store.prepare(draft["id"], draft["revision"])
    assert job["inputs"][0]["path"] == path
    # Parse script with bash only. Never run generated job scripts locally.
    checked = subprocess.run(["bash", "-n"], input=job["script"], text=True, capture_output=True)
    assert checked.returncode == 0
    assert "--nodes=1" in job["script"] and "--gpus-per-node=1" in job["script"]
    assert not (tmp_path / "marker").exists()
    store.demo(job["id"], "success")
    store.collect(job["id"])
    assert json.loads(store.result_file(job["id"], "summary.json"))["uploaded_program_executed"] is False


@pytest.mark.parametrize("path", ["/etc/passwd", "../x", "a/../x", "a//x", "./x", "C:/x", "a\\x", "a/\0x", "a/\nx", "-option/x", "a/\u202ex", "a" * 241, "a/" * 513, ".ssh/id_rsa", ".env"])
def test_unsafe_paths(path):
    with pytest.raises(BatchError):
        safe_path(path)


def test_archive_zip_slip_symlink_conflicts_bombs(tmp_path):
    error("unsafe_path", lambda: read_archive(make_zip({"../escape": b"x"})))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        link = zipfile.ZipInfo("link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        z.writestr(link, "/etc/passwd")
    error("unsafe_archive", lambda: read_archive(buffer.getvalue()))
    error("path_conflict", lambda: read_archive(make_zip({"a": b"x", "a/x": b"y"})))
    error("path_conflict", lambda: read_archive(make_zip({"A": b"x", "a": b"y"})))
    error("input_limit", lambda: read_archive(make_zip({"big": b"0" * (FILE_BYTES + 1)})))
    error("invalid_archive", lambda: read_archive(b"notzip"))
    error("empty_archive", lambda: read_archive(make_zip({})))
    assert not (tmp_path / "escape").exists()


def test_symlink_state_and_private_key_and_no_overwrite(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)
    error("unsafe_state", lambda: BatchStore(tmp_path / "link"))
    (tmp_path / "real" / "batch.sqlite3").symlink_to(tmp_path / "outside")
    error("unsafe_state", lambda: BatchStore(tmp_path / "real"))
    store = BatchStore(tmp_path / "safe")
    draft = store.create("python")
    error("credential_file", lambda: store.upload(draft["id"], {"key.pem": b"-----BEGIN RSA PRIVATE KEY-----\nprivate"}))
    draft = store.upload(draft["id"], {"main.py": b"original"})
    error("file_exists", lambda: store.upload(draft["id"], {"main.py": b"replace"}))
    error("has_files", lambda: store.example(draft["id"]))
    assert store.draft(draft["id"]) == draft


def test_revision_missing_entry_and_capacity(tmp_path, monkeypatch):
    import selfhost_models.batch as batch
    store = BatchStore(tmp_path)
    draft = store.create("python")
    error("missing_entrypoint", lambda: store.prepare(draft["id"], draft["revision"]))
    error("revision_conflict", lambda: store.update(draft["id"], 0, draft["spec"]))
    error("revision_conflict", lambda: store.prepare(draft["id"], 0))
    monkeypatch.setattr(batch, "STATE_BYTES", 2)
    error("state_full", lambda: store.upload(draft["id"], {"a.py": b"abc"}))
    assert not store.draft(draft["id"])["files"]
    monkeypatch.setattr(batch, "RECORD_COUNT", 1)
    error("state_full", lambda: store.create("python"))


def test_unknown_cancel_terminal_result_and_no_retry(tmp_path):
    store = BatchStore(tmp_path)
    draft, job = ready(store)
    job = store.demo(job["id"], "unknown")
    assert job["attempt_count"] == 1 and job["work_state"] == "unknown"
    store = BatchStore(tmp_path)
    error("no_retry", lambda: store.demo(job["id"], "success"))
    job = store.cancel(job["id"])
    assert job["cancel_requested"] and job["work_state"] == "unknown"
    assert store.cancel(job["id"]) == job
    error("not_collectable", lambda: store.collect(job["id"]))
    job = store.demo_confirm(job["id"], "succeeded")
    assert job["result_state"] == "pending"
    error("results_unavailable", lambda: store.result_files(job["id"]))
    job = store.collect(job["id"])
    assert job["result_state"] == "available" and job["result_provenance"] == "offline_fixture"
    assert store.collect(job["id"]) == job
    assert json.loads(store.result_file(job["id"], "summary.json"))["sum"] == 45
    _, other = ready(store, "training")
    other = store.demo(other["id"], "cancel")
    assert other["work_state"] == "running" and other["cancel_requested"]
    other = store.demo_confirm(other["id"], "canceled")
    assert other["work_state"] == "canceled" and other["result_state"] == "none"
    error("no_retry", lambda: store.demo(other["id"], "success"))


def test_result_import_is_not_work_confirmation_and_integrity(tmp_path):
    store = BatchStore(tmp_path)
    _, job = ready(store)
    job = store.demo(job["id"], "unknown")
    package = make_zip({"中文結果/summary.html": b"<script>evil()</script>", "log.txt": b"done"})
    imported = store.import_results(job["id"], package)
    assert imported["work_state"] == "unknown" and imported["result_state"] == "available"
    assert imported["result_provenance"] == "manual_import"
    error("results_exist", lambda: store.import_results(job["id"], package))
    error("unsafe_path", lambda: store.result_file(job["id"], "../secret"))
    with store.connection() as db:
        db.execute("UPDATE result_files SET data=? WHERE job_id=? AND path=?", (b"corrupt", job["id"], "log.txt"))
    error("result_integrity", lambda: store.result_files(job["id"]))


def test_live_disabled_no_env_or_cli_bypass(tmp_path, monkeypatch):
    store = BatchStore(tmp_path)
    _, job = ready(store)
    for name in ("NANO5_LIVE", "LIVE_ENABLED", "BATCH_TRANSPORT", "SSH_HOST"):
        monkeypatch.setenv(name, "true")
    def never(*args, **kwargs):
        pytest.fail("Network or process execution was attempted")
    monkeypatch.setattr(socket, "getaddrinfo", never)
    monkeypatch.setattr(socket.socket, "connect", never)
    monkeypatch.setattr(subprocess, "Popen", never)
    error("live_disabled", lambda: store.submit(job["id"]))
    assert store.job(job["id"])["attempt_count"] == 0
    store.demo(job["id"], "success")
    store.collect(job["id"])


def test_offline_guard_in_real_process():
    result = subprocess.run([sys.executable, "-m", "selfhost_models.batch_cli", "--verify-offline"], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "denied before network calls" in result.stdout
    rejected = subprocess.run([sys.executable, "-m", "selfhost_models.batch_cli", "--live"], capture_output=True, text=True, timeout=10)
    assert rejected.returncode != 0 and "unrecognized arguments" in rejected.stderr


@pytest.mark.asyncio
async def test_http_browser_contract_and_security(tmp_path, monkeypatch):
    app = BatchApp(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:18771") as client:
        boot = (await client.get("/api/bootstrap")).json()
        assert boot["live_enabled"] is False
        client.headers.update({"X-Batch-CSRF": boot["csrf_token"]})
        draft = (await client.post("/api/drafts", json={"template": "training"})).json()
        draft = (await client.post(f"/api/drafts/{draft['id']}/example", json={})).json()
        job = (await client.post(f"/api/drafts/{draft['id']}/prepare", json={"revision": draft["revision"]})).json()
        url = "/api/jobs/" + job["id"]
        assert (await client.post(url + "/submit", json={})).status_code == 403
        assert (await client.get(url + "/package")).headers["content-disposition"].startswith("attachment")
        job = (await client.post(url + "/demo", json={"scenario": "success"})).json()
        assert job["work_state"] == "succeeded" and job["result_state"] == "pending"
        assert (await client.get(url + "/results/download")).status_code == 409
        assert (await client.post(url + "/results/collect", json={})).json()["result_state"] == "available"
        downloaded = await client.get(url + "/results/download")
        assert json.loads(read_archive(downloaded.content)["summary.json"])["nano5_connected"] is False
        preview = await client.get(url + "/results/preview", params={"path": "stdout.txt"})
        assert "離線" in preview.json()["text"]
        assert "default-src 'none'" in preview.headers["content-security-policy"]
        assert (await client.get(url + "/results/file", params={"path": "summary.json"})).headers["content-type"] == "application/octet-stream"
        assert (await client.get(url + "/results/preview", params={"path": "../secret"})).status_code == 422
        assert (await client.get("/api/drafts", headers={"Host": "evil.example:18771"})).status_code == 403
        assert (await client.post("/api/drafts", headers={"Origin": "https://evil.example"}, json={"template": "python"})).status_code == 403
        assert (await client.post("/api/drafts", headers={"X-Batch-CSRF": "wrong"}, json={"template": "python"})).status_code == 403
        assert (await client.get("/api/drafts", headers={"Sec-Fetch-Site": "cross-site"})).status_code == 403
        assert (await client.post("/api/drafts", json={"template": []})).status_code == 422
        assert (await client.post("/api/drafts", json={"template": "python", "password": "never-store"})).status_code == 400
        assert (await client.post("/api/drafts", content=b'{"template":"python","template":"training"}', headers={"Content-Type":"application/json"})).status_code == 400
        assert (await client.post("/api/drafts", json={"template": "python"}, headers={"Content-Length": str(MAX_BODY + 1)})).status_code == 413
        assert (await client.put("/api/drafts/" + draft["id"], json={"revision": 0, "spec": draft["spec"]})).status_code == 409
        assert (await client.get("/api/jobs")).json()["jobs"][0]["id"] == job["id"]


def test_archive_only_deflate_stored_with_hostile_lzma_dictionary():
    import struct
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_LZMA) as z:
        z.writestr('x.txt', b'x')
    data = bytearray(buffer.getvalue())
    name_len, extra_len = struct.unpack_from('<HH', data, 26)
    start = 30 + name_len + extra_len
    assert struct.unpack_from('<H', data, start + 2)[0] == 5
    struct.pack_into('<I', data, start + 5, 1 << 30)
    # Reject method before opening the member; do not instantiate its 1 GiB decoder.
    error('unsafe_compression', lambda: read_archive(bytes(data)))
    for method in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', compression=method) as z:
            z.writestr('x.txt', b'x')
        assert read_archive(buffer.getvalue()) == {'x.txt': b'x'}


def test_delete_optimistic_revision_and_hash_prevents_stale_window_loss(tmp_path):
    store = BatchStore(tmp_path)
    a = store.upload(store.create('python')['id'], {'code/main.py': b'old'})
    sha = a['files'][0]['sha256']
    b = store.remove_file(a['id'], 'code/main.py', a['revision'], sha)
    b = store.upload(a['id'], {'code/main.py': b'new'})
    error('revision_conflict', lambda: store.remove_file(a['id'], 'code/main.py', a['revision'], sha))
    error('revision_conflict', lambda: store.remove_file(a['id'], 'code/main.py', b['revision'], sha))
    assert store.draft(a['id']) == b
    removed = store.remove_file(b['id'], 'code/main.py', b['revision'], b['files'][0]['sha256'])
    assert removed['files'] == []


@pytest.mark.asyncio
async def test_invalid_unicode_no_mutation_and_unicode_download_filename(tmp_path):
    from urllib.parse import unquote
    app = BatchApp(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:18771') as client:
        boot = (await client.get('/api/bootstrap')).json()
        client.headers['X-Batch-CSRF'] = boot['csrf_token']
        draft = (await client.post('/api/drafts', json={'template': 'python'})).json()
        # ASCII JSON can carry a lone surrogate even though it has no valid UTF-8 encoding.
        raw = json.dumps({'files': [{'path': 'code/' + chr(55296) + '.py', 'data_base64': 'eA=='}]}).encode()
        response = await client.post('/api/drafts/' + draft['id'] + '/files', content=raw, headers={'Content-Type': 'application/json'})
        assert response.status_code == 422 and response.json()['error']['code'] == 'unsafe_path'
        assert app.store.draft(draft['id']) == draft
        draft = app.store.example(draft['id'])
        job = app.store.prepare(draft['id'], draft['revision'])
        name = "中文 結果'報告.csv"
        app.store.import_results(job['id'], make_zip({'nested/' + name: b'a,b\n1,2\n'}))
        response = await client.get('/api/jobs/' + job['id'] + '/results/file', params={'path': 'nested/' + name})
        assert response.status_code == 200 and response.content == b'a,b\n1,2\n'
        header = response.headers['content-disposition']
        assert '.csv' in header and "filename*=UTF-8''" in header
        assert unquote(header.split("filename*=UTF-8''", 1)[1]) == name
        assert '\n' not in header and '\r' not in header
        unsafe = make_zip({'evil\r\nInjected.txt': b'no'})
        response = await client.post('/api/jobs/' + job['id'] + '/results/import', json={'data_base64': base64.b64encode(unsafe).decode()})
        assert response.status_code == 422


@pytest.mark.parametrize('template,output_name', [('python', 'summary.json'), ('training', 'metrics.json')])
def test_trusted_builtin_samples_only_cpu_bounded(tmp_path, template, output_name):
    """Execute only repository-owned samples, never files from drafts/uploads."""
    import os
    from selfhost_models.batch import EXAMPLES
    program = next(data for path, data in EXAMPLES[template].items() if path.endswith('.py'))
    sample = tmp_path / 'trusted_sample.py'
    sample.write_bytes(program)
    output = tmp_path / 'outputs'
    def bounds():
        if sys.platform != 'win32':
            import resource
            resource.setrlimit(resource.RLIMIT_CPU, (2, 2))
            resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
            resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))
    result = subprocess.run([sys.executable, '-I', str(sample)],
                            env={'BATCH_OUTPUT_DIR': str(output), 'CUDA_VISIBLE_DEVICES': '', 'PATH': os.defpath},
                            capture_output=True, timeout=5,
                            preexec_fn=bounds if sys.platform != 'win32' else None)
    assert result.returncode == 0, result.stderr.decode()
    actual = json.loads((output / output_name).read_bytes())
    if template == 'training':
        assert actual == {'example_only': True, 'training_performed': False}
    else:
        assert actual == {'example': True, 'sum': 6}


@pytest.mark.asyncio
@pytest.mark.parametrize('sequence', ['prepared_then_demo', 'unknown_then_confirm'])
async def test_imported_results_survive_later_success_and_download(tmp_path, sequence):
    app = BatchApp(tmp_path)
    _, job = ready(app.store)
    if sequence == 'unknown_then_confirm':
        app.store.demo(job['id'], 'unknown')
    original = {'中文資料/結果.csv': b'value\n42\n'}
    app.store.import_results(job['id'], make_zip(original))
    if sequence == 'prepared_then_demo':
        completed = app.store.demo(job['id'], 'success')
    else:
        completed = app.store.demo_confirm(job['id'], 'succeeded')
    assert completed['work_state'] == 'succeeded' and completed['result_state'] == 'available'
    assert completed['result_provenance'] == 'manual_import'
    if sequence == 'prepared_then_demo':
        assert '已有成果可下載' in completed['events'][-1]['message']
        assert '尚未收集' not in completed['events'][-1]['message']
    assert app.store.collect(job['id']) == completed
    reopened = BatchStore(tmp_path)
    assert reopened.result_files(job['id']) == original
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:18771') as client:
        response = await client.get('/api/jobs/' + job['id'] + '/results/download')
        assert response.status_code == 200 and read_archive(response.content) == original
