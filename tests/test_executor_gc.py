"""Output release is distinct from receipt ACK and dedup metadata retention."""
import json
import sqlite3
import time

import httpx
import pytest

from selfhost_models.execution_protocol import receipt_hash
from selfhost_models.executor import Executor, Journal
from selfhost_models.scheduler_schema import SchedulerError
from test_execution_protocol import AUTHORITY, EXECUTOR, KEY, environment, execution


def finish(journal, dep, *, stream=False):
    cmd = execution(dep, stream=stream)
    journal.accept(cmd)
    journal.executing(cmd.id)
    if stream:
        journal.frame(cmd.id, 'data: ' + json.dumps({'choices': [{'delta': {'content': 'output' * 100}}]}))
    receipt = journal.finish(cmd, {'stream_terminal': True} if stream else {'text': 'result' * 100})
    return cmd, receipt


def test_ack_alone_never_collects_legacy_spool(environment):
    journal, _, dep = environment
    cmd, receipt = finish(journal, dep, stream=True)
    journal.ack(cmd.id, receipt_hash(receipt))
    assert journal.collect(0)['collected_commands'] == 0
    assert journal.status(cmd.id)['frames']
    assert journal.usage()['reclaimable_commands'] == 0
    journal.ack(cmd.id, receipt_hash(receipt), release_outputs=True)
    before = journal.usage()
    assert before['reclaimable_commands'] == 1 and before['reclaimable_bytes'] > 0
    assert journal.collect(0)['collected_commands'] == 1
    status = journal.status(cmd.id)
    assert status['state'] == 'collected' and status['receipt'] is None and status['frames'] == []
    assert status['receipt_hash'] == receipt_hash(receipt)
    assert status['terminal_summary']['grant'] == cmd.grant.model_dump()
    assert journal.usage()['reserved_bytes'] < before['reserved_bytes']


def test_collected_replay_conflict_ack_and_metadata_survive_restart(environment):
    journal, _, dep = environment
    cmd, receipt = finish(journal, dep)
    journal.ack(cmd.id, receipt_hash(receipt), True)
    journal.collect(0)
    other = Journal(journal.root, AUTHORITY, EXECUTOR)
    other.recover()
    assert other.accept(cmd) is False and other.executing(cmd.id) is False
    assert other.status(cmd.id)['state'] == 'collected'
    other.ack(cmd.id, receipt_hash(receipt), True)
    with pytest.raises(SchedulerError, match='receipt_ack_mismatch'):
        other.ack(cmd.id, '0' * 64)
    with pytest.raises(SchedulerError, match='command_conflict'):
        other.accept(cmd.model_copy(update={'result_bytes': 1024}))
    with other.tx() as c:
        row = c.execute('SELECT command,receipt,reservation FROM commands WHERE id=?', (cmd.id,)).fetchone()
        compact = json.loads(row['command'])
        assert 'payload' not in compact and row['receipt'] is None and row['reservation'] > 0
        assert compact['grant'] == cmd.grant.model_dump() and compact['fence'] == cmd.fence
    with pytest.raises(SchedulerError, match='command_not_executing'):
        other.frame(cmd.id, 'late output')
    with pytest.raises(SchedulerError, match='command_not_executing'):
        other.finish(cmd, {'text': 'replacement'})


@pytest.mark.parametrize('state', ['accepted', 'executing', 'unknown'])
def test_active_and_unknown_never_collected_even_with_release_flags(environment, state):
    journal, _, dep = environment
    cmd = execution(dep)
    journal.accept(cmd)
    with journal.tx() as c:
        c.execute('UPDATE commands SET state=?,acknowledged=1,outputs_released=1,terminal_at=0,released_at=0 WHERE id=?',
                  (state, cmd.id))
    assert journal.collect(0)['collected_commands'] == 0
    assert journal.status(cmd.id)['state'] == state
    assert journal.usage()['reclaimable_commands'] == 0


def test_release_retention_is_monotonic_and_ack_requires_result_hash(environment):
    journal, _, dep = environment
    cmd, receipt = finish(journal, dep)
    with pytest.raises(SchedulerError, match='receipt_ack_mismatch'):
        journal.ack(cmd.id, '0' * 64, True)
    assert not journal.status(cmd.id)['outputs_released']
    journal.ack(cmd.id, receipt_hash(receipt), True)
    with journal.tx() as c:
        released = c.execute('SELECT released_at FROM commands WHERE id=?', (cmd.id,)).fetchone()[0]
    journal.ack(cmd.id, receipt_hash(receipt))
    journal.ack(cmd.id, receipt_hash(receipt), True)
    with journal.tx() as c:
        assert c.execute('SELECT released_at FROM commands WHERE id=?', (cmd.id,)).fetchone()[0] == released
    assert journal.collect(3600)['collected_commands'] == 0
    with journal.tx() as c:
        c.execute('UPDATE commands SET terminal_at=?,released_at=? WHERE id=?', (time.time()-3601,time.time()-3601,cmd.id))
    assert journal.collect(3600)['collected_commands'] == 1
    assert journal.collect(0)['collected_commands'] == 0


def test_gc_transaction_rollback_keeps_frames_result_and_recoverable_bytes(environment):
    journal, _, dep = environment
    cmd, receipt = finish(journal, dep, stream=True)
    journal.ack(cmd.id, receipt_hash(receipt), True)
    before = journal.usage()['reserved_bytes']
    with journal.tx() as c:
        c.execute("CREATE TRIGGER interrupt_gc BEFORE UPDATE OF state ON commands "
                  "WHEN NEW.state='collected' BEGIN SELECT RAISE(ABORT,'simulated interruption'); END")
    with pytest.raises(sqlite3.IntegrityError, match='simulated interruption'):
        journal.collect(0)
    other = Journal(journal.root, AUTHORITY, EXECUTOR)
    assert other.status(cmd.id)['receipt'] == receipt and other.status(cmd.id)['frames']
    assert other.usage()['reserved_bytes'] == before
    with other.tx() as c:
        c.execute('DROP TRIGGER interrupt_gc')
    assert other.collect(0)['collected_commands'] == 1


