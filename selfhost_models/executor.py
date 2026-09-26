"""Executor-owned journal and ASGI service. No control Store or shared paths."""
import asyncio
import base64
import contextlib
import hashlib
import hmac
import inspect
import json
import math
import os
import sqlite3
import time
from pathlib import Path

from filelock import FileLock
from pydantic import ValidationError

from .execution_protocol import Command, receipt_hash, validate_manifest
from .scheduler_schema import Deployment, SchedulerError, canonical
from .scheduler_store import full_manifest
from .video import VideoError


class Journal:
    def __init__(self, root, authority, executor, *, max_commands=10000, storage_bytes=1024**3):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.db = self.root / 'executor.sqlite3'
        self.authority, self.executor = authority, executor
        self.max_commands, self.storage_bytes = max_commands, storage_bytes
        with self.tx() as c:
            c.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS identity(authority TEXT, executor TEXT, fence INTEGER);
                CREATE TABLE IF NOT EXISTS assets(ref TEXT PRIMARY KEY, path TEXT, manifest TEXT);
                CREATE TABLE IF NOT EXISTS deployments(id TEXT PRIMARY KEY, spec TEXT);
                CREATE TABLE IF NOT EXISTS engine(id INTEGER PRIMARY KEY CHECK(id=1), value TEXT);
                CREATE TABLE IF NOT EXISTS engine_exits(handle TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS commands(id TEXT PRIMARY KEY, hash TEXT, command TEXT,
                    state TEXT, receipt TEXT, acknowledged INTEGER DEFAULT 0, bytes INTEGER DEFAULT 0,
                    reservation INTEGER, created REAL);
                CREATE TABLE IF NOT EXISTS frames(command TEXT, seq INTEGER, value TEXT,
                    PRIMARY KEY(command,seq));
                CREATE TABLE IF NOT EXISTS cancellations(id TEXT PRIMARY KEY);
            ''')
            columns = {r[1] for r in c.execute('PRAGMA table_info(commands)')}
            for name, declaration in (
                ('receipt_digest', 'TEXT'), ('terminal_at', 'REAL'),
                ('outputs_released', 'INTEGER NOT NULL DEFAULT 0'),
                ('released_at', 'REAL'), ('collected_at', 'REAL'), ('terminal_summary', 'TEXT'),
            ):
                if name not in columns:
                    c.execute(f'ALTER TABLE commands ADD COLUMN {name} {declaration}')
            # Existing ACKs never imply output release. Start retention on upgrade
            # for old terminal rows whose actual finish time was not recorded.
            for old in c.execute("SELECT id,receipt FROM commands WHERE state='terminal' AND receipt_digest IS NULL").fetchall():
                c.execute('UPDATE commands SET receipt_digest=?,terminal_at=? WHERE id=?',
                          (receipt_hash(json.loads(old['receipt'])), time.time(), old['id']))
            row = c.execute('SELECT * FROM identity').fetchone()
            if row and (row['authority'], row['executor']) != (authority, executor):
                raise SchedulerError('executor_identity_mismatch', 409)
            if not row:
                c.execute('INSERT INTO identity VALUES(?,?,0)', (authority, executor))

    @contextlib.contextmanager
    def tx(self):
        c = sqlite3.connect(self.db, timeout=10, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA synchronous=FULL')
        try:
            c.execute('BEGIN IMMEDIATE')
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def recover(self):
        with self.tx() as c:
            # Neither accepted nor executing is replayed after a process crash.
            c.execute("UPDATE commands SET state='unknown' WHERE state IN ('accepted','executing')")
            row = c.execute('SELECT value FROM engine WHERE id=1').fetchone()
            if row:
                e = json.loads(row[0])
                if e.get('phase') != 'exited' and c.execute("SELECT 1 FROM commands WHERE state='unknown' AND json_extract(command,'$.handle')=?", (e['handle'],)).fetchone():
                    e['phase'] = 'unknown'
                    c.execute('UPDATE engine SET value=? WHERE id=1', (canonical(e).decode(),))

    def fence(self, authority, executor, epoch):
        if authority != self.authority or executor != self.executor or type(epoch) is not int or epoch < 1:
            raise SchedulerError('executor_identity_mismatch', 409)
        with self.tx() as c:
            old = c.execute('SELECT fence FROM identity').fetchone()[0]
            if epoch < old:
                raise SchedulerError('stale_controller', 409)
            c.execute('UPDATE identity SET fence=?', (epoch,))

    def register_asset(self, path, expected=None):
        manifest = full_manifest(path)
        if expected is not None and expected != manifest:
            raise SchedulerError('asset_hash_mismatch')
        ref = validate_manifest(manifest)
        with self.tx() as c:
            old = c.execute('SELECT path FROM assets WHERE ref=?', (ref,)).fetchone()
            if old and old[0] != str(Path(path).resolve()):
                raise SchedulerError('asset_already_registered', 409)
            c.execute('INSERT OR IGNORE INTO assets VALUES(?,?,?)', (ref, str(Path(path).resolve()), canonical(manifest).decode()))
        return ref

    def asset(self, ref, verify=False):
        with self.tx() as c:
            row = c.execute('SELECT * FROM assets WHERE ref=?', (ref,)).fetchone()
        if row is None:
            raise SchedulerError('asset_not_found', 404)
        manifest = json.loads(row['manifest'])
        if validate_manifest(manifest) != ref or (verify and full_manifest(row['path']) != manifest):
            raise SchedulerError('asset_hash_mismatch')
        return Path(row['path']), manifest

    def register(self, dep):
        self.asset(dep.asset_ref, verify=True)
        with self.tx() as c:
            c.execute('INSERT OR IGNORE INTO deployments VALUES(?,?)', (dep.id, canonical(dep.model_dump()).decode()))

    def engine(self):
        with self.tx() as c:
            row = c.execute('SELECT value FROM engine WHERE id=1').fetchone()
            return json.loads(row[0]) if row else None

    def set_engine(self, value):
        with self.tx() as c:
            if value is None:
                c.execute('DELETE FROM engine')
            else:
                c.execute('INSERT OR REPLACE INTO engine VALUES(1,?)', (canonical(value).decode(),))
                if value.get('phase') == 'exited' and value.get('exit_container_id'):
                    if (not c.execute('SELECT 1 FROM engine_exits WHERE handle=?', (value['handle'],)).fetchone()
                            and c.execute('SELECT COUNT(*) FROM engine_exits').fetchone()[0] >= self.max_commands):
                        raise SchedulerError('executor_exit_metadata_full', 503)
                    c.execute('INSERT OR REPLACE INTO engine_exits VALUES(?,?)', (value['handle'], canonical(value).decode()))

    def exit_proof(self, handle):
        with self.tx() as c:
            row = c.execute('SELECT value FROM engine_exits WHERE handle=?', (handle,)).fetchone()
            return json.loads(row[0]) if row else None

    def accept(self, command):
        body = canonical(command.model_dump()).decode()
        with self.tx() as c:
            if (command.authority, command.executor) != (self.authority, self.executor):
                raise SchedulerError('executor_identity_mismatch', 409)
            old = c.execute('SELECT * FROM commands WHERE id=?', (command.id,)).fetchone()
            if old:
                if old['hash'] != command.content_hash:
                    raise SchedulerError('command_conflict', 409)
                return False
            if c.execute('SELECT fence FROM identity').fetchone()[0] != command.fence:
                raise SchedulerError('stale_controller', 409)
            if command.deployment:
                dep = c.execute('SELECT spec FROM deployments WHERE id=?', (command.deployment.id,)).fetchone()
                if not dep or dep[0] != canonical(command.deployment.model_dump()).decode():
                    raise SchedulerError('deployment_not_registered', 409)
            count, reserved = c.execute('SELECT COUNT(*),COALESCE(SUM(reservation),0) FROM commands').fetchone()
            reservation = len(body.encode()) + command.result_bytes + 4096
            if count >= self.max_commands or reserved + reservation > self.storage_bytes:
                raise SchedulerError('executor_journal_full', 503)
            if command.kind == 'execute':
                engine_row = c.execute('SELECT value FROM engine WHERE id=1').fetchone()
                e = json.loads(engine_row[0]) if engine_row else {}
                if (e.get('handle'), e.get('deployment'), e.get('worker_epoch'), e.get('fence'), e.get('phase')) != (
                        command.handle, command.deployment.id, command.grant.worker_epoch, command.fence, 'ready'):
                    raise SchedulerError('engine_grant_mismatch', 409)
                # Defense in depth; control still owns shared admission and budgets.
                active = c.execute("SELECT COUNT(*) FROM commands WHERE state IN ('accepted','executing','unknown') AND json_extract(command,'$.kind')='execute' AND json_extract(command,'$.handle')=?", (command.handle,)).fetchone()[0]
                if active >= command.deployment.load.capacity:
                    raise SchedulerError('executor_capacity', 409)
            c.execute("INSERT INTO commands(id,hash,command,state,reservation,created) VALUES(?,?,?,'accepted',?,?)",
                      (command.id, command.content_hash, body, reservation, time.time()))
        return True

    def executing(self, cid):
        with self.tx() as c:
            row = c.execute('SELECT command,state FROM commands WHERE id=?', (cid,)).fetchone()
            if not row or row['state'] != 'accepted':
                return False
            command = Command.model_validate(json.loads(row[0]))
            if c.execute('SELECT fence FROM identity').fetchone()[0] != command.fence:
                # An accepted old intent never runs after fence advancement.
                c.execute("UPDATE commands SET state='unknown' WHERE id=?", (cid,))
                return False
            return c.execute("UPDATE commands SET state='executing' WHERE id=? AND state='accepted'", (cid,)).rowcount == 1

    def check_fence(self, command):
        with self.tx() as c:
            if c.execute('SELECT fence FROM identity').fetchone()[0] != command.fence:
                raise SchedulerError('stale_controller', 409)

    def cancel(self, cid, authority, executor, fence):
        if (authority, executor) != (self.authority, self.executor):
            raise SchedulerError('executor_identity_mismatch', 409)
        with self.tx() as c:
            if type(fence) is not int or c.execute('SELECT fence FROM identity').fetchone()[0] != fence:
                raise SchedulerError('stale_controller', 409)
            if not c.execute('SELECT 1 FROM commands WHERE id=?', (cid,)).fetchone():
                raise SchedulerError('command_not_found', 404)
            c.execute('INSERT OR IGNORE INTO cancellations VALUES(?)', (cid,))

    def canceled(self, cid):
        with self.tx() as c:
            return c.execute('SELECT 1 FROM cancellations WHERE id=?', (cid,)).fetchone() is not None

    def frame(self, cid, value):
        with self.tx() as c:
            row = c.execute('SELECT command,bytes,state FROM commands WHERE id=?', (cid,)).fetchone()
            if not row or row['state'] != 'executing':
                raise SchedulerError('command_not_executing', 409)
            limit = json.loads(row['command'])['result_bytes']
            size = len(value.encode())
            if len(value) > 1048576 or row['bytes'] + size > limit:
                raise SchedulerError('result_too_large', 503)
            seq = c.execute('SELECT COALESCE(MAX(seq),0)+1 FROM frames WHERE command=?', (cid,)).fetchone()[0]
            c.execute('INSERT INTO frames VALUES(?,?,?)', (cid, seq, value))
            c.execute('UPDATE commands SET bytes=bytes+? WHERE id=?', (size, cid))

    def finish(self, command, result, error=None, *, state='terminal'):
        receipt = {'version': 1, 'executor': self.executor, 'authority': self.authority,
                   'command': command.id, 'hash': command.content_hash,
                   'grant': command.grant.model_dump() if command.grant else None,
                   'result': result, 'error': error}
        raw = canonical(receipt)
        if len(canonical(result)) > command.result_bytes:
            raise SchedulerError('result_too_large', 503)
        with self.tx() as c:
            old = c.execute('SELECT state,receipt_digest FROM commands WHERE id=?', (command.id,)).fetchone()
            if not old or old['state'] == 'collected':
                raise SchedulerError('command_not_executing', 409)
            if old['state'] == 'terminal':
                if state != 'terminal' or old['receipt_digest'] != receipt_hash(receipt):
                    raise SchedulerError('receipt_conflict', 409)
                return receipt
            c.execute('UPDATE commands SET state=?,receipt=?,receipt_digest=?,terminal_at=? WHERE id=?',
                      (state, raw.decode(), receipt_hash(receipt), time.time() if state == 'terminal' else None, command.id))
        return receipt

    def status(self, cid, after=0):
        with self.tx() as c:
            row = c.execute('SELECT * FROM commands WHERE id=?', (cid,)).fetchone()
            if not row:
                raise SchedulerError('command_not_found', 404)
            frames = [{'seq': r[0], 'value': r[1]} for r in c.execute('SELECT seq,value FROM frames WHERE command=? AND seq>? ORDER BY seq LIMIT 32', (cid, after))]
            return {'version': 1, 'executor': self.executor, 'id': cid, 'hash': row['hash'], 'state': row['state'],
                    'receipt': json.loads(row['receipt']) if row['receipt'] else None,
                    'acknowledged': bool(row['acknowledged']), 'frames': frames,
                    'receipt_hash': row['receipt_digest'], 'outputs_released': bool(row['outputs_released']),
                    'terminal_summary': json.loads(row['terminal_summary']) if row['terminal_summary'] else None}

    def ack(self, cid, expected, release_outputs=False):
        if type(release_outputs) is not bool:
            raise SchedulerError('invalid_output_release')
        with self.tx() as c:
            row = c.execute('SELECT state,receipt_digest FROM commands WHERE id=?', (cid,)).fetchone()
            if (not row or row['state'] not in ('terminal', 'collected')
                    or row['receipt_digest'] is None or row['receipt_digest'] != expected):
                raise SchedulerError('receipt_ack_mismatch', 409)
            # ACK and reader release are distinct. Repeated ACK cannot revoke a
            # release or extend its retention timer; tombstones retain ACK proof.
            c.execute('UPDATE commands SET acknowledged=1,outputs_released=MAX(outputs_released,?), '
                      'released_at=CASE WHEN ? THEN COALESCE(released_at,?) ELSE released_at END WHERE id=?',
                      (int(release_outputs), release_outputs, time.time(), cid))

    @staticmethod
    def retention(value):
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 31536000:
            raise SchedulerError('invalid_retention')
        return value

    @staticmethod
    def compact(row):
        command = json.loads(row['command'])
        receipt = json.loads(row['receipt'])
        identity = {k: command[k] for k in ('version', 'authority', 'executor', 'fence', 'id', 'kind', 'handle', 'grant')}
        identity['deployment_id'] = Deployment.model_validate(command['deployment']).id if command['deployment'] else None
        summary = {k: receipt[k] for k in ('version', 'executor', 'authority', 'command', 'hash', 'grant', 'error')}
        summary['result_hash'] = hashlib.sha256(canonical(receipt['result'])).hexdigest()
        summary['terminal_at'] = row['terminal_at']
        command_text, summary_text = canonical(identity).decode(), canonical(summary).decode()
        # Keep a conservative allowance for indexes, hashes, flags and timestamps.
        reservation = len(command_text.encode()) + len(summary_text.encode()) + 1024
        return command_text, summary_text, reservation

    def usage(self):
        with self.tx() as c:
            count, reserved = c.execute('SELECT COUNT(*),COALESCE(SUM(reservation),0) FROM commands').fetchone()
            reclaimable, eligible_count = 0, 0
            for row in c.execute("SELECT * FROM commands WHERE state='terminal' AND acknowledged=1 AND outputs_released=1"):
                reclaimable += max(0, row['reservation'] - self.compact(row)[2])
                eligible_count += 1
            states = dict(c.execute('SELECT state,COUNT(*) FROM commands GROUP BY state').fetchall())
            page_size = c.execute('PRAGMA page_size').fetchone()[0]
            page_count = c.execute('PRAGMA page_count').fetchone()[0]
            free_pages = c.execute('PRAGMA freelist_count').fetchone()[0]
            exits = c.execute('SELECT COUNT(*) FROM engine_exits').fetchone()[0]
            fence = c.execute('SELECT fence FROM identity').fetchone()[0]
        minimum_admission = len(canonical(Command(authority=self.authority, executor=self.executor,
            fence=max(1, fence), id='0' * 32, kind='release', result_bytes=1024).model_dump())) + 5120
        available = max(0, self.storage_bytes - reserved)
        # Admission may also reject a particular larger envelope even when a
        # smaller one fits; report that fact without claiming unlimited space.
        reasons = []
        if count >= self.max_commands:
            reasons.append('metadata_slots_exhausted')
        if available < minimum_admission:
            reasons.append('reservation_bytes_exhausted')
        return {'commands': count, 'states': states, 'max_commands': self.max_commands,
                'metadata_slots_remaining': max(0, self.max_commands - count),
                'storage_bytes': self.storage_bytes, 'reserved_bytes': reserved,
                'available_bytes': available, 'reclaimable_bytes': reclaimable,
                'reclaimable_commands': eligible_count, 'full_reasons': reasons,
                'minimum_admission_bytes': minimum_admission,
                'admission_depends_on_command_size': True, 'engine_exit_proofs': exits,
                'engine_exit_slots_remaining': max(0, self.max_commands - exits),
                'sqlite': {'page_size': page_size, 'page_count': page_count,
                           'free_pages': free_pages, 'reusable_bytes': free_pages * page_size,
                           'allocated_page_bytes': page_count * page_size,
                           'database_file_bytes': self.db.stat().st_size,
                           'wal_file_bytes': (Path(str(self.db) + '-wal').stat().st_size
                                              if Path(str(self.db) + '-wal').exists() else 0)}}

    def collect(self, retention_seconds=86400):
        retention_seconds = self.retention(retention_seconds)
        now, freed, count = time.time(), 0, 0
        with self.tx() as c:
            rows = c.execute("SELECT id FROM commands WHERE state='terminal' AND acknowledged=1 "
                             "AND outputs_released=1 AND terminal_at<=? AND released_at<=?",
                             (now - retention_seconds, now - retention_seconds)).fetchall()
            for candidate in rows:
                row = c.execute('SELECT * FROM commands WHERE id=?', (candidate['id'],)).fetchone()
                command, summary, reservation = self.compact(row)
                c.execute('DELETE FROM frames WHERE command=?', (row['id'],))
                c.execute('DELETE FROM cancellations WHERE id=?', (row['id'],))
                c.execute("UPDATE commands SET state='collected',command=?,receipt=NULL,bytes=0,"
                          'reservation=?,terminal_summary=?,collected_at=? WHERE id=?',
                          (command, reservation, summary, now, row['id']))
                freed += row['reservation'] - reservation
                count += 1
        # No VACUUM/checkpoint is implied: SQLite reuses freed pages; physical
        # database/WAL size is measured separately in usage().
        return {'collected_commands': count, 'released_reserved_bytes': freed, 'retention_seconds': retention_seconds}


class Executor:
    def __init__(self, journal, runtime, key):
        if len(key) < 32:
            raise ValueError('executor secret too short')
        self.journal, self.runtime, self.key = journal, runtime, key
        self.tasks = set()
        self.lifecycle = asyncio.Lock()
        self.lock = FileLock(str(journal.root / 'executor.lock'), timeout=0)
        self.clients = 0

    async def start(self):
        self.lock.acquire()
        self.journal.recover()
        if self.runtime.secret_path.exists():
            self.runtime.key = self.runtime.secret_path.read_text().strip()

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.runtime.close()
        self.lock.release()

    async def submit(self, command):
        if self.journal.accept(command):
            task = asyncio.create_task(self.run(command))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        return self.journal.status(command.id)

    async def engine_exited(self, state):
        e = self.journal.exit_proof(state['handle']) or self.journal.engine()
        if not e or e['handle'] != state['handle'] or e['deployment'] != state['deployment']:
            raise SchedulerError('engine_identity_unconfirmed', 409)
        if e.get('phase') == 'exited' and e.get('exit_container_id'):
            return True  # Persisted exact exit proof also survives partial retire.
        info = await self.runtime.inspect(e)
        if e.get('container_id') and info['Id'] != e['container_id']:
            raise SchedulerError('engine_identity_unconfirmed', 409)
        exited = await self.runtime.exited(e)
        if exited:
            e['phase'] = 'exited'
            e['exit_container_id'] = info['Id']
            self.journal.set_engine(e)
        return exited

    async def run(self, command):
        if not self.journal.executing(command.id):
            return
        state = 'terminal'
        try:
            if command.kind == 'execute':
                limit = getattr(getattr(self.runtime, 'config', None), 'drain_seconds', 600)
                async with asyncio.timeout(limit):
                    result, error = await self.execute(command)
            else:
                async with self.lifecycle:
                    self.journal.check_fence(command)
                    config = getattr(self.runtime, 'config', None)
                    field = {'load': 'load_seconds', 'prepare': 'load_seconds',
                             'warmup': 'warmup_seconds'}.get(command.kind, 'unload_seconds')
                    async with asyncio.timeout(getattr(config, field, 300)):
                        result, error = await self.operation(command), None
        except asyncio.CancelledError:
            with contextlib.suppress(OSError, sqlite3.Error):
                self.journal.finish(command, None, 'executor_stopped', state='unknown')
            raise
        except Exception as exc:
            # No transport/runtime exception is evidence of non-dispatch or terminal.
            result, error, state = None, getattr(exc, 'code', 'execution_unconfirmed'), 'unknown'
        # Once output exists, retry only persistence. Do not discard the sole
        # result by replacing it with an unknown receipt on a transient I/O error.
        while True:
            try:
                self.journal.finish(command, result, error, state=state)
                return
            except (OSError, sqlite3.Error):
                await asyncio.sleep(.1)

    async def operation(self, cmd):
        dep, e = cmd.deployment, self.journal.engine()
        if cmd.kind == 'retire':
            e = self.journal.exit_proof(cmd.handle) or e
        if cmd.kind == 'prepare':
            if e is not None:
                raise SchedulerError('engine_still_owned', 409)
            path, _ = await asyncio.to_thread(self.journal.asset, dep.asset_ref, True)
            self.journal.check_fence(cmd)
            await self.runtime.prepare(dep, path)
            return {'prepared': True}
        if cmd.kind == 'load':
            if e is not None:
                raise SchedulerError('engine_still_owned', 409)
            path, _ = await asyncio.to_thread(self.journal.asset, dep.asset_ref, True)
            self.journal.check_fence(cmd)
            e = {'handle': cmd.handle, 'deployment': dep.id, 'fence': cmd.fence, 'phase': 'loading', 'spec': dep.model_dump()}
            self.journal.set_engine(e)  # Persist before Docker side effect.
            worker_epoch = await self.runtime.load(dep, path, cmd.handle)
            info = await self.runtime.inspect(e)
            e.update(worker_epoch=worker_epoch, phase='warming', container_id=info['Id'])
            self.journal.set_engine(e)
            return {'worker_epoch': worker_epoch}
        if cmd.kind == 'release':
            if e is not None:
                raise SchedulerError('engine_still_owned', 409)
            await self.runtime.release_ownership(engine_unloaded=True, leases_resolved=True)
            return {'released': True}
        if not e or (e['handle'], e['deployment']) != (cmd.handle, dep.id):
            raise SchedulerError('engine_identity_unconfirmed', 409)
        if cmd.kind == 'warmup':
            if e['fence'] != cmd.fence or e['phase'] != 'warming':
                raise SchedulerError('engine_grant_mismatch', 409)
            if self.runtime.backend is None:
                self.runtime.set_backend(dep)
            await self.runtime.warmup(dep, e['worker_epoch'])
            e['phase'] = 'ready'
            self.journal.set_engine(e)
            return {'ready': True}
        if cmd.kind == 'unload':
            e['phase'] = 'unloading'
            self.journal.set_engine(e)
            await self.runtime.unload(e)
            if not await self.engine_exited(e):
                raise SchedulerError('engine_exit_unconfirmed', 503)
            return {'exited': True, 'handle': e['handle']}
        if cmd.kind == 'retire':
            if not await self.engine_exited(e):
                raise SchedulerError('engine_exit_unconfirmed', 503)
            await self.runtime.retire(e)
            current = self.journal.engine()
            if current and current['handle'] == e['handle']:
                self.journal.set_engine(None)
            return {'retired': True}
        raise SchedulerError('unknown_operation')

    async def execute(self, cmd):
        dep, g = cmd.deployment, cmd.grant
        payload = cmd.payload.model_dump(exclude_none=True)
        deadline = g.started + g.execution_limit
        if time.time() >= deadline or self.journal.canceled(cmd.id):
            return {'canceled_before_gpu': True}, 'canceled_before_gpu'
        if dep.runtime == 'whisper':
            raw = base64.b64decode(payload['audio_base64'], validate=True)
            if len(raw) > 1048576 or hashlib.sha256(raw).hexdigest() != payload.pop('audio_sha256'):
                return {'error': 'invalid_audio_digest'}, 'invalid_audio_digest'
        if not payload.get('stream'):
            try:
                return await self.runtime.execute_payload(dep, payload, cmd.id, g.worker_epoch, cmd.result_bytes, deadline,
                                                          cancelled=lambda: self.journal.canceled(cmd.id))
            except VideoError as exc:
                return {'error': exc.code, 'error_status': exc.status}, exc.code  # Decoder reaped, no GPU dispatch.
        # Journal each chunk before exposing it. Consumer disconnect cannot cancel us.
        try:
            async with self.runtime.generate_stream(dep, payload, cmd.id, g.worker_epoch, deadline=deadline,
                                                    cancelled=lambda: self.journal.canceled(cmd.id)) as response:
                if response.status_code in (400, 404, 422):
                    return {'error': 'worker_rejected'}, 'worker_rejected'
                response.raise_for_status()
                first = True
                async for line in response.aiter_lines():
                    if not line.startswith('data:'):
                        continue
                    data = line[5:].strip()
                    if data == '[DONE]':
                        # Adapter emits DONE only after the terminal receipt is durable.
                        return {'stream_terminal': True}, None
                    value = json.loads(data)
                    if 'error' in value:
                        raise SchedulerError('worker_stream_failed', 503)
                    if first and response.extensions.get('selfhost_video'):
                        value['video'] = response.extensions['selfhost_video']
                    first = False
                    self.journal.frame(cmd.id, 'data: ' + json.dumps(value))
        except VideoError as exc:
            return {'error': exc.code, 'error_status': exc.status}, exc.code
        except SchedulerError as exc:
            if exc.code == 'canceled_before_gpu':
                return {'canceled_before_gpu': True}, exc.code
            raise
        raise SchedulerError('worker_stream_incomplete', 503)

    async def reply(self, send, status, value):
        await send({'type': 'http.response.start', 'status': status, 'headers': [(b'content-type', b'application/json')]})
        await send({'type': 'http.response.body', 'body': canonical(value)})

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'lifespan':
            while True:
                event = await receive()
                if event['type'] == 'lifespan.startup':
                    try:
                        await self.start()
                        await send({'type': 'lifespan.startup.complete'})
                    except Exception:
                        await send({'type': 'lifespan.startup.failed', 'message': 'executor startup failed'})
                        return
                else:
                    await self.close()
                    await send({'type': 'lifespan.shutdown.complete'})
                    return
        if scope['type'] != 'http':
            return
        entered = False
        try:
            headers = dict(scope['headers'])
            if not hmac.compare_digest(headers.get(b'authorization', b''), ('Bearer ' + self.key).encode()):
                raise SchedulerError('unauthorized', 401)
            if self.clients >= 8:
                raise SchedulerError('executor_busy', 429)
            self.clients += 1
            entered = True
            path, method = scope['path'], scope['method']
            body = bytearray()
            if method == 'POST':
                async with asyncio.timeout(15):
                    while True:
                        event = await receive()
                        if event['type'] == 'http.disconnect':
                            return
                        body.extend(event.get('body', b''))
                        if len(body) > 26 * 1024**2:
                            raise SchedulerError('input_too_large', 413)
                        if not event.get('more_body'):
                            break
                data = json.loads(body)
            if path == '/internal/v1/identity' and method == 'GET':
                value = {'version': 1, 'executor': self.journal.executor, 'authority': self.journal.authority}
            elif path == '/internal/v1/inventory' and method == 'GET':
                identity_method = getattr(self.runtime, 'inventory_identity', None)
                if identity_method is None:
                    raise SchedulerError('executor_inventory_unavailable', 503)
                identity = identity_method()
                if inspect.isawaitable(identity):
                    identity = await identity
                if (not isinstance(identity, dict) or set(identity) != {'resource_id', 'kind'}
                        or any(not isinstance(v, str) or not 1 <= len(v) <= 256 for v in identity.values())):
                    raise SchedulerError('executor_inventory_unavailable', 503)
                with self.journal.tx() as c:
                    deployments = [Deployment.model_validate(json.loads(row[0])).model_dump()
                                   for row in c.execute('SELECT spec FROM deployments ORDER BY id')]
                value = {'version': 1, 'authority': self.journal.authority,
                         'executor': self.journal.executor, **identity, 'deployments': deployments}
            elif path == '/internal/v1/usage' and method == 'GET':
                value = self.journal.usage()
            elif path == '/internal/v1/collect' and method == 'POST':
                if not isinstance(data, dict) or set(data) != {'retention_seconds'}:
                    raise SchedulerError('invalid_request')
                value = self.journal.collect(data['retention_seconds'])
            elif path == '/internal/v1/fence' and method == 'POST':
                if set(data) != {'authority', 'executor', 'fence'}:
                    raise SchedulerError('invalid_request')
                self.journal.fence(data['authority'], data['executor'], data['fence'])
                value = {'fenced': True}
            elif path == '/internal/v1/commands' and method == 'POST':
                value = await self.submit(Command.model_validate(data))
            elif path.startswith('/internal/v1/commands/') and path.endswith('/cancel') and method == 'POST':
                if set(data) != {'authority', 'executor', 'fence'}:
                    raise SchedulerError('invalid_request')
                self.journal.cancel(path.split('/')[-2], **data)
                value = {'cancel_requested': True}
            elif path.startswith('/internal/v1/commands/'):
                cid = path.rsplit('/', 1)[1]
                if method == 'GET':
                    from urllib.parse import parse_qs
                    query = parse_qs(scope.get('query_string', b'').decode())
                    after = int(query.get('after', ['0'])[0])
                    if after < 0:
                        raise SchedulerError('invalid_cursor')
                    value = self.journal.status(cid, after)
                elif method == 'POST' and set(data) in ({'receipt_hash'}, {'receipt_hash', 'release_outputs'}):
                    self.journal.ack(cid, data['receipt_hash'], data.get('release_outputs', False))
                    value = {'acknowledged': True}
                else:
                    raise SchedulerError('not_found', 404)
            elif path == '/internal/v1/engine' and method == 'GET':
                e = self.journal.engine()
                epoch = None
                if e and e['phase'] == 'ready':
                    self.runtime.set_backend(Deployment.model_validate(e['spec'])) if self.runtime.backend is None else None
                    epoch = await self.runtime.identity()
                value = {'executor': self.journal.executor, 'handle': e['handle'] if e else None,
                         'deployment': e['deployment'] if e else None, 'worker_epoch': epoch}
            elif path == '/internal/v1/exited' and method == 'POST':
                if set(data) != {'handle', 'deployment'}:
                    raise SchedulerError('invalid_request')
                value = {'exited': await self.engine_exited(data), **data}
            elif path.startswith('/internal/v1/assets/') and method == 'GET':
                _, manifest = self.journal.asset(path.rsplit('/', 1)[1])
                value = {'manifest': manifest}  # Never disclose executor path.
            else:
                raise SchedulerError('not_found', 404)
            await self.reply(send, 200, value)
        except (SchedulerError, ValidationError, ValueError, TimeoutError) as exc:
            await self.reply(send, getattr(exc, 'status', 400), {'error': getattr(exc, 'code', 'invalid_request')})
        except Exception:
            await self.reply(send, 503, {'error': 'executor_unavailable'})
        finally:
            if entered:
                self.clients -= 1
