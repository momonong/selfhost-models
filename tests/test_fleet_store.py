"""Atomic fleet policy and executor-scoped recovery in one control database."""
import concurrent.futures
import contextlib
import json
import sqlite3
import threading

import pytest
from filelock import FileLock

from selfhost_models.fleet_store import Fleet
from selfhost_models.scheduler_schema import SchedulerError
from selfhost_models.scheduler_store import Store
from test_scheduler import setup_store, submit


def setup_fleet(tmp_path, *, shared=False, capacity=1):
    store, q, w, clock = setup_store(tmp_path, capacity=capacity)
    fleet = Fleet(store)
    fleet.enable()
    for executor, deps in (("executor-aaa", [q]), ("executor-bbb", [q, w] if shared else [w])):
        fleet.add_executor("control-test", executor, "http://127.0.0.1:19000", tmp_path / (executor + ".key"))
        fleet.verify_executor(executor, dict(version=1, authority="control-test", executor=executor,
            resource_id="cpu-fixture:" + executor, kind="cpu", deployments=[dep.model_dump() for dep in deps]))
    return fleet, store, q, w, clock


def ready(worker, dep, epoch=None):
    epoch = worker.acquire_controller() if epoch is None else epoch
    worker.phase(epoch, "loading", deployment=dep.id, handle="selfhost-scheduler-" + worker.executor)
    worker.phase(epoch, "ready", worker_epoch="same-worker-epoch")
    worker.heartbeat(epoch)
    return epoch


def dispatch(worker, epoch, job):
    selected = worker.next_job(epoch)
    assert selected and selected["id"] == job["id"]
    result = worker.dispatch(epoch, job["id"])
    assert result
    return result


def test_explicit_enable_migrates_unloaded_single_binding_and_primary(tmp_path):
    store, q, _, _ = setup_store(tmp_path)
    store.bind_execution("control-test", "executor-old", "http://127.0.0.1:19000", tmp_path / "key")
    fleet = Fleet(store)
    assert not fleet.enabled() and fleet.workers() == []
    with FileLock(str(store.root / "controller.lock")):
        with pytest.raises(SchedulerError, match="requires_offline"):
            fleet.enable()
    epoch = store.acquire_controller()
    store.phase(epoch, "loading", deployment=q.id, handle="old-handle")
    with pytest.raises(SchedulerError, match="requires_unloaded"):
        fleet.enable()
    store.engine_exited(epoch)
    store.cleanup_done(epoch, "old-handle")
    before = store.budget_state()
    fleet.enable()
    assert fleet.enabled() and fleet.primary().executor == "executor-old"
    assert fleet.primary().state()["epoch"] == epoch
    assert fleet.primary().execution_binding() == store.execution_binding()
    assert store.budget_state() == before
    assert not fleet.view("executor-old")["available"]
    fleet.add_executor("control-test", "executor-new", "http://127.0.0.1:19001", tmp_path / "new-key")
    assert Fleet(Store(store.root)).primary().executor == "executor-old"


def test_inventory_full_deployment_match_identity_resource_uniqueness_and_public_view(tmp_path):
    fleet, store, q, w, _ = setup_fleet(tmp_path)
    a, b = fleet.workers()
    assert fleet.primary().executor == a.executor
    inventory = dict(version=1, authority="control-test", executor=b.executor,
        resource_id="cpu-fixture:" + a.executor, kind="cpu", deployments=[w.model_dump()])
    # Existing resource identities cannot be replaced or aliased, even offline.
    fleet.mark_available(a.executor, False)
    with pytest.raises(SchedulerError, match="resource_changed|resource_duplicate"):
        fleet.verify_executor(b.executor, inventory)
    c = fleet.add_executor("control-test", "executor-ccc", "http://127.0.0.1:19002", tmp_path / "c-key")
    with pytest.raises(SchedulerError, match="resource_duplicate"):
        fleet.verify_executor(c.executor, {**inventory, "executor": c.executor})
    with pytest.raises(SchedulerError, match="identity_mismatch"):
        fleet.verify_executor(c.executor, {**inventory, "executor": c.executor, "authority": "other-control"})
    changed = q.model_copy(update={"load": q.load.model_copy(update={"context": 4096})})
    with pytest.raises(SchedulerError, match="deployment_not_found"):
        fleet.verify_executor(c.executor, {**inventory, "executor": c.executor, "resource_id": "new", "deployments": [changed.model_dump()]})
    view = fleet.view(a.executor)
    assert view["primary"] and view["deployments"] == [q.id]
    assert not {"url", "key_file", "path"} & set(view)
    assert a.db == b.db == store.db