def test_metadata_slots_stay_bounded_after_payload_gc(environment):
    journal, _, dep = environment
    journal.max_commands = 1
    cmd, receipt = finish(journal, dep)
    journal.ack(cmd.id, receipt_hash(receipt), True)
    journal.collect(0)
    usage = journal.usage()
    assert usage['metadata_slots_remaining'] == 0
    assert 'metadata_slots_exhausted' in usage['full_reasons']
    assert usage['states']['collected'] == 1
    assert {'page_size', 'page_count', 'free_pages', 'wal_file_bytes', 'database_file_bytes'} <= usage['sqlite'].keys()
    with pytest.raises(SchedulerError, match='executor_journal_full'):
        journal.accept(execution(dep))
    assert not journal.accept(cmd)


def test_gc_keeps_engine_exit_proof(environment):
    journal, _, dep = environment
    engine = journal.engine()
    engine.update(phase='exited', exit_container_id='original-engine-container')
    journal.set_engine(engine)
    # Lifecycle receipt can be collected without discarding engine-exit evidence.
    journal.set_engine({**engine, 'phase': 'ready'})
    cmd, receipt = finish(journal, dep)
    journal.ack(cmd.id, receipt_hash(receipt), True)
    journal.collect(0)
    journal.set_engine(None)
    assert Journal(journal.root, AUTHORITY, EXECUTOR).exit_proof(engine['handle']) == engine


def test_old_schema_upgrade_preserves_ack_without_assuming_reader_release(environment):
    journal, _, dep = environment
    cmd, receipt = finish(journal, dep)
    journal.ack(cmd.id, receipt_hash(receipt))
    with journal.tx() as c:
        for name in ('receipt_digest','terminal_at','outputs_released','released_at','collected_at','terminal_summary'):
            c.execute(f'ALTER TABLE commands DROP COLUMN {name}')
    other = Journal(journal.root, AUTHORITY, EXECUTOR)
    assert other.status(cmd.id)['acknowledged'] and not other.status(cmd.id)['outputs_released']
    assert other.status(cmd.id)['receipt_hash'] == receipt_hash(receipt)
    assert other.collect(0)['collected_commands'] == 0
    other.ack(cmd.id, receipt_hash(receipt), True)
    assert other.collect(0)['collected_commands'] == 1


@pytest.mark.parametrize('value', [-1, True, float('inf'), float('nan'), '0', 31536001])
def test_retention_validation(environment, value):
    journal, _, _ = environment
    with pytest.raises(SchedulerError, match='invalid_retention'):
        journal.collect(value)


async def test_authenticated_gc_endpoints_preserve_old_ack_semantics(environment):
    journal, runtime, dep = environment
    cmd, receipt = finish(journal, dep, stream=True)
    app = Executor(journal, runtime, KEY)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://executor') as client:
        assert (await client.get('/internal/v1/usage')).status_code == 401
        client.headers['authorization'] = 'Bearer ' + KEY
        path = '/internal/v1/commands/' + cmd.id
        assert (await client.post(path, json={'receipt_hash': receipt_hash(receipt)})).status_code == 200
        assert (await client.post('/internal/v1/collect', json={'retention_seconds': 0})).json()['collected_commands'] == 0
        assert (await client.post(path, json={'receipt_hash': receipt_hash(receipt), 'release_outputs': 'true'})).status_code == 400
        assert (await client.post(path, json={'receipt_hash': receipt_hash(receipt), 'release_outputs': True})).status_code == 200
        assert (await client.get('/internal/v1/usage')).json()['reclaimable_commands'] == 1
        assert (await client.post('/internal/v1/collect', json={'retention_seconds': 0})).json()['collected_commands'] == 1
        replay = await client.post('/internal/v1/commands', json=cmd.model_dump())
        assert replay.json()['state'] == 'collected' and runtime.calls == 0


async def test_inventory_requires_runtime_identity_and_never_discloses_paths(environment):
    journal, runtime, dep = environment
    app = Executor(journal, runtime, KEY)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://executor') as client:
        assert (await client.get('/internal/v1/inventory')).status_code == 401
        client.headers['authorization'] = 'Bearer ' + KEY
        assert (await client.get('/internal/v1/inventory')).status_code == 503
        runtime.inventory_identity = lambda: {'resource_id': 'unique-cpu-fixture', 'kind': 'cpu-fixture'}
        response = await client.get('/internal/v1/inventory')
        assert response.json() == {'version': 1, 'authority': AUTHORITY, 'executor': EXECUTOR,
                                   'resource_id': 'unique-cpu-fixture', 'kind': 'cpu-fixture',
                                   'deployments': [dep.model_dump()]}
        assert str(journal.root) not in response.text and 'path' not in response.text
        async def identity():
            return {'resource_id': 'GPU-fixture-uuid', 'kind': 'gpu'}
        runtime.inventory_identity = identity
        assert (await client.get('/internal/v1/inventory')).json()['resource_id'] == 'GPU-fixture-uuid'
        runtime.inventory_identity = lambda: {'kind': 'gpu'}
        assert (await client.get('/internal/v1/inventory')).status_code == 503
