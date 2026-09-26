"""Executor-scoped state and atomic assignment over one authoritative Store DB."""
import contextlib
import json
import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from filelock import FileLock, Timeout as LockTimeout

from .scheduler_schema import Deployment, SchedulerError, canonical
from .scheduler_store import Store


def check_admission(c):
    """Called inside the same transaction as a new public admission."""
    if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fleet_settings'").fetchone():
        row = c.execute("SELECT enabled,maintenance FROM fleet_settings WHERE id=1").fetchone()
        if row and row["enabled"] and row["maintenance"] != "open":
            raise SchedulerError("maintenance_admission_closed", 503)


def check_fleet_budget_idle(c):
    if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fleet_settings'").fetchone():
        enabled = c.execute("SELECT enabled FROM fleet_settings WHERE id=1").fetchone()
        if enabled and enabled[0] and c.execute(
                "SELECT 1 FROM executor_states WHERE phase NOT IN ('unloaded','ready')").fetchone():
            raise SchedulerError("budget_window_requires_idle", 409)


class Fleet:
    def __init__(self, store):
        self.store = store
        with contextlib.closing(store.connect()) as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS fleet_settings(id INTEGER PRIMARY KEY CHECK(id=1),
                    enabled INTEGER NOT NULL, maintenance TEXT NOT NULL, primary_executor TEXT);
                INSERT OR IGNORE INTO fleet_settings(id,enabled,maintenance) VALUES(1,0,'open');
                CREATE TABLE IF NOT EXISTS executor_bindings(executor TEXT PRIMARY KEY, control_id TEXT NOT NULL,
                    url TEXT NOT NULL, key_file TEXT NOT NULL, resource_id TEXT, kind TEXT,
                    catalog TEXT NOT NULL DEFAULT '[]', available INTEGER NOT NULL DEFAULT 0,
                    seen REAL, maintenance TEXT NOT NULL DEFAULT 'open');
                CREATE UNIQUE INDEX IF NOT EXISTS executor_resource ON executor_bindings(resource_id)
                    WHERE resource_id IS NOT NULL;
                CREATE TABLE IF NOT EXISTS executor_states(executor TEXT PRIMARY KEY, epoch INTEGER NOT NULL,
                    phase TEXT NOT NULL, deployment TEXT, worker_epoch TEXT, handle TEXT,
                    reuse_count INTEGER NOT NULL, changed REAL NOT NULL, heartbeat REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS job_assignments(job TEXT PRIMARY KEY, executor TEXT NOT NULL,
                    reason TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS executor_blocked(executor TEXT NOT NULL, deployment TEXT NOT NULL,
                    reason TEXT NOT NULL, PRIMARY KEY(executor,deployment));
            ''')
            if "primary_executor" not in {r[1] for r in c.execute("PRAGMA table_info(fleet_settings)")}:
                c.execute("ALTER TABLE fleet_settings ADD COLUMN primary_executor TEXT")

    def enabled(self, c=None):
        if c is None:
            with contextlib.closing(self.store.connect()) as db:
                return self.enabled(db)
        return bool(c.execute("SELECT enabled FROM fleet_settings WHERE id=1").fetchone()[0])

    @contextlib.contextmanager
    def _offline(self):
        try:
            with contextlib.ExitStack() as stack:
                for name in ("fleet-admin.lock", "api.lock", "controller.lock"):
                    stack.enter_context(FileLock(str(self.store.root / name), timeout=0))
                with contextlib.closing(self.store.connect()) as c:
                    ids = [row[0] for row in c.execute("SELECT executor FROM executor_bindings ORDER BY executor")]
                for executor in ids:
                    stack.enter_context(FileLock(str(self.store.root / f"controller-{executor}.lock"), timeout=0))
                yield
        except LockTimeout:
            raise SchedulerError("fleet_registration_requires_offline", 409) from None

    def enable(self):
        with self._offline(), self.store.tx() as c:
            if self.enabled(c):
                return
            old_state = self.store.state(c)
            if (old_state["phase"] != "unloaded" or c.execute("SELECT 1 FROM leases").fetchone()
                    or c.execute("SELECT 1 FROM pins WHERE owner='model'").fetchone()
                    or c.execute("SELECT 1 FROM engine_cleanup").fetchone()):
                raise SchedulerError("fleet_migration_requires_unloaded", 409)
            if c.execute("SELECT 1 FROM jobs j WHERE j.attempt IS NOT NULL AND j.result_state!='available' "
                         "AND NOT EXISTS (SELECT 1 FROM execution_grants g WHERE g.attempt=j.attempt)").fetchone():
                raise SchedulerError("fleet_migration_pending_local_receipt", 409)
            binding = self.store.execution_binding(c)
            if binding:
                executor = binding["executor_id"]
                c.execute("INSERT INTO executor_bindings(executor,control_id,url,key_file) VALUES(?,?,?,?)",
                          (executor, binding["control_id"], binding["url"], binding["key_file"]))
                c.execute("INSERT INTO executor_states VALUES(?,?,?,?,?,?,?,?,?)", (executor, old_state["epoch"],
                    "unloaded", None, None, None, 0, old_state["changed"], old_state["heartbeat"]))
                c.execute("INSERT OR IGNORE INTO job_assignments SELECT a.job,?,'single_executor_migration' "
                          "FROM attempts a JOIN execution_grants g ON g.attempt=a.id "
                          "WHERE json_extract(g.envelope,'$.executor')=?", (executor, executor))
                c.execute("INSERT OR IGNORE INTO executor_blocked SELECT ?,id,reason FROM blocked_deployments", (executor,))
                # The scoped table now owns these failures; other executors can
                # run the same deployment without inheriting a host-local fault.
                c.execute("DELETE FROM blocked_deployments")
                c.execute("UPDATE fleet_settings SET primary_executor=? WHERE id=1", (executor,))
            c.execute("UPDATE fleet_settings SET enabled=1 WHERE id=1")
            self.store.event(c, "fleet_enabled")

    def add_executor(self, control_id, executor, url, key_file):
        if any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", v) for v in (control_id, executor)):
            raise SchedulerError("invalid_execution_identity")
        try:
            parsed = urlsplit(url)
            parsed.port
            valid = (parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username
                     and not parsed.password and not parsed.query and not parsed.fragment and parsed.path in ("", "/"))
        except (ValueError, TypeError):
            valid = False
        if not valid or not isinstance(key_file, (str, Path)) or not str(key_file):
            raise SchedulerError("invalid_execution_binding")
        value = (control_id, url.rstrip("/"), str(Path(key_file).resolve()))
        with self._offline(), self.store.tx() as c:
            if not self.enabled(c):
                raise SchedulerError("fleet_not_enabled", 409)
            old = c.execute("SELECT control_id,url,key_file FROM executor_bindings WHERE executor=?", (executor,)).fetchone()
            if old and tuple(old) != value:
                raise SchedulerError("execution_binding_conflict", 409)
            c.execute("INSERT OR IGNORE INTO executor_bindings(executor,control_id,url,key_file) VALUES(?,?,?,?)", (executor, *value))
            c.execute("INSERT OR IGNORE INTO executor_states VALUES(?,0,'unloaded',NULL,NULL,NULL,0,0,0)", (executor,))
            c.execute("UPDATE fleet_settings SET primary_executor=? WHERE id=1 AND primary_executor IS NULL", (executor,))
        return self.worker(executor)

    def verify_executor(self, executor, inventory):
        if (not isinstance(inventory, dict) or inventory.get("version") != 1
                or inventory.get("executor") != executor
                or not isinstance(inventory.get("resource_id"), str) or not inventory["resource_id"]
                or len(inventory["resource_id"]) > 256 or inventory.get("kind") not in ("cpu", "gpu")
                or not isinstance(inventory.get("deployments"), list)):
            raise SchedulerError("executor_inventory_invalid", 409)
        try:
            deployments = [Deployment.model_validate(dep) for dep in inventory["deployments"]]
        except ValueError:
            raise SchedulerError("executor_inventory_invalid", 409) from None
        if len(deployments) > 1000 or len({dep.id for dep in deployments}) != len(deployments):
            raise SchedulerError("executor_inventory_invalid", 409)
        with self.store.tx() as c:
            binding = self._binding(c, executor)
            if inventory.get("authority") != binding["control_id"]:
                raise SchedulerError("executor_identity_mismatch", 409)
            if binding["resource_id"] and binding["resource_id"] != inventory["resource_id"]:
                raise SchedulerError("executor_resource_changed", 409)
            if c.execute("SELECT 1 FROM executor_bindings WHERE resource_id=? AND executor!=?",
                         (inventory["resource_id"], executor)).fetchone():
                raise SchedulerError("executor_resource_duplicate", 409)
            # Only exact registered deployment contracts can enter the catalog.
            for dep in deployments:
                if self.store.deployment(dep.id, c) != dep:
                    raise SchedulerError("deployment_mismatch", 409)
            catalog = canonical(sorted(d.id for d in deployments)).decode()
            changed = (binding["resource_id"], binding["kind"], binding["catalog"], binding["available"]) != (inventory["resource_id"], inventory["kind"], catalog, 1)
            c.execute("UPDATE executor_bindings SET resource_id=?,kind=?,catalog=?,available=1,seen=? WHERE executor=?",
                      (inventory["resource_id"], inventory["kind"], catalog,
                       self.store.clock(), executor))
            self._release_invalid(c)
            if changed:
                self.store.event(c, "executor_inventory_verified", executor, deployments=sorted(d.id for d in deployments))
        return self.view(executor)

    def _binding(self, c, executor):
        row = c.execute("SELECT * FROM executor_bindings WHERE executor=?", (executor,)).fetchone()
        if row is None:
            raise SchedulerError("executor_not_found", 404)
        return row

    def mark_available(self, executor, available):
        if type(available) is not bool:
            raise SchedulerError("invalid_availability")
        with self.store.tx() as c:
            old = self._binding(c, executor)
            c.execute("UPDATE executor_bindings SET available=?,seen=CASE WHEN ? THEN ? ELSE seen END WHERE executor=?",
                      (int(available), int(available), self.store.clock(), executor))
            self._release_invalid(c)
            if bool(old["available"]) != available:
                self.store.event(c, "executor_availability_changed", executor, available=available)

    def workers(self):
        with contextlib.closing(self.store.connect()) as c:
            ids = [r[0] for r in c.execute("SELECT executor FROM executor_bindings ORDER BY executor")]
        return [ExecutorStore(self, executor) for executor in ids]

    def worker(self, executor):
        with contextlib.closing(self.store.connect()) as c:
            self._binding(c, executor)
        return ExecutorStore(self, executor)

    def primary(self):
        with contextlib.closing(self.store.connect()) as c:
            executor = c.execute("SELECT primary_executor FROM fleet_settings WHERE id=1").fetchone()[0]
        if executor is None:
            raise SchedulerError("primary_executor_not_configured", 409)
        return self.worker(executor)

    def views(self):
        return [self.view(worker.executor) for worker in self.workers()]

    def view(self, executor):
        with contextlib.closing(self.store.connect()) as c:
            b = self._binding(c, executor)
            state = dict(c.execute("SELECT * FROM executor_states WHERE executor=?", (executor,)).fetchone())
            settings = c.execute("SELECT * FROM fleet_settings WHERE id=1").fetchone()
            global_mode = settings["maintenance"]
            queued = c.execute("SELECT COUNT(*) FROM job_assignments a JOIN jobs j ON a.job=j.id "
                               "WHERE a.executor=? AND j.state='queued' AND j.attempt IS NULL", (executor,)).fetchone()[0]
        return {"executor": executor, "resource_id": b["resource_id"], "kind": b["kind"],
                "available": bool(b["available"]), "seen": b["seen"], "maintenance": b["maintenance"],
                "global_maintenance": global_mode, "deployments": json.loads(b["catalog"]),
                "primary": settings["primary_executor"] == executor,
                "queued_assigned": queued, **state, **self.worker(executor).health()}

    def explain(self, job_id):
        """Read-only explanation; never plans, refreshes deadlines or reassigns."""
        with contextlib.closing(self.store.connect()) as c:
            job = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job is None:
                raise SchedulerError("job_not_found", 404)
            assignment = c.execute("SELECT executor,reason FROM job_assignments WHERE job=?", (job_id,)).fetchone()
            if job["attempt"]:
                grant = self.store.execution_grant(job["attempt"], c)
                executor = grant["executor"] if grant else (assignment["executor"] if assignment else None)
                reason = "execution_unconfirmed" if job["state"] == "unknown" else job["state"]
                if job["result_state"] in ("pending", "storage_failed"):
                    reason = "result_storage_pending"
                return {"executor": executor, "reason": reason}
            if job["state"] != "queued":
                return {"executor": assignment["executor"] if assignment else None, "reason": job["state"]}
            if assignment:
                return dict(assignment)
            if c.execute("SELECT maintenance FROM fleet_settings WHERE id=1").fetchone()[0] == "pause":
                return {"executor": None, "reason": "global_pause"}
            if c.execute("SELECT 1 FROM dependencies d JOIN jobs p ON p.id=d.parent WHERE d.job=? "
                         "AND (p.state!='succeeded' OR p.result_state!='available')", (job_id,)).fetchone():
                return {"executor": None, "reason": "dependency_waiting"}
            if self.store.clock() >= job["deadline"]:
                return {"executor": None, "reason": "queue_deadline"}
            matching = [b for b in c.execute("SELECT * FROM executor_bindings") if job["deployment"] in json.loads(b["catalog"])]
            if not matching:
                reason = "not_in_verified_catalog"
            elif not any(b["available"] for b in matching):
                reason = "executor_offline"
            elif not any(b["available"] and b["maintenance"] not in ("pause", "drain") for b in matching):
                reason = "worker_maintenance"
            elif self.best_worker(c, job):
                reason = "awaiting_assignment"
            else:
                reason = "capacity_or_lifecycle"
            return {"executor": None, "reason": reason}

    def maintenance(self, mode, executor=None):
        if mode not in ("open", "pause", "drain", "admissions"):
            raise SchedulerError("invalid_maintenance_mode")
        with self.store.tx() as c:
            if not self.enabled(c):
                raise SchedulerError("fleet_not_enabled", 409)
            if executor is None:
                c.execute("UPDATE fleet_settings SET maintenance=? WHERE id=1", (mode,))
            else:
                self._binding(c, executor)
                c.execute("UPDATE executor_bindings SET maintenance=? WHERE executor=?", (mode, executor))
            self._release_invalid(c)
            self.store.event(c, "maintenance_changed", executor, mode=mode)
        return self.views() if executor is None else self.view(executor)

    def _release_invalid(self, c):
        c.execute("DELETE FROM job_assignments WHERE NOT EXISTS (SELECT 1 FROM jobs WHERE jobs.id=job_assignments.job)")
        rows = c.execute("SELECT a.job,a.executor,j.deployment FROM job_assignments a JOIN jobs j ON j.id=a.job "
                         "WHERE j.state='queued' AND j.attempt IS NULL").fetchall()
        for row in rows:
            binding = self._binding(c, row["executor"])
            state = c.execute("SELECT phase,heartbeat FROM executor_states WHERE executor=?", (row["executor"],)).fetchone()
            if (not binding["available"] or binding["maintenance"] in ("pause", "drain")
                    or state["phase"] == "unknown"
                    or (state["phase"] == "ready" and self.store.clock() - state["heartbeat"] > 10)
                    or row["deployment"] not in json.loads(binding["catalog"])):
                c.execute("DELETE FROM job_assignments WHERE job=?", (row["job"],))
                self.store.event(c, "queued_assignment_released", row["job"], executor=row["executor"])

    def best_worker(self, c, job, *, replacing=None):
        candidates = []
        for b in c.execute("SELECT * FROM executor_bindings ORDER BY executor"):
            if not b["available"] or b["maintenance"] in ("pause", "drain") or job["deployment"] not in json.loads(b["catalog"]):
                continue
            s = c.execute("SELECT * FROM executor_states WHERE executor=?", (b["executor"],)).fetchone()
            if s["phase"] not in ("ready", "unloaded"):
                continue
            if c.execute("SELECT 1 FROM executor_blocked WHERE executor=? AND deployment=?", (b["executor"], job["deployment"])).fetchone():
                continue
            worker = ExecutorStore(self, b["executor"])
            leases = worker._lease_count(c)
            planned = c.execute("SELECT COUNT(*) FROM job_assignments a JOIN jobs j ON a.job=j.id "
                                "WHERE a.executor=? AND j.state='queued' AND j.attempt IS NULL", (b["executor"],)).fetchone()[0]
            if b["executor"] == replacing:
                planned = 0  # Reevaluate queued plans at a safe priority boundary.
            limit = self.store.deployment(s["deployment"], c).load.capacity if s["phase"] == "ready" else 1
            if leases + planned >= limit:
                continue
            if s["phase"] == "ready" and self.store.clock() - s["heartbeat"] > 10:
                continue
            affinity = s["phase"] == "ready" and s["deployment"] == job["deployment"]
            candidates.append((not affinity, b["executor"]))
        if not candidates:
            return None
        affinity, executor = min(candidates)
        return executor, "ready_deployment_affinity" if not affinity else "available_executor_id"


class ExecutorStore(Store):
    def __init__(self, fleet, executor):
        self.__dict__.update(fleet.store.__dict__)
        self.fleet, self.executor = fleet, executor
        self.controller_lock_name = f"controller-{executor}.lock"
        self.model_pin = "model:" + executor

    def execution_binding(self, c=None):
        if c is None:
            with contextlib.closing(self.connect()) as db:
                return self.execution_binding(db)
        row = self.fleet._binding(c, self.executor)
        return {"control_id": row["control_id"], "executor_id": self.executor, "url": row["url"], "key_file": row["key_file"]}

    def execution_grant(self, attempt, c=None):
        grant = super().execution_grant(attempt, c)
        return grant if grant and grant["executor"] == self.executor else None

    def execution_pending(self):
        return [grant for grant in super().execution_pending() if grant["executor"] == self.executor]

    def execution_ack(self, attempt):
        if self.execution_grant(attempt) is None:
            raise SchedulerError("execution_grant_mismatch", 409)
        return super().execution_ack(attempt)

    def state(self, c=None):
        if c is None:
            with contextlib.closing(self.connect()) as db:
                return self.state(db)
        return dict(c.execute("SELECT * FROM executor_states WHERE executor=?", (self.executor,)).fetchone())

    def _lease_ids(self, c):
        return [r[0] for r in c.execute("SELECT l.id FROM leases l JOIN execution_grants g ON g.attempt=l.id "
            "WHERE json_extract(g.envelope,'$.executor')=?", (self.executor,))]

    def _lease_count(self, c):
        return len(self._lease_ids(c))

    def lease_count(self):
        with contextlib.closing(self.connect()) as c:
            return self._lease_count(c)

    def _owned_attempt(self, c, attempt):
        if self.execution_grant(attempt, c) is None:
            raise SchedulerError("execution_grant_mismatch", 409)

    def acquire_controller(self):
        with self.tx() as c:
            s = self.state(c)
            epoch = s["epoch"] + 1
            if s["phase"] == "loading" and s["handle"] is None and not self._lease_count(c):
                c.execute("INSERT OR REPLACE INTO executor_blocked VALUES(?,?,'preparation_interrupted')", (self.executor, s["deployment"]))
                c.execute("UPDATE jobs SET state='failed',error='preparation_interrupted',ended=?,reservation=0 "
                          "WHERE state='queued' AND id IN (SELECT job FROM job_assignments WHERE executor=?)",
                          (self.clock(), self.executor))
                c.execute("DELETE FROM pins WHERE owner=?", (self.model_pin,))
                c.execute("UPDATE executor_states SET phase='unloaded',deployment=NULL,worker_epoch=NULL,handle=NULL WHERE executor=?", (self.executor,))
                s = self.state(c)
            phase = "unloaded" if s["phase"] == "unloaded" else "unknown"
            c.execute("UPDATE executor_states SET epoch=?,phase=?,heartbeat=? WHERE executor=?", (epoch, phase, self.clock(), self.executor))
            for attempt in self._lease_ids(c):
                c.execute("UPDATE jobs SET state='unknown',error='controller_restarted' WHERE attempt=? AND state IN ('dispatching','running','draining')", (attempt,))
                c.execute("UPDATE leases SET state='unknown' WHERE id=?", (attempt,))
            self.event(c, "controller_acquired", self.executor, epoch=epoch)
            self.fleet._release_invalid(c)
            return epoch

    def heartbeat(self, epoch):
        with self.tx() as c:
            self.fenced(c, epoch)
            c.execute("UPDATE executor_states SET heartbeat=? WHERE executor=?", (self.clock(), self.executor))

    def phase(self, epoch, phase, *, deployment=None, worker_epoch=None, handle=None):
        with self.tx() as c:
            s = self.fenced(c, epoch)
            if phase in ("loading", "unloading") and self._lease_count(c):
                raise SchedulerError("leases_unresolved", 409)
            dep = deployment if deployment is not None else s["deployment"]
            c.execute("UPDATE executor_states SET phase=?,deployment=?,worker_epoch=?,handle=?,changed=? WHERE executor=?",
                (phase, dep, worker_epoch or s["worker_epoch"], handle or s["handle"], self.clock(), self.executor))
            if dep:
                c.execute("INSERT OR IGNORE INTO pins VALUES(?,?)", (self.model_pin, self.deployment(dep, c).asset_ref))
            self.event(c, "model_phase", dep, executor=self.executor, phase=phase)

    def engine_exited(self, epoch):
        with self.tx() as c:
            state = self.fenced(c, epoch)
            if state["handle"]:
                c.execute("INSERT OR IGNORE INTO engine_cleanup VALUES(?,?)", (state["handle"], canonical(state).decode()))
            for attempt in self._lease_ids(c):
                c.execute("UPDATE jobs SET state='unknown',error='engine_exited_unknown' WHERE attempt=? AND state IN ('dispatching','running','draining')", (attempt,))
                c.execute("DELETE FROM leases WHERE id=?", (attempt,))
            c.execute("DELETE FROM pins WHERE owner=?", (self.model_pin,))
            c.execute("UPDATE executor_states SET phase='unloaded',deployment=NULL,worker_epoch=NULL,handle=NULL,reuse_count=0,changed=? WHERE executor=?", (self.clock(), self.executor))
            self.event(c, "engine_exit_confirmed", self.executor)

    def cleanup_pending(self):
        return [state for state in super().cleanup_pending() if state.get("executor") == self.executor]

    def cleanup_done(self, epoch, handle):
        with self.tx() as c:
            self.fenced(c, epoch)
            row = c.execute("SELECT state FROM engine_cleanup WHERE handle=?", (handle,)).fetchone()
            if row and json.loads(row[0]).get("executor") != self.executor:
                raise SchedulerError("execution_grant_mismatch", 409)
            c.execute("DELETE FROM engine_cleanup WHERE handle=?", (handle,))

    def preparation_failed(self, epoch, jid, error="asset_preparation_failed"):
        with self.tx() as c:
            s = self.fenced(c, epoch)
            if s["phase"] != "loading" or s["handle"] is not None or self._lease_count(c):
                raise SchedulerError("external_load_intent_exists", 409)
            c.execute("UPDATE jobs SET state='failed',error=?,ended=?,reservation=0 WHERE id=? AND state='queued' "
                      "AND id IN (SELECT job FROM job_assignments WHERE executor=?)", (error, self.clock(), jid, self.executor))
            c.execute("DELETE FROM pins WHERE owner=?", (self.model_pin,))
            c.execute("UPDATE executor_states SET phase='unloaded',deployment=NULL,worker_epoch=NULL,handle=NULL WHERE executor=?", (self.executor,))
            self.event(c, "asset_preparation_failed", jid, executor=self.executor)

    def lifecycle_failed(self, epoch, deployment):
        with self.tx() as c:
            self.fenced(c, epoch)
            c.execute("INSERT OR REPLACE INTO executor_blocked VALUES(?,?,'lifecycle_failed')", (self.executor, deployment))
            c.execute("UPDATE jobs SET state='failed',error='lifecycle_failed',ended=?,reservation=0 WHERE deployment=? "
                      "AND state='queued' AND id IN (SELECT job FROM job_assignments WHERE executor=?)",
                      (self.clock(), deployment, self.executor))
            self.event(c, "deployment_blocked", deployment, executor=self.executor)

    def resume_deployment(self, ref):
        with self.tx() as c:
            if self.state(c)["phase"] != "unloaded" or self._lease_count(c):
                raise SchedulerError("engine_exit_unconfirmed", 409)
            c.execute("DELETE FROM executor_blocked WHERE executor=? AND deployment=?", (self.executor, ref))
            self.event(c, "operator_resumed_deployment", ref, executor=self.executor)

    def choose(self, c):
        if c.execute("SELECT maintenance FROM fleet_settings WHERE id=1").fetchone()[0] == "pause":
            return None
        binding = self.fleet._binding(c, self.executor)
        if not binding["available"] or binding["maintenance"] in ("pause", "drain"):
            return None
        self.fleet._release_invalid(c)
        catalog = set(json.loads(binding["catalog"]))
        rows = [r for r in self.eligible(c) if r["deployment"] in catalog and not c.execute(
            "SELECT 1 FROM executor_blocked WHERE executor=? AND deployment=?", (self.executor, r["deployment"])).fetchone()]
        state = self.state(c)
        if state["phase"] == "unknown":
            return None
        rows = [r for r in rows if not (assignment := c.execute("SELECT executor FROM job_assignments WHERE job=?", (r["id"],)).fetchone()) or assignment[0] == self.executor]
        if not rows:
            return None
        urgent = [r for r in rows if r["urgent"]]
        normal = [r for r in rows if not r["urgent"]]
        if normal and self.clock() - normal[0]["created"] < self.config.aging_seconds and state["reuse_count"] < self.config.reuse_dispatches:
            normal.sort(key=lambda row: row["deployment"] != state["deployment"])
        for row in urgent + normal:
            assignment = c.execute("SELECT executor FROM job_assignments WHERE job=?", (row["id"],)).fetchone()
            if assignment:
                return row
            best = self.fleet.best_worker(c, row, replacing=self.executor)
            if best and best[0] == self.executor:
                # A planned queued job has no execution entitlement. A newly
                # eligible higher-priority job may replace that plan before load.
                c.execute("DELETE FROM job_assignments WHERE executor=? AND job IN "
                          "(SELECT id FROM jobs WHERE state='queued' AND attempt IS NULL)", (self.executor,))
                c.execute("INSERT INTO job_assignments VALUES(?,?,?)", (row["id"], self.executor, best[1]))
                self.event(c, "job_assigned", row["id"], executor=self.executor, reason=best[1])
                return row
        return None

    def next_job(self, epoch):
        with self.tx() as c:
            self.fenced(c, epoch)
            row = self.choose(c)
            return dict(row) if row else None

    def dispatch(self, epoch, jid):
        with self.tx() as c:
            state = self.fenced(c, epoch)
            row = self.choose(c)
            if row is None or row["id"] != jid or state["phase"] != "ready" or state["deployment"] != row["deployment"]:
                return None
            if self.clock() - state["heartbeat"] > 10 or self._lease_count(c) >= self.deployment(row["deployment"], c).load.capacity:
                return None
            error = None
            if c.execute("SELECT generations FROM budget WHERE id=1").fetchone()[0] >= self.config.max_generation_attempts:
                error = "generation_budget_exhausted"
            else:
                try: self.check_budget_window(c, generations=1)
                except SchedulerError: error = "budget_window_exhausted"
            if error:
                c.execute("UPDATE jobs SET state='failed',error=?,ended=?,reservation=0 WHERE id=?", (error, self.clock(), jid))
                return None
            c.execute("UPDATE budget SET generations=generations+1 WHERE id=1")
            attempt, now = uuid.uuid4().hex, self.clock()
            c.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?,NULL,NULL)", (attempt, jid, epoch, state["worker_epoch"], row["deployment"], now))
            c.execute("INSERT INTO leases VALUES(?,?,?,?,?,'active',?)", (attempt, "job", row["deployment"], state["worker_epoch"], str(epoch), now))
            c.execute("UPDATE jobs SET state='dispatching',attempt=? WHERE id=?", (attempt, jid))
            c.execute("UPDATE executor_states SET reuse_count=reuse_count+1 WHERE executor=?", (self.executor,))
            self._execution_grant(c, attempt, epoch, "job", now, row["execution_limit"])
            self.event(c, "dispatch_intent", jid, attempt=attempt, owner=epoch, worker_epoch=state["worker_epoch"], executor=self.executor)
            return dict(c.execute("SELECT * FROM attempts WHERE id=?", (attempt,)).fetchone())

    def health(self):
        with contextlib.closing(self.connect()) as c:
            state = self.state(c)
            rows = c.execute("SELECT l.state,j.cancel_requested,j.state AS job_state FROM leases l "
                "JOIN execution_grants g ON g.attempt=l.id LEFT JOIN jobs j ON j.attempt=l.id "
                "WHERE json_extract(g.envelope,'$.executor')=?", (self.executor,)).fetchall()
            binding = self.fleet._binding(c, self.executor)
            mode = c.execute("SELECT maintenance FROM fleet_settings WHERE id=1").fetchone()[0]
            uncertain = sum(row["state"] == "unknown" for row in rows)
            ready = (state["phase"] == "ready" and not uncertain and self.clock() - state["heartbeat"] <= 10
                     and binding["available"] and binding["maintenance"] == "open" and mode == "open")
            return {"ready": bool(ready), "worker_epoch": state["worker_epoch"], "phase": state["phase"],
                    "deployment_id": state["deployment"], "inflight": len(rows), "uncertain": uncertain,
                    "detached": sum(row["state"] == "detached" or bool(row["cancel_requested"]) or row["job_state"] == "draining" for row in rows)}

    def attempt_state(self, epoch, attempt, state, error=None):
        with self.tx() as c:
            self.fenced(c, epoch)
            self._owned_attempt(c, attempt)
            row = c.execute("SELECT * FROM attempts WHERE id=? AND owner=?", (attempt, epoch)).fetchone()
            if row is None:
                raise SchedulerError("stale_attempt", 409)
            changed = c.execute("UPDATE jobs SET state=?,error=? WHERE attempt=? AND state IN ('dispatching','running','draining')", (state, error, attempt)).rowcount
            if state == "unknown" and changed:
                if c.execute("UPDATE leases SET state='unknown' WHERE id=?", (attempt,)).rowcount:
                    c.execute("UPDATE executor_states SET phase='unknown' WHERE executor=? AND worker_epoch=?", (self.executor, row["worker_epoch"]))

    def legacy_admit(self, rid, deployment, worker_epoch, owner, execution_limit=None):
        execution_limit = self.config.drain_seconds if execution_limit is None else execution_limit
        if type(execution_limit) not in (int, float) or not 0 < execution_limit <= self.config.drain_seconds:
            raise SchedulerError("execution_limit_exceeds_drain")
        with self.tx() as c:
            check_admission(c)
            binding = self.fleet._binding(c, self.executor)
            state = self.state(c)
            if binding["maintenance"] != "open" or not binding["available"]:
                raise SchedulerError("maintenance_admission_closed", 503)
            if (deployment not in json.loads(binding["catalog"]) or state["phase"] != "ready" or state["deployment"] != deployment or state["worker_epoch"] != worker_epoch
                    or self.clock() - state["heartbeat"] > 10):
                raise SchedulerError("worker_not_ready", 503)
            if self.choose(c) or self._lease_count(c) >= self.deployment(deployment, c).load.capacity:
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
            if self.execution_grant(rid, c) is None:
                raise SchedulerError("execution_grant_mismatch", 409)
            if terminal:
                c.execute("DELETE FROM leases WHERE id=? AND worker_epoch=? AND owner=?", (rid, worker_epoch, owner))
            elif c.execute("UPDATE leases SET state='unknown' WHERE id=? AND worker_epoch=? AND owner=?", (rid, worker_epoch, owner)).rowcount:
                c.execute("UPDATE executor_states SET phase='unknown' WHERE executor=? AND worker_epoch=?", (self.executor, worker_epoch))

    def legacy_detach(self, rid, owner):
        if self.execution_grant(rid) is None:
            raise SchedulerError("execution_grant_mismatch", 409)
        return super().legacy_detach(rid, owner)

    def legacy_restart(self, owner):
        with self.tx() as c:
            changed = False
            for attempt in self._lease_ids(c):
                changed |= bool(c.execute("UPDATE leases SET state='unknown' WHERE id=? AND kind='legacy' AND owner!=?", (attempt, owner)).rowcount)
            if changed:
                c.execute("UPDATE executor_states SET phase='unknown' WHERE executor=?", (self.executor,))

    def terminal(self, epoch, attempt, result, error=None):
        # Immutable grants cannot change between this check and the parent transaction.
        with contextlib.closing(self.connect()) as c:
            self._owned_attempt(c, attempt)
        return super().terminal(epoch, attempt, result, error)

    def _accept_receipt(self, c, attempt, receipt):
        self._owned_attempt(c, attempt["id"])
        return super()._accept_receipt(c, attempt, receipt)

    def publish(self, epoch, attempt):
        with contextlib.closing(self.connect()) as c:
            self._owned_attempt(c, attempt)
        return super().publish(epoch, attempt)

    def recover_receipts(self, epoch):
        with self.tx() as c:
            self.fenced(c, epoch)
            rows = c.execute("SELECT a.* FROM attempts a JOIN jobs j ON j.id=a.job "
                "JOIN execution_grants g ON g.attempt=a.id WHERE j.result_state!='available' "
                "AND json_extract(g.envelope,'$.executor')=?", (self.executor,)).fetchall()
            for attempt in rows:
                path = self.receipt_path(attempt["id"])
                if path.exists():
                    receipt = json.loads(path.read_bytes())
                    self._accept_receipt(c, attempt, receipt)
                    self._execution_receipt(c, attempt["id"], receipt["result"], receipt["error"])
        for attempt in rows:
            if self.receipt_path(attempt["id"]).exists():
                self.publish(epoch, attempt["id"])

    def should_drain(self):
        with contextlib.closing(self.connect()) as c:
            binding = self.fleet._binding(c, self.executor)
            mode = c.execute("SELECT maintenance FROM fleet_settings WHERE id=1").fetchone()[0]
            return (binding["maintenance"] == "drain" or (mode == "drain" and not c.execute(
                "SELECT 1 FROM jobs WHERE state='queued'").fetchone()))
