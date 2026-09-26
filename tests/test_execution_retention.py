"""Control custody, live stream readers, restart and bounded tombstone retention."""
import json
import sqlite3
import uuid

import httpx
import pytest

from selfhost_models.execution_client import RemoteProvider, RemoteResponse
from selfhost_models.execution_protocol import Command
from selfhost_models.execution_retention import ControlRetention, can_collect_attempt, reserve_execution_grant
from selfhost_models.executor import Executor
from selfhost_models.scheduler_schema import SchedulerError, canonical, digest
from selfhost_models.scheduler_store import Store
from test_execution_protocol import environment, AUTHORITY, EXECUTOR, KEY, HANDLE


@pytest.fixture
def control(environment, tmp_path):
    journal, runtime, dep = environment
    store = Store(tmp_path / 'control')
    key = store.root / 'executor-key'
    key.write_text(KEY)
    store.bind_execution(AUTHORITY, EXECUTOR, 'http://127.0.0.1:19000', key)
    store.assets_import(journal.asset(dep.asset_ref)[1])
    store.register(dep)
    epoch = store.acquire_controller()
    store.phase(epoch, 'loading', deployment=dep.id, handle=HANDLE)
    store.phase(epoch, 'ready', worker_epoch='worker-fixture')
    app = Executor(journal, runtime, KEY)
    provider = RemoteProvider(store, transport=httpx.ASGITransport(app=app))
    provider.epoch = epoch
    return store, provider, journal, runtime, dep


def terminal_legacy(control, *, stream=False, owner='api-owner'):
    store, provider, journal, _, dep = control
    cid = uuid.uuid4().hex
    grant = store.legacy_admit(cid, dep.id, 'worker-fixture', owner, 30)
    command = provider.command('execute', dep, HANDLE, grant=grant,
        payload={'model': dep.model, 'messages': [{'role':'user','content':'fixture'}], 'stream':stream})
    journal.accept(command)
    assert journal.executing(cid)
    if stream:
        journal.frame(cid, 'data: {"choices":[{"delta":{"content":"held"}}]}')
    receipt = journal.finish(command, {'stream_terminal': True} if stream else {'choices': []})
    return command, receipt


async def test_controller_ack_cannot_collect_live_stream_and_reader_finish_releases(control):
    store, provider, journal, runtime, _ = control
    command, _ = terminal_legacy(control, stream=True)
    await provider.reconcile(provider.epoch)
    assert journal.status(command.id)['acknowledged']
    assert not journal.status(command.id)['outputs_released']
    assert journal.collect(retention_seconds=0)['collected_commands'] == 0
    with store.tx() as db:
        assert not can_collect_attempt(db, command.id)
    lines = [line async for line in RemoteResponse(provider, command).aiter_lines()]
    assert 'held' in lines[0] and lines[-1] == 'data: [DONE]'
    assert journal.status(command.id)['outputs_released']
    with store.tx() as db:
        assert can_collect_attempt(db, command.id)
    assert journal.collect(retention_seconds=0)['collected_commands'] == 1
    assert provider.retention.collect(retention_seconds=0)['collected'] == 1
    with pytest.raises(SchedulerError, match='outputs_collected'):
        provider.persist(command)
    with pytest.raises(SchedulerError, match='command_conflict'):
        provider.persist(command.model_copy(update={'result_bytes':1024}))
    assert runtime.calls == 0
    await provider.close()


async def test_lost_release_ack_response_retried_after_restart_without_reading_collected_bytes(control):
    store, provider, journal, runtime, _ = control
    command, receipt = terminal_legacy(control)
    real_request = provider.request
    async def lost_ack(method,path,**kwargs):
        value = await real_request(method,path,**kwargs)
        if method == 'POST' and path == '/commands/'+command.id:
            raise httpx.ReadError('ACK response lost')
        return value
    provider.request = lost_ack
    assert await RemoteResponse(provider, command).terminal(journal.status(command.id)) == receipt['result']
    assert journal.collect(retention_seconds=0)['collected_commands'] == 1
    assert not provider.retention.row(command.id)['released']
    provider.request = real_request
    # This models reopened durable state; retry uses the saved receipt hash only.
    provider.retention = ControlRetention(Store(store.root))
    await provider.reconcile(provider.epoch)
    assert provider.retention.row(command.id)['released']
    assert provider.retention.pending(EXECUTOR) == []
    assert runtime.calls == 0
    await provider.close()


