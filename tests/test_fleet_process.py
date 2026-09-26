"""Independent real HTTP CPU executors; no claim of multi-GPU validation."""
import asyncio
import base64
import io
import json
import os
from pathlib import Path
import sys
import wave

import httpx
import pytest

from selfhost_models.scheduler_schema import Deployment

FIXTURE = Path(__file__).parent / 'fixtures' / 'fleet_executor_process.py'
AUTHORITY = 'control-fleet-fixture'


async def launch(config):
    env = {**os.environ, 'PYTHONPATH': str(FIXTURE.resolve().parents[2]), 'PYTHONDONTWRITEBYTECODE': '1'}
    proc = await asyncio.create_subprocess_exec(sys.executable, str(FIXTURE), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
    proc.stdin.write(json.dumps(config).encode() + b'\n')
    await proc.stdin.drain()
    proc.stdin.close()
    try:
        line = await asyncio.wait_for(proc.stdout.readline(), 20)
        if not line:
            raise AssertionError((await proc.stderr.read()).decode())
        info = json.loads(line)
        if 'unsupported' in info:
            pytest.skip(info['unsupported'])
        assert info['ready']
        return proc, info
    except BaseException:
        await stop(proc)
        raise


async def stop(proc):
    if proc is None or proc.returncode is not None:
        return
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), 5)
    except TimeoutError:
        proc.kill()
        await proc.wait()


async def eventually(fetch, predicate, seconds=15):
    last = None
    try:
        async with asyncio.timeout(seconds):
            while True:
                last = await fetch()
                if predicate(last):
                    return last
                await asyncio.sleep(.05)
    except TimeoutError:
        pytest.fail('fleet condition timed out; last snapshot: ' + json.dumps(last))


async def get(client, path='/fixture/state'):
    r = await client.get(path)
    assert r.status_code == 200, r.text
    return r.json()


async def post(client, path, value=None):
    r = await client.post(path, json={} if value is None else value)
    assert r.status_code == 200, r.text
    return r.json()


def job(snapshot, jid):
    return next(j for j in snapshot['jobs'] if j['id'] == jid)


def wav():
    out = io.BytesIO()
    with wave.open(out, 'wb') as f:
        f.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        f.writeframes(b'\0\0' * 160)
    return out.getvalue()


