"""Single-host, same-OS transactional queue and conservative resource ownership.

SQLite is authoritative for admission. Files are immutable, fsynced receipts;
publishing a result never calls the execution provider again.
"""
import contextlib
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from filelock import FileLock, Timeout as LockTimeout

from .scheduler_schema import Deployment, SchedulerConfig, SchedulerError, SubmitJob, canonical, digest

FINAL = ("succeeded", "failed", "canceled", "expired", "dependency_failed")
ACTIVE = ("dispatching", "running", "draining", "unknown")


def require_native_state(path):
    if sys.platform != "linux":
        raise SchedulerError("managed_state_requires_linux", 503)
    probe = Path(path).resolve()
    while not probe.exists():
        probe = probe.parent
    result = subprocess.run(["stat", "-f", "-c", "%T", str(probe)], capture_output=True, timeout=5)
    if result.returncode or result.stdout.strip() not in (b"ext2/ext3", b"xfs", b"btrfs"):
        raise SchedulerError("state_requires_local_linux_filesystem", 503)


def durable_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("xb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
        if os.name != "nt":
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        if path.read_bytes() != data:
            raise OSError("durable readback failed")
    finally:
        temp.unlink(missing_ok=True)


def full_manifest(root):
    root = Path(root).resolve(strict=True)
    files = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if ".cache" in rel.parts:
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise SchedulerError("asset_symlink")
        if not path.is_file():
            continue
        sha = hashlib.sha256()
        with path.open("rb") as src:
            for chunk in iter(lambda: src.read(1024 * 1024), b""):
                sha.update(chunk)
        files[rel.as_posix()] = {"size": path.stat().st_size, "sha256": sha.hexdigest()}
    if not files or "config.json" not in files or not any(n.endswith(".safetensors") for n in files):
        raise SchedulerError("asset_files_missing")
    index = root / "model.safetensors.index.json"
    if index.exists():
        names = json.loads(index.read_bytes()).get("weight_map", {}).values()
        if not names or any(not isinstance(n, str) or n not in files or ":" in n or "\\" in n or
                            ".." in Path(n).parts or Path(n).is_absolute() for n in names):
            raise SchedulerError("invalid_shard_manifest")
    return files


class Store:
    def __init__(self, root, config=None, *, clock=time.time, fault=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / "scheduler.sqlite3"
        self.clock, self.fault = clock, fault or (lambda point: None)
        self.config = config or SchedulerConfig()
        with contextlib.closing(self.connect()) as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript('''
                CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS assets (ref TEXT PRIMARY KEY, path TEXT NOT NULL, manifest TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS deployments (id TEXT PRIMARY KEY, spec TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS blocked_deployments (id TEXT PRIMARY KEY, reason TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS budget (id INTEGER PRIMARY KEY CHECK(id=1), loads INTEGER NOT NULL, generations INTEGER NOT NULL);
                INSERT OR IGNORE INTO budget VALUES(1,0,0);
                CREATE TABLE IF NOT EXISTS budget_windows (name TEXT PRIMARY KEY, opened REAL NOT NULL,
                    closed REAL, start_loads INTEGER NOT NULL, start_generations INTEGER NOT NULL,
                    max_loads INTEGER NOT NULL, max_generations INTEGER NOT NULL,
                    end_loads INTEGER, end_generations INTEGER);
                CREATE TABLE IF NOT EXISTS artifacts (ref TEXT PRIMARY KEY, size INTEGER NOT NULL, media TEXT NOT NULL,
                    description TEXT, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS pins (owner TEXT NOT NULL, ref TEXT NOT NULL, PRIMARY KEY(owner,ref));
                CREATE TABLE IF NOT EXISTS jobs (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                    idem TEXT UNIQUE NOT NULL, content_hash TEXT NOT NULL, spec_ref TEXT NOT NULL, deployment TEXT NOT NULL,
                    urgent INTEGER NOT NULL, created REAL NOT NULL, deadline REAL NOT NULL, execution_limit INTEGER NOT NULL,
                    state TEXT NOT NULL, cancel_requested INTEGER NOT NULL DEFAULT 0, error TEXT,
                    result_state TEXT NOT NULL DEFAULT 'none', result_ref TEXT, attempt TEXT, ended REAL,
                    reservation INTEGER NOT NULL DEFAULT 0, result_description TEXT);
                CREATE TABLE IF NOT EXISTS dependencies (job TEXT NOT NULL, parent TEXT NOT NULL, PRIMARY KEY(job,parent));
                CREATE TABLE IF NOT EXISTS attempts (id TEXT PRIMARY KEY, job TEXT UNIQUE NOT NULL, owner INTEGER NOT NULL,
                    worker_epoch TEXT NOT NULL, deployment TEXT NOT NULL, started REAL NOT NULL, terminal REAL, error TEXT);
                CREATE TABLE IF NOT EXISTS leases (id TEXT PRIMARY KEY, kind TEXT NOT NULL, deployment TEXT NOT NULL,
                    worker_epoch TEXT NOT NULL, owner TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS controller (id INTEGER PRIMARY KEY CHECK(id=1), epoch INTEGER NOT NULL,
                    phase TEXT NOT NULL, deployment TEXT, worker_epoch TEXT, handle TEXT, reuse_count INTEGER NOT NULL,
                    changed REAL NOT NULL, heartbeat REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL,
                    kind TEXT NOT NULL, ref TEXT, detail TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS garbage (kind TEXT NOT NULL, ref TEXT NOT NULL, PRIMARY KEY(kind,ref));
                CREATE TABLE IF NOT EXISTS engine_cleanup (handle TEXT PRIMARY KEY, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS execution_binding (id INTEGER PRIMARY KEY CHECK(id=1),
                    control_id TEXT NOT NULL, executor_id TEXT NOT NULL, url TEXT NOT NULL, key_file TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS execution_grants (attempt TEXT PRIMARY KEY, envelope TEXT NOT NULL,
                    receipt TEXT, acked INTEGER NOT NULL DEFAULT 0);
                INSERT OR IGNORE INTO controller VALUES(1,0,'unloaded',NULL,NULL,NULL,0,0,0);
            ''')
            # Preserve state made by earlier v1 development snapshots.
            if "result_description" not in {r[1] for r in c.execute("PRAGMA table_info(jobs)")}:
                c.execute("ALTER TABLE jobs ADD COLUMN result_description TEXT")
                for row in c.execute("SELECT id,spec_ref FROM jobs").fetchall():
                    spec = json.loads(self.artifact_path(row["spec_ref"]).read_bytes())
                    c.execute("UPDATE jobs SET result_description=? WHERE id=?", (spec.get("result_description"), row["id"]))
            saved = c.execute("SELECT value FROM settings WHERE id=1").fetchone()
            if saved:
                actual = SchedulerConfig.model_validate(json.loads(saved[0]))
                if config is not None and actual != config:
                    raise SchedulerError("configuration_mismatch")
                self.config = actual
            else:
                c.execute("INSERT INTO settings VALUES(1,?)", (canonical(self.config.model_dump()).decode(),))

    def connect(self):
        c = sqlite3.connect(self.db, timeout=10, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA synchronous=FULL")
        c.execute("PRAGMA busy_timeout=10000")
        return c

    def execution_binding(self, c=None):
        if c is None:
            with contextlib.closing(self.connect()) as db:
                return self.execution_binding(db)
        row = c.execute("SELECT control_id,executor_id,url,key_file FROM execution_binding WHERE id=1").fetchone()
        return dict(row) if row else None

    def bind_execution(self, control_id, executor_id, url, key_file):
        if any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", value)
               for value in (control_id, executor_id)):
            raise SchedulerError("invalid_execution_identity")
        try:
            endpoint = urlsplit(url)
            endpoint.port  # Validate malformed/out-of-range ports, including optional default ports.
            valid = (endpoint.scheme in ("http", "https") and endpoint.hostname
                     and not endpoint.username and not endpoint.password and not endpoint.query
                     and not endpoint.fragment and endpoint.path in ("", "/"))
        except (ValueError, TypeError):
            valid = False
        if not valid or not isinstance(key_file, (str, Path)) or not str(key_file):
            raise SchedulerError("invalid_execution_binding")
        binding = dict(control_id=control_id, executor_id=executor_id, url=url.rstrip("/"),
                       key_file=str(Path(key_file).resolve()))
        try:
            with FileLock(str(self.root / "api.lock"), timeout=0), FileLock(str(self.root / "controller.lock"), timeout=0):
                with self.tx() as c:
                    if (self.state(c)["phase"] != "unloaded" or c.execute("SELECT 1 FROM leases").fetchone()
                            or c.execute("SELECT 1 FROM pins WHERE owner='model'").fetchone()
                            or c.execute("SELECT 1 FROM engine_cleanup").fetchone()
                            or c.execute("SELECT 1 FROM jobs WHERE state IN ('queued','dispatching','running','draining')").fetchone()):
                        raise SchedulerError("execution_binding_requires_idle", 409)
                    old = self.execution_binding(c)
                    if old and old != binding:
                        raise SchedulerError("execution_binding_conflict", 409)
                    c.execute("INSERT OR IGNORE INTO execution_binding VALUES(1,?,?,?,?)",
                              (control_id, executor_id, binding["url"], binding["key_file"]))
        except LockTimeout:
            raise SchedulerError("execution_binding_requires_offline", 409) from None
        return binding

    def execution_grant(self, attempt, c=None):
        if c is None:
            with contextlib.closing(self.connect()) as db:
                return self.execution_grant(attempt, db)
        row = c.execute("SELECT envelope FROM execution_grants WHERE attempt=?", (attempt,)).fetchone()
        return json.loads(row[0]) if row else None

    def execution_pending(self):
        with contextlib.closing(self.connect()) as c:
            return [json.loads(row[0]) for row in c.execute(
                "SELECT envelope FROM execution_grants WHERE acked=0 ORDER BY rowid")]

    def execution_ack(self, attempt):
        with self.tx() as c:
            row = c.execute("SELECT receipt FROM execution_grants WHERE attempt=?", (attempt,)).fetchone()
            if not row or row[0] is None:
                raise SchedulerError("execution_receipt_not_saved", 409)
            c.execute("UPDATE execution_grants SET acked=1 WHERE attempt=?", (attempt,))

    def _execution_grant(self, c, attempt, owner, kind, started, execution_limit):
        binding = self.execution_binding(c)
        if binding is None:
            return None
        state = self.state(c)
        grant = {"authority": binding["control_id"], "executor": binding["executor_id"],
                 "fence": state["epoch"], "handle": state["handle"], "deployment": state["deployment"],
                 "worker_epoch": state["worker_epoch"], "attempt": attempt, "owner": owner,
                 "kind": kind, "started": float(started), "execution_limit": execution_limit}
        from .execution_retention import reserve_execution_grant
        reserve_execution_grant(c, grant)
        c.execute("INSERT INTO execution_grants(attempt,envelope) VALUES(?,?)", (attempt, canonical(grant).decode()))
        return grant

    def _execution_receipt(self, c, attempt, result, error):
        row = c.execute("SELECT receipt FROM execution_grants WHERE attempt=?", (attempt,)).fetchone()
        if row is None:
            return
        marker = canonical({"result_hash": digest(result), "error": error}).decode()
        if row[0] is not None and row[0] != marker:
            raise SchedulerError("receipt_conflict", 409)
        c.execute("UPDATE execution_grants SET receipt=? WHERE attempt=?", (marker, attempt))

    @contextlib.contextmanager
    def tx(self):
        c = self.connect()
        try:
            c.execute("BEGIN IMMEDIATE")
            yield c
            self.fault("before_commit")
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def event(self, c, kind, ref=None, **detail):
        c.execute("INSERT INTO events(at,kind,ref,detail) VALUES(?,?,?,?)",
                  (self.clock(), kind, ref, canonical(detail).decode()))
        c.execute("DELETE FROM events WHERE seq <= (SELECT COALESCE(MAX(seq),0)-100000 FROM events)")

    def state(self, c=None):
        if c is not None:
            return dict(c.execute("SELECT * FROM controller WHERE id=1").fetchone())
        with contextlib.closing(self.connect()) as db:
            return self.state(db)

    def fenced(self, c, epoch):
        state = self.state(c)
        if state["epoch"] != epoch:
            raise SchedulerError("stale_controller", 409)
        return state

    def acquire_controller(self):
        # Caller MUST hold the OS lock for the entire controller lifetime.
        with self.tx() as c:
            s = self.state(c)
            epoch = s["epoch"] + 1
            if s["phase"] == "loading" and s["handle"] is None and not c.execute("SELECT 1 FROM leases").fetchone():
                # No external start intent existed. Crash during asset/image/network
                # preparation is a known non-start, not an ambiguous engine exit.
                c.execute("INSERT OR REPLACE INTO blocked_deployments VALUES(?,'preparation_interrupted')", (s["deployment"],))
                c.execute("UPDATE jobs SET state='failed',error='preparation_interrupted',ended=?,reservation=0 WHERE deployment=? AND state='queued'", (self.clock(), s["deployment"]))
                c.execute("DELETE FROM pins WHERE owner='model'")
                c.execute("UPDATE controller SET phase='unloaded',deployment=NULL,worker_epoch=NULL,handle=NULL WHERE id=1")
                self.event(c, "preparation_interrupted", s["deployment"])
                s = self.state(c)
            phase = "unloaded" if s["phase"] == "unloaded" else "unknown"
            c.execute("UPDATE controller SET epoch=?,phase=?,heartbeat=? WHERE id=1", (epoch, phase, self.clock()))
            c.execute("UPDATE jobs SET state='unknown',error='controller_restarted' WHERE state IN ('dispatching','running','draining')")
            c.execute("UPDATE leases SET state='unknown'")
            self.event(c, "controller_acquired", epoch=epoch)
            return epoch

    def heartbeat(self, epoch):
        with self.tx() as c:
            self.fenced(c, epoch)
            c.execute("UPDATE controller SET heartbeat=? WHERE id=1", (self.clock(),))

    def phase(self, epoch, phase, *, deployment=None, worker_epoch=None, handle=None):
        with self.tx() as c:
            s = self.fenced(c, epoch)
            if phase in ("loading", "unloading") and c.execute("SELECT 1 FROM leases LIMIT 1").fetchone():
                raise SchedulerError("leases_unresolved", 409)
            dep = deployment if deployment is not None else s["deployment"]
            c.execute("UPDATE controller SET phase=?,deployment=?,worker_epoch=?,handle=?,changed=? WHERE id=1",
                      (phase, dep, worker_epoch or s["worker_epoch"], handle or s["handle"], self.clock()))
            if dep:
                spec = self.deployment(dep, c)
                c.execute("INSERT OR IGNORE INTO pins VALUES('model',?)", (spec.asset_ref,))
            self.event(c, "model_phase" if phase != s["phase"] or dep != s["deployment"] else "model_handle_reserved", dep, phase=phase)

    def engine_exited(self, epoch):
        """Only callable after the provider verifies the entire exact engine exited."""
        with self.tx() as c:
            state = self.fenced(c, epoch)
            if state["handle"]:
                c.execute("INSERT OR IGNORE INTO engine_cleanup VALUES(?,?)", (state["handle"], canonical(state).decode()))
            c.execute("UPDATE jobs SET state='unknown',error='engine_exited_unknown' WHERE state IN ('dispatching','running','draining')")
            c.execute("DELETE FROM leases")
            c.execute("DELETE FROM pins WHERE owner='model'")
            c.execute("UPDATE controller SET phase='unloaded',deployment=NULL,worker_epoch=NULL,handle=NULL,reuse_count=0,changed=? WHERE id=1", (self.clock(),))
            self.event(c, "engine_exit_confirmed")

    def cleanup_pending(self):
        with contextlib.closing(self.connect()) as c:
            return [json.loads(row[0]) for row in c.execute("SELECT state FROM engine_cleanup")]

    def cleanup_done(self, epoch, handle):
        with self.tx() as c:
            self.fenced(c, epoch)
            c.execute("DELETE FROM engine_cleanup WHERE handle=?", (handle,))

    def preparation_failed(self, epoch, jid, error="asset_preparation_failed"):
        with self.tx() as c:
            s = self.fenced(c, epoch)
            if s["phase"] != "loading" or s["handle"] is not None or c.execute("SELECT 1 FROM leases").fetchone():
                raise SchedulerError("external_load_intent_exists", 409)
            c.execute("UPDATE jobs SET state='failed',error=?,ended=?,reservation=0 WHERE id=? AND state='queued'", (error, self.clock(), jid))
            c.execute("DELETE FROM pins WHERE owner='model'")
            c.execute("UPDATE controller SET phase='unloaded',deployment=NULL,worker_epoch=NULL,handle=NULL WHERE id=1")
            self.event(c, "asset_preparation_failed", jid)

    def reserve_load(self, epoch, dep):
        warmups = 1 if dep.runtime == "whisper" else 2 + (dep.load.capacity if dep.load.capacity > 1 else 0) + (dep.load.capacity if dep.load.video else 0)
        with self.tx() as c:
            self.fenced(c, epoch)
            b = c.execute("SELECT * FROM budget WHERE id=1").fetchone()
            if b["loads"] >= self.config.max_load_attempts or b["generations"] + warmups > self.config.max_generation_attempts:
                raise SchedulerError("lifecycle_budget_exhausted", 429)
            self.check_budget_window(c, loads=1, generations=warmups)
            c.execute("UPDATE budget SET loads=loads+1,generations=generations+? WHERE id=1", (warmups,))
            self.event(c, "load_budget_reserved", dep.id, warmup_generation_upper_bound=warmups)

    def lifecycle_failed(self, epoch, deployment):
        with self.tx() as c:
            self.fenced(c, epoch)
            c.execute("INSERT OR REPLACE INTO blocked_deployments VALUES(?,'lifecycle_failed')", (deployment,))
            c.execute("UPDATE jobs SET state='failed',error='lifecycle_failed',ended=?,reservation=0 WHERE deployment=? AND state='queued'", (self.clock(), deployment))
            self.event(c, "deployment_blocked", deployment)

    def resume_deployment(self, ref):
        with self.tx() as c:
            if self.state(c)["phase"] != "unloaded" or c.execute("SELECT 1 FROM leases").fetchone():
                raise SchedulerError("engine_exit_unconfirmed", 409)
            c.execute("DELETE FROM blocked_deployments WHERE id=?", (ref,))
            self.event(c, "operator_resumed_deployment", ref)

    def budget_state(self):
        with contextlib.closing(self.connect()) as c:
            return dict(c.execute("SELECT loads,generations FROM budget WHERE id=1").fetchone())

    def check_budget_window(self, c, *, loads=0, generations=0):
        window = c.execute("SELECT * FROM budget_windows WHERE closed IS NULL").fetchone()
        if window:
            b = c.execute("SELECT * FROM budget WHERE id=1").fetchone()
            if (b["loads"] + loads - window["start_loads"] > window["max_loads"] or
                    b["generations"] + generations - window["start_generations"] > window["max_generations"]):
                raise SchedulerError("budget_window_exhausted", 429)

    def open_budget_window(self, name, loads, generations):
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name) or type(loads) is not int or
                type(generations) is not int or not 1 <= loads <= 100000 or not 1 <= generations <= 10000000):
            raise SchedulerError("invalid_budget_window")
        with self.tx() as c:
            from .fleet_store import check_fleet_budget_idle
            check_fleet_budget_idle(c)
            if (self.state(c)["phase"] not in ("unloaded", "ready") or
                    c.execute("SELECT 1 FROM leases").fetchone() or
                    c.execute("SELECT 1 FROM jobs WHERE state IN ('queued','dispatching','running','draining')").fetchone()):
                raise SchedulerError("budget_window_requires_idle", 409)
            if c.execute("SELECT 1 FROM budget_windows WHERE closed IS NULL OR name=?", (name,)).fetchone():
                raise SchedulerError("budget_window_exists", 409)
            b = c.execute("SELECT * FROM budget WHERE id=1").fetchone()
            c.execute("INSERT INTO budget_windows VALUES(?,?,NULL,?,?,?,?,NULL,NULL)",
                      (name, self.clock(), b["loads"], b["generations"], loads, generations))
            self.event(c, "budget_window_opened", name, max_loads=loads, max_generations=generations)

    def close_budget_window(self, name):
        with self.tx() as c:
            window = c.execute("SELECT * FROM budget_windows WHERE name=?", (name,)).fetchone()
            if not window:
                raise SchedulerError("budget_window_not_found", 404)
            if window["closed"] is not None:
                return  # A duplicate close cannot change its historical receipt.
            from .fleet_store import check_fleet_budget_idle
            check_fleet_budget_idle(c)
            if (self.state(c)["phase"] not in ("unloaded", "ready") or
                    c.execute("SELECT 1 FROM leases").fetchone() or
                    c.execute("SELECT 1 FROM jobs WHERE state IN ('queued','dispatching','running','draining')").fetchone()):
                raise SchedulerError("budget_window_requires_idle", 409)
            b = c.execute("SELECT * FROM budget WHERE id=1").fetchone()
            c.execute("UPDATE budget_windows SET closed=?,end_loads=?,end_generations=? WHERE name=?",
                      (self.clock(), b["loads"], b["generations"], name))
            self.event(c, "budget_window_closed", name, loads=b["loads"]-window["start_loads"],
                       generations=b["generations"]-window["start_generations"])

    def budget_windows(self):
        with contextlib.closing(self.connect()) as c:
            b = c.execute("SELECT * FROM budget WHERE id=1").fetchone()
            return [{**dict(w), "used_loads": (w["end_loads"] if w["closed"] is not None else b["loads"])-w["start_loads"],
                     "used_generations": (w["end_generations"] if w["closed"] is not None else b["generations"])-w["start_generations"]}
                    for w in c.execute("SELECT * FROM budget_windows ORDER BY opened,name")]

    def asset_register(self, path, expected=None):
        manifest = full_manifest(path)
        if expected is not None and manifest != expected:
            raise SchedulerError("asset_hash_mismatch")
        ref = "asset_" + digest(manifest)
        with self.tx() as c:
            old = c.execute("SELECT path FROM assets WHERE ref=?", (ref,)).fetchone()
            if old and Path(old[0]) != Path(path).resolve():
                raise SchedulerError("asset_already_registered")
            c.execute("INSERT OR IGNORE INTO assets VALUES(?,?,?)", (ref, str(Path(path).resolve()), canonical(manifest).decode()))
        return ref

    def assets_import(self, manifest):
        if not isinstance(manifest, dict) or len(manifest) > 10000 or "config.json" not in manifest or not any(
                isinstance(name, str) and name.endswith(".safetensors") for name in manifest):
            raise SchedulerError("asset_files_missing")
        for name, entry in manifest.items():
            if (not isinstance(name, str) or not name or name.startswith("/") or ":" in name or "\\" in name
                    or any(part in ("", ".", "..") for part in name.split("/")) or "\x00" in name
                    or not isinstance(entry, dict) or set(entry) != {"size", "sha256"}
                    or type(entry["size"]) is not int or entry["size"] < 0
                    or not isinstance(entry["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])):
                raise SchedulerError("invalid_asset_manifest")
        if len(canonical(manifest)) > 2097152:
            raise SchedulerError("invalid_asset_manifest")
        ref = "asset_" + digest(manifest)
        with self.tx() as c:
            if self.execution_binding(c) is None:
                raise SchedulerError("execution_binding_required", 409)
            # An imported manifest never replaces an existing local asset path.
            c.execute("INSERT OR IGNORE INTO assets VALUES(?,'',?)", (ref, canonical(manifest).decode()))
        return ref

    def asset_manifest(self, ref):
        with contextlib.closing(self.connect()) as c:
            row = c.execute("SELECT manifest FROM assets WHERE ref=?", (ref,)).fetchone()
        if not row:
            raise SchedulerError("asset_not_found", 404)
        manifest = json.loads(row[0])
        if "asset_" + digest(manifest) != ref:
            raise SchedulerError("asset_hash_mismatch")
        return manifest

    def asset_verify(self, ref):
        with contextlib.closing(self.connect()) as c:
            row = c.execute("SELECT * FROM assets WHERE ref=?", (ref,)).fetchone()
        if not row:
            raise SchedulerError("asset_not_found", 404)
        if not row["path"]:
            raise SchedulerError("asset_requires_executor", 409)
        if full_manifest(row["path"]) != json.loads(row["manifest"]):
            raise SchedulerError("asset_hash_mismatch")
        return Path(row["path"])

    def asset_forget(self, ref):
        with self.tx() as c:
            if c.execute("SELECT 1 FROM pins WHERE ref=?", (ref,)).fetchone():
                raise SchedulerError("asset_pinned", 409)
            if any(self.deployment(r[0], c).asset_ref == ref for r in c.execute("SELECT id FROM deployments")):
                raise SchedulerError("asset_referenced", 409)
            c.execute("DELETE FROM assets WHERE ref=?", (ref,))
        # Never deletes an operator's model directory.

    def register(self, deployment):
        if self.execution_binding():
            self.asset_manifest(deployment.asset_ref)
        else:
            self.asset_verify(deployment.asset_ref)
        with self.tx() as c:
            c.execute("INSERT OR IGNORE INTO deployments VALUES(?,?)", (deployment.id, canonical(deployment.model_dump()).decode()))
        return deployment.id

    def deployment(self, ref, c=None):
        if c is None:
            with contextlib.closing(self.connect()) as db:
                return self.deployment(ref, db)
        row = c.execute("SELECT spec FROM deployments WHERE id=?", (ref,)).fetchone()
        if not row:
            raise SchedulerError("deployment_not_found", 404)
        return Deployment.model_validate(json.loads(row[0]))

    def catalog(self):
        with contextlib.closing(self.connect()) as c:
            return [{"deployment_id": row["id"], **json.loads(row["spec"])} for row in c.execute("SELECT * FROM deployments ORDER BY id")]

    def artifact_path(self, ref):
        if not re.fullmatch(r"artifact_[0-9a-f]{64}", ref):
            raise SchedulerError("invalid_artifact_ref")
        return self.root / "artifacts" / ref

    def budget(self, c, additional=0):
        # Failed commits may leave immutable files with no DB row. They still
        # consume quota until orphan collection, including after process restart.
        used = sum(p.stat().st_size for p in (self.root / "artifacts").glob("artifact_*") if p.is_file())
        reserved = c.execute("SELECT COALESCE(SUM(reservation),0) FROM jobs").fetchone()[0]
        receipts = sum(p.stat().st_size for p in (self.root / "receipts").glob("*.json") if p.is_file())
        reserved = max(reserved, receipts)
        if used + reserved + additional > self.config.storage_bytes:
            raise SchedulerError("storage_capacity", 429)

    def put(self, c, data, media, description=None, *, reserved=False):
        ref = "artifact_" + hashlib.sha256(data).hexdigest()
        if not c.execute("SELECT 1 FROM artifacts WHERE ref=?", (ref,)).fetchone():
            if not reserved:
                self.budget(c, len(data))
            durable_write(self.artifact_path(ref), data)
            c.execute("INSERT INTO artifacts VALUES(?,?,?,?,?)", (ref, len(data), media, description, self.clock()))
        return ref

    def upload(self, data, media="audio/wav", description=None):
        from .wav import validate_wav
        if media != "audio/wav":
            raise SchedulerError("unsupported_media")
        validate_wav(data)
        with self.tx() as c:
            ref = self.put(c, data, media, description)
        return {"artifact_ref": ref, "media_type": media, "size": len(data)}

    def artifact(self, ref):
        with contextlib.closing(self.connect()) as c:
            row = c.execute("SELECT * FROM artifacts WHERE ref=?", (ref,)).fetchone()
        if not row:
            raise SchedulerError("artifact_not_found", 404)
        data = self.artifact_path(ref).read_bytes()
        if len(data) != row["size"] or hashlib.sha256(data).hexdigest() != ref[9:]:
            raise SchedulerError("artifact_corrupt", 503)
        return data, row["media"]

    def submit(self, spec, key):
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", key):
            raise SchedulerError("invalid_idempotency_key")
        content = canonical(spec.model_dump())
        if spec.execution_timeout_seconds > self.config.drain_seconds:
            raise SchedulerError("execution_limit_exceeds_drain")
        if len(content) > self.config.input_bytes:
            raise SchedulerError("input_too_large", 413)
        h = hashlib.sha256(content).hexdigest()
        with self.tx() as c:
            old = c.execute("SELECT * FROM jobs WHERE idem=?", (key,)).fetchone()
            if old:
                if old["content_hash"] != h:
                    raise SchedulerError("idempotency_conflict", 409)
                return self.public_job(old), False
            from .fleet_store import check_admission
            check_admission(c)
            dep = self.deployment(spec.deployment_id, c)
            if c.execute("SELECT 1 FROM blocked_deployments WHERE id=?", (dep.id,)).fetchone():
                raise SchedulerError("deployment_blocked", 503)
            if (spec.operation == "chat") != (dep.runtime == "vllm"):
                raise SchedulerError("unsupported_operation")
            if spec.operation == "chat":
                from .capabilities import validate_capabilities
                if spec.input.model != dep.model:
                    raise SchedulerError("model_deployment_mismatch")
                validate_capabilities(spec.input.model_dump(exclude_none=True), "vllm", "qwen3_5", dep.load.video)
            else:
                row = c.execute("SELECT media FROM artifacts WHERE ref=?", (spec.input.audio_ref,)).fetchone()
                if not row or row[0] != "audio/wav":
                    raise SchedulerError("audio_artifact_not_found", 404)
            for parent in spec.depends_on:
                if not c.execute("SELECT 1 FROM jobs WHERE id=?", (parent,)).fetchone():
                    raise SchedulerError("dependency_not_found", 404)
            if c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] >= self.config.retained_jobs:
                raise SchedulerError("retained_jobs_capacity", 429)
            if c.execute("SELECT COUNT(*) FROM jobs WHERE state='queued'").fetchone()[0] >= self.config.queue_capacity:
                raise SchedulerError("queue_capacity", 429)
            # Space for both receipt and published result; released only on publication.
            reservation = self.config.result_bytes * 2 + 4096
            self.budget(c, reservation + len(content))
            ref = self.put(c, content, "application/json")
            jid, now = "job_" + uuid.uuid4().hex, self.clock()
            c.execute("INSERT INTO jobs(id,idem,content_hash,spec_ref,deployment,urgent,created,deadline,execution_limit,state,reservation,result_description) VALUES(?,?,?,?,?,?,?,?,?,'queued',?,?)",
                      (jid, key, h, ref, dep.id, int(spec.urgent), now, now + spec.queue_timeout_seconds, spec.execution_timeout_seconds, reservation, spec.result_description))
            c.execute("INSERT INTO pins VALUES(?,?)", (jid, ref))
            if spec.operation == "transcribe":
                c.execute("INSERT INTO pins VALUES(?,?)", (jid, spec.input.audio_ref))
            c.executemany("INSERT INTO dependencies VALUES(?,?)", [(jid, p) for p in spec.depends_on])
            self.event(c, "submitted", jid, deployment=dep.id, urgent=spec.urgent)
            return self.public_job(c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()), True

    @staticmethod
    def public_job(row):
        return {k: row[k] for k in ("id", "seq", "deployment", "urgent", "created", "deadline", "state",
                "cancel_requested", "error", "result_state", "result_ref", "result_description", "attempt", "ended")}

    def job(self, jid, private=False):
        with contextlib.closing(self.connect()) as c:
            row = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
            if not row:
                raise SchedulerError("job_not_found", 404)
            return dict(row) if private else self.public_job(row)

    def jobs(self, after=0, limit=100):
        if not 0 <= after or not 1 <= limit <= 100:
            raise SchedulerError("invalid_pagination")
        with contextlib.closing(self.connect()) as c:
            return [self.public_job(r) for r in c.execute("SELECT * FROM jobs WHERE seq>? ORDER BY seq LIMIT ?", (after, limit))]

    def spec(self, jid):
        return SubmitJob.model_validate(json.loads(self.artifact(self.job(jid, True)["spec_ref"])[0]))

    def cancel(self, jid):
        with self.tx() as c:
            row = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
            if not row:
                raise SchedulerError("job_not_found", 404)
            if row["state"] == "queued":
                c.execute("UPDATE jobs SET state='canceled',cancel_requested=1,ended=?,reservation=0 WHERE id=?", (self.clock(), jid))
            elif row["state"] in ACTIVE:
                c.execute("UPDATE jobs SET cancel_requested=1 WHERE id=?", (jid,))
            self.event(c, "cancel_requested", jid)
        return self.job(jid)

    def refresh_queue(self, c):
        now = self.clock()
        c.execute("UPDATE jobs SET state='expired',error='queue_deadline',ended=?,reservation=0 WHERE state='queued' AND deadline<=?", (now, now))
        # Dependencies only point backwards to existing jobs, so this converges.
        while True:
            n = c.execute("""UPDATE jobs SET state='dependency_failed',error='dependency_failed',ended=?,reservation=0
                WHERE state='queued' AND id IN (SELECT d.job FROM dependencies d JOIN jobs p ON p.id=d.parent
                WHERE p.state IN ('failed','canceled','expired','dependency_failed'))""", (now,)).rowcount
            if not n:
                break

    def eligible(self, c):
        self.refresh_queue(c)
        return c.execute("""SELECT * FROM jobs j WHERE j.state='queued' AND NOT EXISTS
            (SELECT 1 FROM dependencies d JOIN jobs p ON p.id=d.parent WHERE d.job=j.id
            AND (p.state!='succeeded' OR p.result_state!='available')) ORDER BY j.seq""").fetchall()

    def choose(self, c):
        rows = self.eligible(c)
        if not rows:
            return None
        urgent = [r for r in rows if r["urgent"]]
        if urgent:
            return urgent[0]
        s = self.state(c)
        if (self.clock() - rows[0]["created"] >= self.config.aging_seconds or
                s["reuse_count"] >= self.config.reuse_dispatches):
            return rows[0]
        return next((r for r in rows if r["deployment"] == s["deployment"]), rows[0])

    def next_job(self, epoch):
        with self.tx() as c:
            self.fenced(c, epoch)
            row = self.choose(c)
            return dict(row) if row else None

    def lease_count(self):
        with contextlib.closing(self.connect()) as c:
            return c.execute("SELECT COUNT(*) FROM leases").fetchone()[0]

    def health(self):
        with contextlib.closing(self.connect()) as c:
            s = self.state(c)
            uncertain = c.execute("SELECT COUNT(*) FROM leases WHERE state='unknown'").fetchone()[0]
            inflight = c.execute("SELECT COUNT(*) FROM leases").fetchone()[0]
            detached = c.execute("""SELECT COUNT(*) FROM leases l LEFT JOIN jobs j ON l.id=j.attempt
                WHERE l.state='detached' OR j.cancel_requested=1 OR j.state='draining'""").fetchone()[0]
            return {"ready": s["phase"] == "ready" and not uncertain and self.clock()-s["heartbeat"] <= 10,
                    "worker_epoch": s["worker_epoch"], "inflight": inflight, "detached": detached, "uncertain": uncertain,
                    "phase": s["phase"], "deployment_id": s["deployment"]}

    def legacy_detach(self, rid, owner):
        with self.tx() as c:
            c.execute("UPDATE leases SET state='detached' WHERE id=? AND owner=? AND state='active'", (rid, owner))

    def dispatch(self, epoch, jid):
        with self.tx() as c:
            s = self.fenced(c, epoch)
            row = self.choose(c)
            if not row or row["id"] != jid:
                return None
            dep = self.deployment(row["deployment"], c)
            if s["phase"] != "ready" or s["deployment"] != dep.id:
                return None
            if c.execute("SELECT COUNT(*) FROM leases").fetchone()[0] >= dep.load.capacity:
                return None
            if c.execute("SELECT generations FROM budget WHERE id=1").fetchone()[0] >= self.config.max_generation_attempts:
                c.execute("UPDATE jobs SET state='failed',error='generation_budget_exhausted',ended=?,reservation=0 WHERE id=?", (self.clock(), jid))
                return None
            try:
                self.check_budget_window(c, generations=1)
            except SchedulerError:
                c.execute("UPDATE jobs SET state='failed',error='budget_window_exhausted',ended=?,reservation=0 WHERE id=?", (self.clock(), jid))
                return None
            c.execute("UPDATE budget SET generations=generations+1 WHERE id=1")
            aid, now = uuid.uuid4().hex, self.clock()
            c.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?,NULL,NULL)", (aid, jid, epoch, s["worker_epoch"], dep.id, now))
            c.execute("INSERT INTO leases VALUES(?,?,?,?,?,'active',?)", (aid, "job", dep.id, s["worker_epoch"], str(epoch), now))
            c.execute("UPDATE jobs SET state='dispatching',attempt=? WHERE id=?", (aid, jid))
            c.execute("UPDATE controller SET reuse_count=reuse_count+1 WHERE id=1")
            self._execution_grant(c, aid, epoch, "job", now, row["execution_limit"])
            self.event(c, "dispatch_intent", jid, attempt=aid, owner=epoch, worker_epoch=s["worker_epoch"])
            return dict(c.execute("SELECT * FROM attempts WHERE id=?", (aid,)).fetchone())

    def attempt_state(self, epoch, attempt, state, error=None):
        with self.tx() as c:
            self.fenced(c, epoch)
            row = c.execute("SELECT * FROM attempts WHERE id=? AND owner=?", (attempt, epoch)).fetchone()
            if not row:
                raise SchedulerError("stale_attempt", 409)
            changed = c.execute("UPDATE jobs SET state=?,error=? WHERE attempt=? AND state IN ('dispatching','running','draining')", (state, error, attempt)).rowcount
            if state == "unknown" and changed:
                leased = c.execute("UPDATE leases SET state='unknown' WHERE id=? AND worker_epoch=?", (attempt, row["worker_epoch"])).rowcount
                if leased:
                    c.execute("UPDATE controller SET phase='unknown' WHERE worker_epoch=?", (row["worker_epoch"],))

    def legacy_admit(self, rid, deployment, worker_epoch, owner, execution_limit=None):
        execution_limit = self.config.drain_seconds if execution_limit is None else execution_limit
        if (type(execution_limit) not in (int, float) or not 0 < execution_limit <= self.config.drain_seconds):
            raise SchedulerError("execution_limit_exceeds_drain")
        with self.tx() as c:
            s = self.state(c)
            if (s["phase"] != "ready" or s["deployment"] != deployment or s["worker_epoch"] != worker_epoch or
                    self.clock() - s["heartbeat"] > 10):
                raise SchedulerError("worker_not_ready", 503)
            dep = self.deployment(deployment, c)
            if self.choose(c) or c.execute("SELECT COUNT(*) FROM leases").fetchone()[0] >= dep.load.capacity:
                raise SchedulerError("overloaded", 429)
            if c.execute("SELECT generations FROM budget WHERE id=1").fetchone()[0] >= self.config.max_generation_attempts:
                raise SchedulerError("generation_budget_exhausted", 429)
            self.check_budget_window(c, generations=1)
            c.execute("UPDATE budget SET generations=generations+1 WHERE id=1")
            now = self.clock()
            c.execute("INSERT INTO leases VALUES(?,?,?,?,?,'active',?)", (rid, "legacy", deployment, worker_epoch, owner, now))
            return self._execution_grant(c, rid, owner, "legacy", now, execution_limit)

    def legacy_terminal(self, rid, worker_epoch, owner, terminal):
        with self.tx() as c:
            if terminal:
                c.execute("DELETE FROM leases WHERE id=? AND worker_epoch=? AND owner=?", (rid, worker_epoch, owner))
            else:
                changed = c.execute("UPDATE leases SET state='unknown' WHERE id=? AND worker_epoch=? AND owner=?", (rid, worker_epoch, owner)).rowcount
                if changed:
                    c.execute("UPDATE controller SET phase='unknown' WHERE worker_epoch=?", (worker_epoch,))

    def legacy_restart(self, owner):
        with self.tx() as c:
            if c.execute("SELECT 1 FROM leases WHERE kind='legacy' AND owner!=?", (owner,)).fetchone():
                c.execute("UPDATE leases SET state='unknown' WHERE kind='legacy' AND owner!=?", (owner,))
                c.execute("UPDATE controller SET phase='unknown' WHERE id=1")

    def receipt_path(self, attempt):
        if not re.fullmatch(r"[0-9a-f]{32}", attempt):
            raise SchedulerError("invalid_attempt")
        return self.root / "receipts" / (attempt + ".json")

    def terminal(self, epoch, attempt, result, error=None):
        body = canonical(result)
        if len(body) > self.config.result_bytes:
            raise SchedulerError("result_too_large")
        # Receipt write is fenced while holding the same transaction as terminal.
        # A failed commit leaves the immutable receipt recoverable, with the lease held.
        with self.tx() as c:
            self.fenced(c, epoch)
            a = c.execute("SELECT * FROM attempts WHERE id=? AND owner=?", (attempt, epoch)).fetchone()
            if not a:
                raise SchedulerError("stale_attempt", 409)
            receipt = {"attempt": attempt, "job": a["job"], "worker_epoch": a["worker_epoch"], "owner": epoch,
                       "result": result, "error": error, "terminal_at": self.clock()}
            path = self.receipt_path(attempt)
            if path.exists():
                receipt = json.loads(path.read_bytes())
                if receipt["result"] != result or receipt["error"] != error:
                    raise SchedulerError("receipt_conflict", 409)
            else:
                self.fault("receipt_write")
                durable_write(path, canonical(receipt))
            self.fault("after_receipt")
            self._accept_receipt(c, a, receipt)
            self._execution_receipt(c, attempt, result, error)
        self.publish(epoch, attempt)

    def import_executor_receipt(self, current_epoch, attempt, result, error, grant):
        if len(canonical(result)) > self.config.result_bytes:
            raise SchedulerError("result_too_large")
        with self.tx() as c:
            self.fenced(c, current_epoch)
            saved = self.execution_grant(attempt, c)
            if saved is None or canonical(saved) != canonical(grant):
                raise SchedulerError("execution_grant_mismatch", 409)
            binding = self.execution_binding(c)
            if (saved["attempt"] != attempt or saved["authority"] != binding["control_id"]
                    or saved["executor"] != binding["executor_id"]):
                raise SchedulerError("execution_grant_mismatch", 409)
            if saved["kind"] == "job":
                a = c.execute("SELECT * FROM attempts WHERE id=?", (attempt,)).fetchone()
                job = c.execute("SELECT execution_limit FROM jobs WHERE attempt=?", (attempt,)).fetchone()
                if (not a or not job or any(saved[k] != a[k] for k in ("owner", "deployment", "worker_epoch", "started"))
                        or saved["fence"] != a["owner"] or saved["execution_limit"] != job[0]):
                    raise SchedulerError("execution_grant_mismatch", 409)
                # The current controller may receive an old attempt's terminal
                # proof; retain its original owner in the immutable receipt.
                receipt = {"attempt": attempt, "job": a["job"], "worker_epoch": a["worker_epoch"],
                           "owner": a["owner"], "result": result, "error": error, "terminal_at": self.clock()}
                path = self.receipt_path(attempt)
                if path.exists():
                    receipt = json.loads(path.read_bytes())
                    if receipt["result"] != result or receipt["error"] != error:
                        raise SchedulerError("receipt_conflict", 409)
                else:
                    self.fault("receipt_write")
                    durable_write(path, canonical(receipt))
                self.fault("after_receipt")
                self._accept_receipt(c, a, receipt)
            elif saved["kind"] == "legacy":
                lease = c.execute("SELECT * FROM leases WHERE id=?", (attempt,)).fetchone()
                if lease and any(saved[k] != lease[k] for k in ("kind", "owner", "deployment", "worker_epoch")):
                    raise SchedulerError("execution_grant_mismatch", 409)
                # Legacy delivery may have released this exact lease already.
                # Persist the control receipt marker even in that case, so a
                # repeated reconciliation can safely acknowledge the executor.
                self.fault("receipt_write")
                c.execute("DELETE FROM leases WHERE id=? AND kind='legacy' AND owner=? AND worker_epoch=?",
                          (attempt, saved["owner"], saved["worker_epoch"]))
            else:
                raise SchedulerError("execution_grant_mismatch", 409)
            self._execution_receipt(c, attempt, result, error)
        if saved["kind"] == "job":
            self.publish(current_epoch, attempt)

    def _accept_receipt(self, c, a, receipt):
        if any(receipt[k] != a[k] for k in ("job", "worker_epoch", "owner")) or receipt["attempt"] != a["id"]:
            raise SchedulerError("receipt_identity_mismatch", 409)
        row = c.execute("SELECT * FROM jobs WHERE id=?", (a["job"],)).fetchone()
        if row["result_state"] == "available":
            return
        if a["terminal"] is not None:
            return
        state = "canceled" if row["cancel_requested"] else ("failed" if receipt["error"] or row["error"] == "execution_timeout" else "succeeded")
        c.execute("UPDATE attempts SET terminal=?,error=? WHERE id=?", (receipt["terminal_at"], receipt["error"], a["id"]))
        c.execute("UPDATE jobs SET state=?,result_state='pending',ended=?,error=COALESCE(?,error) WHERE id=?",
                  (state, receipt["terminal_at"], receipt["error"], a["job"]))
        c.execute("DELETE FROM leases WHERE id=?", (a["id"],))
        self.event(c, "compute_terminal", a["job"], attempt=a["id"], state=state)

    def recover_receipts(self, epoch):
        with self.tx() as c:
            self.fenced(c, epoch)
            pending = c.execute("SELECT a.* FROM attempts a JOIN jobs j ON j.id=a.job WHERE j.result_state!='available'").fetchall()
            for a in pending:
                path = self.receipt_path(a["id"])
                if path.exists():
                    receipt = json.loads(path.read_bytes())
                    self._accept_receipt(c, a, receipt)
                    self._execution_receipt(c, a["id"], receipt["result"], receipt["error"])
        for a in pending:
            if self.receipt_path(a["id"]).exists():
                self.publish(epoch, a["id"])

    def publish(self, epoch, attempt):
        try:
            with self.tx() as c:
                self.fenced(c, epoch)
                a = c.execute("SELECT * FROM attempts WHERE id=?", (attempt,)).fetchone()
                if not a or a["terminal"] is None:
                    raise SchedulerError("compute_not_terminal", 409)
                row = c.execute("SELECT * FROM jobs WHERE id=?", (a["job"],)).fetchone()
                if row["result_state"] == "available":
                    return
                receipt = json.loads(self.receipt_path(attempt).read_bytes())
                self.fault("publish")
                ref = self.put(c, canonical(receipt["result"]), "application/json", reserved=True)
                self.fault("publish_commit")
                c.execute("INSERT OR IGNORE INTO pins VALUES(?,?)", (a["job"], ref))
                # Receipt retained until job retention, budget its actual bytes.
                c.execute("UPDATE jobs SET result_state='available',result_ref=?,reservation=? WHERE id=?",
                          (ref, self.receipt_path(attempt).stat().st_size, a["job"]))
                self.event(c, "result_available", a["job"])
        except (OSError, sqlite3.Error):
            with self.tx() as c:
                self.fenced(c, epoch)
                c.execute("UPDATE jobs SET result_state='storage_failed' WHERE attempt=? AND result_state!='available'", (attempt,))
            raise

    def result(self, jid):
        row = self.job(jid)
        if row["result_state"] != "available":
            raise SchedulerError("result_not_available", 409)
        return self.artifact(row["result_ref"])[0]

    def events(self, after=0, limit=100):
        if after < 0 or not 1 <= limit <= 1000:
            raise SchedulerError("invalid_pagination")
        with contextlib.closing(self.connect()) as c:
            return [{**dict(row), "detail": json.loads(row["detail"])} for row in
                    c.execute("SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT ?", (after, limit))]

    def collect(self):
        """Explicit retention: never removes unknown, pending receipts, or pinned inputs."""
        with self.tx() as c:
            cutoff = self.clock() - self.config.retention_seconds
            rows = c.execute("""SELECT * FROM jobs j WHERE ended<? AND state IN ('succeeded','failed','canceled','expired','dependency_failed')
                AND result_state IN ('none','available') AND NOT EXISTS (SELECT 1 FROM dependencies WHERE parent=j.id)""", (cutoff,)).fetchall()
            from .execution_retention import can_collect_attempt
            rows = [row for row in rows if not row["attempt"] or can_collect_attempt(c, row["attempt"])]
            for row in rows:
                if row["attempt"]:
                    c.execute("INSERT OR IGNORE INTO garbage VALUES('receipt',?)", (row["attempt"],))
                c.execute("DELETE FROM pins WHERE owner=?", (row["id"],))
                c.execute("DELETE FROM dependencies WHERE job=?", (row["id"],))
                c.execute("DELETE FROM attempts WHERE job=?", (row["id"],))
                c.execute("DELETE FROM jobs WHERE id=?", (row["id"],))
            stale = c.execute("SELECT ref FROM artifacts WHERE created<? AND NOT EXISTS (SELECT 1 FROM pins WHERE pins.ref=artifacts.ref)", (cutoff,)).fetchall()
            for row in stale:
                c.execute("INSERT OR IGNORE INTO garbage VALUES('artifact',?)", (row[0],))
                c.execute("DELETE FROM artifacts WHERE ref=?", (row[0],))
            for path in (self.root / "artifacts").glob("artifact_*"):
                if path.stat().st_mtime < cutoff and not c.execute("SELECT 1 FROM artifacts WHERE ref=?", (path.name,)).fetchone():
                    c.execute("INSERT OR IGNORE INTO garbage VALUES('artifact',?)", (path.name,))
        self.fault("after_gc_commit")
        self.sweep()
        return {"jobs": len(rows), "artifacts": len(stale)}

    def sweep(self):
        # DB no longer promises these bytes. Recheck under write lock in case an
        # identical content-addressed file has been uploaded since GC committed.
        with self.tx() as c:
            for row in c.execute("SELECT * FROM garbage").fetchall():
                ref = row["ref"]
                if row["kind"] == "artifact":
                    if not c.execute("SELECT 1 FROM artifacts WHERE ref=?", (ref,)).fetchone():
                        self.artifact_path(ref).unlink(missing_ok=True)
                elif not c.execute("SELECT 1 FROM attempts WHERE id=?", (ref,)).fetchone():
                    self.receipt_path(ref).unlink(missing_ok=True)
                c.execute("DELETE FROM garbage WHERE kind=? AND ref=?", (row["kind"], ref))