def test_heterogeneous_urgent_head_does_not_block_compatible_worker(tmp_path):
    fleet, store, q, w, _ = setup_fleet(tmp_path)
    a, b = fleet.workers()
    ea, eb = ready(a, q), ready(b, w)
    whisper = submit(store, w, "old-urgent", urgent=True)
    qwen = submit(store, q, "new-urgent", urgent=True)
    qa = dispatch(a, ea, qwen)
    wb = dispatch(b, eb, whisper)
    assert a.execution_grant(qa["id"])["executor"] == a.executor
    assert b.execution_grant(wb["id"])["executor"] == b.executor
    assert a.lease_count() == b.lease_count() == 1
    assert store.lease_count() == 2


def test_atomic_competing_assignment_dispatch_and_full_worker_routing(tmp_path):
    fleet, store, q, _, _ = setup_fleet(tmp_path, shared=True)
    a, b = fleet.workers()
    ea, eb = ready(a, q), ready(b, q)
    first = submit(store, q, "first", urgent=True)
    barrier = threading.Barrier(2)
    def compete(worker, epoch):
        barrier.wait()
        selected = worker.next_job(epoch)
        return worker.dispatch(epoch, selected["id"]) if selected else None
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(compete, worker, epoch) for worker, epoch in ((a, ea), (b, eb))]
        results = [future.result() for future in futures]
    assert sum(value is not None for value in results) == 1
    assert store.budget_state()["generations"] == 1
    assert store.job(first["id"])["attempt"] == results[0]["id"]
    second = submit(store, q, "second", urgent=True)
    attempt = dispatch(b, eb, second)
    assert b.execution_grant(attempt["id"])["executor"] == b.executor
    assert a.lease_count() == b.lease_count() == 1


def test_unloaded_planning_reserves_one_target_per_worker(tmp_path):
    fleet, store, q, _, _ = setup_fleet(tmp_path, shared=True)
    a, b = fleet.workers()
    ea, eb = a.acquire_controller(), b.acquire_controller()
    first, second = submit(store, q, "one"), submit(store, q, "two")
    assert a.next_job(ea)["id"] == first["id"]
    assert b.next_job(eb)["id"] == second["id"]
    assert a.next_job(ea)["id"] == first["id"]
    assert len([event for event in store.events() if event["kind"] == "job_assigned"]) == 2


def test_affinity_then_aging_and_urgent_fifo(tmp_path):
    fleet, store, q, w, clock = setup_fleet(tmp_path)
    a, b = fleet.workers()
    fleet.verify_executor(a.executor, dict(version=1, authority="control-test", executor=a.executor,
        resource_id="cpu-fixture:" + a.executor, kind="cpu", deployments=[q.model_dump(), w.model_dump()]))
    fleet.mark_available(b.executor, False)
    epoch = ready(a, q)
    old = submit(store, w, "old-normal")
    same = submit(store, q, "same-normal")
    attempt = dispatch(a, epoch, same)
    a.terminal(epoch, attempt["id"], {"ok": True})
    clock.now += store.config.aging_seconds + 1
    a.heartbeat(epoch)
    newer = submit(store, q, "newer-normal")
    assert a.next_job(epoch)["id"] == old["id"]
    urgent1, urgent2 = submit(store, q, "urgent-one", urgent=True), submit(store, q, "urgent-two", urgent=True)
    # Existing normal plans are not a reservation ahead of newly urgent work.
    assert a.next_job(epoch)["id"] == urgent1["id"]
    attempt = a.dispatch(epoch, urgent1["id"])
    assert attempt
    a.terminal(epoch, attempt["id"], {"ok": True})
    assert a.next_job(epoch)["id"] == urgent2["id"]
    assert store.job(newer["id"])["state"] == "queued"


def test_offline_replans_queued_but_never_reassigns_an_attempt(tmp_path):
    fleet, store, q, _, _ = setup_fleet(tmp_path, shared=True)
    a, b = fleet.workers()
    ea, eb = ready(a, q), ready(b, q)
    first = submit(store, q, "queued")
    assert a.next_job(ea)["id"] == first["id"]
    fleet.mark_available(a.executor, False)
    attempt = dispatch(b, eb, first)
    fleet.mark_available(b.executor, False)
    fleet.mark_available(a.executor, True)
    b.attempt_state(eb, attempt["id"], "unknown", "lost_transport")
    assert a.next_job(ea) is None
    with contextlib.closing(store.connect()) as c:
        assert c.execute("SELECT executor FROM job_assignments WHERE job=?", (first["id"],)).fetchone()[0] == b.executor
    assert store.budget_state()["generations"] == 1


