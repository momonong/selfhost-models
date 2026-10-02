"""Project/job behavior, additive migration, and trusted fixture contracts."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import zipfile

import httpx
import pytest

from selfhost_models.batch import BatchError, BatchStore, make_zip, manifest, STATE_BYTES
from selfhost_models.batch_app import BatchApp
from selfhost_models.batch_projects import ProjectStore, PROJECT_EXAMPLE, exclusion, remote_path


def ready(store):
    p = store.create_project("多檔範例")
    p = store.project_example(p["id"], p["revision"])
    d = store.create_work_draft(p["id"])
    return p, d, store.prepare(d["id"], d["revision"])


def fingerprint(db):
    return {table: db.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall()
            for table in ("drafts", "draft_files", "jobs", "result_files")}


def test_additive_migration_preserves_every_legacy_byte_and_download(tmp_path):
    old = BatchStore(tmp_path)
    d = old.example(old.create("python")["id"])
    j = old.prepare(d["id"], d["revision"])
    old.demo(j["id"], "unknown")
    old.import_results(j["id"], make_zip({"中文/result.csv": b"value\n9\n"}))
    package, results = old.package(j["id"]), old.result_files(j["id"])
    with sqlite3.connect(old.path) as db:
        before = fingerprint(db)
    migrated = ProjectStore(tmp_path)
    with sqlite3.connect(old.path) as db:
        assert fingerprint(db) == before
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
    assert migrated.package(j["id"]) == package
    assert migrated.result_files(j["id"]) == results
    assert migrated.job(j["id"])["work_state"] == "unknown"
    assert "snapshot" not in migrated.job(j["id"])
    ProjectStore(tmp_path)
    with sqlite3.connect(old.path) as db:
        assert fingerprint(db) == before


def test_project_reuse_and_updates_do_not_change_job_or_existing_draft(tmp_path):
    s = ProjectStore(tmp_path)
    p, d, j = ready(s)
    old_package = s.package(j["id"])
    another = s.create_work_draft(p["id"])
    p = s.import_project(p["id"], p["revision"], {"main.py": b"raise RuntimeError('never executed')"})
    p = s.update_project(p["id"], p["revision"], p["config"] | {"name": "新版專案", "arguments": ["--epochs", "2"]})
    assert s.package(j["id"]) == old_package
    assert s.work_draft(another["id"])["snapshot"]["project_revision"] == d["snapshot"]["project_revision"]
    new = s.create_work_draft(p["id"])
    assert new["snapshot"]["project_revision"] == p["revision"]
    assert new["spec"]["arguments"] == ["--epochs", "2"]
    clone = s.clone_job(j["id"])
    assert clone["snapshot"] == j["snapshot"] and clone["files"] == j["inputs"]
    assert clone["id"] != d["id"]
    cloned_job = s.prepare(clone["id"], clone["revision"])
    assert cloned_job["id"] != j["id"] and cloned_job["attempt_count"] == 0
    assert cloned_job["snapshot"]["project_name"] == "多檔範例"
    with pytest.raises(BatchError, match="已準備"):
        s.update_work_draft(d["id"], d["revision"], d["spec"], [])


def test_metadata_locations_cwd_and_readonly_bind_contract(tmp_path):
    s = ProjectStore(tmp_path)
    env = s.register_environment("預備 PyTorch 環境", "/work/images/python.sif")
    data = s.register_location("影像資料", "dataset", "/work/data/images")
    model = s.register_location("模型權重", "model", "/work/models/fixed-revision")
    p = s.project_example(s.create_project("專案")["id"], 1)
    p = s.update_project(p["id"], p["revision"], p["config"] | {"environment_id": env["id"], "location_ids": [data["id"], model["id"]]})
    d = s.create_work_draft(p["id"])
    d = s.update_work_draft(d["id"], d["revision"], d["spec"] | {"account": "PROJECT123", "arguments": ["a b", "$(never-run)"]}, [model["id"], data["id"]])
    assert [x["id"] for x in d["snapshot"]["locations"]] == [model["id"], data["id"]]
    j = s.prepare(d["id"], d["revision"])
    script = j["script"]
    assert '--bind "$PWD:/workspace:ro"' in script
    assert '--bind "$OUT:/batch-output:rw"' in script
    assert '--pwd /workspace/inputs/code' in script and "python3 main.py" in script
    assert "/work/data/images:/batch-assets/a2:ro" in script
    assert "BATCH_MODEL_DIR=/batch-assets/a1" in script and "BATCH_DATASET_DIR=/batch-assets/a2" in script
    assert 'mkdir -- "$OUT"' in script and j["id"] in script
    assert all(not x["verified"] for x in j["snapshot"]["locations"])
    assert j["snapshot"]["environment"]["verified"] is False
    assert subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True).returncode == 0
    with zipfile.ZipFile(io.BytesIO(s.package(j["id"]))) as z:
        assert z.read("inputs/code/package/calc.py") == PROJECT_EXAMPLE["package/calc.py"]
        assert json.loads(z.read("manifest.json"))["snapshot"] == j["snapshot"]
        readme = z.read("README.txt").decode()
        assert "cwd=/workspace/inputs/code" in readme and "BATCH_OUTPUT_DIR=/batch-output" in readme
        assert f"outputs/{j['id']}-<SLURM_JOB_ID>" in readme and "唯讀" in readme
    with pytest.raises(BatchError):
        s.update_work_draft(d["id"], d["revision"], d["spec"] | {"sif_path": "/other.sif"}, [])


@pytest.mark.parametrize("path", ["/", "../data", "/a/../b", "/a//b", "/a:b", "/a,b", "/a b", "/a/$(cmd)", "/a/.ssh", "/a/.", "/a/", [], "\ud800"])
def test_remote_references_reject_ambiguous_or_unsafe_paths(path):
    with pytest.raises(BatchError):
        remote_path(path)


def test_project_import_reports_exclusion_and_atomic_errors(tmp_path):
    s = ProjectStore(tmp_path)
    p = s.create_project("資料夾")
    p = s.import_project(p["id"], 1, {"main.py": b"pass", ".git/config": b"ignored", ".env.local": b"secret", "nested/__pycache__/x.pyc": b"ignored"})
    assert [f["path"] for f in p["files"]] == ["main.py"]
    assert len(p["exclusions"]) == 3
    with s.connection() as db:
        assert all(bytes(r[0]) not in (b"secret", b"ignored") for r in db.execute("SELECT data FROM project_files"))
    for bad in ({"../file.py": b"x"}, {".env/../file": b"x"}, {"\ud800": b"x"}, {"main.py": b"-----BEGIN RSA PRIVATE KEY-----"}, {"x.py": b"a", "X.py": b"b"}):
        with pytest.raises(BatchError):
            s.import_project(p["id"], p["revision"], bad)
        assert s.project(p["id"]) == p
    with pytest.raises(BatchError):
        s.import_project(p["id"], 1, {"main.py": b"new"})
    assert s.project(p["id"]) == p


def test_project_revision_noop_root_and_capacity(tmp_path, monkeypatch):
    s = ProjectStore(tmp_path)
    p = s.project_example(s.create_project("專案")["id"], 1)
    assert s.update_project(p["id"], p["revision"], p["config"]) == p
    with pytest.raises(BatchError):
        s.update_project(p["id"], p["revision"], p["config"] | {"working_dir": "nested"})
    s.import_project(p["id"], p["revision"], {"nested/main.py": b"pass"})
    p = s.project(p["id"])
    p = s.update_project(p["id"], p["revision"], p["config"] | {"working_dir": "nested", "entrypoint": "nested/main.py"})
    d = s.create_work_draft(p["id"])
    j = s.prepare(d["id"], d["revision"])
    assert "--pwd /workspace/inputs/code/nested" in j["script"] and "python3 main.py" in j["script"]
    with s.connection() as db:
        used = db.execute("SELECT sum(length(data)) FROM project_files").fetchone()[0]
    monkeypatch.setattr("selfhost_models.batch.STATE_BYTES", used)
    with pytest.raises(BatchError, match="256 MiB"):
        s.import_project(p["id"], p["revision"], {"main.py": b"new"})


def test_empty_nested_root_and_entrypoint_move_are_recoverable(tmp_path):
    s = ProjectStore(tmp_path)
    p = s.create_project("子目錄根")
    p = s.update_project(p["id"], p["revision"], p["config"] | {"working_dir": "subfolder"})
    p = s.import_project(p["id"], p["revision"], {"main.py": b"pass"})
    assert p["config"]["entrypoint"] == ""
    with pytest.raises(BatchError, match="選擇入口"):
        s.create_work_draft(p["id"])
    p = s.import_project(p["id"], p["revision"], {"main.py": b"pass", "subfolder/run.py": b"pass"})
    assert p["config"]["entrypoint"] == "subfolder/run.py"
    d = s.create_work_draft(p["id"])
    assert "--pwd /workspace/inputs/code/subfolder" in s.prepare(d["id"], d["revision"])["script"]
    p = s.import_project(p["id"], p["revision"], {"subfolder/renamed.py": b"pass"})
    assert p["config"]["entrypoint"] == "subfolder/renamed.py"


@pytest.mark.parametrize("path", [".git/\ud800", ".git/\x00", ".env/" + "x" * 241, ".git/" + "x" * 1025])
def test_excluded_paths_still_obey_basic_text_and_size_validation(path):
    with pytest.raises(BatchError):
        exclusion(path)


@pytest.mark.parametrize("path", ["keys/api.json", ".azure/config", ".gnupg/pubring.kbx", "credentials.json", "secrets.toml", "token.txt", "password.txt", "id_ecdsa.pub", "app.jks", "module.pyc"])
def test_program_import_exclusion_names_are_visible_without_content(path):
    assert exclusion(path)


def test_future_schema_is_not_downgraded(tmp_path):
    s = ProjectStore(tmp_path)
    with sqlite3.connect(s.path) as db:
        db.execute("PRAGMA user_version=3")
    with pytest.raises(BatchError, match="較新"):
        ProjectStore(tmp_path)
    with sqlite3.connect(s.path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3


def test_project_unknown_results_and_clone_are_independent(tmp_path):
    s = ProjectStore(tmp_path)
    _, _, j = ready(s)
    s.demo(j["id"], "unknown")
    s.cancel(j["id"])
    original = {"中文成果.csv": b"a,b\n1,2\n"}
    s.import_results(j["id"], make_zip(original))
    assert s.job(j["id"])["work_state"] == "unknown"
    clone = s.clone_job(j["id"])
    assert s.prepare(clone["id"], clone["revision"])["attempt_count"] == 0
    with pytest.raises(BatchError, match="不可重送"):
        s.demo(j["id"], "success")
    s.demo_confirm(j["id"], "succeeded")
    assert s.result_files(j["id"]) == original
    assert s.collect(j["id"])["result_provenance"] == "manual_import"
    assert ProjectStore(tmp_path).result_files(j["id"]) == original


@pytest.mark.asyncio
async def test_project_http_full_flow_and_legacy_routes_cannot_mutate_snapshot(tmp_path):
    app = BatchApp(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:18771") as c:
        c.headers["X-Batch-CSRF"] = (await c.get("/api/bootstrap")).json()["csrf_token"]
        p = (await c.post("/api/projects", json={"name": "HTTP 專案"})).json()
        url = "/api/projects/" + p["id"]
        p = (await c.post(url + "/files", json={"revision": 1, "files": [{"path": "main.py", "data_base64": "cGFzcw=="}, {"path": ".env", "data_base64": "not decoded"}]})).json()
        assert len(p["exclusions"]) == 1
        d = (await c.post("/api/work-drafts", json={"project_id": p["id"]})).json()
        assert d["snapshot"]["project_name"] == "HTTP 專案"
        bad = await c.put("/api/drafts/" + d["id"], json={"revision": 1, "spec": d["spec"] | {"sif_path": "/bypass.sif"}})
        assert bad.status_code == 409
        durl = "/api/work-drafts/" + d["id"]
        d = (await c.put(durl, json={"revision": d["revision"], "spec": d["spec"] | {"arguments": ["--test"]}, "location_ids": []})).json()
        j = (await c.post(durl + "/prepare", json={"revision": d["revision"]})).json()
        assert j["origin"] == "project" and j["spec"]["arguments"] == ["--test"]
        assert (await c.post("/api/jobs/" + j["id"] + "/submit", json={})).status_code == 403
        clone = (await c.post("/api/jobs/" + j["id"] + "/clone", json={})).json()
        assert clone["id"] != d["id"]
        assert len((await c.get("/api/work-drafts")).json()["drafts"]) == 1
        legacy = (await c.post("/api/drafts", json={"template": "python"})).json()
        origins = {item["id"]: item["origin"] for item in (await c.get("/api/drafts")).json()["drafts"]}
        assert origins[legacy["id"]] == "legacy" and origins[d["id"]] == origins[clone["id"]] == "project"
        assert (await c.post("/api/work-drafts", json={"project_id": []})).status_code == 422
        assert (await c.get("/api/projects?path=/etc/passwd")).status_code == 200
        assert (await c.get("/api/projects/../../etc/passwd")).status_code == 404


def test_trusted_multifile_sample_root_cwd_relative_config_only(tmp_path):
    # Only repository-owned bytes. Never use a project/draft upload in this test.
    for path, data in PROJECT_EXAMPLE.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    output = tmp_path / "output"
    def limits():
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (2, 2))
        resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024,) * 2)
    # -I removes cwd imports, so insert the exact trusted example root explicitly.
    launcher = "import sys,runpy;sys.path.insert(0,sys.argv[1]);runpy.run_path('main.py',run_name='__main__')"
    result = subprocess.run([sys.executable, "-I", "-c", launcher, str(tmp_path)], cwd=tmp_path,
                            env={"BATCH_OUTPUT_DIR": str(output), "CUDA_VISIBLE_DEVICES": "", "PATH": os.defpath},
                            capture_output=True, timeout=5, preexec_fn=limits if os.name != "nt" else None)
    assert result.returncode == 0, result.stderr.decode()
    assert json.loads((output / "summary.json").read_text()) == {"trusted_example": True, "sum": 6}
