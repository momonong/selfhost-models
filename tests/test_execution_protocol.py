"""Executor journal/protocol fault cases; all runtimes are CPU-only fixtures."""
import asyncio
from contextlib import asynccontextmanager
import json
import time
import uuid

import httpx
import pytest

from selfhost_models.execution_protocol import Command, receipt_hash
from selfhost_models.executor import Executor, Journal
from selfhost_models.scheduler_schema import Deployment, LoadConfig, SchedulerError

AUTHORITY = 'test-control'
EXECUTOR = 'test-executor'
KEY = 'test-executor-secret-' + 'x' * 32
HANDLE = 'selfhost-scheduler-fixture-engine'


class FakeRuntime:
    def __init__(self, root):
        self.secret_path = root / 'worker-key'
        self.backend = None
        self.started = asyncio.Event()
        self.complete = asyncio.Event()
        self.calls = 0
        self.loads = 0
        self.stream_status = 200
        self.stream_done = True

    async def execute_payload(self, dep, payload, request_id, epoch, limit, deadline=None, *, cancelled=None):
        self.calls += 1
        self.started.set()
        await self.complete.wait()
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': 'retained'}}]}, None

    @asynccontextmanager
    async def generate_stream(self, dep, payload, request_id, epoch, *, deadline=None, cancelled=None):
        runtime = self
        self.calls += 1
        self.started.set()

        class Response:
            status_code = runtime.stream_status
            extensions = {}

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise RuntimeError('synthetic worker failure')

            async def aiter_lines(self):
                yield 'data: ' + json.dumps({'choices': [{'delta': {'content': 'first'}}]})
                await runtime.complete.wait()
                if runtime.stream_done:
                    yield 'data: [DONE]'

        yield Response()

    async def prepare(self, dep, path):
        pass

    async def load(self, dep, path, handle):
        self.loads += 1
        return 'worker-fixture'

    async def inspect(self, state):
        return {'Id': 'container-fixture', 'State': {'Running': True, 'Status': 'running'}}

    async def close(self):
        pass


@pytest.fixture
def environment(tmp_path):
    journal = Journal(tmp_path / 'executor', AUTHORITY, EXECUTOR)
    asset = tmp_path / 'weights'
    asset.mkdir()
    (asset / 'config.json').write_text('{}')
    (asset / 'model.safetensors').write_bytes(b'CPU fixture, not model weights')
    ref = journal.register_asset(asset)
    dep = Deployment(model='Qwen/Qwen3.5-4B', revision='851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a',
                     asset_ref=ref, runtime='vllm', runtime_version='vllm-0.29.0',
                     image='sha256:' + 'a' * 64, load=LoadConfig(capacity=2))
    journal.register(dep)
    journal.fence(AUTHORITY, EXECUTOR, 1)
    journal.set_engine({'handle': HANDLE, 'deployment': dep.id, 'worker_epoch': 'worker-fixture',
                        'phase': 'ready', 'fence': 1, 'spec': dep.model_dump(), 'container_id': 'container-fixture'})
    runtime = FakeRuntime(journal.root)
    return journal, runtime, dep


def execution(dep, *, stream=False, cid=None, fence=1):
    cid = cid or uuid.uuid4().hex
    return Command.model_validate({'authority': AUTHORITY, 'executor': EXECUTOR, 'fence': fence,
        'id': cid, 'kind': 'execute', 'handle': HANDLE, 'deployment': dep.model_dump(),
        'grant': {'authority': AUTHORITY, 'executor': EXECUTOR, 'fence': fence, 'handle': HANDLE,
                  'deployment': dep.id, 'worker_epoch': 'worker-fixture', 'attempt': cid,
                  'owner': 'api-fixture' if stream else 1, 'kind': 'legacy' if stream else 'job',
                  'started': time.time(), 'execution_limit': 60},
        'payload': {'model': dep.model, 'messages': [{'role': 'user', 'content': 'fixture'}], 'stream': stream}})


async def settled(app):
    if app.tasks:
        await asyncio.wait_for(asyncio.gather(*list(app.tasks)), 3)


def test_journal_duplicate_conflict_and_identity_survive_reopen(environment):
    journal, _, dep = environment
    cmd = execution(dep)
    assert journal.accept(cmd)
    other = Journal(journal.root, AUTHORITY, EXECUTOR)
    assert not other.accept(cmd)
    changed = cmd.model_copy(update={'result_bytes': 1024})
    with pytest.raises(SchedulerError, match='command_conflict'):
        other.accept(changed)
    with pytest.raises(SchedulerError, match='executor_identity_mismatch'):
        Journal(journal.root, AUTHORITY, 'different-executor')
    assert other.status(cmd.id)['state'] == 'accepted'


