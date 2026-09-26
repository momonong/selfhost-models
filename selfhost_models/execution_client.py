"""Control-side transport. It owns no Docker socket, model path or worker key."""
import asyncio
import base64
import contextlib
import hashlib
import json
import sqlite3
import uuid
from pathlib import Path

import httpx

from .execution_protocol import Command, TerminalRejection, validate_manifest
from .execution_retention import ControlRetention
from .scheduler_schema import SchedulerError, canonical, digest


class RemoteProvider:
    remote_execution = True

    def __init__(self, store, *, transport=None):
        self.store = store
        self.binding = store.execution_binding()
        if self.binding is None:
            raise SchedulerError('execution_binding_required', 409)
        key = Path(self.binding['key_file']).read_text().strip()
        if len(key) < 32:
            raise SchedulerError('invalid_executor_secret')
        self.client = httpx.AsyncClient(base_url=self.binding['url'], trust_env=False,
            headers={'Authorization': 'Bearer ' + key}, timeout=httpx.Timeout(10, read=30), transport=transport)
        self.epoch = None
        self.retention = ControlRetention(store)

    async def request(self, method, path, **kwargs):
        response = await self.client.request(method, '/internal/v1' + path, **kwargs)
        if response.is_error:
            # Protocol rejection is NEVER exposed as a terminal worker 400/404/422.
            raise SchedulerError('executor_protocol_unconfirmed', 503)
        return response.json()

    async def preflight(self):
        value = await self.request('GET', '/identity')
        if value != {'version': 1, 'authority': self.binding['control_id'], 'executor': self.binding['executor_id']}:
            raise SchedulerError('executor_identity_mismatch', 409)

    async def fence(self, epoch):
        await self.request('POST', '/fence', json={'authority': self.binding['control_id'],
            'executor': self.binding['executor_id'], 'fence': epoch})
        self.epoch = epoch

    def persist(self, command):
        return self.retention.persist(command)

    def command(self, kind, dep=None, handle=None, *, grant=None, payload=None):
        fence = grant['fence'] if grant else self.epoch
        cid = grant['attempt'] if grant else digest([self.binding['executor_id'], kind, handle, dep.id if dep else None,
            fence if kind in ('prepare', 'unload', 'retire', 'release') else None])
        # Retain old persisted lifecycle IDs on schema upgrades. Their immutable
        # envelope binds the executor, so this cannot alias another worker.
        if grant is None and kind != 'prepare':
            previous_id = digest([kind, handle, dep.id if dep else None,
                fence if kind in ('unload', 'retire', 'release') else None])
            previous = self.retention.row(previous_id)
            if previous and previous['executor'] == self.binding['executor_id']:
                cid = previous_id
        # prepare has no GPU effect; each fresh cycle verifies prerequisites.
        if kind == 'prepare':
            cid = uuid.uuid4().hex
        with self.store.tx() as c:
            old = c.execute('SELECT command FROM execution_commands WHERE id=?', (cid,)).fetchone()
        if old and kind != 'execute':
            return Command.model_validate(json.loads(old[0]))
        return self.persist(Command.model_validate({'version': 1,
            'authority': self.binding['control_id'], 'executor': self.binding['executor_id'],
            'fence': fence, 'id': cid, 'kind': kind, 'handle': handle,
            'deployment': dep.model_dump() if dep else None, 'grant': grant, 'payload': payload,
            'result_bytes': self.store.config.result_bytes}))

    def checked(self, command, status):
        if (status.get('version'), status.get('executor'), status.get('id'), status.get('hash')) != (
                1, self.binding['executor_id'], command.id, command.content_hash):
            raise SchedulerError('executor_response_mismatch', 503)
        receipt = status.get('receipt')
        if receipt is not None and (receipt.get('version'), receipt.get('executor'), receipt.get('authority'),
                receipt.get('command'), receipt.get('hash'), receipt.get('grant')) != (
                1, self.binding['executor_id'], self.binding['control_id'], command.id, command.content_hash,
                command.grant.model_dump() if command.grant else None):
            raise SchedulerError('executor_receipt_mismatch', 503)
        return status

    async def start_command(self, command):
        # A lost response only retries the SAME persisted envelope, never a new attempt.
        try:
            return self.checked(command, await self.request('POST', '/commands', json=command.model_dump()))
        except httpx.TransportError:
            return self.checked(command, await self.request('POST', '/commands', json=command.model_dump()))

    async def status(self, command, after=0):
        return self.checked(command, await self.request('GET', '/commands/' + command.id, params={'after': after}))

    async def wait(self, command):
        status = await self.start_command(command)
        while status['state'] in ('accepted', 'executing'):
            await asyncio.sleep(.05)
            status = await self.status(command)
        if status['state'] != 'terminal' or status['receipt'] is None:
            raise SchedulerError('executor_outcome_unknown', 503)
        self.retention.save_receipt(command.id, status['receipt'])
        return status['receipt']

    async def lifecycle(self, kind, dep=None, handle=None):
        command = self.command(kind, dep, handle)
        saved = self.retention.row(command.id)
        # A persisted receipt is authoritative even after remote output GC. Never
        # resend a lifecycle effect just to reconstruct already retained evidence.
        receipt = json.loads(saved['receipt']) if saved['receipt'] else await self.wait(command)
        self.retention.release(command.id)
        try:
            await self.ack(command.id)
        except (httpx.HTTPError, SchedulerError, OSError, sqlite3.Error):
            pass  # Durable ACK/release outbox retries on reconciliation.
        if receipt['error']:
            raise SchedulerError('executor_lifecycle_failed', 503)
        return receipt['result']

    async def verify_asset(self, ref):
        data = await self.request('GET', '/assets/' + ref)
        if validate_manifest(data['manifest']) != ref or data['manifest'] != self.store.asset_manifest(ref):
            raise SchedulerError('asset_hash_mismatch', 409)
        return ref  # A content reference, never an executor-local path.

    async def prepare(self, dep, asset):
        await self.lifecycle('prepare', dep)

    def handle(self, dep, epoch):
        return 'selfhost-scheduler-' + digest(self.binding['executor_id'])[:16] + '-' + str(epoch) + '-' + uuid.uuid4().hex[:12]

    async def load(self, dep, asset, handle):
        return (await self.lifecycle('load', dep, handle))['worker_epoch']

    async def warmup(self, dep, epoch):
        await self.lifecycle('warmup', dep, self.store.state()['handle'])

    async def identity(self):
        state = self.store.state()
        value = await self.request('GET', '/engine')
        if (value['executor'], value['handle'], value['deployment']) != (
                self.binding['executor_id'], state['handle'], state['deployment']):
            raise SchedulerError('executor_engine_mismatch', 503)
        return value['worker_epoch']

    async def execute(self, dep, spec, attempt, store):
        grant = store.execution_grant(attempt['id'])
        payload = spec.input.model_dump(exclude_none=True)
        if dep.runtime == 'whisper':
            data, _ = store.artifact(payload.pop('audio_ref'))
            payload.update(model=dep.model, audio_base64=base64.b64encode(data).decode(), audio_sha256=hashlib.sha256(data).hexdigest())
        command = self.command('execute', dep, grant['handle'], grant=grant, payload=payload)
        receipt = await self.wait(command)
        return receipt['result'], receipt['error']

    async def ack(self, attempt, *, release_outputs=None):
        row = self.retention.row(attempt)
        if row is None:
            return
        grant = json.loads(row['grant_json']) if row['grant_json'] else None
        if row['executor'] != self.binding['executor_id']:
            raise SchedulerError('executor_identity_mismatch', 409)
        if grant:
            # ACK must never precede control-side receipt custody.
            with self.store.tx() as c:
                marker = c.execute('SELECT receipt FROM execution_grants WHERE attempt=?', (attempt,)).fetchone()
            if marker is None or marker[0] is None:
                raise SchedulerError('execution_receipt_not_saved', 409)
        if row['receipt_hash'] is None:
            command = Command.model_validate(json.loads(row['command']))
            status = await self.status(command)
            if status['state'] != 'terminal' or status['receipt'] is None:
                raise SchedulerError('executor_outcome_unknown', 503)
            self.retention.save_receipt(attempt, status['receipt'])
        # Controller ACK of legacy output does not end a live API reader's pin.
        if release_outputs is True or (grant and grant['kind'] == 'job'):
            self.retention.release(attempt)
        row = self.retention.row(attempt)
        release = bool(row['release_intent'])
        await self.request('POST', '/commands/' + attempt, json={
            'receipt_hash': row['receipt_hash'], 'release_outputs': release})
        if grant:
            self.store.execution_ack(attempt)
        self.retention.confirmed(attempt, released=release)

    async def reconcile(self, epoch):
        pending = {g['attempt']: g for g in self.store.execution_pending()}
        pending.update({g['attempt']: g for g in self.retention.release_grants(self.binding['executor_id'])})
        for grant in pending.values():
            if grant['executor'] != self.binding['executor_id']:
                continue
            row = self.retention.row(grant['attempt'])
            if not row or not row['command']:
                continue  # Never synthesize/replay an unconfirmed or collected command.
            command = Command.model_validate(json.loads(row['command']))
            try:
                if grant['kind'] == 'job':
                    with self.store.tx() as c:
                        canceled = c.execute('SELECT cancel_requested FROM jobs WHERE attempt=?', (grant['attempt'],)).fetchone()
                    if canceled and canceled[0]:
                        await self.request('POST', '/commands/' + command.id + '/cancel', json={
                            'authority': self.binding['control_id'], 'executor': self.binding['executor_id'], 'fence': epoch})
                status = await self.status(command)
                if status['state'] == 'terminal' and status['receipt'] is not None:
                    receipt = status['receipt']
                    self.retention.save_receipt(command.id, receipt)
                    self.store.import_executor_receipt(epoch, grant['attempt'], receipt['result'], receipt['error'], grant)
                    await self.ack(grant['attempt'])
            except (httpx.HTTPError, SchedulerError, OSError, sqlite3.Error):
                continue
        # Includes acknowledged legacy receipts whose reader finished later, and
        # lifecycle receipts. These no longer appear in execution_pending().
        for row in self.retention.pending(self.binding['executor_id']):
            try:
                await self.ack(row['id'])
            except (httpx.HTTPError, SchedulerError, OSError, sqlite3.Error):
                continue

    async def unload(self, state):
        await self.lifecycle('unload', self.store.deployment(state['deployment']), state['handle'])

    async def exited(self, state):
        if not state.get('handle'):
            return False
        try:
            value = await self.request('POST', '/exited', json={'handle': state['handle'], 'deployment': state['deployment']})
            return value == {'exited': True, 'handle': state['handle'], 'deployment': state['deployment']}
        except (httpx.HTTPError, SchedulerError):
            return False

    async def retire(self, state):
        await self.lifecycle('retire', self.store.deployment(state['deployment']), state['handle'])

    async def release_ownership(self):
        if self.store.state()['phase'] != 'unloaded' or self.store.lease_count():
            raise SchedulerError('gpu_exit_unconfirmed', 409)
        await self.lifecycle('release')

    async def close(self):
        await self.client.aclose()


