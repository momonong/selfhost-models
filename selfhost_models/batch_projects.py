"""Versioned local projects and immutable job snapshots; no remote transport."""
from __future__ import annotations

import json
from pathlib import PurePosixPath
import re
import time
import unicodedata
import uuid

from .batch import (BatchError, BatchStore, RECORD_COUNT, manifest,
                    parse_spec, safe_path, validate_files, Spec)

EXCLUDED_DIRS = {".git", ".hg", ".svn", ".venv", "venv", "__pycache__", ".cache",
                 ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules", ".ssh",
                 ".aws", ".azure", ".gnupg", ".codex", ".agents", "keys"}


def exclusion(path):
    if not isinstance(path, str):
        safe_path(path)
    parts = path.split("/")
    if path.startswith(("/", "\\")) or "\\" in path or ":" in path or any(p in ("", ".", "..") for p in parts):
        raise BatchError("unsafe_path", "專案檔案必須是安全的相對路徑。")
    try:
        safe_path(path)
    except BatchError as exc:
        if exc.code != "credential_file":
            raise
        secrets = {".ssh", ".env", "id_rsa", "id_ed25519", "credentials", "secrets.toml"}
        safe_path("/".join("x" * len(p) if p.casefold() in secrets else p for p in parts))
    if any(p.casefold() in EXCLUDED_DIRS for p in parts):
        return "版本控制、環境、快取或私密設定目錄"
    name = parts[-1].casefold()
    if any(p.casefold().startswith(".env") for p in parts) or name.split(".")[0] in {"credentials", "secrets", "token", "password", "id_rsa", "id_ed25519", "id_ecdsa"} or name.endswith((".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".pyc", ".pyo")):
        return "憑證或秘密設定（不匯入內容）"
    safe_path(path)
    return None


def label(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 80:
        raise BatchError("name", "名稱需為 1–80 個字。")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise BatchError("name", "名稱包含無效 Unicode。") from None
    if any(ord(c) < 32 or ord(c) == 127 or unicodedata.category(c) == "Cf" for c in value):
        raise BatchError("name", "名稱不可包含控制或隱藏字元。")
    return value.strip()


def remote_path(value):
    # Metadata only. Never resolve/stat/read this path on the local host.
    if not isinstance(value, str) or not 2 <= len(value) <= 512 or not re.fullmatch(r"/[A-Za-z0-9_./-]+", value):
        raise BatchError("remote_path", "國網位置需為絕對路徑，僅允許英數字、_、-、.、/；不在本機讀取。")
    if any(p in ("", ".", "..") for p in value[1:].split("/")):
        raise BatchError("remote_path", "國網位置不可包含重複斜線或 . / .. 片段。")
    if any(p.casefold() in {".ssh", ".env", "credentials", ".aws"} for p in value.split("/")):
        raise BatchError("remote_path", "不可登錄憑證或秘密目錄。")
    return value


PROJECT_EXAMPLE = {
    "main.py": b'import os, json\nfrom pathlib import Path\nfrom package.calc import total\nconfig = json.loads(Path("config/settings.json").read_text())\nout = Path(os.environ["BATCH_OUTPUT_DIR"])\nout.mkdir(exist_ok=True)\n(out / "summary.json").write_text(json.dumps({"trusted_example":True,"sum":total(config["values"])}))\n',
    "package/__init__.py": b"",
    "package/calc.py": b"def total(values):\n    return sum(values)\n",
    "config/settings.json": b'{"values":[1,2,3]}\n',
    "README.txt": "可信多檔範例；專案根目錄為 cwd，包含子模組與相對設定檔。不上國網，不是真訓練。\n".encode(),
}


class ProjectStore(BatchStore):
    def __init__(self, root):
        super().__init__(root)
        with self.connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise BatchError("schema_version", "這份 state 來自較新或不支援版本，請保留資料並使用相符程式。", 409)
            db.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY,revision INTEGER NOT NULL,updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS project_versions(project_id TEXT NOT NULL,revision INTEGER NOT NULL,config TEXT NOT NULL,exclusions TEXT NOT NULL,PRIMARY KEY(project_id,revision),FOREIGN KEY(project_id) REFERENCES projects(id));
            CREATE TABLE IF NOT EXISTS project_files(project_id TEXT NOT NULL,revision INTEGER NOT NULL,path TEXT NOT NULL,data BLOB NOT NULL,PRIMARY KEY(project_id,revision,path),FOREIGN KEY(project_id,revision) REFERENCES project_versions(project_id,revision));
            CREATE TABLE IF NOT EXISTS environments(id TEXT PRIMARY KEY,record TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS locations(id TEXT PRIMARY KEY,record TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS work_drafts(draft_id TEXT PRIMARY KEY,snapshot TEXT NOT NULL,FOREIGN KEY(draft_id) REFERENCES drafts(id));
            PRAGMA user_version=2;
            COMMIT;
            """)

    def _named(self, db, table, ident):
        row = db.execute(f"SELECT record FROM {table} WHERE id=?", (ident,)).fetchone()
        if not row:
            raise BatchError("not_found", "找不到已登錄的環境或資料位置。", 404)
        return json.loads(row[0])

    def named(self, table):
        assert table in ("environments", "locations")
        with self.connection() as db:
            return [json.loads(r[0]) for r in db.execute(f"SELECT record FROM {table} ORDER BY rowid DESC")]

    def register_environment(self, name, sif_path):
        name = label(name)
        spec = parse_spec({"sif_path": sif_path})
        if not spec.sif_path:
            raise BatchError("sif_path", "登錄環境需填國網上預先準備的 SIF 路徑；也可暫不登錄，先離線準備。")
        remote_path(spec.sif_path)
        return self._register("environments", {"name": name, "sif_path": spec.sif_path})

    def register_location(self, name, kind, path):
        if kind not in ("dataset", "model"):
            raise BatchError("location_kind", "資料位置類型需為 dataset 或 model。")
        return self._register("locations", {"name": label(name), "kind": kind, "path": remote_path(path)})

    def _register(self, table, record):
        with self.connection() as db:
            if db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] >= RECORD_COUNT:
                raise BatchError("state_full", "環境或資料位置最多各 100 筆。", 409)
            record.update(id=("environment_" if table == "environments" else "location_") + uuid.uuid4().hex,
                          verified=False)
            db.execute(f"INSERT INTO {table} VALUES(?,?)", (record["id"], json.dumps(record)))
            return record

    def _locations(self, db, ids):
        if not isinstance(ids, list) or len(ids) > 8 or any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
            raise BatchError("locations", "最多選 8 個不重複的已登錄資料位置。")
        return [self._named(db, "locations", ident) for ident in ids]

    def _config(self, db, config):
        required = {"name", "working_dir", "entrypoint", "arguments", "environment_id", "location_ids"}
        if not isinstance(config, dict) or set(config) != required:
            raise BatchError("project_config", "專案設定欄位不正確。")
        result = dict(config)
        result["name"] = label(config["name"])
        checked = parse_spec({"entrypoint": config["entrypoint"], "arguments": config["arguments"]})
        result.update(entrypoint=checked.entrypoint, arguments=checked.arguments)
        working = config["working_dir"]
        if not isinstance(working, str):
            raise BatchError("working_dir", "專案根目錄需為相對目錄或空字串。")
        if working:
            safe_path(working)
            if checked.entrypoint and not checked.entrypoint.startswith(working + "/"):
                raise BatchError("entrypoint", "入口需位於選定的專案根目錄內。")
        env = config["environment_id"]
        if env is not None:
            if not isinstance(env, str):
                raise BatchError("environment", "請選擇已登錄環境或待核對環境。")
            self._named(db, "environments", env)
        self._locations(db, config["location_ids"])
        return result

    def _project(self, db, ident, revision=None):
        row = db.execute("SELECT * FROM projects WHERE id=?", (ident,)).fetchone()
        if not row:
            raise BatchError("not_found", "找不到這個專案。", 404)
        revision = revision or row["revision"]
        version = db.execute("SELECT * FROM project_versions WHERE project_id=? AND revision=?", (ident, revision)).fetchone()
        if not version:
            raise BatchError("not_found", "找不到這個專案版本。", 404)
        files = self._project_files(db, ident, revision)
        return {"id": ident, "revision": revision, "updated_at": row["updated"],
                "config": json.loads(version["config"]), "files": manifest(files),
                "exclusions": json.loads(version["exclusions"])}

    def _project_files(self, db, ident, revision):
        return {r["path"]: bytes(r["data"]) for r in db.execute("SELECT path,data FROM project_files WHERE project_id=? AND revision=?", (ident, revision))}

    def project(self, ident):
        with self.connection() as db:
            return self._project(db, ident)

    def projects(self):
        with self.connection() as db:
            return [self._project(db, r[0]) for r in db.execute("SELECT id FROM projects ORDER BY updated DESC")]

    def create_project(self, name):
        config = {"name": label(name), "working_dir": "", "entrypoint": "", "arguments": [], "environment_id": None, "location_ids": []}
        with self.connection() as db:
            if db.execute("SELECT count(*) FROM projects").fetchone()[0] >= RECORD_COUNT:
                raise BatchError("state_full", "最多保存 100 個專案。", 409)
            ident = "project_" + uuid.uuid4().hex
            db.execute("INSERT INTO projects VALUES(?,?,?)", (ident, 1, time.time()))
            db.execute("INSERT INTO project_versions VALUES(?,?,?,?)", (ident, 1, json.dumps(config), "[]"))
            return self._project(db, ident)

    def _new_version(self, db, old, config, files, exclusions):
        revision = old["revision"] + 1
        if revision > 100:
            raise BatchError("state_full", "每專案最多保存 100 個不可變版本，請備份後規劃新專案。", 409)
        validate_files(files)
        self._capacity(db, sum(map(len, files.values())))
        ident = old["id"]
        db.execute("INSERT INTO project_versions VALUES(?,?,?,?)", (ident, revision, json.dumps(config), json.dumps(exclusions)))
        db.executemany("INSERT INTO project_files VALUES(?,?,?,?)", [(ident, revision, p, data) for p, data in files.items()])
        db.execute("UPDATE projects SET revision=?,updated=? WHERE id=?", (revision, time.time(), ident))
        return self._project(db, ident)

    def _revision(self, old, revision):
        if type(revision) is not int or old["revision"] != revision:
            raise BatchError("revision_conflict", "專案或草稿已更新，請重新讀取後保存。", 409)

    def update_project(self, ident, revision, config):
        with self.connection() as db:
            old = self._project(db, ident)
            self._revision(old, revision)
            config = self._config(db, config)
            if config == old["config"]:
                return old
            return self._new_version(db, old, config, self._project_files(db, ident, revision), old["exclusions"])

    def import_project(self, ident, revision, files):
        accepted, excluded = {}, []
        if not isinstance(files, dict) or not 1 <= len(files) <= 2000:
            raise BatchError("files", "請選取完整小型專案，最多 2000 個選取項目，匯入最多 100 檔。")
        for path, data in files.items():
            reason = exclusion(path)
            if reason:
                excluded.append({"path": path, "reason": reason})
            else:
                accepted[path] = data
        if not accepted:
            raise BatchError("empty_project", "排除環境/秘密/快取後沒有可匯入的程式檔案。", fields={"exclusions": excluded})
        validate_files(accepted)
        with self.connection() as db:
            old = self._project(db, ident)
            self._revision(old, revision)
            config = old["config"]
            if config["entrypoint"] not in accepted:
                config["entrypoint"] = ""
                working = config["working_dir"]
                candidates = [p for p in accepted if p.endswith(".py") and (not working or p.startswith(working + "/"))]
                default = (working + "/" if working else "") + "main.py"
                if default in accepted:
                    config["entrypoint"] = default
                elif len(candidates) == 1:
                    config["entrypoint"] = candidates[0]
            config = self._config(db, config)
            return self._new_version(db, old, config, accepted, excluded)

    def project_example(self, ident, revision):
        if self.project(ident)["files"]:
            raise BatchError("has_files", "範例只可載入空專案，避免覆寫已匯入程式。", 409)
        return self.import_project(ident, revision, PROJECT_EXAMPLE)

    def _snapshot(self, db, project):
        c = project["config"]
        env = self._named(db, "environments", c["environment_id"]) if c["environment_id"] else {"id": None, "name": "待核對環境", "sif_path": "", "verified": False}
        return {"project_id": project["id"], "project_revision": project["revision"], "project_name": c["name"],
                "working_dir": c["working_dir"], "entrypoint": c["entrypoint"],
                "environment": env, "locations": self._locations(db, c["location_ids"])}

    def _insert_draft(self, db, spec, files, snapshot):
        if db.execute("SELECT count(*) FROM drafts").fetchone()[0] >= RECORD_COUNT:
            raise BatchError("state_full", "最多保存 100 份工作草稿（包含舊版）。", 409)
        validate_files(files)
        self._capacity(db, sum(map(len, files.values())))
        ident = "draft_" + uuid.uuid4().hex
        db.execute("INSERT INTO drafts VALUES(?,?,?,?)", (ident, 1, json.dumps(spec), time.time()))
        db.executemany("INSERT INTO draft_files VALUES(?,?,?)", [(ident, p, data) for p, data in files.items()])
        db.execute("INSERT INTO work_drafts VALUES(?,?)", (ident, json.dumps(snapshot)))
        return self._work_draft(db, ident)

    def create_work_draft(self, project_id):
        if not isinstance(project_id, str) or not re.fullmatch(r"project_[a-f0-9]{32}", project_id):
            raise BatchError("project_id", "請選擇已建立的專案。")
        with self.connection() as db:
            project = self._project(db, project_id)
            c = project["config"]
            files = self._project_files(db, project_id, project["revision"])
            if not c["entrypoint"] or c["entrypoint"] not in files:
                raise BatchError("missing_entrypoint", "先匯入完整專案並選擇入口，再建立工作。")
            self._config(db, c)
            if c["working_dir"] and not any(p.startswith(c["working_dir"] + "/") for p in files):
                raise BatchError("working_dir", "選定的專案根目錄沒有檔案，請核對匯入階層。")
            snapshot = self._snapshot(db, project)
            spec = Spec(title=c["name"][:75] + " 工作", entrypoint="code/" + c["entrypoint"], arguments=c["arguments"],
                        sif_path=snapshot["environment"]["sif_path"]).model_dump()
            return self._insert_draft(db, spec, {"code/" + p: b for p, b in files.items()}, snapshot)

    def _work_draft(self, db, ident):
        row = db.execute("SELECT snapshot FROM work_drafts WHERE draft_id=?", (ident,)).fetchone()
        if not row:
            raise BatchError("not_found", "找不到新版工作草稿。", 404)
        result = self._draft(db, ident)
        result.update(snapshot=json.loads(row[0]), origin="project")
        return result

    def work_draft(self, ident):
        with self.connection() as db:
            return self._work_draft(db, ident)

    def work_drafts(self):
        with self.connection() as db:
            return [self._work_draft(db, r[0]) for r in db.execute("SELECT w.draft_id FROM work_drafts w JOIN drafts d ON w.draft_id=d.id WHERE NOT EXISTS(SELECT 1 FROM jobs j WHERE j.draft_id=d.id AND j.revision=d.revision) ORDER BY d.updated DESC")]

    def update_work_draft(self, ident, revision, spec, location_ids):
        checked = parse_spec(spec).model_dump()
        with self.connection() as db:
            old = self._work_draft(db, ident)
            self._revision(old, revision)
            if db.execute("SELECT 1 FROM jobs WHERE draft_id=?", (ident,)).fetchone():
                raise BatchError("prepared_draft", "已準備的工作不可修改，請複製建立新工作草稿。", 409)
            snap = dict(old["snapshot"])
            if checked["entrypoint"] != "code/" + snap["entrypoint"] or checked["sif_path"] != snap["environment"]["sif_path"]:
                raise BatchError("snapshot", "工作入口與環境來自不可變專案快照，請改專案後建立新工作。")
            snap["locations"] = self._locations(db, location_ids)
            if checked == old["spec"] and snap == old["snapshot"]:
                return old
            db.execute("UPDATE drafts SET spec=?,revision=revision+1,updated=? WHERE id=?", (json.dumps(checked), time.time(), ident))
            db.execute("UPDATE work_drafts SET snapshot=? WHERE draft_id=?", (json.dumps(snap), ident))
            return self._work_draft(db, ident)

    def _preparation_context(self, db, ident):
        row = db.execute("SELECT snapshot FROM work_drafts WHERE draft_id=?", (ident,)).fetchone()
        return {"origin": "project", "snapshot": json.loads(row[0])} if row else {}

    def clone_job(self, ident):
        with self.connection() as db:
            job = self._job(db, ident)
            if job.get("origin") != "project":
                raise BatchError("legacy", "舊版工作沒有專案來源快照；請先建立專案，不杜撰來源。", 409)
            files = self._files(db, "draft_files", "draft_id", job["draft_id"])
            if manifest(files) != job["inputs"]:
                # Read immutable package, not a draft that might have been edited.
                import io, zipfile
                package = bytes(db.execute("SELECT package FROM jobs WHERE id=?", (ident,)).fetchone()[0])
                with zipfile.ZipFile(io.BytesIO(package)) as z:
                    files = {f["path"]: z.read("inputs/" + f["path"]) for f in job["inputs"]}
            if manifest(files) != job["inputs"]:
                raise BatchError("input_integrity", "原工作程式快照完整性核對失敗，不能複製。", 409)
            return self._insert_draft(db, job["spec"], files, job["snapshot"])