def test_restarting_one_controller_preserves_other_engine_lease_and_pin(tmp_path):
    fleet, store, q, w, _ = setup_fleet(tmp_path)
    a, b = fleet.workers()
    ea, eb = ready(a, q), ready(b, w)
    ja, jb = submit(store, q, "a"), submit(store, w, "b")
    aa, ab = dispatch(a, ea, ja), dispatch(b, eb, jb)
    next_epoch = a.acquire_controller()
    assert next_epoch > ea and a.state()["phase"] == "unknown"
    assert b.state()["phase"] == "ready" and store.job(jb["id"])["state"] == "dispatching"
    a.engine_exited(next_epoch)
    assert a.lease_count() == 0 and b.lease_count() == 1
    assert len(a.cleanup_pending()) == 1 and b.cleanup_pending() == []
    with contextlib.closing(store.connect()) as c:
        assert c.execute("SELECT 1 FROM pins WHERE owner=?", (b.model_pin,)).fetchone()
        assert c.execute("SELECT 1 FROM pins WHERE owner=?", (a.model_pin,)).fetchone() is None
    b.terminal(eb, ab["id"], {"only_b": True})
    assert store.job(ja["id"])["state"] == "unknown"
    assert store.job(jb["id"])["state"] == "succeeded"
    assert store.execution_grant(aa["id"])["executor"] == a.executor


def test_equal_epochs_cannot_cross_complete_ack_or_recover_receipts(tmp_path):
    fleet, store, q, w, _ = setup_fleet(tmp_path)
    a, b = fleet.workers()
    ea, eb = ready(a, q), ready(b, w)
    assert ea == eb
    job = submit(store, w, "b")
    attempt = dispatch(b, eb, job)
    with pytest.raises(SchedulerError, match="grant_mismatch"):
        a.terminal(ea, attempt["id"], {"forged": True})
    with pytest.raises(SchedulerError, match="grant_mismatch"):
        a.attempt_state(ea, attempt["id"], "unknown")
    b.fault = lambda point: (_ for _ in ()).throw(OSError("after fsync")) if point == "after_receipt" else None
    with pytest.raises(OSError): b.terminal(eb, attempt["id"], {"genuine": True})
    a.recover_receipts(ea)
    assert b.lease_count() == 1 and store.job(job["id"])["result_state"] == "none"
    b.fault = lambda _: None
    b.recover_receipts(eb)
    assert json.loads(store.result(job["id"])) == {"genuine": True}
    with pytest.raises(SchedulerError, match="grant_mismatch"):
        a.execution_ack(attempt["id"])
    b.execution_ack(attempt["id"])
    assert b.execution_pending() == []


def test_lifecycle_failure_is_scoped_and_old_receipt_keeps_owner(tmp_path):
    fleet, store, q, _, _ = setup_fleet(tmp_path, shared=True)
    a, b = fleet.workers()
    ea, eb = ready(a, q), ready(b, q)
    first, second = submit(store, q, "one"), submit(store, q, "two")
    assert a.next_job(ea)["id"] == first["id"]
    a.lifecycle_failed(ea, q.id)
    assert store.job(first["id"])["state"] == "failed"
    assert store.job(second["id"])["state"] == "queued"
    attempt = dispatch(b, eb, second)
    newer = b.acquire_controller()
    grant = b.execution_grant(attempt["id"])
    b.import_executor_receipt(newer, attempt["id"], {"recovered": True}, None, grant)
    assert json.loads(b.receipt_path(attempt["id"]).read_bytes())["owner"] == eb
    assert a.state()["phase"] == "ready"


@pytest.mark.parametrize("mode", ["pause", "drain", "admissions"])
def test_global_maintenance_is_persistent_and_atomic_with_public_admission(tmp_path, mode):
    fleet, store, q, w, _ = setup_fleet(tmp_path)
    a, _ = fleet.workers()
    epoch = ready(a, q)
    queued = submit(store, q, "before-maintenance")
    fleet.maintenance(mode)
    with pytest.raises(SchedulerError, match="maintenance_admission_closed"):
        submit(store, w, "blocked")
    with pytest.raises(SchedulerError, match="maintenance_admission_closed"):
        a.legacy_admit("new-legacy", q.id, "same-worker-epoch", "api")
    if mode == "pause":
        assert a.next_job(epoch) is None and a.dispatch(epoch, queued["id"]) is None
    else:
        attempt = dispatch(a, epoch, queued)
        a.terminal(epoch, attempt["id"], {"ok": True})
        assert a.should_drain() == (mode == "drain")
    assert Fleet(Store(store.root)).view(a.executor)["global_maintenance"] == mode
    fleet.maintenance("open")
    assert submit(store, w, "allowed")["state"] == "queued"