@pytest.mark.skipif(sys.platform != 'linux', reason='Landlock isolation requires Linux')
async def test_independent_fleet_processes_concurrency_faults_gc_and_maintenance(tmp_path):
    roots = {name: tmp_path / name for name in ('control', 'executor-a', 'executor-b')}
    for root in roots.values():
        root.mkdir()
        (root / 'private.canary').write_text('private per-plane data')
    for name in ('executor-a', 'executor-b'):
        weights = roots[name] / 'weights'
        weights.mkdir()
        (weights / 'config.json').write_text('{}')
        (weights / 'model.safetensors').write_bytes(b'cpu-fixture-only')
        (roots[name] / 'executor.sqlite3').touch()
    (roots['control'] / 'scheduler.sqlite3').touch()
    keys = {name: 'fixture-key-' + name + '-' + 'x' * 40 for name in roots}
    configs = {}
    for name, root in roots.items():
        forbidden = []
        for peer, peer_root in roots.items():
            if peer == name:
                continue
            forbidden += [{'path': str(peer_root / 'private.canary'), 'label': peer + '-private', 'directory': False},
                          {'path': str(peer_root), 'label': peer + '-directory', 'directory': True},
                          {'path': str(peer_root / ('scheduler.sqlite3' if peer == 'control' else 'executor.sqlite3')),
                           'label': peer + '-database', 'directory': False}]
        configs[name] = {'role': 'control' if name == 'control' else 'executor', 'root': str(root),
                         'executor': name, 'key': keys[name], 'forbidden': forbidden,
                         'catalog': 'qwen' if name == 'executor-a' else 'both'}
    processes, ready, clients = {}, {}, {}
    evidence = {}
    try:
        for name in ('executor-a', 'executor-b'):
            processes[name], ready[name] = await launch(configs[name])
        configs['control']['executors'] = [{**ready[name], 'key': keys[name]} for name in ('executor-a', 'executor-b')]
        processes['control'], ready['control'] = await launch(configs['control'])
        for name in roots:
            clients[name] = httpx.AsyncClient(base_url=ready[name]['url'],
                headers={'authorization': 'Bearer ' + keys[name]}, timeout=5, trust_env=False)
        control, a, b = clients['control'], clients['executor-a'], clients['executor-b']
        first = await eventually(lambda: get(control), lambda s: all(w['available'] for w in s['workers']))
        assert not first['loop_error']
        isolations = [ready[name]['isolation'] for name in roots]
        assert len({v['pid'] for v in isolations}) == 3
        assert all(v['mechanism'] == 'landlock' and len(v['denied']) == 6 for v in isolations)
        assert all(d['error'] == 'PermissionError' for v in isolations for d in v['denied'])
        q = Deployment.model_validate(ready['executor-a']['deployments'][0])
        w = next(Deployment.model_validate(v) for v in ready['executor-b']['deployments'] if v['runtime'] == 'whisper')
        inventories = {name: await get(clients[name], '/internal/v1/inventory') for name in ('executor-a', 'executor-b')}
        assert inventories['executor-a']['resource_id'] != inventories['executor-b']['resource_id']
        changed_resource = {**inventories['executor-b'], 'resource_id': inventories['executor-a']['resource_id']}
        rejected = await control.post('/fixture/verify', json={'executor': 'executor-b', 'inventory': changed_resource})
        assert rejected.status_code == 409 and rejected.json()['error'] in ('executor_resource_changed', 'executor_resource_duplicate')
        unmatched = q.model_copy(update={'load': q.load.model_copy(update={'context': 1024})})
        await post(control, '/fixture/register', {'executor': 'executor-a', 'deployment': unmatched.model_dump()})

        async def submit(dep, key, urgent=False):
            if dep.runtime == 'whisper':
                ref = (await post(control, '/fixture/upload', {'data': base64.b64encode(wav()).decode()}))['artifact_ref']
                payload = {'audio_ref': ref}
            else:
                payload = {'model': dep.model, 'messages': [{'role': 'user', 'content': 'CPU fleet fixture'}]}
            return await post(control, '/fixture/submit', {'key': key, 'spec': {'deployment_id': dep.id,
                'operation': 'chat' if dep.runtime == 'vllm' else 'transcribe', 'input': payload,
                'urgent': urgent, 'execution_timeout_seconds': 20}})

        blocked = await submit(unmatched, 'unmatched-urgent', True)
        qjob, wjob = await submit(q, 'parallel-qwen'), await submit(w, 'parallel-whisper')
        concurrent = await eventually(lambda: get(control), lambda s:
            job(s, qjob['id'])['state'] == job(s, wjob['id'])['state'] == 'running' and len(s['leases']) == 2)
        assert job(concurrent, blocked['id'])['state'] == 'queued'
        assignment = {x['job']: x['executor'] for x in concurrent['assignments']}
        assert assignment[qjob['id']] == 'executor-a' and assignment[wjob['id']] == 'executor-b'
        concurrent_executors = {}
        for name, jid in (('executor-a', qjob['id']), ('executor-b', wjob['id'])):
            attempt = next(t['id'] for t in concurrent['attempts'] if t['job'] == jid)
            concurrent_executors[name] = await eventually(
                lambda name=name: get(clients[name], '/fixture/evidence'),
                lambda snapshot, attempt=attempt: attempt in snapshot['held_attempts'])
            assert any(row['id'] == attempt and row['state'] == 'executing' for row in concurrent_executors[name]['commands'])
        # Both independent runtime producers are suspended in execution together,
        # not merely represented by two control-side dispatch intentions.
        ack_attempt = next(t['id'] for t in concurrent['attempts'] if t['job'] == qjob['id'])
        await post(a, '/fixture/drop-ack-response', {'attempt': ack_attempt})
        for client in (a, b):
            await post(client, '/fixture/finish')
        complete = await eventually(lambda: get(control), lambda s:
            all(job(s, jid)['result_state'] == 'available' for jid in (qjob['id'], wjob['id'])))
        assert (await post(control, '/fixture/result', {'job': qjob['id']}))['choices'][0]['message']['content'] == 'executor-a'
        assert (await post(control, '/fixture/result', {'job': wjob['id']}))['terminal']
        ack_recovered = await eventually(lambda: get(a, '/fixture/evidence'), lambda s:
            s['ack_fault']['dropped'] and s['ack_fault']['ack_requests'] >= 2)
        # The saved receipt hash permits ACK retry without another GET; the
        # executor has already collected the full receipt before that retry.
        fault = ack_recovered['ack_fault']
        assert fault['acknowledged_state'] == 'terminal'
        assert fault['state_before_response_loss'] == 'collected'
        assert fault['gc']['collected_commands'] >= 1
        ack_control = await eventually(lambda: get(control), lambda s:
            any(g['attempt'] == ack_attempt and g['acked'] and g['receipt'] for g in s['grants']))
        assert job(ack_control, wjob['id'])['result_state'] == 'available'
        assert not ack_control['loop_error']
        assert sum(call.get('attempt') == ack_attempt for call in ack_recovered['calls']) == 1
        tombstone = await get(a, '/internal/v1/commands/' + ack_attempt)
        assert tombstone['state'] == 'collected' and tombstone['receipt'] is None
        assert tombstone['receipt_hash'] == fault['receipt_hash']
        for client in (a, b):
            await eventually(lambda client=client: get(client, '/fixture/evidence'), lambda s:
                any(row['outputs_released'] for row in s['commands']))
            collected = await post(client, '/internal/v1/collect', {'retention_seconds': 0})
            assert collected['collected_commands'] + (fault['gc']['collected_commands'] if client is a else 0) >= 1
            assert (await get(client, '/internal/v1/usage'))['states']['collected'] >= 1
        # ACK+GC cannot remove the durable result on the control side.
        assert (await post(control, '/fixture/result', {'job': qjob['id']}))['choices']

        await post(control, '/fixture/maintenance', {'mode': 'pause', 'executor': 'executor-b'})
        waiting = await submit(w, 'paused-whisper')
        await asyncio.sleep(.2)
        paused = await get(control)
        assert job(paused, waiting['id'])['state'] == 'queued'
        assert next(v for v in paused['workers'] if v['executor'] == 'executor-a')['maintenance'] == 'open'
        await post(control, '/fixture/maintenance', {'mode': 'open', 'executor': 'executor-b'})
        await eventually(lambda: get(control), lambda s: job(s, waiting['id'])['result_state'] == 'available')

        await post(a, '/fixture/hold')
        lost = await submit(q, 'unknown-a')
        executing = await eventually(lambda: get(control), lambda s: job(s, lost['id'])['state'] == 'running')
        aid = next(t['id'] for t in executing['attempts'] if t['job'] == lost['id'])
        before_a = await eventually(lambda: get(a, '/fixture/evidence'),
            lambda snapshot: any(call.get('attempt') == aid for call in snapshot['calls']))
        # Force executor-process loss while its accepted attempt has no terminal proof.
        processes['executor-a'].kill()
        await processes['executor-a'].wait()
        unknown = await eventually(lambda: get(control), lambda s: job(s, lost['id'])['state'] == 'unknown')
        assert any(row['id'] == aid for row in unknown['leases'])
        other = await submit(w, 'b-survives-offline-a')
        await eventually(lambda: get(control), lambda s: job(s, other['id'])['result_state'] == 'available')
        # B also advertises Qwen: it may run a new job, but not A's unknown attempt.
        fresh = await submit(q, 'new-qwen-on-b')
        survived = await eventually(lambda: get(control), lambda s: job(s, fresh['id'])['result_state'] == 'available')
        assert job(survived, lost['id'])['state'] == 'unknown'
        calls_b = (await get(b, '/fixture/evidence'))['calls']
        assert not any(call.get('attempt') == aid for call in calls_b)

        configs['executor-a']['port'] = int(ready['executor-a']['url'].rsplit(':', 1)[1])
        processes['executor-a'], restarted = await launch(configs['executor-a'])
        status = await get(a, '/internal/v1/commands/' + aid)
        assert status['state'] == 'unknown'
        after_a = await get(a, '/fixture/evidence')
        assert sum(call.get('attempt') == aid for call in after_a['calls']) == 1
        assert sum(call.get('attempt') == ack_attempt for call in after_a['calls']) == 1
        recovered_tombstone = await get(a, '/internal/v1/commands/' + ack_attempt)
        assert recovered_tombstone['state'] == 'collected'
        assert recovered_tombstone['receipt_hash'] == fault['receipt_hash']
        assert not any(call.get('attempt') == ack_attempt for call in calls_b)
        epoch = next(worker['epoch'] for worker in survived['workers'] if worker['executor'] == 'executor-a')
        await post(a, '/internal/v1/fence', {'authority': AUTHORITY, 'executor': 'executor-a', 'fence': epoch + 1})
        stale = await a.post('/internal/v1/fence', json={'authority': AUTHORITY, 'executor': 'executor-a', 'fence': epoch})
        assert stale.status_code == 409 and stale.json()['error'] == 'stale_controller'
        evidence = {'isolation': isolations, 'parallel': concurrent, 'executor_parallel': concurrent_executors, 'offline': survived,
                    'restart': restarted['isolation'], 'unknown_attempt': aid,
                    'ack_response_loss': {'executor': ack_recovered, 'control': ack_control,
                                          'tombstone_after_restart': recovered_tombstone},
                    'a_calls': after_a['calls'], 'b_calls': calls_b}
        (tmp_path / 'fleet-process-evidence.json').write_text(json.dumps(evidence, indent=2) + '\n')
    finally:
        for client in clients.values():
            await client.aclose()
        for name in ('control', 'executor-a', 'executor-b'):
            await stop(processes.get(name))