async def test_api_restart_releases_old_owner_but_preserves_current_owner(control):
    store, provider, journal, _, _ = control
    old, _ = terminal_legacy(control, owner='api-old')
    await provider.reconcile(provider.epoch)  # Old receipt already ACKed, excluded from execution_pending.
    current, _ = terminal_legacy(control, owner='api-current')
    await provider.reconcile(provider.epoch)
    assert store.execution_pending() == []
    provider.retention.abandon_legacy('api-current')
    await provider.reconcile(provider.epoch)
    assert journal.status(old.id)['outputs_released']
    assert not journal.status(current.id)['outputs_released']
    assert journal.collect(retention_seconds=0)['collected_commands'] == 1
    await provider.close()


async def test_api_restart_pending_unknown_never_releases_before_import(control):
    store, provider, journal, _, dep = control
    command, receipt = terminal_legacy(control, owner='api-old')
    provider.retention.abandon_legacy('api-new')
    with pytest.raises(SchedulerError, match='receipt_not_saved'):
        await provider.ack(command.id)
    assert not journal.status(command.id)['acknowledged']
    await provider.reconcile(provider.epoch)
    assert journal.status(command.id)['outputs_released']
    assert provider.retention.row(command.id)['receipt_hash']
    await provider.close()


async def test_lifecycle_cached_receipt_survives_remote_gc_no_second_effect(control):
    _, provider, journal, runtime, dep = control
    command = provider.command('load', dep, HANDLE)
    journal.accept(command)
    journal.executing(command.id)
    journal.finish(command, {'worker_epoch':'worker-fixture'})
    assert await provider.load(dep,None,HANDLE) == 'worker-fixture'
    assert journal.collect(retention_seconds=0)['collected_commands'] == 1
    assert journal.status(command.id)['state'] == 'collected'
    real_request = provider.request
    async def forbid_resend(method,path,**kwargs):
        assert path != '/commands', 'saved lifecycle effect must never be resent'
        return await real_request(method,path,**kwargs)
    provider.request = forbid_resend
    assert await provider.load(dep,None,HANDLE) == 'worker-fixture'
    assert runtime.loads == 0
    await provider.close()


def test_legacy_command_schema_migrates_without_rewriting_payload(tmp_path):
    store = Store(tmp_path / 'state')
    command = Command(authority=AUTHORITY,executor=EXECUTOR,fence=1,id='a'*32,kind='release',result_bytes=1024)
    body = canonical(command.model_dump()).decode()
    with store.tx() as db:
        db.execute('CREATE TABLE execution_commands(id TEXT PRIMARY KEY,command TEXT NOT NULL)')
        db.execute('INSERT INTO execution_commands VALUES(?,?)',(command.id,body))
    retention = ControlRetention(store)
    assert retention.row(command.id)['command'] == body
    assert retention.row(command.id)['hash'] == command.content_hash
    assert retention.persist(command) == command


def test_bounded_capacity_fails_before_dispatch_and_tombstones_do_not_recycle(tmp_path):
    store = Store(tmp_path/'state')
    retention = ControlRetention(store,max_commands=1,storage_bytes=100000)
    command = Command(authority=AUTHORITY,executor=EXECUTOR,fence=1,id='b'*32,kind='release',result_bytes=1024)
    retention.persist(command)
    with pytest.raises(SchedulerError,match='metadata_full'):
        retention.persist(command.model_copy(update={'id':'c'*32}))
    assert ControlRetention(Store(store.root)).usage()['remaining_slots'] == 0
    with store.tx() as db:
        db.execute('INSERT INTO execution_grants(attempt,envelope) VALUES(?,?)',('grant','{}'))
        with pytest.raises(SchedulerError,match='grant_metadata_full'):
            reserve_execution_grant(db)
    other = ControlRetention(Store(tmp_path/'small'),storage_bytes=1024)
    with pytest.raises(SchedulerError,match='storage_full'):
        other.persist(command)
    assert other.usage()['commands'] == 0


def test_receipt_disk_failure_preserves_command_and_retry_does_not_change_identity(tmp_path):
    store = Store(tmp_path/'state')
    retention = ControlRetention(store)
    command = Command(authority=AUTHORITY,executor=EXECUTOR,fence=1,id='d'*32,kind='release',result_bytes=1024)
    retention.persist(command)
    receipt = {'result': {'released':True}, 'error':None}
    def fail(point):
        if point == 'before_commit': raise sqlite3.OperationalError('database or disk is full')
    store.fault = fail
    with pytest.raises(sqlite3.OperationalError): retention.save_receipt(command.id,receipt)
    store.fault = lambda _: None
    assert retention.row(command.id)['receipt'] is None
    retention.save_receipt(command.id,receipt)
    assert retention.row(command.id)['hash'] == command.content_hash
    assert json.loads(retention.row(command.id)['receipt']) == receipt