def test_worker_drain_keeps_active_lease_and_allows_other_worker(tmp_path):
    fleet, store, q, _, _ = setup_fleet(tmp_path, shared=True, capacity=2)
    a, b = fleet.workers()
    ea, eb = ready(a, q), ready(b, q)
    active = submit(store, q, "active")
    attempt = dispatch(a, ea, active)
    queued = submit(store, q, "queued")
    assert a.next_job(ea)["id"] == queued["id"]
    fleet.maintenance("drain", a.executor)
    assert a.next_job(ea) is None and a.should_drain()
    assert a.lease_count() == 1 and store.job(active["id"])["state"] == "dispatching"
    assert dispatch(b, eb, queued)
    with pytest.raises(SchedulerError, match="maintenance_admission_closed"):
        a.legacy_admit("new-legacy", q.id, "same-worker-epoch", "api")
    a.terminal(ea, attempt["id"], {"ok": True})
    assert a.should_drain() and b.lease_count() == 1


def test_legacy_unknown_and_restart_remain_worker_scoped(tmp_path):
    fleet, store, q, _, _ = setup_fleet(tmp_path, shared=True)
    a, b = fleet.workers()
    ready(a, q)
    ready(b, q)
    a.legacy_admit("legacy-a", q.id, "same-worker-epoch", "api-a")
    b.legacy_admit("legacy-b", q.id, "same-worker-epoch", "api-b")
    a.legacy_restart("api-new")
    assert a.health()["uncertain"] == 1 and b.health()["uncertain"] == 0
    assert b.state()["phase"] == "ready"
    a.legacy_terminal("legacy-a", "same-worker-epoch", "api-a", False)
    assert b.lease_count() == 1 and b.state()["phase"] == "ready"
    with pytest.raises(SchedulerError, match="grant_mismatch"):
        a.legacy_terminal("legacy-b", "same-worker-epoch", "api-b", True)
    assert b.lease_count() == 1


def test_maintenance_dispatch_race_never_starts_after_pause_commit(tmp_path):
    fleet, store, q, _, _ = setup_fleet(tmp_path)
    a = fleet.primary()
    epoch = ready(a, q)
    job = submit(store, q, "race")
    assert a.next_job(epoch)
    started = threading.Event()
    def dispatch_after_lock():
        started.set()
        return a.dispatch(epoch, job["id"])
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        with store.tx() as c:
            future = pool.submit(dispatch_after_lock)
            assert started.wait(1)
            c.execute("UPDATE fleet_settings SET maintenance='pause' WHERE id=1")
        assert future.result() is None
    assert store.job(job["id"])["attempt"] is None
    assert store.budget_state()["generations"] == 0 and a.lease_count() == 0


def test_inventory_refresh_emits_events_only_for_changes(tmp_path):
    fleet, store, q, _, clock = setup_fleet(tmp_path)
    worker = fleet.primary()
    inventory = dict(version=1, authority="control-test", executor=worker.executor,
        resource_id="cpu-fixture:" + worker.executor, kind="cpu", deployments=[q.model_dump()])
    before = len(store.events(limit=1000))
    clock.now += 1
    fleet.verify_executor(worker.executor, inventory)
    assert len(store.events(limit=1000)) == before
    assert fleet.view(worker.executor)["seen"] == clock.now
    fleet.mark_available(worker.executor, False)
    fleet.mark_available(worker.executor, False)
    assert len(store.events(limit=1000)) == before + 1
    fleet.verify_executor(worker.executor, inventory)
    assert len(store.events(limit=1000)) == before + 2


def test_migration_refuses_unpublished_local_result_until_recovery(tmp_path):
    store, q, _, _ = setup_store(tmp_path)
    epoch = store.acquire_controller()
    store.phase(epoch, "loading", deployment=q.id, handle="local-history")
    store.phase(epoch, "ready", worker_epoch="old-worker")
    job = submit(store, q, "local")
    attempt = store.dispatch(epoch, job["id"])
    store.fault = lambda point: (_ for _ in ()).throw(OSError("publish")) if point == "publish" else None
    with pytest.raises(OSError): store.terminal(epoch, attempt["id"], {"sole": "local result"})
    store.fault = lambda _: None
    store.engine_exited(epoch)
    store.cleanup_done(epoch, "local-history")
    fleet = Fleet(store)
    with pytest.raises(SchedulerError, match="pending_local_receipt"):
        fleet.enable()
    assert not fleet.enabled() and store.receipt_path(attempt["id"]).exists()
    store.recover_receipts(epoch)
    fleet.enable()
    assert json.loads(store.result(job["id"])) == {"sole": "local result"}
    assert store.job(job["id"])["attempt"] == attempt["id"]


