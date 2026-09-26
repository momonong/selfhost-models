"""Bounded control command outbox and explicit remote-output release intents.

ACK proves receipt custody; release additionally proves no live output reader.
Tombstones remain for the lifetime of this authority, so the metadata slot limit
is deliberate and is never recycled into permission to replay a command.
"""
import json
import math
from pathlib import Path

from .execution_protocol import receipt_hash
from .scheduler_schema import SchedulerError, canonical, digest


class ControlRetention:
    def __init__(self, store, *, max_commands=None, storage_bytes=None):
        self.store = store
        with store.tx() as c:
            c.execute('CREATE TABLE IF NOT EXISTS execution_retention_limits(id INTEGER PRIMARY KEY CHECK(id=1), max_commands INTEGER NOT NULL, storage_bytes INTEGER NOT NULL)')
            c.execute('INSERT OR IGNORE INTO execution_retention_limits VALUES(1,?,?)',
                      (max_commands if max_commands is not None else 10000,
                       storage_bytes if storage_bytes is not None else 268435456))
            self.max_commands, self.storage_bytes = c.execute('SELECT max_commands,storage_bytes FROM execution_retention_limits WHERE id=1').fetchone()
            if (max_commands is not None and max_commands != self.max_commands) or (storage_bytes is not None and storage_bytes != self.storage_bytes):
                raise SchedulerError('control_retention_limits_immutable', 409)
            if self.max_commands < 1 or self.storage_bytes < 1:
                raise SchedulerError('invalid_retention_limits', 400)
            c.execute('CREATE TABLE IF NOT EXISTS execution_commands(id TEXT PRIMARY KEY, command TEXT NOT NULL)')
            c.execute('''CREATE TABLE IF NOT EXISTS execution_retention(
                id TEXT PRIMARY KEY, hash TEXT NOT NULL, executor TEXT NOT NULL, kind TEXT NOT NULL,
                grant_json TEXT, receipt TEXT, receipt_hash TEXT, acked INTEGER NOT NULL DEFAULT 0,
                release_intent INTEGER NOT NULL DEFAULT 0, released INTEGER NOT NULL DEFAULT 0,
                collected INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, reservation INTEGER NOT NULL, released_at REAL)''')
            if 'released_at' not in {r[1] for r in c.execute('PRAGMA table_info(execution_retention)')}:
                c.execute('ALTER TABLE execution_retention ADD COLUMN released_at REAL')
            for row in c.execute('''SELECT * FROM execution_commands WHERE id NOT IN
                    (SELECT id FROM execution_retention)''').fetchall():
                body = json.loads(row['command'])
                self._insert(c, body, digest(body))

    def _insert(self, c, body, content_hash):
        c.execute('''INSERT INTO execution_retention(id,hash,executor,kind,grant_json,created,reservation)
            VALUES(?,?,?,?,?,?,?)''', (body['id'], content_hash, body['executor'], body['kind'],
            canonical(body['grant']).decode() if body.get('grant') else None,
            self.store.clock(), body.get('result_bytes', 2097152) + 8192))

    def _usage(self, c):
        count, size, reserved, collected = c.execute('''SELECT COUNT(*),
            COALESCE(SUM(length(CAST(x.command AS BLOB)) + COALESCE(length(CAST(r.receipt AS BLOB)),0)
                + COALESCE(length(CAST(r.grant_json AS BLOB)),0) + 1024),0),
            COALESCE(SUM(reservation),0), COALESCE(SUM(collected),0)
            FROM execution_retention r JOIN execution_commands x USING(id)''').fetchone()
        grant_records = c.execute('SELECT COUNT(*) FROM execution_grants').fetchone()[0]
        size += grant_records * 8192  # Fixed allowance covers envelope, marker and index metadata.
        return {'grant_records': grant_records, 'remaining_grant_slots': max(0, self.max_commands-grant_records),
                'commands': count, 'max_commands': self.max_commands,
                'remaining_slots': max(0, self.max_commands-count), 'metadata_slots_recycled': False,
                'logical_bytes': size, 'reserved_bytes': reserved, 'storage_bytes': self.storage_bytes,
                'remaining_bytes': max(0, self.storage_bytes-size-reserved), 'collected': collected}

    def usage(self):
        with self.store.tx() as c:
            result = self._usage(c)
            result['sqlite_allocated_bytes'] = c.execute('PRAGMA page_count').fetchone()[0] * c.execute('PRAGMA page_size').fetchone()[0]
            result['sqlite_free_pages'] = c.execute('PRAGMA freelist_count').fetchone()[0]
            database = Path(c.execute('PRAGMA database_list').fetchone()[2])
            result['sqlite_file_bytes'] = database.stat().st_size if database.exists() else 0
            try:
                result['sqlite_wal_bytes'] = Path(str(database)+'-wal').stat().st_size
            except FileNotFoundError:
                result['sqlite_wal_bytes'] = 0
            result['pending_release'] = c.execute('SELECT COUNT(*) FROM execution_retention WHERE release_intent=1 AND released=0').fetchone()[0]
        return result

    def persist(self, command):
        body = canonical(command.model_dump()).decode()
        with self.store.tx() as c:
            old = c.execute('SELECT * FROM execution_retention WHERE id=?', (command.id,)).fetchone()
            if old:
                if old['hash'] != command.content_hash:
                    raise SchedulerError('command_conflict', 409)
                if old['collected'] and command.kind == 'execute':
                    raise SchedulerError('command_outputs_collected', 409)
                return command
            usage = self._usage(c)
            required = len(body.encode()) + command.result_bytes + 8192 + 1024
            if command.grant:
                required += len(canonical(command.grant.model_dump()))
            if usage['remaining_slots'] < 1:
                raise SchedulerError('control_command_metadata_full', 503)
            if required > usage['remaining_bytes']:
                raise SchedulerError('control_command_storage_full', 503)
            c.execute('INSERT INTO execution_commands VALUES(?,?)', (command.id, body))
            self._insert(c, command.model_dump(), command.content_hash)
        return command

    def row(self, cid):
        with self.store.tx() as c:
            row = c.execute('SELECT r.*,x.command FROM execution_retention r JOIN execution_commands x USING(id) WHERE id=?', (cid,)).fetchone()
            return dict(row) if row else None

    def save_receipt(self, cid, receipt):
        body, rhash = canonical(receipt).decode(), receipt_hash(receipt)
        with self.store.tx() as c:
            row = c.execute('SELECT * FROM execution_retention WHERE id=?', (cid,)).fetchone()
            if row is None:
                raise SchedulerError('command_not_saved', 409)
            if row['receipt_hash'] and row['receipt_hash'] != rhash:
                raise SchedulerError('receipt_conflict', 409)
            if row['collected']:
                return
            # The reservation made BEFORE dispatch guarantees normal receipt space.
            if row['receipt'] is None and len(body.encode()) > row['reservation']:
                raise SchedulerError('control_receipt_storage_full', 503)
            c.execute('UPDATE execution_retention SET receipt=?,receipt_hash=?,reservation=0 WHERE id=?', (body,rhash,cid))

    def release(self, cid):
        with self.store.tx() as c:
            row = c.execute('SELECT * FROM execution_retention WHERE id=?', (cid,)).fetchone()
            if row is None or row['receipt_hash'] is None:
                raise SchedulerError('execution_receipt_not_saved', 409)
            if row['grant_json']:
                marker = c.execute('SELECT receipt FROM execution_grants WHERE attempt=?', (cid,)).fetchone()
                if not marker or marker[0] is None:
                    raise SchedulerError('execution_receipt_not_saved', 409)
            c.execute('UPDATE execution_retention SET release_intent=1 WHERE id=?', (cid,))

    def abandon_legacy(self, current_owner):
        """Call only after the API singleton lock proves previous producers exited."""
        binding = self.store.execution_binding()
        with self.store.tx() as c:
            rows = c.execute('SELECT id,grant_json,executor FROM execution_retention WHERE grant_json IS NOT NULL AND released=0').fetchall()
            for row in rows:
                grant = json.loads(row['grant_json'])
                if binding and row['executor'] != binding['executor_id']:
                    continue
                if grant['kind'] == 'legacy' and grant['owner'] != current_owner:
                    c.execute('UPDATE execution_retention SET release_intent=1 WHERE id=?', (row['id'],))

    def confirmed(self, cid, *, released):
        with self.store.tx() as c:
            c.execute('UPDATE execution_retention SET acked=1,released=MAX(released,?),released_at=CASE WHEN ? THEN COALESCE(released_at,?) ELSE released_at END WHERE id=?', (int(released),int(released),self.store.clock(),cid))

    def release_grants(self, executor):
        with self.store.tx() as c:
            return [json.loads(r[0]) for r in c.execute('SELECT grant_json FROM execution_retention WHERE executor=? AND release_intent=1 AND released=0 AND grant_json IS NOT NULL AND receipt_hash IS NULL', (executor,))]

    def pending(self, executor):
        with self.store.tx() as c:
            return [dict(r) for r in c.execute('''SELECT * FROM execution_retention WHERE executor=?
                AND receipt_hash IS NOT NULL AND (acked=0 OR (release_intent=1 AND released=0))''', (executor,))]

    def collect(self, *, retention_seconds=86400):
        if type(retention_seconds) not in (int,float) or not math.isfinite(retention_seconds) or retention_seconds < 0:
            raise SchedulerError('invalid_retention', 400)
        with self.store.tx() as c:
            rows = c.execute('''SELECT id FROM execution_retention WHERE kind='execute' AND collected=0
                AND acked=1 AND release_intent=1 AND released=1 AND receipt_hash IS NOT NULL AND released_at<=?''',
                (self.store.clock()-retention_seconds,)).fetchall()
            for row in rows:
                c.execute("UPDATE execution_commands SET command='' WHERE id=?", (row['id'],))
                c.execute('UPDATE execution_retention SET receipt=NULL,collected=1 WHERE id=?', (row['id'],))
        return {'collected_commands': len(rows), **self.usage()}


