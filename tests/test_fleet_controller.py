import asyncio

from selfhost_models.fleet_store import Fleet
from selfhost_models.scheduler_controller import Controller
from test_scheduler import FakeProvider, setup_store, submit, tick, finish


def test_worker_drain_finishes_active_and_does_not_stop_peer(tmp_path):
    async def run():
        parent, qwen, whisper, clock = setup_store(tmp_path)
        key = tmp_path / 'key'
        key.write_text('x' * 40)
        fleet = Fleet(parent)
        fleet.enable()
        for executor in ('executor-a', 'executor-b'):
            fleet.add_executor('authority-1', executor, 'http://127.0.0.1:18090', key)
            fleet.verify_executor(executor, dict(version=1, authority='authority-1', executor=executor,
                resource_id='cpu-fixture:' + executor, kind='cpu', deployments=[qwen.model_dump()]))
        a, b = fleet.workers()
        pa, pb = FakeProvider(), FakeProvider()
        ca, cb = Controller(a, pa), Controller(b, pb)
        await ca.start(); await cb.start()
        try:
            ja = submit(parent, qwen, 'a')
            jb = submit(parent, qwen, 'b')
            await tick(ca, 2); await tick(cb, 2)
            assert a.lease_count() == b.lease_count() == 1
            fleet.maintenance('drain', a.executor)
            await tick(ca)
            assert pa.alive and pb.alive
            await finish(ca, pa, ja)
            await tick(ca)
            assert a.state()['phase'] == 'unloaded'
            assert b.lease_count() == 1 and pb.alive
            await finish(cb, pb, jb)
            fleet.maintenance('drain')
            await tick(cb)
            assert b.state()['phase'] == 'unloaded'
        finally:
            await ca.close(); await cb.close()
    asyncio.run(run())


def test_offline_operator_unload_requires_exact_exit_proof(tmp_path):
    async def run():
        store, qwen, _, _ = setup_store(tmp_path)
        provider = FakeProvider()
        controller = Controller(store, provider)
        await controller.start()
        store.phase(controller.epoch, 'loading', deployment=qwen.id, handle='fixture')
        store.phase(controller.epoch, 'unknown', worker_epoch='worker-one')
        provider.alive = True
        try:
            await controller.unload_idle()
            assert store.state()['phase'] == 'unknown' and provider.alive
            await controller.unload_idle(operator=True)
            assert store.state()['phase'] == 'unloaded' and not provider.alive
        finally:
            await controller.close()
    asyncio.run(run())


def test_explicit_operator_unload_keeps_unknown_lease_until_exit(tmp_path):
    async def run():
        store, qwen, _, _ = setup_store(tmp_path)
        provider = FakeProvider()
        controller = Controller(store, provider)
        await controller.start()
        store.phase(controller.epoch, 'loading', deployment=qwen.id, handle='fixture')
        store.phase(controller.epoch, 'ready', worker_epoch='worker-one')
        store.legacy_admit('request', qwen.id, 'worker-one', 'api')
        store.phase(controller.epoch, 'unknown')
        provider.alive = True
        try:
            provider.fail = 'unload'
            await controller.unload_idle(operator=True)
            assert store.lease_count() == 1 and store.state()['phase'] == 'unknown'
            provider.fail = None
            await controller.unload_idle(operator=True)
            assert store.lease_count() == 0 and store.state()['phase'] == 'unloaded'
        finally:
            await controller.close()
    asyncio.run(run())


def test_missing_worker_credential_does_not_cancel_peer(tmp_path):
    from selfhost_models.fleet_controller import FleetController
    from selfhost_models.execution_client import RemoteProvider
    async def run():
        parent, qwen, _, _ = setup_store(tmp_path)
        fleet = Fleet(parent); fleet.enable()
        for executor in ('executor-a', 'executor-b'):
            fleet.add_executor('authority-1', executor, 'http://127.0.0.1:18090', tmp_path / executor)
        seen = asyncio.Event()
        class Peer(FakeProvider):
            async def request(self, method, path):
                seen.set()
                return dict(version=1, authority='authority-1', executor='executor-b',
                    resource_id='cpu-fixture:executor-b',kind='cpu',deployments=[qwen.model_dump()])
        def factory(store):
            return RemoteProvider(store) if store.executor == 'executor-a' else Peer()
        manager = FleetController(fleet, factory)
        running = asyncio.create_task(manager.run())
        try:
            await asyncio.wait_for(seen.wait(), 3)
            await asyncio.sleep(.1)
            assert not running.done()
            assert not fleet.view('executor-a')['available']
            assert fleet.view('executor-b')['available']
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
    asyncio.run(run())