def test_migration_keeps_remote_history_grants_assignments_and_budget(tmp_path):
    store, q, _, _ = setup_store(tmp_path)
    store.bind_execution("control-test", "executor-old", "http://127.0.0.1:19000", tmp_path / "key")
    epoch = store.acquire_controller()
    store.phase(epoch, "loading", deployment=q.id, handle="remote-history")
    store.phase(epoch, "ready", worker_epoch="old-worker")
    job = submit(store, q, "remote")
    attempt = store.dispatch(epoch, job["id"])
    grant = store.execution_grant(attempt["id"])
    store.terminal(epoch, attempt["id"], {"sole": "remote result"})
    store.engine_exited(epoch)
    store.cleanup_done(epoch, "remote-history")
    budget = store.budget_state()
    fleet = Fleet(store)
    fleet.enable()
    worker = fleet.primary()
    assert worker.execution_grant(attempt["id"]) == grant
    assert fleet.explain(job["id"]) == {"executor": worker.executor, "reason": "succeeded"}
    assert store.job(job["id"])["attempt"] == attempt["id"] and store.budget_state() == budget
    assert json.loads(store.result(job["id"])) == {"sole": "remote result"}


def test_fleet_budget_idle_guard_catches_other_worker_lifecycle(tmp_path):
    from selfhost_models.fleet_store import check_fleet_budget_idle
    # Parent's legacy controller row remains unloaded throughout this case.
    fleet, store, q, w, _ = setup_fleet(tmp_path)
    a, b = fleet.workers()
    ready(a, q)
    epoch = b.acquire_controller()
    b.phase(epoch, "loading", deployment=w.id, handle="other-engine")
    assert store.state()["phase"] == "unloaded" and store.lease_count() == 0
    with store.tx() as c:
        with pytest.raises(SchedulerError, match="budget_window_requires_idle"):
            check_fleet_budget_idle(c)
    b.phase(epoch, "ready", worker_epoch="other-worker")
    with store.tx() as c:
        check_fleet_budget_idle(c)


def test_scheduling_explanations_are_read_only_and_unknown_never_reassigns(tmp_path):
    fleet, store, q, w, _ = setup_fleet(tmp_path)
    a, b = fleet.workers()
    ea = ready(a, q)
    epoch = b.acquire_controller()
    job = submit(store, w, "waiting")
    before = len(store.events(limit=1000))
    assert fleet.explain(job["id"])["reason"] == "awaiting_assignment"
    fleet.mark_available(b.executor, False)
    assert fleet.explain(job["id"])["reason"] == "executor_offline"
    fleet.mark_available(b.executor, True)
    fleet.maintenance("drain", b.executor)
    assert fleet.explain(job["id"])["reason"] == "worker_maintenance"
    fleet.maintenance("open", b.executor)
    b.phase(epoch, "loading", deployment=w.id, handle="load")
    assert fleet.explain(job["id"])["reason"] == "capacity_or_lifecycle"
    ready(b, w, epoch)
    attempt = dispatch(b, epoch, job)
    b.attempt_state(epoch, attempt["id"], "unknown", "transport")
    expected = {"executor": b.executor, "reason": "execution_unconfirmed"}
    assert fleet.explain(job["id"]) == expected
    known = len(store.events(limit=1000))
    assert known > before
    for _ in range(3): assert fleet.explain(job["id"]) == expected
    assert len(store.events(limit=1000)) == known
    assert a.next_job(ea) is None


def test_explain_pause_dependencies_and_unmatched_deployment(tmp_path):
    fleet, store, q, w, _ = setup_fleet(tmp_path)
    parent = submit(store, q, "parent")
    child = submit(store, w, "child", depends_on=[parent["id"]])
    assert fleet.explain(child["id"])["reason"] == "dependency_waiting"
    fleet.maintenance("pause")
    assert fleet.explain(child["id"])["reason"] == "global_pause"
    fleet.maintenance("open")
    other = q.model_copy(update={"load": q.load.model_copy(update={"context": 4096})})
    store.register(other)
    unmatched = submit(store, other, "unmatched")
    assert fleet.explain(unmatched["id"])["reason"] == "not_in_verified_catalog"