def can_collect_attempt(c, attempt):
    """Read within Store.collect's transaction; missing remote metadata is pinned."""
    grant = c.execute('SELECT receipt,acked FROM execution_grants WHERE attempt=?', (attempt,)).fetchone()
    if grant is None:
        return True  # Local runtime attempt has no remote output contract.
    if grant['receipt'] is None or not grant['acked']:
        return False
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_retention'").fetchone():
        return False
    row = c.execute('SELECT acked,released FROM execution_retention WHERE id=?', (attempt,)).fetchone()
    return bool(row and row['acked'] and row['released'])


def reserve_execution_grant(c, grant=None):
    """Call before inserting a new grant, within its admission transaction."""
    if grant is not None and len(canonical(grant)) > 6144:
        raise SchedulerError('control_grant_metadata_too_large', 503)
    exists = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_retention_limits'").fetchone()
    limit = c.execute('SELECT max_commands FROM execution_retention_limits WHERE id=1').fetchone()[0] if exists else 10000
    if c.execute('SELECT COUNT(*) FROM execution_grants').fetchone()[0] >= limit:
        raise SchedulerError('control_grant_metadata_full', 503)
    if exists:
        storage_limit = c.execute('SELECT storage_bytes FROM execution_retention_limits WHERE id=1').fetchone()[0]
        used = c.execute("""SELECT COALESCE(SUM(length(CAST(x.command AS BLOB))
            + COALESCE(length(CAST(r.receipt AS BLOB)),0)
            + COALESCE(length(CAST(r.grant_json AS BLOB)),0) + 1024 + r.reservation),0)
            FROM execution_retention r JOIN execution_commands x USING(id)""").fetchone()[0]
        used += c.execute('SELECT COUNT(*) FROM execution_grants').fetchone()[0] * 8192
        if used + 8192 > storage_limit:
            raise SchedulerError('control_grant_storage_full', 503)
