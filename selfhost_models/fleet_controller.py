"""Independent worker loops sharing one authoritative scheduling database."""
import asyncio

from filelock import FileLock

from .execution_client import RemoteProvider
from .scheduler_controller import Controller
from .scheduler_schema import SchedulerError


class FleetController:
    def __init__(self, fleet, provider_factory=RemoteProvider):
        self.fleet = fleet
        self.provider_factory = provider_factory
        self.lock = FileLock(str(fleet.store.root / 'controller.lock'), timeout=0)
        self.tasks = []
        self.stopped = False

    async def worker_loop(self, store):
        executor = store.execution_binding()['executor_id']
        while not self.stopped:
            provider = controller = None
            try:
                provider = self.provider_factory(store)
                controller = Controller(store, provider)
                await provider.preflight()
                self.fleet.verify_executor(executor, await provider.request('GET', '/inventory'))
                await controller.start()
                last_probe = 0
                while not self.stopped:
                    if store.clock() - last_probe >= 2:
                        try:
                            await provider.preflight()
                            self.fleet.verify_executor(executor, await provider.request('GET', '/inventory'))
                        except Exception:
                            self.fleet.mark_available(executor, False)
                        last_probe = store.clock()
                    # A disconnected worker may still own active work. Only its
                    # loop reconciles that work; other worker loops keep running.
                    try:
                        await controller.tick()
                    except Exception:
                        self.fleet.mark_available(executor, False)
                    await asyncio.sleep(.05)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.fleet.mark_available(executor, False)
            finally:
                if controller is not None:
                    await controller.close()
                elif provider is not None:
                    await provider.close()
            if not self.stopped:
                await asyncio.sleep(1)

    async def run(self):
        self.lock.acquire()
        try:
            self.tasks = [asyncio.create_task(self.worker_loop(s)) for s in self.fleet.workers()]
            if not self.tasks:
                raise SchedulerError('executor_registry_empty', 409)
            await asyncio.gather(*self.tasks)
        finally:
            await self.close()

    async def close(self):
        self.stopped = True
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.lock.release()

    async def unload(self):
        """Offline operator action; active/unknown work is never force-released."""
        with self.lock:
            failures = []
            for store in self.fleet.workers():
                provider = controller = None
                try:
                    provider = self.provider_factory(store)
                    controller = Controller(store, provider)
                    await controller.start()
                    await controller.unload_idle(operator=True)
                    if store.state()['phase'] != 'unloaded':
                        raise SchedulerError('engine_exit_unconfirmed', 503)
                    await provider.release_ownership()
                except Exception:
                    failures.append(store.execution_binding()['executor_id'])
                finally:
                    if controller is not None:
                        await controller.close()
                    elif provider is not None:
                        await provider.close()
            if failures:
                raise SchedulerError('executors_not_drained', 409)
