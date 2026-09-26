"""Three-plane real HTTP fixture. CPU only; no Docker/GPU operations.

Per-process Landlock permits its own state and denies peer/control state.
The inherited CPU runtime represents uncertain engine ownership conservatively
across executor restart; its durable diagnostic marker is not GPU exit evidence.
"""
import asyncio
import base64
import hashlib
import json
from pathlib import Path
import sys

import uvicorn

from executor_process import CPURuntime, isolate
from selfhost_models.executor import Executor, Journal
from selfhost_models.fleet_store import Fleet
from selfhost_models.fleet_controller import FleetController
from selfhost_models.scheduler_schema import Deployment, LoadConfig, SchedulerConfig, SchedulerError, SubmitJob
from selfhost_models.scheduler_store import Store

AUTHORITY = 'control-fleet-fixture'


class Calls(list):
    def __init__(self, path):
        self.path = path
        super().__init__([json.loads(line) for line in path.read_text().splitlines()] if path.exists() else [])

    def append(self, value):
        super().append(value)
        with self.path.open('a') as stream:
            stream.write(json.dumps(value) + '\n')
            stream.flush()


class FleetRuntime(CPURuntime):
    def __init__(self, root, executor, journal):
        super().__init__(root)
        self.calls = Calls(root / 'calls.jsonl')
        self.executor = executor
        self.hold = True
        self.gates = {}
        self.config = SchedulerConfig(drain_seconds=30)
        self.marker = root / 'runtime-marker.json'
        if self.marker.exists():
            marker = json.loads(self.marker.read_text())
            self.epoch, self.container_id, self.alive = marker['epoch'], marker['container_id'], marker['alive']

    def inventory_identity(self):
        return {'resource_id': 'cpu-fixture:' + self.executor, 'kind': 'cpu'}

    async def load(self, dep, path, handle):
        epoch = await super().load(dep, path, handle)
        self.marker.write_text(json.dumps({'epoch': self.epoch, 'container_id': self.container_id, 'alive': self.alive}))
        return epoch

    async def unload(self, state):
        await super().unload(state)
        self.marker.write_text(json.dumps({'epoch': self.epoch, 'container_id': self.container_id, 'alive': False}))

    async def execute_payload(self, dep, payload, attempt, epoch, result_bytes, deadline=None, *, cancelled=None):
        assert self.alive and epoch == self.epoch
        self.calls.append({'kind': 'execute', 'model': dep.model, 'attempt': attempt, 'executor': self.executor})
        gate = self.gates.setdefault(attempt, asyncio.Event())
        if self.hold:
            await gate.wait()
        if dep.runtime == 'whisper':
            raw = base64.b64decode(payload['audio_base64'], validate=True)
            assert raw[:4] == b'RIFF'
            return {'terminal': True, 'text': self.executor, 'sha256': hashlib.sha256(raw).hexdigest()}, None
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': self.executor}}]}, None


async def body(receive):
    data = bytearray()
    while True:
        event = await receive()
        if event['type'] == 'http.disconnect':
            raise RuntimeError('fixture client disconnected')
        data.extend(event.get('body', b''))
        if not event.get('more_body'):
            return json.loads(data) if data else {}


async def serve(app, config, ready):
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=config.get('port', 0),
        loop='asyncio', ws='none', lifespan='on', access_log=False, log_level='critical'))
    running = asyncio.create_task(server.serve())
    async with asyncio.timeout(15):
        while not server.started:
            if running.done():
                await running
                raise RuntimeError('fixture failed to start')
            await asyncio.sleep(.01)
    ready['url'] = 'http://127.0.0.1:' + str(server.servers[0].sockets[0].getsockname()[1])
    print(json.dumps(ready), flush=True)
    await running


