"""Control SQLite remains authoritative across a separate executor boundary."""
import asyncio
import contextlib
import json
import shutil
import sqlite3

import pytest
from filelock import FileLock

from selfhost_models.scheduler_controller import Controller
from selfhost_models.scheduler_schema import SchedulerError, digest
from selfhost_models.scheduler_store import Store
from test_scheduler import FakeProvider, ready, setup_store, submit, tick


def bind(store):
    return store.bind_execution("control-one", "executor-one", "http://127.0.0.1:19000", store.root / "executor-key")


def test_bind_requires_offline_idle_and_preserves_existing_binding(tmp_path):
    s, q, _, _ = setup_store(tmp_path)
    for name in ("api.lock", "controller.lock"):
        with FileLock(str(s.root / name)):
            with pytest.raises(SchedulerError, match="requires_offline"):
                bind(s)
    binding = bind(s)
    assert bind(s) == binding == Store(s.root).execution_binding()
    with pytest.raises(SchedulerError, match="binding_conflict"):
        s.bind_execution("different-control", "executor-one", binding["url"], binding["key_file"])
    ready(s, q)
    with pytest.raises(SchedulerError, match="requires_idle"):
        bind(s)


def test_imported_manifest_has_no_local_path_and_registration_does_not_read_weights(tmp_path, monkeypatch):
    s, q, _, _ = setup_store(tmp_path)
    manifest = s.asset_manifest(q.asset_ref)
    with pytest.raises(SchedulerError, match="binding_required"):
        s.assets_import(manifest)
    bind(s)
    # Importing the same manifest does not rewrite a pre-existing local path.
    s.assets_import(manifest)
    assert s.asset_verify(q.asset_ref) == tmp_path / "asset"
    remote = {**manifest, "remote.safetensors": {"size": 4, "sha256": "a" * 64}}
    ref = s.assets_import(remote)
    assert ref == "asset_" + digest(remote)
    with contextlib.closing(s.connect()) as db:
        assert db.execute("SELECT path FROM assets WHERE ref=?", (ref,)).fetchone()[0] == ""
    with pytest.raises(SchedulerError, match="requires_executor"):
        s.asset_verify(ref)
    monkeypatch.setattr(s, "asset_verify", lambda _: pytest.fail("control must not read executor weights"))
    deployment = q.model_copy(update={"asset_ref": ref})
    assert s.register(deployment) == deployment.id
    with s.tx() as db:
        db.execute("UPDATE assets SET manifest='{}' WHERE ref=?", (ref,))
    with pytest.raises(SchedulerError, match="asset_hash_mismatch"):
        s.register(deployment)


@pytest.mark.parametrize("field,value", [("../bad", {}), ("/bad", {}), ("bad\\name", {}),
    ("bad", {"size": True, "sha256": "a" * 64}), ("bad", {"size": 1, "sha256": "x"})])
def test_imported_manifest_rejects_unsafe_or_invalid_metadata(tmp_path, field, value):
    s, q, _, _ = setup_store(tmp_path)
    bind(s)
    with pytest.raises(SchedulerError, match="invalid_asset_manifest"):
        s.assets_import({**s.asset_manifest(q.asset_ref), field: value})


@pytest.mark.parametrize("kind", ["job", "legacy"])
def test_grant_and_admission_commit_atomically(tmp_path, kind):
    s, q, _, _ = setup_store(tmp_path)
    bind(s)
    epoch = ready(s, q)
    job = submit(s, q, "job") if kind == "job" else None
    def admit():
        return s.dispatch(epoch, job["id"]) if job else s.legacy_admit("legacy-one", q.id, "worker-one", "api-one", 30)
    s.fault = lambda point: (_ for _ in ()).throw(sqlite3.OperationalError("commit failed")) if point == "before_commit" else None
    with pytest.raises(sqlite3.Error): admit()
    assert s.execution_pending() == [] and s.lease_count() == 0 and s.budget_state()["generations"] == 0
    s.fault = lambda _: None
    admitted = admit()
    grant = s.execution_grant(admitted["id"] if job else "legacy-one")
    assert grant == s.execution_pending()[0]
    assert grant["authority"] == "control-one" and grant["executor"] == "executor-one"
    assert grant["fence"] == epoch and grant["handle"] == "fixture" and grant["deployment"] == q.id
    assert grant["worker_epoch"] == "worker-one" and grant["kind"] == kind
    assert grant["owner"] == (epoch if job else "api-one")
    assert grant["execution_limit"] == (60 if job else 30)
    assert s.lease_count() == 1 and s.budget_state()["generations"] == 1