def test_fence_blocks_new_and_delayed_commands_but_preserves_queries(environment):
    journal, _, dep = environment
    cmd = execution(dep)
    journal.accept(cmd)
    journal.fence(AUTHORITY, EXECUTOR, 2)
    assert not journal.executing(cmd.id)
    assert journal.status(cmd.id)['state'] == 'unknown'
    assert not journal.accept(cmd)  # Replay reports existing intent, never starts again.
    with pytest.raises(SchedulerError, match='stale_controller'):
        journal.accept(execution(dep))
    with pytest.raises(SchedulerError, match='stale_controller'):
        journal.fence(AUTHORITY, EXECUTOR, 1)
    with pytest.raises(SchedulerError, match='engine_grant_mismatch'):
        journal.accept(execution(dep, fence=2))


@pytest.mark.parametrize('executing', [False, True])
def test_executor_restart_keeps_incomplete_command_unknown(environment, executing):
    journal, _, dep = environment
    cmd = execution(dep)
    journal.accept(cmd)
    if executing:
        assert journal.executing(cmd.id)
    other = Journal(journal.root, AUTHORITY, EXECUTOR)
    other.recover()
    assert other.status(cmd.id)['state'] == 'unknown'
    assert not other.accept(cmd)
    assert not other.executing(cmd.id)
    assert other.engine()['handle'] == HANDLE


def test_terminal_result_ack_and_frames_remain_durable(environment):
    journal, _, dep = environment
    cmd = execution(dep, stream=True)
    journal.accept(cmd)
    assert journal.executing(cmd.id)
    journal.frame(cmd.id, 'data: {"choices":[]}')
    receipt = journal.finish(cmd, {'stream_terminal': True})
    with pytest.raises(SchedulerError, match='receipt_ack_mismatch'):
        journal.ack(cmd.id, '0' * 64)
    other = Journal(journal.root, AUTHORITY, EXECUTOR)
    other.recover()
    assert other.status(cmd.id)['receipt'] == receipt
    other.ack(cmd.id, receipt_hash(receipt))
    other.ack(cmd.id, receipt_hash(receipt))
    reopened = Journal(journal.root, AUTHORITY, EXECUTOR)
    state = reopened.status(cmd.id)
    assert state['acknowledged'] and state['state'] == 'terminal'
    assert state['receipt'] == receipt and len(state['frames']) == 1
    assert not reopened.accept(cmd)
    assert not reopened.executing(cmd.id)


def test_unknown_receipt_cannot_be_acknowledged(environment):
    journal, _, dep = environment
    cmd = execution(dep)
    journal.accept(cmd)
    journal.executing(cmd.id)
    receipt = journal.finish(cmd, None, 'execution_unconfirmed', state='unknown')
    with pytest.raises(SchedulerError, match='receipt_ack_mismatch'):
        journal.ack(cmd.id, receipt_hash(receipt))
    assert journal.status(cmd.id)['state'] == 'unknown'


async def test_lost_accept_response_query_and_resend_never_execute_twice(environment):
    journal, runtime, dep = environment
    app = Executor(journal, runtime, KEY)
    await app.start()
    cmd = execution(dep)
    scope = {'type': 'http', 'method': 'POST', 'path': '/internal/v1/commands',
             'headers': [(b'authorization', ('Bearer ' + KEY).encode())]}

    async def receive():
        return {'type': 'http.request', 'body': json.dumps(cmd.model_dump()).encode(), 'more_body': False}

    async def lost_send(message):
        raise ConnectionResetError('accept response deliberately lost')

    try:
        with pytest.raises(ConnectionResetError):
            await app(scope, receive, lost_send)
        await asyncio.wait_for(runtime.started.wait(), 2)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://executor',
                                     headers={'authorization': 'Bearer ' + KEY}) as client:
            first = await client.get('/internal/v1/commands/' + cmd.id)
            duplicate = await client.post('/internal/v1/commands', json=cmd.model_dump())
            assert first.json()['state'] == duplicate.json()['state'] == 'executing'
            assert runtime.calls == 1
            runtime.complete.set()
            await settled(app)
            terminal = (await client.get('/internal/v1/commands/' + cmd.id)).json()
            assert terminal['state'] == 'terminal' and terminal['receipt']['result']['choices']
            replay = await client.post('/internal/v1/commands', json=cmd.model_dump())
            assert replay.json()['receipt'] == terminal['receipt'] and runtime.calls == 1
    finally:
        runtime.complete.set()
        await app.close()