class RemoteResponse:
    status_code = 200

    def __init__(self, provider, command):
        self.provider, self.command = provider, command
        self.headers = {'x-worker-epoch': command.grant.worker_epoch}

    async def terminal(self, status):
        if status['state'] != 'terminal' or status['receipt'] is None:
            raise SchedulerError('executor_outcome_unknown', 503)
        r = status['receipt']
        # Persist controller-side receipt before Gateway observes terminal.
        self.provider.store.import_executor_receipt(self.provider.store.state()['epoch'], self.command.id,
                                                    r['result'], r['error'], self.command.grant.model_dump())
        self.provider.retention.save_receipt(self.command.id, r)
        self.provider.retention.release(self.command.id)
        try:
            await self.provider.ack(self.command.id, release_outputs=True)
        except (httpx.HTTPError, SchedulerError, OSError, sqlite3.Error):
            pass  # Durable control receipt exists; controller will retry ACK.
        if r['error']:
            status = r['result'].get('error_status', 504 if r['error'] == 'canceled_before_gpu' else 400)
            raise TerminalRejection(r['error'], status)
        return r['result']

    async def aiter_bytes(self):
        status = await self.provider.status(self.command)
        while status['state'] in ('accepted', 'executing'):
            await asyncio.sleep(.05)
            status = await self.provider.status(self.command)
        yield canonical(await self.terminal(status))

    async def aiter_lines(self):
        cursor = 0
        while True:
            status = await self.provider.status(self.command, cursor)
            for frame in status['frames']:
                if frame['seq'] != cursor + 1:
                    raise SchedulerError('executor_stream_gap', 503)
                cursor = frame['seq']
                yield frame['value']
            # Poll again if a bounded page was full, including terminal trailing frames.
            if len(status['frames']) == 32:
                continue
            if status['state'] == 'terminal':
                result = await self.terminal(status)
                if result != {'stream_terminal': True}:
                    raise SchedulerError('executor_stream_incomplete', 503)
                yield 'data: [DONE]'
                return
            if status['state'] not in ('accepted', 'executing'):
                raise SchedulerError('executor_outcome_unknown', 503)
            await asyncio.sleep(.02)


class RemoteBackend:
    remote_execution = True

    def __init__(self, store, dep):
        self.provider, self.dep = RemoteProvider(store), dep

    async def identity(self):
        return await self.provider.identity()

    @contextlib.asynccontextmanager
    async def generate(self, payload, request_id):
        grant = self.provider.store.execution_grant(request_id)
        if grant is None:
            raise SchedulerError('execution_grant_required', 503)
        command = self.provider.command('execute', self.dep, grant['handle'], grant=grant, payload=payload)
        await self.provider.start_command(command)
        yield RemoteResponse(self.provider, command)

    async def close(self):
        await self.provider.close()