@pytest.mark.parametrize("field", ["authority", "executor", "fence", "handle", "deployment", "worker_epoch",
                                   "attempt", "owner", "kind", "started", "execution_limit"])
def test_recovered_receipt_rejects_any_changed_grant(tmp_path, field):
    s, q, _, _ = setup_store(tmp_path)
    bind(s)
    epoch = ready(s, q)
    job = submit(s, q, "job")
    attempt = s.dispatch(epoch, job["id"])
    grant = s.execution_grant(attempt["id"])
    with pytest.raises(SchedulerError, match="execution_grant_mismatch"):
        s.import_executor_receipt(epoch, attempt["id"], {"text": "done"}, None, {**grant, field: "changed"})
    assert s.lease_count() == 1 and s.job(job["id"])["state"] == "dispatching"
    with pytest.raises(SchedulerError, match="receipt_not_saved"):
        s.execution_ack(attempt["id"])


def test_reconcile_old_attempt_keeps_original_owner_and_does_not_reexecute(tmp_path):
    s, q, _, _ = setup_store(tmp_path)
    bind(s)
    epoch = ready(s, q)
    job = submit(s, q, "job")
    attempt = s.dispatch(epoch, job["id"])
    grant = s.execution_grant(attempt["id"])
    newer = s.acquire_controller()
    assert s.job(job["id"])["state"] == "unknown"
    with pytest.raises(SchedulerError, match="stale_controller"):
        s.import_executor_receipt(epoch, attempt["id"], {"text": "done"}, None, grant)
    s.import_executor_receipt(newer, attempt["id"], {"text": "done"}, None, grant)
    s.import_executor_receipt(newer, attempt["id"], {"text": "done"}, None, grant)
    assert json.loads(s.result(job["id"])) == {"text": "done"}
    assert s.job(job["id"])["state"] == "succeeded" and s.lease_count() == 0
    assert json.loads(s.receipt_path(attempt["id"]).read_bytes())["owner"] == epoch
    assert s.budget_state()["generations"] == 1 and s.state()["phase"] == "unknown"
    with pytest.raises(SchedulerError, match="receipt_conflict"):
        s.import_executor_receipt(newer, attempt["id"], {"text": "different"}, None, grant)
    s.execution_ack(attempt["id"])
    assert Store(s.root).execution_pending() == []


@pytest.mark.parametrize("already_released", [False, True])
def test_legacy_receipt_reconciles_after_api_restart_or_normal_delivery(tmp_path, already_released):
    s, q, _, _ = setup_store(tmp_path)
    bind(s)
    epoch = ready(s, q)
    grant = s.legacy_admit("legacy-one", q.id, "worker-one", "api-one")
    if already_released:
        s.legacy_terminal("legacy-one", "worker-one", "api-one", True)
    else:
        s.legacy_restart("api-two")
    s.import_executor_receipt(epoch, "legacy-one", {"terminal": True}, None, grant)
    s.import_executor_receipt(epoch, "legacy-one", {"terminal": True}, None, grant)
    assert s.lease_count() == 0
    with pytest.raises(SchedulerError, match="receipt_conflict"):
        s.import_executor_receipt(epoch, "legacy-one", {"terminal": False}, None, grant)
    s.execution_ack("legacy-one")
    assert Store(s.root).execution_pending() == []


def test_normal_terminal_saves_ack_marker_and_storage_retry_does_not_compute(tmp_path):
    s, q, _, _ = setup_store(tmp_path)
    bind(s)
    epoch = ready(s, q)
    job = submit(s, q, "job")
    attempt = s.dispatch(epoch, job["id"])
    s.fault = lambda point: (_ for _ in ()).throw(OSError("disk")) if point == "publish" else None
    with pytest.raises(OSError):
        s.terminal(epoch, attempt["id"], {"sole_result": True})
    assert s.receipt_path(attempt["id"]).exists()
    s.execution_ack(attempt["id"])  # The sole bytes are already fsynced in control storage.
    s.fault = lambda _: None
    s.recover_receipts(epoch)
    assert json.loads(s.result(job["id"])) == {"sole_result": True}
    assert s.budget_state()["generations"] == 1 and s.execution_pending() == []