async def executor_main(config, isolation):
    root, executor = Path(config['root']), config['executor']
    journal = Journal(root, AUTHORITY, executor)
    ref = journal.register_asset(root / 'weights')
    q = Deployment(model='Qwen/Qwen3.5-4B', revision='851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a',
        asset_ref=ref, runtime='vllm', runtime_version='vllm-0.29.0', image='sha256:'+'a'*64, load=LoadConfig(capacity=2))
    w = Deployment(model='openai/whisper-small', revision='973afd24965f72e36ca33b3055d56a652f456b4d',
        asset_ref=ref, runtime='whisper', runtime_version='transformers-5.16.1', image='sha256:'+'b'*64,
        load=LoadConfig(dtype='float16', attention='sdpa', capacity=1, host_memory_gib=8, gpu_memory=.17))
    deps = [q] if config['catalog'] == 'qwen' else [q, w]
    for dep in deps:
        journal.register(dep)
    runtime = FleetRuntime(root, executor, journal)
    app = Executor(journal, runtime, config['key'])
    ack_fault = {'attempt': None, 'dropped': False, 'ack_requests': 0, 'queries_after_drop': 0}

    async def wrapper(scope, receive, send):
        if scope['type'] == 'http' and scope['path'].startswith('/fixture/'):
            if dict(scope['headers']).get(b'authorization') != ('Bearer ' + config['key']).encode():
                await app.reply(send, 401, {'error': 'unauthorized'})
                return
            path = scope['path']
            if path == '/fixture/finish':
                runtime.hold = False
                for gate in runtime.gates.values():
                    gate.set()
                value = {'finished': True}
            elif path == '/fixture/drop-ack-response':
                request = await body(receive)
                ack_fault.update(attempt=request['attempt'], dropped=False, ack_requests=0, queries_after_drop=0)
                value = {'armed': True}
            elif path == '/fixture/hold':
                runtime.hold = True
                value = {'hold': True}
            else:
                with journal.tx() as c:
                    commands = [dict(row) for row in c.execute('SELECT id,state,acknowledged,outputs_released FROM commands')]
                value = {'isolation': isolation, 'calls': runtime.calls, 'commands': commands, 'ack_fault': ack_fault,
                         'held_attempts': [aid for aid, gate in runtime.gates.items() if runtime.hold and not gate.is_set()]}
            await app.reply(send, 200, value)
        else:
            targeted = (scope['type'] == 'http' and ack_fault['attempt'] is not None
                        and scope['path'] == '/internal/v1/commands/' + ack_fault['attempt'])
            if targeted and scope['method'] == 'GET' and ack_fault['dropped']:
                ack_fault['queries_after_drop'] += 1
            if targeted and scope['method'] == 'POST':
                ack_fault['ack_requests'] += 1
                if not ack_fault['dropped']:
                    messages = []
                    async def capture(message):
                        messages.append(message)
                    # Run the production authentication/ACK route to completion
                    # before deliberately truncating its real HTTP response.
                    await app(scope, receive, capture)
                    if messages[0]['status'] == 200:
                        acknowledged = journal.status(ack_fault['attempt'])
                        assert acknowledged['acknowledged'] and acknowledged['outputs_released']
                        ack_fault.update(dropped=True, receipt_hash=acknowledged['receipt_hash'],
                                         acknowledged_state=acknowledged['state'])
                        ack_fault['gc'] = journal.collect(0)
                        ack_fault['state_before_response_loss'] = journal.status(ack_fault['attempt'])['state']
                        await send({'type': 'http.response.start', 'status': 200,
                                    'headers': [(b'content-type', b'application/json'), (b'content-length', b'128')]})
                        # Uvicorn closes the TCP response after these headers;
                        # httpx must report an incomplete response, not success.
                        raise ConnectionResetError('fixture: ACK committed but response body lost')
                    for message in messages:
                        await send(message)
                    return
            await app(scope, receive, send)
    await serve(wrapper, config, {'ready': True, 'isolation': isolation,
        'manifest': journal.asset(ref)[1], 'deployments': [d.model_dump() for d in deps], 'executor': executor})


async def control_main(config, isolation):
    root = Path(config['root'])
    store = Store(root, SchedulerConfig(drain_seconds=30, unload_seconds=5, load_seconds=5, warmup_seconds=5))
    fleet = Fleet(store)
    fleet.enable()
    for item in config['executors']:
        key_file = root / (item['executor'] + '.key')
        key_file.write_text(item['key'])
        worker = fleet.add_executor(AUTHORITY, item['executor'], item['url'], key_file)
        worker.assets_import(item['manifest'])
        for value in item['deployments']:
            worker.register(Deployment.model_validate(value))
    loop = FleetController(fleet)
    running = None
    # Reuse Executor.reply without constructing an executor or opening peer state.
    async def reply(send, status, value):
        await send({'type': 'http.response.start', 'status': status, 'headers': [(b'content-type', b'application/json')]})
        await send({'type': 'http.response.body', 'body': json.dumps(value).encode()})

    async def app(scope, receive, send):
        nonlocal running
        if scope['type'] == 'lifespan':
            while True:
                event = await receive()
                if event['type'] == 'lifespan.startup':
                    running = asyncio.create_task(loop.run())
                    await send({'type': 'lifespan.startup.complete'})
                else:
                    await loop.close()
                    await asyncio.gather(running, return_exceptions=True)
                    await send({'type': 'lifespan.shutdown.complete'})
                    return
        if scope['type'] != 'http':
            return
        if dict(scope['headers']).get(b'authorization') != ('Bearer ' + config['key']).encode():
            await reply(send, 401, {'error': 'unauthorized'})
            return
        try:
            data = await body(receive) if scope['method'] == 'POST' else {}
            path = scope['path']
            if path == '/fixture/submit':
                value = store.submit(SubmitJob.model_validate(data['spec']), data['key'])[0]
            elif path == '/fixture/register':
                value = {'deployment_id': fleet.worker(data['executor']).register(Deployment.model_validate(data['deployment']))}
            elif path == '/fixture/upload':
                value = store.upload(base64.b64decode(data['data']))
            elif path == '/fixture/maintenance':
                value = fleet.maintenance(data['mode'], data.get('executor'))
            elif path == '/fixture/verify':
                value = fleet.verify_executor(data['executor'], data['inventory'])
            elif path == '/fixture/result':
                value = json.loads(store.result(data['job']))
            else:
                with store.tx() as c:
                    attempts = [dict(r) for r in c.execute('SELECT * FROM attempts')]
                    assignments = [dict(r) for r in c.execute('SELECT * FROM job_assignments')]
                    leases = [dict(r) for r in c.execute('SELECT * FROM leases')]
                    grants = [dict(r) for r in c.execute('SELECT attempt,receipt,acked FROM execution_grants')]
                value = {'workers': fleet.views(), 'jobs': store.jobs(), 'attempts': attempts,
                         'assignments': assignments, 'leases': leases, 'grants': grants, 'isolation': isolation,
                         'loop_error': str(running.exception()) if running.done() and not running.cancelled() else None}
            await reply(send, 200, value)
        except SchedulerError as exc:
            await reply(send, exc.status, {'error': exc.code})
    await serve(app, config, {'ready': True, 'isolation': isolation})


async def main():
    config = json.loads(sys.stdin.readline())
    try:
        isolation = isolate(Path(config['root']), config['forbidden'])
    except NotImplementedError as exc:
        print(json.dumps({'unsupported': str(exc)}), flush=True)
        return
    if config['role'] == 'executor':
        await executor_main(config, isolation)
    else:
        await control_main(config, isolation)


if __name__ == '__main__':
    asyncio.run(main())
