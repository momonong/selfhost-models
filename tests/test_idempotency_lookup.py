"""Recover an accepted job without another submission or model execution."""

import sqlite3
from urllib.parse import quote

import httpx
import pytest

from selfhost_models.api import Gateway
from selfhost_models.scheduler_store import Store
from test_scheduler import ready, setup_store, submit


def snapshot(store):
    with sqlite3.connect(store.db) as connection:
        return list(connection.iterdump())


@pytest.mark.parametrize("state", ["queued", "succeeded"])
async def test_exact_lookup_recovers_lost_reply_after_restart_without_writes(tmp_path, state):
    store, model, _, _ = setup_store(tmp_path)
    job = submit(store, model, "accepted:one-1")
    submit(store, model, "accepted:one-10")
    if state == "succeeded":
        epoch = ready(store, model)
        attempt = store.dispatch(epoch, job["id"])
        store.terminal(epoch, attempt["id"], {"text": "synthetic result"})
    reopened = Store(store.root)
    gateway = Gateway(key="k" * 32, scheduler=reopened, state_file=tmp_path / "unused.json")
    before = snapshot(reopened)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(gateway), base_url="http://test",
                                 headers={"Authorization": "Bearer " + "k" * 32}) as client:
        response = await client.get("/v1/jobs/by-idempotency/accepted:one-1")
        assert response.status_code == 200
        assert response.json()["id"] == job["id"]
        assert response.json()["state"] == state
        assert response.json() == (await client.get("/v1/jobs/" + job["id"])).json()
        assert not {"idem", "spec_ref", "input", "content_hash"} & response.json().keys()
        assert "synthetic" not in response.text
        assert (await client.get("/v1/jobs/by-idempotency/accepted:one")).status_code == 404
    assert snapshot(reopened) == before


async def test_lookup_authentication_precedes_existence_and_validation(tmp_path):
    store, model, _, _ = setup_store(tmp_path)
    submit(store, model, "known")
    gateway = Gateway(key="k" * 32, scheduler=store, state_file=tmp_path / "unused.json")
    before = snapshot(store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(gateway), base_url="http://test") as client:
        for key in ("known", "missing", "bad%20key"):
            response = await client.get("/v1/jobs/by-idempotency/" + key)
            assert response.status_code == 401
            assert key not in response.text
    assert snapshot(store) == before


@pytest.mark.parametrize("key", ["bad key", "a" * 129, "' OR 1=1--", "秘密", "bad%key"])
async def test_invalid_lookup_keys_are_rejected_without_mutation(tmp_path, key):
    store, _, _, _ = setup_store(tmp_path)
    gateway = Gateway(key="k" * 32, scheduler=store, state_file=tmp_path / "unused.json")
    before = snapshot(store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(gateway), base_url="http://test",
                                 headers={"Authorization": "Bearer " + "k" * 32}) as client:
        response = await client.get("/v1/jobs/by-idempotency/" + quote(key, safe=""))
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_idempotency_key"
    assert snapshot(store) == before


async def test_collected_job_lookup_is_missing_and_does_not_recreate_it(tmp_path):
    store, model, _, clock = setup_store(tmp_path, retention_seconds=60)
    job = submit(store, model, "collected")
    store.cancel(job["id"])
    clock.now += 61
    store.collect()
    before = snapshot(store)
    gateway = Gateway(key="k" * 32, scheduler=store, state_file=tmp_path / "unused.json")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(gateway), base_url="http://test",
                                 headers={"Authorization": "Bearer " + "k" * 32}) as client:
        response = await client.get("/v1/jobs/by-idempotency/collected")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "job_not_found"
    assert snapshot(store) == before
