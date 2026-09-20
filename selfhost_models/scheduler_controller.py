"""Exclusive lifecycle owner. Timeout never proves engine termination."""
import asyncio
import sqlite3

from filelock import FileLock

from .scheduler_schema import SchedulerError


class Controller:
    def __init__(self, store, provider):
        self.store, self.provider = store, provider
        self.epoch = None
        self.lock = FileLock(str(store.root / "controller.lock"), timeout=0)
        self.tasks = {}
        self.stopped = False
        self.pending_results = {}

    async def start(self):
        self.lock.acquire()
        try:
            # Providers check conflicting managed/static GPU owners before any mutation.
            await self.provider.preflight()
            self.epoch = self.store.acquire_controller()
            self.store.recover_receipts(self.epoch)
            s = self.store.state()
            if s["phase"] == "unknown" and await self.provider.exited(s):
                self.store.engine_exited(self.epoch)
            await self.cleanup_exited()
        except BaseException:
            self.lock.release()
            raise

    async def tick(self):
        self.store.heartbeat(self.epoch)
        # Reattempt storage only, never computation. Preserve in-memory output if
        # even the receipt write failed; report unknown on a subsequent crash.
        for aid, (result, error) in list(self.pending_results.items()):
            try:
                self.store.terminal(self.epoch, aid, result, error)
            except (OSError, sqlite3.Error):
                return
            else:
                del self.pending_results[aid]
        self.store.recover_receipts(self.epoch)
        s = self.store.state()
        if s["phase"] == "unknown":
            if await self.provider.exited(s):
                self.store.engine_exited(self.epoch)
                await self.cleanup_exited()
            return
        row = self.store.next_job(self.epoch)
        if row is None:
            return
        if s["deployment"] != row["deployment"]:
            if s["phase"] != "unloaded":
                # Seal now, including with free capacity, so ordinary traffic
                # cannot indefinitely delay a queued urgent switch.
                if s["phase"] != "draining":
                    self.store.phase(self.epoch, "draining")
                if self.store.lease_count():
                    if self.store.clock() - self.store.state()["changed"] > self.store.config.drain_seconds:
                        self.store.phase(self.epoch, "unknown")
                    return
                self.store.phase(self.epoch, "unloading")
                try:
                    async with asyncio.timeout(self.store.config.unload_seconds):
                        await self.provider.unload(self.store.state())
                        if not await self.provider.exited(self.store.state()):
                            raise SchedulerError("engine_exit_unconfirmed", 503)
                    self.store.engine_exited(self.epoch)
                except Exception:
                    self.store.phase(self.epoch, "unknown")
                    return
                await self.cleanup_exited()
            # Re-evaluate after a lifecycle boundary: urgent may have arrived.
            row = self.store.next_job(self.epoch)
            if row is None:
                return
            dep = self.store.deployment(row["deployment"])
            self.store.phase(self.epoch, "loading", deployment=dep.id)
            try:
                asset_path = await asyncio.to_thread(self.store.asset_verify, dep.asset_ref)
                self.store.reserve_load(self.epoch, dep)
                # Preparation cannot start a GPU engine. A failure here needs no
                # engine-exit acknowledgment; the load budget is still consumed.
                async with asyncio.timeout(self.store.config.load_seconds):
                    await self.provider.prepare(dep, asset_path)
                # Persist the unique handle BEFORE the external start call.
                handle = self.provider.handle(dep, self.epoch)
                self.store.phase(self.epoch, "loading", handle=handle)
                async with asyncio.timeout(self.store.config.load_seconds):
                    worker_epoch = await self.provider.load(dep, asset_path, handle)
                self.store.phase(self.epoch, "warming", worker_epoch=worker_epoch)
                async with asyncio.timeout(self.store.config.warmup_seconds):
                    await self.provider.warmup(dep, worker_epoch)
                self.store.phase(self.epoch, "ready")
            except Exception as exc:
                if self.store.state()["handle"] is None:
                    self.store.preparation_failed(self.epoch, row["id"], exc.code if isinstance(exc, SchedulerError) else "asset_preparation_failed")
                else:
                    self.store.phase(self.epoch, "unknown")
                self.store.lifecycle_failed(self.epoch, dep.id)
            return
        if s["phase"] == "draining":
            # A canceled/expired switch target can make the current deployment
            # preferred again; only return to ready with the same live identity.
            if await self.provider.identity() != s["worker_epoch"]:
                self.store.phase(self.epoch, "unknown")
                return
            self.store.phase(self.epoch, "ready")
        if self.store.state()["phase"] != "ready":
            return
        # One transaction selects and reserves each safe slot, including legacy
        # leases. Arrival between next_job and dispatch triggers reselection.
        for _ in range(self.store.deployment(row["deployment"]).load.capacity):
            row = self.store.next_job(self.epoch)
            if not row or row["deployment"] != self.store.state()["deployment"]:
                break
            attempt = self.store.dispatch(self.epoch, row["id"])
            if attempt is None:
                break
            self.store.fault("after_dispatch_intent")
            task = asyncio.create_task(self.execute(attempt))
            self.tasks[attempt["id"]] = task
            task.add_done_callback(lambda _, aid=attempt["id"]: self.tasks.pop(aid, None))

    async def execute(self, attempt):
        aid = attempt["id"]
        running = None
        try:
            spec = self.store.spec(attempt["job"])
            self.store.attempt_state(self.epoch, aid, "running")
            self.store.fault("before_external_dispatch")
            running = asyncio.create_task(self.provider.execute(self.store.deployment(spec.deployment_id), spec, attempt, self.store))
            done, _ = await asyncio.wait([running], timeout=spec.execution_timeout_seconds)
            if not done:
                self.store.attempt_state(self.epoch, aid, "draining", "execution_timeout")
                done, _ = await asyncio.wait([running], timeout=max(0, self.store.config.drain_seconds - spec.execution_timeout_seconds))
            if not done:
                self.store.attempt_state(self.epoch, aid, "unknown", "drain_timeout")
                return
            result, error = await running
            self.pending_results[aid] = (result, error)
            try:
                self.store.terminal(self.epoch, aid, result, error)
                del self.pending_results[aid]
            except (OSError, sqlite3.Error):
                # Do not overwrite terminal with unknown or discard sole output.
                pass
        except asyncio.CancelledError:
            self.store.attempt_state(self.epoch, aid, "unknown", "controller_stopped")
            raise
        except Exception:
            self.store.attempt_state(self.epoch, aid, "unknown", "execution_unconfirmed")
        finally:
            if running and not running.done():
                running.cancel()  # Close transport, NOT a GPU release acknowledgment.
                await asyncio.gather(running, return_exceptions=True)

    async def cleanup_exited(self):
        if not hasattr(self.provider, "retire"):
            return
        for state in self.store.cleanup_pending():
            try:
                await self.provider.retire(state)
                self.store.cleanup_done(self.epoch, state["handle"])
            except Exception:
                with self.store.tx() as db:
                    self.store.event(db, "exited_container_cleanup_failed")

    async def run(self):
        await self.start()
        try:
            while not self.stopped:
                await self.tick()
                await asyncio.sleep(0.05)
        finally:
            await self.close()

    async def close(self):
        self.stopped = True
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.provider.close()
        # Deliberately leave running engine/pins/unknown leases intact.
        self.lock.release()