def test_receipt_recovery_restores_ack_marker_after_control_commit_failure(tmp_path):
    s, q, _, _ = setup_store(tmp_path)
    bind(s)
    epoch = ready(s, q)
    job = submit(s, q, "receipt-survives")
    attempt = s.dispatch(epoch, job["id"])
    s.fault = lambda point: (_ for _ in ()).throw(OSError("commit interrupted")) if point == "after_receipt" else None
    with pytest.raises(OSError):
        s.terminal(epoch, attempt["id"], {"sole_result": True})
    assert s.lease_count() == 1 and s.receipt_path(attempt["id"]).exists()
    with pytest.raises(SchedulerError, match="receipt_not_saved"):
        s.execution_ack(attempt["id"])
    s = Store(s.root)
    newer = s.acquire_controller()
    s.recover_receipts(newer)
    s.execution_ack(attempt["id"])
    assert s.lease_count() == 0 and s.execution_pending() == []
    assert json.loads(s.result(job["id"])) == {"sole_result": True}
    assert s.budget_state()["generations"] == 1


def test_additive_migration_of_copied_state_preserves_ids_and_counts(tmp_path):
    s, q, _, _ = setup_store(tmp_path)
    epoch = ready(s, q)
    job = submit(s, q, "history")
    attempt = s.dispatch(epoch, job["id"])
    s.terminal(epoch, attempt["id"], {"history": "kept"})
    target = tmp_path / "copied-state"
    shutil.copytree(s.root, target)
    with contextlib.closing(s.connect()) as source, sqlite3.connect(target / "scheduler.sqlite3") as copied:
        source.backup(copied)
        copied.executescript("DROP TABLE execution_binding; DROP TABLE execution_grants;")
        tables = [r[0] for r in copied.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        before = {name: copied.execute(f'SELECT * FROM "{name}"').fetchall() for name in tables}
    migrated = Store(target)
    with contextlib.closing(migrated.connect()) as db:
        after = {name: [tuple(row) for row in db.execute(f'SELECT * FROM "{name}"')] for name in tables}
    assert before == after
    assert migrated.job(job["id"])["attempt"] == attempt["id"]
    assert migrated.budget_state() == s.budget_state()
    assert json.loads(migrated.result(job["id"])) == {"history": "kept"}
    assert migrated.execution_binding() is None and migrated.execution_pending() == []


async def test_controller_remote_hooks_preserve_order_and_retry_ack_without_compute(tmp_path):
    s, q, _, _ = setup_store(tmp_path)
    bind(s)
    class Provider(FakeProvider):
        async def fence(self, epoch): self.calls.append(("fence", epoch))
        async def reconcile(self, epoch): self.calls.append(("reconcile", epoch))
        async def verify_asset(self, ref):
            self.calls.append(("verify", ref))
            return ref
        async def ack(self, attempt):
            self.calls.append(("ack", attempt))
            assert s.receipt_path(attempt).exists()
            s.execution_ack(attempt)
    provider = Provider()
    controller = Controller(s, provider)
    await controller.start()
    try:
        assert provider.calls[:2] == [("fence", controller.epoch), ("reconcile", controller.epoch)]
        job = submit(s, q, "remote")
        await tick(controller, 2)
        s.fault = lambda point: (_ for _ in ()).throw(OSError("disk")) if point == "receipt_write" else None
        provider.finished[job["id"]].set()
        await asyncio.gather(*controller.tasks.values())
        assert controller.pending_results and not any(k == "ack" for k, _ in provider.calls)
        s.fault = lambda _: None
        await controller.tick()
        assert not controller.pending_results and s.execution_pending() == []
        assert len([v for k, v in provider.calls if k == "execute"]) == 1
        assert len([v for k, v in provider.calls if k == "ack"]) == 1
        assert ("verify", q.asset_ref) in provider.calls
    finally:
        await controller.close()
