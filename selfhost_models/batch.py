"""Local Nano5 batch preparation. No executor, shell, or network client exists here."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import sqlite3
import stat
import time
import unicodedata
import uuid
import zipfile
from contextlib import contextmanager
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

FILE_BYTES = 16 * 1024 * 1024
TOTAL_BYTES = 16 * 1024 * 1024
FILE_COUNT = 100
STATE_BYTES = 256 * 1024 * 1024
RECORD_COUNT = 100
SOURCE_DATE = "2026-10-02"
PARTITIONS = [
    {"id": "dev", "gpu": "H100", "max_minutes": 120, "account_gpu_cap": 8},
    {"id": "normal", "gpu": "H100", "max_minutes": 2880, "account_gpu_cap": 16},
    {"id": "normal2", "gpu": "H200", "max_minutes": 2880, "account_gpu_cap": 16},
]
RATES = {"H100": {"nstc": 25, "academic": 50, "government": 50, "enterprise": 120},
         "H200": {"nstc": 30, "academic": 60, "government": 60, "enterprise": 150}}


class BatchError(Exception):
    def __init__(self, code: str, message: str, status: int = 422, fields=None):
        super().__init__(message)
        self.code, self.message, self.status, self.fields = code, message, status, fields


def safe_path(value: str) -> str:
    """Archive-relative POSIX path; reject ambiguity rather than normalize traversal."""
    try:
        encoded = value.encode("utf-8") if isinstance(value, str) else b""
    except UnicodeEncodeError:
        raise BatchError("unsafe_path", "檔案名稱包含無效 Unicode，請重新命名。") from None
    if not isinstance(value, str) or not value or len(encoded) > 1024:
        raise BatchError("unsafe_path", "檔案路徑為空或超過 1024 bytes。")
    if unicodedata.normalize("NFC", value) != value:
        raise BatchError("unsafe_path", "檔案名稱需使用 NFC Unicode 格式。")
    if value.startswith(("/", "\\")) or "\\" in value or ":" in value:
        raise BatchError("unsafe_path", "請使用相對檔案路徑，不可使用絕對路徑或磁碟代號。")
    if any(ord(c) < 32 or ord(c) == 127 or unicodedata.category(c) == "Cf" for c in value):
        raise BatchError("unsafe_path", "檔案名稱不可包含控制或隱藏方向字元。")
    parts = value.split("/")
    if any(p.casefold() in {".ssh", ".env", "id_rsa", "id_ed25519", "credentials", "secrets.toml"} for p in parts):
        raise BatchError("credential_file", "不可匯入憑證或秘密設定檔。")
    if any(p in ("", ".", "..") or p.startswith("-") or len(p.encode()) > 240 for p in parts):
        raise BatchError("unsafe_path", "檔案路徑含不安全或過長的片段。")
    return value


def decode_file(value: str) -> bytes:
    if not isinstance(value, str) or len(value) > (FILE_BYTES + 2) // 3 * 4:
        raise BatchError("file_limit", "單檔上限為 16 MiB。")
    try:
        data = base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        raise BatchError("invalid_base64", "檔案編碼無效，請重新選取檔案。") from None
    if len(data) > FILE_BYTES:
        raise BatchError("file_limit", "單檔上限為 16 MiB。")
    return data


def validate_files(files: dict[str, bytes]):
    if len(files) > FILE_COUNT or sum(map(len, files.values())) > TOTAL_BYTES:
        raise BatchError("input_limit", "最多 100 個檔案，解壓後合計上限為 16 MiB。")
    keys = []
    for path, data in files.items():
        safe_path(path)
        if b"-----BEGIN " in data[:256] and b"PRIVATE KEY-----" in data[:256]:
            raise BatchError("credential_file", "不可匯入私鑰。")
        if len(data) > FILE_BYTES:
            raise BatchError("file_limit", "單檔上限為 16 MiB。")
        key = path.casefold()
        if key in keys or any(key.startswith(k + "/") or k.startswith(key + "/") for k in keys):
            raise BatchError("path_conflict", "檔案路徑重複、大小寫衝突，或與資料夾名稱衝突。")
        keys.append(key)


def read_archive(data: bytes) -> dict[str, bytes]:
    if len(data) > FILE_BYTES:
        raise BatchError("file_limit", "ZIP 上限為 16 MiB。")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            entries = z.infolist()
            if len(entries) > FILE_COUNT * 2:
                raise BatchError("input_limit", "ZIP 項目過多。")
            files, total = {}, 0
            for item in entries:
                if item.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    raise BatchError("unsafe_compression", "ZIP 僅支援 stored 或 deflate 壓縮；請重新壓縮。")
                mode = item.external_attr >> 16
                kind = stat.S_IFMT(mode)
                if kind not in (0, stat.S_IFREG, stat.S_IFDIR) or item.flag_bits & 1:
                    raise BatchError("unsafe_archive", "ZIP 不可含 symlink、特殊檔案或加密項目。")
                path = safe_path(item.filename.rstrip("/") if item.is_dir() else item.filename)
                if item.is_dir():
                    continue
                if kind == stat.S_IFDIR:
                    raise BatchError("unsafe_archive", "ZIP 檔案型態不一致。")
                if path in files:
                    raise BatchError("path_conflict", "ZIP 包含重複路徑。")
                total += item.file_size
                if item.file_size > FILE_BYTES or total > TOTAL_BYTES or len(files) >= FILE_COUNT:
                    raise BatchError("input_limit", "ZIP 解壓後超過 16 MiB 或 100 個檔案。")
                # Bounded reads also cover false declared sizes and decompression bombs.
                with z.open(item) as f:
                    content = f.read(FILE_BYTES + 1)
                if len(content) != item.file_size:
                    raise BatchError("unsafe_archive", "ZIP 檔案大小不一致。")
                files[path] = content
            if not files:
                raise BatchError("empty_archive", "ZIP 沒有可匯入的檔案。")
            validate_files(files)
            return files
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError, OSError, ValueError):
        raise BatchError("invalid_archive", "ZIP 損毀或格式不支援。") from None


def make_zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=(2026, 10, 2, 0, 0, 0))
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, files[name])
    return buffer.getvalue()


def manifest(files: dict[str, bytes]):
    return [{"path": p, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for p, data in sorted(files.items())]


class Spec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    template: Literal["python", "training"] = "python"
    title: str = Field(default="Python 批次工作", min_length=1, max_length=80)
    entrypoint: str = ""
    arguments: list[str] = Field(default_factory=list, max_length=64)
    partition: Literal["dev", "normal", "normal2"] = "dev"
    gpus: int = Field(default=1, ge=1, le=8)
    minutes: int = Field(default=20, ge=1, le=2880)
    cpus: int = Field(default=4, ge=1, le=16)
    memory_gib: int = Field(default=16, ge=1, le=128)
    project_type: Literal["unknown", "nstc", "academic", "government", "enterprise"] = "unknown"
    max_cost_twd: float = Field(default=500.0, gt=0, le=150000)
    account: str = Field(default="", max_length=64)
    sif_path: str = Field(default="", max_length=512)
    email: str = Field(default="", max_length=254)

    @field_validator("title")
    @classmethod
    def title_clean(cls, value):
        value.encode("utf-8")
        if not value.strip() or any(ord(c) < 32 or ord(c) == 127 or unicodedata.category(c) == "Cf" for c in value):
            raise ValueError("工作名稱不可為空或含控制字元")
        return value

    @field_validator("entrypoint")
    @classmethod
    def entry_clean(cls, value):
        if value:
            safe_path(value)
            if not value.lower().endswith(".py"):
                raise ValueError("主程式必須為 .py 檔案")
        return value

    @field_validator("arguments")
    @classmethod
    def args_clean(cls, value):
        if sum(len(v.encode()) for v in value) > 8192 or any("\0" in v or "\n" in v or "\r" in v for v in value):
            raise ValueError("參數合計上限 8192 bytes，每行一個參數，不可含換行或 NUL")
        return value

    @field_validator("account")
    @classmethod
    def account_clean(cls, value):
        if value and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value):
            raise ValueError("計畫 ID 僅允許英數字、底線與連字號")
        return value

    @field_validator("email")
    @classmethod
    def email_clean(cls, value):
        if value and not re.fullmatch(r"[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", value):
            raise ValueError("通知信箱格式無效")
        return value

    @field_validator("sif_path")
    @classmethod
    def sif_clean(cls, value):
        if value and (not value.startswith("/") or not value.endswith(".sif")
                      or not re.fullmatch(r"/[A-Za-z0-9_./-]+", value)
                      or ".." in value.split("/") or ":" in value):
            raise ValueError("SIF 請填國網上已核對的絕對 .sif 路徑（英數字、_、-、/、.）")
        return value

    @model_validator(mode="after")
    def bounds(self):
        if not math.isfinite(self.max_cost_twd):
            raise ValueError("費用上限必須為有限數值")
        part = next(p for p in PARTITIONS if p["id"] == self.partition)
        if self.minutes > part["max_minutes"]:
            raise ValueError(f"{self.partition} 最長時間為 {part['max_minutes']} 分鐘")
        # For unknown project types enforce the most expensive documented rate.
        rate = RATES[part["gpu"]].get(self.project_type, max(RATES[part["gpu"]].values()))
        if self.gpus * self.minutes / 60 * rate > self.max_cost_twd + 1e-9:
            raise ValueError("運算估算超過費用上限；計畫類別未知時以最高公開費率檢查")
        return self


def parse_spec(value) -> Spec:
    try:
        return Spec.model_validate(value)
    except ValidationError as exc:
        fields = {".".join(map(str, e["loc"])) or "resources": e["msg"].removeprefix("Value error, ")
                  for e in exc.errors(include_input=False, include_context=False)}
        raise BatchError("invalid_fields", "設定尚未通過檢查，請修正標示欄位。", fields=fields) from None


def templates():
    return [{"id": "python", "title": "Python 批次工作", "description": "資料處理或短程式；單節點、1 張 GPU、20 分鐘。Nano5 為 GPU 主機。",
             "defaults": Spec().model_dump()},
            {"id": "training", "title": "模型訓練", "description": "使用自己的訓練程式與預先準備的 SIF；單節點、1 張 GPU、60 分鐘。",
             "defaults": Spec(template="training", title="模型訓練工作", partition="normal", minutes=60, memory_gib=32).model_dump()}]


def estimate(spec: Spec):
    part = next(p for p in PARTITIONS if p["id"] == spec.partition)
    hours = spec.gpus * spec.minutes / 60
    rate = RATES[part["gpu"]].get(spec.project_type)
    return {"gpu_hours": round(hours, 4), "rate_twd": rate,
            "estimated_max_twd": round(hours * rate, 2) if rate is not None else None,
            "conservative_max_twd": round(hours * max(RATES[part["gpu"]].values()), 2),
            "cap_twd": spec.max_cost_twd, "source_date": SOURCE_DATE, "storage_included": False}


def blockers(spec: Spec):
    reasons = ["國網連線與提交在本版本硬性停用。", "帳號、partition 權限、計畫餘額及期限尚未在國網核對。",
               "SIF 與 Python/CUDA/套件相容性、程式的 GPU 使用方式尚未在國網驗證。"]
    if not spec.account:
        reasons.append("計畫 ID 待設定；腳本保留 NANO5_PROJECT_REQUIRED 占位值，不可直接提交。")
    if not spec.sif_path:
        reasons.append("國網 SIF 路徑待設定；腳本會在執行運算前拒絕。")
    if spec.project_type == "unknown":
        reasons.append("計畫類別未知，費用未確認；以最高公開費率檢查上限。")
    return reasons


def slurm_script(spec: Spec, job_id: str, snapshot=None) -> str:
    # Metadata uses a generated ASCII name, never a display title in directives.
    lines = ["#!/bin/bash", f"#SBATCH --account={spec.account or 'NANO5_PROJECT_REQUIRED'}",
             f"#SBATCH --job-name={job_id[:24]}", f"#SBATCH --partition={spec.partition}",
             "#SBATCH --nodes=1", "#SBATCH --ntasks-per-node=1", f"#SBATCH --gpus-per-node={spec.gpus}",
             f"#SBATCH --cpus-per-task={spec.cpus}", f"#SBATCH --mem={spec.memory_gib}G",
             f"#SBATCH --time={spec.minutes // 60:02d}:{spec.minutes % 60:02d}:00",
             "#SBATCH --output=slurm-%j.out", "#SBATCH --error=slurm-%j.err"]
    if spec.email:
        lines += ["#SBATCH --mail-type=END,FAIL", f"#SBATCH --mail-user={spec.email}"]
    lines += ["", "# PREPARATION ONLY: account/partition/SIF must be verified in person.",
              "# This file does not submit itself or download/install anything.", "set -euo pipefail",
              'if [[ -z "${SLURM_JOB_ID:-}" ]]; then echo "Run only as a verified Slurm batch job." >&2; exit 64; fi']
    if not spec.account or not spec.sif_path:
        lines += ['echo "Missing verified project ID or SIF path; preparation only." >&2', "exit 64"]
    lines += ['cd -- "${SLURM_SUBMIT_DIR:?}"', "module purge", "module load singularity",
              f"SIF={shlex.quote(spec.sif_path or '/NANO5_SIF_REQUIRED.sif')}",
              '[[ -f "$SIF" && ! -L "$SIF" ]] || { echo "SIF missing or symlink." >&2; exit 66; }']
    if snapshot:
        # Each exact prepared job/Slurm execution gets a fresh output directory.
        # Refuse an existing destination rather than overwrite a previous result.
        lines += ['[[ "$SLURM_JOB_ID" =~ ^[0-9]+$ ]] || exit 64',
                  '[[ ! -L outputs ]] || exit 66', 'mkdir -p -- outputs',
                  f'OUT="$PWD/outputs/{job_id}-$SLURM_JOB_ID"',
                  'mkdir -- "$OUT"',
                  'export SINGULARITYENV_BATCH_INPUT_DIR=/workspace/inputs',
                  'export SINGULARITYENV_BATCH_OUTPUT_DIR=/batch-output']
        binds = ['"$PWD:/workspace:ro"', '"$OUT:/batch-output:rw"']
        kinds = set()
        for index, location in enumerate(snapshot.get("locations", []), 1):
            destination = f"/batch-assets/a{index}"
            lines += [f"# Asset {index}: metadata only; availability not verified.",
                      f"export SINGULARITYENV_BATCH_ASSET_{index}={destination}"]
            if location["kind"] not in kinds:
                kinds.add(location["kind"])
                variable = "DATASET" if location["kind"] == "dataset" else "MODEL"
                lines += [f"export SINGULARITYENV_BATCH_{variable}_DIR={destination}"]
            binds.append(shlex.quote(location["path"] + ":" + destination + ":ro"))
        root = "/workspace/inputs/code"
        working = snapshot.get("working_dir", "")
        if working:
            root += "/" + working
        entry = spec.entrypoint.removeprefix("code/")
        if working:
            entry = str(PurePosixPath(entry).relative_to(working))
        lines += ['srun singularity exec --nv --cleanenv --no-home '
                  + " ".join("--bind " + bind for bind in binds)
                  + " --pwd " + shlex.quote(root) + ' "$SIF" '
                  + shlex.join(["python3", entry, *spec.arguments]), ""]
        return "\n".join(lines)
    lines += ["mkdir -p -- outputs", 'export SINGULARITYENV_BATCH_INPUT_DIR=/workspace/inputs',
              'export SINGULARITYENV_BATCH_OUTPUT_DIR=/workspace/outputs',
              'srun singularity exec --nv --cleanenv --no-home --bind "$PWD:/workspace" --pwd /workspace "$SIF" '
              + shlex.join(["python3", "inputs/" + spec.entrypoint, *spec.arguments]), ""]
    return "\n".join(lines)


EXAMPLES = {
    "python": {"code/main.py": b'import os, pathlib, json\nout = pathlib.Path(os.environ["BATCH_OUTPUT_DIR"])\nout.mkdir(exist_ok=True)\n(out / "summary.json").write_text(json.dumps({"example": True, "sum": sum([1,2,3])}))\n',
               "data/\u7bc4\u4f8b.csv": "value\n1\n2\n3\n".encode()},
    "training": {"code/train.py": b'# Preparation example: replace with your real training program.\nimport os, pathlib, json\nout = pathlib.Path(os.environ["BATCH_OUTPUT_DIR"])\nout.mkdir(exist_ok=True)\n(out / "metrics.json").write_text(json.dumps({"example_only": True, "training_performed": False}))\n',
                 "data/\u8a13\u7df4\u7bc4\u4f8b.json": b'{"example_only":true,"samples":[1,2,3]}\n'}
}


class BatchStore:
    """All bytes in private SQLite: uploads never become local executable files."""
    def __init__(self, root: Path):
        root = root.expanduser().absolute()
        if root.resolve() != root:
            raise BatchError("unsafe_state", "State 目錄不可含 symlink。")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not root.is_dir() or root.is_symlink():
            raise BatchError("unsafe_state", "State 必須是私有本機目錄。")
        os.chmod(root, 0o700)
        self.path = root / "batch.sqlite3"
        for suffix in ("", "-wal", "-shm", "-journal"):
            if Path(str(self.path) + suffix).is_symlink():
                raise BatchError("unsafe_state", "State 檔案不可為 symlink。")
        with self.connection() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS drafts(id TEXT PRIMARY KEY,revision INTEGER NOT NULL,spec TEXT NOT NULL,updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS draft_files(draft_id TEXT NOT NULL,path TEXT NOT NULL,data BLOB NOT NULL,PRIMARY KEY(draft_id,path));
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,draft_id TEXT NOT NULL,revision INTEGER NOT NULL,record TEXT NOT NULL,package BLOB NOT NULL,UNIQUE(draft_id,revision));
            CREATE TABLE IF NOT EXISTS result_files(job_id TEXT NOT NULL,path TEXT NOT NULL,data BLOB NOT NULL,PRIMARY KEY(job_id,path));
            """)
        os.chmod(self.path, 0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA foreign_keys=ON")
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _capacity(self, db, extra=0):
        used = db.execute("SELECT (SELECT coalesce(sum(length(data)),0) FROM draft_files)+(SELECT coalesce(sum(length(package)),0) FROM jobs)+(SELECT coalesce(sum(length(data)),0) FROM result_files)").fetchone()[0]
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='project_files'").fetchone():
            used += db.execute("SELECT coalesce(sum(length(data)),0) FROM project_files").fetchone()[0]
        if used + extra > STATE_BYTES:
            raise BatchError("state_full", "本機資料已達 256 MiB 邏輯上限，請先備份並規劃新 state；不會自動刪除紀錄。", 409)

    def _files(self, db, table, id_column, ident):
        return {r["path"]: bytes(r["data"]) for r in db.execute(f"SELECT path,data FROM {table} WHERE {id_column}=?", (ident,))}

    def _draft(self, db, ident):
        row = db.execute("SELECT * FROM drafts WHERE id=?", (ident,)).fetchone()
        if row is None:
            raise BatchError("not_found", "找不到這份草稿。", 404)
        return {"id": row["id"], "revision": row["revision"], "spec": json.loads(row["spec"]),
                "updated_at": row["updated"], "files": manifest(self._files(db, "draft_files", "draft_id", ident))}

    def create(self, template):
        if not isinstance(template, str) or template not in EXAMPLES:
            raise BatchError("template", "請選擇 Python 或模型訓練範本。")
        with self.connection() as db:
            if db.execute("SELECT count(*) FROM drafts").fetchone()[0] >= RECORD_COUNT:
                raise BatchError("state_full", "最多保存 100 份草稿。", 409)
            ident = "draft_" + uuid.uuid4().hex
            spec = next(t["defaults"] for t in templates() if t["id"] == template)
            db.execute("INSERT INTO drafts VALUES(?,?,?,?)", (ident, 1, json.dumps(spec), time.time()))
            return self._draft(db, ident)

    def drafts(self):
        with self.connection() as db:
            return [self._draft(db, row[0]) for row in db.execute("SELECT id FROM drafts ORDER BY updated DESC")]

    def draft(self, ident):
        with self.connection() as db:
            return self._draft(db, ident)

    def update(self, ident, revision, spec):
        checked = parse_spec(spec)
        with self.connection() as db:
            old = self._draft(db, ident)
            if type(revision) is not int or old["revision"] != revision:
                raise BatchError("revision_conflict", "草稿已在另一個視窗更新，請重新開啟後再保存。", 409)
            if old["spec"] == checked.model_dump():
                return old
            db.execute("UPDATE drafts SET spec=?,revision=revision+1,updated=? WHERE id=?", (checked.model_dump_json(), time.time(), ident))
            return self._draft(db, ident)

    def upload(self, ident, files: dict[str, bytes], *, replace=False, entrypoint=None):
        validate_files(files)
        with self.connection() as db:
            draft = self._draft(db, ident)
            merged = {} if replace else self._files(db, "draft_files", "draft_id", ident)
            for path, content in files.items():
                if path in merged and merged[path] != content:
                    raise BatchError("file_exists", "同名檔案已有不同內容；請明確移除舊檔後再匯入，避免覆寫資料。", 409)
            merged.update(files)
            validate_files(merged)
            self._capacity(db, sum(map(len, files.values())))
            if replace:
                db.execute("DELETE FROM draft_files WHERE draft_id=?", (ident,))
            for p, data in files.items():
                db.execute("INSERT OR REPLACE INTO draft_files VALUES(?,?,?)", (ident, p, data))
            spec = draft["spec"]
            if entrypoint is not None:
                spec["entrypoint"] = entrypoint
            db.execute("UPDATE drafts SET revision=revision+1,spec=?,updated=? WHERE id=?", (json.dumps(spec), time.time(), ident))
            return self._draft(db, ident)

    def example(self, ident):
        draft = self.draft(ident)
        template = draft["spec"]["template"]
        # Do not overwrite uploads. Explicitly reject so a click cannot lose user files.
        if draft["files"]:
            raise BatchError("has_files", "這份草稿已有檔案；請建立新草稿載入範例，避免覆寫資料。", 409)
        return self.upload(ident, EXAMPLES[template], entrypoint="code/main.py" if template == "python" else "code/train.py")

    def remove_file(self, ident, path, revision, sha256):
        safe_path(path)
        with self.connection() as db:
            draft = self._draft(db, ident)
            file = next((f for f in draft["files"] if f["path"] == path), None)
            if type(revision) is not int or draft["revision"] != revision or not file or file["sha256"] != sha256:
                raise BatchError("revision_conflict", "檔案已在另一視窗更新；請重新讀取並核對後再移除。", 409)
            db.execute("DELETE FROM draft_files WHERE draft_id=? AND path=?", (ident, path))
            db.execute("UPDATE drafts SET revision=revision+1,updated=? WHERE id=?", (time.time(), ident))
            return self._draft(db, ident)

    def _job(self, db, ident):
        row = db.execute("SELECT record FROM jobs WHERE id=?", (ident,)).fetchone()
        if row is None:
            raise BatchError("not_found", "找不到這份工作。", 404)
        return json.loads(row[0])

    def job(self, ident):
        with self.connection() as db:
            return self._job(db, ident)

    def jobs(self):
        with self.connection() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT record FROM jobs ORDER BY rowid DESC")]

    def _save_job(self, db, job, kind, message):
        job["events"].append({"at": time.time(), "kind": kind, "message": message})
        job["events"] = job["events"][-100:]
        db.execute("UPDATE jobs SET record=? WHERE id=?", (json.dumps(job), job["id"]))
        return job

    def prepare(self, ident, revision):
        with self.connection() as db:
            draft = self._draft(db, ident)
            if type(revision) is not int or revision != draft["revision"]:
                raise BatchError("revision_conflict", "草稿已更新，請重新保存與預覽。", 409)
            existing = db.execute("SELECT id FROM jobs WHERE draft_id=? AND revision=?", (ident, revision)).fetchone()
            if existing:
                return self._job(db, existing[0])
            if db.execute("SELECT count(*) FROM jobs").fetchone()[0] >= RECORD_COUNT:
                raise BatchError("state_full", "最多保存 100 份工作包。", 409)
            spec = parse_spec(draft["spec"])
            files = self._files(db, "draft_files", "draft_id", ident)
            if not spec.entrypoint or spec.entrypoint not in files:
                raise BatchError("missing_entrypoint", "請上傳 .py 程式並選擇主程式。", fields={"entrypoint": "主程式需在輸入檔案清單中"})
            job_id = "batch_" + uuid.uuid4().hex
            context = self._preparation_context(db, ident)
            script = slurm_script(spec, job_id, context.get("snapshot"))
            record = {"id": job_id, "draft_id": ident, "revision": revision, "spec": spec.model_dump(),
                      "inputs": manifest(files), "script": script, "blockers": blockers(spec), "cost": estimate(spec),
                      "work_state": "prepared", "result_state": "none", "mode": "prepared", "cancel_requested": False,
                      "attempt_count": 0, "events": [{"at": time.time(), "kind": "prepared", "message": "本機工作包已準備；未提交國網。"}],
                      "results": [], "result_provenance": None}
            record.update(context)
            package_files = {"inputs/" + p: data for p, data in files.items()}
            readme = "準備用工作包，未提交國網。\n先核對計畫/餘額/權限/SIF與程式相容性。\n不可在登入節點直接執行程式；本工具不連線、不提交、不輪詢。\n從解壓工作包目錄提交，程式讀取 BATCH_INPUT_DIR，結果寫入 BATCH_OUTPUT_DIR（outputs/）。\n單節點單 process；多 GPU 使用由程式負責。\n帳號與環境未設定時腳本會拒絕執行。\n"
            if context.get("snapshot"):
                snapshot = context["snapshot"]
                cwd = "/workspace/inputs/code" + ("/" + snapshot["working_dir"] if snapshot["working_dir"] else "")
                readme = ("準備用專案工作包，未提交國網。帳號/權限/SIF/資料均待現場核對。\n"
                          "從解壓工作包目錄交由 Slurm 排程；不得在登入節點直接運算。\n"
                          f"程式專案 cwd={cwd}，入口相對這個根目錄執行，子模組與設定檔階層保留。\n"
                          "程式工作區唯讀；BATCH_INPUT_DIR=/workspace/inputs。\n"
                          "BATCH_OUTPUT_DIR=/batch-output，獨立可寫 bind；host 位置 "
                          f"outputs/{job_id}-<SLURM_JOB_ID>，必須新建，已存在則拒絕覆寫。\n"
                          "資料/模型位置按 manifest.snapshot.locations 順序唯讀 bind 到 /batch-assets/a1 等。\n"
                          "BATCH_ASSET_1 等對應各位置；BATCH_DATASET_DIR/BATCH_MODEL_DIR 為該類型第一個位置，沒有就不設定。\n"
                          "所有位置僅 metadata，未查證存在/權限；不下載/安裝或自提交。\n"
                          "單節點單 process；多 GPU 協作由程式負責；缺計畫 ID/SIF 時腳本拒絕執行。\n")
            package_files.update({"job.slurm": script.encode(), "manifest.json": json.dumps(record, ensure_ascii=False, indent=2).encode(),
                                  "README.txt": readme.encode()})
            package = make_zip(package_files)
            self._capacity(db, len(package))
            db.execute("INSERT INTO jobs VALUES(?,?,?,?,?)", (job_id, ident, revision, json.dumps(record), package))
            return record

    def _preparation_context(self, db, ident):
        return {}

    def package(self, ident):
        with self.connection() as db:
            self._job(db, ident)
            return bytes(db.execute("SELECT package FROM jobs WHERE id=?", (ident,)).fetchone()[0])

    def submit(self, ident):
        self.job(ident)
        raise BatchError("live_disabled", "國網連線與提交在本版本硬性停用，沒有環境變數或 CLI 開關可啟用。請在使用者在場的新階段核對。", 403)

    def demo(self, ident, scenario):
        if scenario not in ("success", "unknown", "cancel"):
            raise BatchError("scenario", "離線示範情境無效。")
        with self.connection() as db:
            job = self._job(db, ident)
            if job["work_state"] != "prepared" or job["attempt_count"]:
                raise BatchError("no_retry", "工作已有執行意圖或已取消，不可重送；未知結果需先確認。", 409)
            package = bytes(db.execute("SELECT package FROM jobs WHERE id=?", (ident,)).fetchone()[0])
            package_hash = hashlib.sha256(package).hexdigest()
            job.update(mode="offline_fixture", attempt_count=1, work_state="unknown",
                       fixture={"package_sha256": package_hash, "transfer_bytes": len(package),
                                "scheduler_id": "offline-" + ident[-12:], "network_used": False})
            self._save_job(db, job, "demo_intent", "已保存唯一離線示範意圖；未連線國網、不執行上傳程式。")
        # Only fixed bounded bytes/math. Crash after intent remains durable unknown.
        # Simulated transfer checks the exact immutable package bytes in local memory.
        transferred = bytes(bytearray(package))
        if hashlib.sha256(transferred).hexdigest() != package_hash:
            raise BatchError("fixture_transfer", "離線模擬工作包傳輸核對失敗；工作保留不明。", 409)
        fixture_sum = sum(range(10))
        assert fixture_sum == 45
        with self.connection() as db:
            job = self._job(db, ident)
            if job["work_state"] != "unknown":
                return job
            if scenario == "success":
                job.update(work_state="succeeded", result_state="available" if job["result_state"] == "available" else "pending")
                message = ("固定 CPU fixture 完成；已有成果可下載，原匯入資料保留。" if job["result_state"] == "available"
                           else "固定 CPU fixture 完成；成果尚未收集。") + "不是國網或訓練品質驗收。"
            elif scenario == "cancel":
                job.update(work_state="running", cancel_requested=True)
                message = "模擬取消請求已記錄；尚未確認停止。"
            else:
                message = "模擬提交回應遺失；持久不明，禁止重送。"
            return self._save_job(db, job, "demo_" + scenario, message)

    def cancel(self, ident):
        with self.connection() as db:
            job = self._job(db, ident)
            if job["cancel_requested"] or job["work_state"] in ("succeeded", "failed", "canceled"):
                return job
            job["cancel_requested"] = True
            if job["work_state"] == "prepared":
                job["work_state"] = "canceled"
                message = "準備階段取消；沒有執行或國網提交。"
            else:
                message = "取消請求已保存；不表示運算停止或資源釋放。"
            return self._save_job(db, job, "cancel_requested", message)

    def demo_confirm(self, ident, outcome):
        if outcome not in ("succeeded", "canceled", "failed"):
            raise BatchError("outcome", "離線確認結果無效。")
        with self.connection() as db:
            job = self._job(db, ident)
            if job["mode"] != "offline_fixture" or job["work_state"] not in ("unknown", "running"):
                raise BatchError("not_pending", "只有未確認的離線 fixture 可以使用此確認。", 409)
            job.update(work_state=outcome, result_state="pending" if outcome == "succeeded" and job["result_state"] != "available" else job["result_state"])
            return self._save_job(db, job, "demo_terminal", f"離線 fixture 明確確認 {outcome}；不是國網查證。")

    def _results(self, db, job, files, provenance):
        if job["result_state"] == "available":
            raise BatchError("results_exist", "成果已保存，不可覆寫；保留原始結果。", 409)
        validate_files(files)
        self._capacity(db, sum(map(len, files.values())))
        for path, data in files.items():
            db.execute("INSERT INTO result_files VALUES(?,?,?)", (job["id"], path, data))
        job.update(result_state="available", results=manifest(files), result_provenance=provenance)
        return self._save_job(db, job, "results_available", "成果 bytes 與清單已持久保存；不改變運算狀態。")

    def collect(self, ident):
        with self.connection() as db:
            job = self._job(db, ident)
            if job["mode"] != "offline_fixture" or job["work_state"] != "succeeded":
                raise BatchError("not_collectable", "需先確認離線 fixture 運算成功；取消或不明不會自動變為完成。", 409)
            if job["result_state"] == "available":
                return job
            info = {"provenance": "offline_fixture", "template": job["spec"]["template"], "sum": 45,
                    "uploaded_program_executed": False, "gpu_used": False, "nano5_connected": False,
                    "training_performed": False, "simulated_transfer": job.get("fixture")}
            files = {"summary.json": json.dumps(info, ensure_ascii=False, indent=2).encode(),
                     "stdout.txt": "離線固定 CPU fixture：sum(0..9)=45。未執行上傳程式、未訓練、未使用國網。\n".encode(),
                     "metrics.csv": b"fixture,synthetic_value\nsum,45\n"}
            return self._results(db, job, files, "offline_fixture")

    def import_results(self, ident, data):
        files = read_archive(data)
        with self.connection() as db:
            return self._results(db, self._job(db, ident), files, "manual_import")

    def result_files(self, ident):
        with self.connection() as db:
            job = self._job(db, ident)
            if job["result_state"] != "available":
                raise BatchError("results_unavailable", "成果尚未保存，不能下載。", 409)
            files = self._files(db, "result_files", "job_id", ident)
            if manifest(files) != job["results"]:
                raise BatchError("result_integrity", "成果完整性核對失敗，請保留 state 調查。", 409)
            return files

    def result_file(self, ident, path):
        safe_path(path)
        files = self.result_files(ident)
        if path not in files:
            raise BatchError("not_found", "找不到成果檔案。", 404)
        return files[path]