async def test_control_collection_transaction_rollback_preserves_sole_command_receipt(control):
    store, provider, journal, _, _ = control
    command, _ = terminal_legacy(control)
    await RemoteResponse(provider,command).terminal(journal.status(command.id))
    def fail(point):
        if point == 'before_commit': raise sqlite3.OperationalError('interrupted commit')
    store.fault = fail
    with pytest.raises(sqlite3.OperationalError): provider.retention.collect(retention_seconds=0)
    store.fault = lambda _: None
    row = provider.retention.row(command.id)
    assert row['receipt'] and row['command'] and not row['collected']
    assert provider.retention.collect(retention_seconds=0)['collected'] == 1
    assert provider.retention.collect(retention_seconds=0)['collected'] == 1  # total, no duplicate effect
    await provider.close()


async def test_unknown_output_remains_pinned_after_old_api_owner_abandoned(control):
    store, provider, journal, _, dep = control
    cid = uuid.uuid4().hex
    grant = store.legacy_admit(cid,dep.id,'worker-fixture','api-old',30)
    command = provider.command('execute',dep,HANDLE,grant=grant,
        payload={'model':dep.model,'messages':[{'role':'user','content':'unknown'}]})
    journal.accept(command)
    journal.executing(cid)
    journal.finish(command,None,'execution_unconfirmed',state='unknown')
    provider.retention.abandon_legacy('api-new')
    await provider.reconcile(provider.epoch)
    assert not journal.status(cid)['acknowledged']
    assert not journal.status(cid)['outputs_released']
    assert journal.collect(retention_seconds=0)['collected_commands'] == 0
    assert provider.retention.collect(retention_seconds=0)['collected_commands'] == 0
    assert store.lease_count() == 1
    await provider.close()


async def test_previous_lifecycle_id_is_reused_and_executor_identity_prevents_alias(control):
    _, provider, _, _, dep = control
    old_id = digest(['load',HANDLE,dep.id,None])
    old = Command(authority=AUTHORITY,executor=EXECUTOR,fence=1,id=old_id,
        kind='load',handle=HANDLE,deployment=dep)
    provider.persist(old)
    assert provider.command('load',dep,HANDLE).id == old_id
    first = provider.command('release').id
    provider.binding = {**provider.binding,'executor_id':'executor-second'}
    assert provider.command('release').id != first
    await provider.close()


async def test_retention_age_starts_when_reader_releases_not_when_generation_started(control):
    store, provider, journal, _, _ = control
    clock = [1000.0]
    store.clock = lambda: clock[0]
    command, _ = terminal_legacy(control)
    clock[0] += 100000
    await RemoteResponse(provider,command).terminal(journal.status(command.id))
    assert provider.retention.collect(retention_seconds=86400)['collected_commands'] == 0
    clock[0] += 86401
    assert provider.retention.collect(retention_seconds=86400)['collected_commands'] == 1
    await provider.close()


async def test_collected_stream_is_explicitly_unavailable_never_an_infinite_poll(control):
    _, provider, journal, _, _ = control
    command, _ = terminal_legacy(control,stream=True)
    _ = [line async for line in RemoteResponse(provider,command).aiter_lines()]
    journal.collect(retention_seconds=0)
    with pytest.raises(SchedulerError,match='outcome_unknown'):
        _ = [line async for line in RemoteResponse(provider,command).aiter_lines()]
    await provider.close()


async def test_durable_job_output_released_only_after_saved_control_receipt(control):
    from test_scheduler import submit
    store, provider, journal, _, dep = control
    job = submit(store,dep,'durable-retention')
    attempt = store.dispatch(provider.epoch,job['id'])
    grant = store.execution_grant(attempt['id'])
    command = provider.command('execute',dep,HANDLE,grant=grant,
        payload={'model':dep.model,'messages':[{'role':'user','content':'durable'}]})
    journal.accept(command)
    journal.executing(command.id)
    receipt = journal.finish(command,{'choices':[]})
    provider.retention.save_receipt(command.id,receipt)
    with pytest.raises(SchedulerError,match='receipt_not_saved'):
        await provider.ack(command.id)
    assert not journal.status(command.id)['outputs_released']
    store.terminal(provider.epoch,command.id,receipt['result'])
    await provider.ack(command.id)
    assert journal.status(command.id)['outputs_released']
    assert store.result(job['id'])
    assert journal.collect(retention_seconds=0)['collected_commands'] == 1
    await provider.close()