async def test_sse_producer_survives_cancelled_submission_observer(environment):
    journal, runtime, dep = environment
    app = Executor(journal, runtime, KEY)
    await app.start()
    cmd = execution(dep, stream=True)
    response_started = asyncio.Event()
    scope = {'type': 'http', 'method': 'POST', 'path': '/internal/v1/commands',
             'headers': [(b'authorization', ('Bearer ' + KEY).encode())]}

    async def receive():
        return {'type': 'http.request', 'body': json.dumps(cmd.model_dump()).encode()}

    async def blocked_send(message):
        response_started.set()
        await asyncio.Event().wait()

    observer = asyncio.create_task(app(scope, receive, blocked_send))
    try:
        await asyncio.wait_for(response_started.wait(), 2)
        await asyncio.wait_for(runtime.started.wait(), 2)
        assert journal.status(cmd.id)['frames']
        observer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await observer
        assert journal.status(cmd.id)['state'] == 'executing' and runtime.calls == 1
        assert not journal.status(cmd.id)['receipt']
        runtime.complete.set()
        await settled(app)
        status = journal.status(cmd.id)
        assert status['state'] == 'terminal' and status['receipt']['result']['stream_terminal']
        assert len(status['frames']) == 1 and runtime.calls == 1
    finally:
        observer.cancel()
        await asyncio.gather(observer, return_exceptions=True)
        runtime.complete.set()
        await app.close()


@pytest.mark.parametrize('worker_status,done,expected', [(404, True, 'terminal'), (200, False, 'unknown')])
async def test_worker_rejection_differs_from_missing_protocol_command(environment, worker_status, done, expected):
    journal, runtime, dep = environment
    runtime.stream_status, runtime.stream_done = worker_status, done
    runtime.complete.set()
    app = Executor(journal, runtime, KEY)
    await app.start()
    cmd = execution(dep, stream=True)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://executor',
                                     headers={'authorization': 'Bearer ' + KEY}) as client:
            missing = await client.get('/internal/v1/commands/' + '0' * 32)
            assert missing.status_code == 404 and missing.json() == {'error': 'command_not_found'}
            assert 'receipt' not in missing.json()  # Protocol rejection is not a compute-terminal proof.
            await client.post('/internal/v1/commands', json=cmd.model_dump())
            await settled(app)
            status = (await client.get('/internal/v1/commands/' + cmd.id)).json()
            assert status['state'] == expected
            assert status['receipt']['error'] == ('worker_rejected' if worker_status == 404 else 'worker_stream_incomplete')
    finally:
        await app.close()


async def test_lifecycle_waiting_for_lock_is_fenced_before_external_load(environment):
    journal, runtime, dep = environment
    journal.set_engine(None)
    app = Executor(journal, runtime, KEY)
    await app.start()
    cmd = Command(authority=AUTHORITY, executor=EXECUTOR, fence=1, id=uuid.uuid4().hex,
                  kind='load', handle=HANDLE, deployment=dep)
    await app.lifecycle.acquire()
    try:
        await app.submit(cmd)
        # Yield until run has durably marked executing and is waiting for lifecycle.
        for _ in range(20):
            if journal.status(cmd.id)['state'] == 'executing':
                break
            await asyncio.sleep(0)
        assert journal.status(cmd.id)['state'] == 'executing'
        journal.fence(AUTHORITY, EXECUTOR, 2)
        app.lifecycle.release()
        await settled(app)
        assert runtime.loads == 0
        assert journal.status(cmd.id)['state'] == 'unknown'
    finally:
        if app.lifecycle.locked():
            app.lifecycle.release()
        await app.close()


async def test_receipt_storage_failure_retains_sole_result_without_reexecution(environment, monkeypatch):
    journal, runtime, dep = environment
    app = Executor(journal, runtime, KEY)
    await app.start()
    cmd = execution(dep)
    runtime.complete.set()
    original = journal.finish
    failures = 0

    def fail_first_terminal(command, result, error=None, *, state='terminal'):
        nonlocal failures
        if state == 'terminal' and failures == 0:
            failures += 1
            raise OSError('synthetic temporary result storage failure')
        return original(command, result, error, state=state)

    monkeypatch.setattr(journal, 'finish', fail_first_terminal)
    try:
        await app.submit(cmd)
        await settled(app)
        status = journal.status(cmd.id)
        assert failures == 1 and runtime.calls == 1
        assert status['state'] == 'terminal'
        assert status['receipt']['result']['choices'][0]['message']['content'] == 'retained'
        assert status['receipt']['error'] is None
    finally:
        await app.close()
