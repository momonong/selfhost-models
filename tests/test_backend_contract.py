import json

import httpx
import pytest

from selfhost_models.backend import VLLMBackend, TransformersBackend, create_backend
from selfhost_models.capabilities import capabilities
from selfhost_models.cli import compose_command


@pytest.mark.parametrize("backend_class", [VLLMBackend, TransformersBackend])
async def test_identity_generate_close_contract(backend_class):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, headers={"x-worker-epoch": "epoch"}, json={
            "choices": [{"finish_reason": "stop"}], "usage": {"completion_tokens": 4}})
    backend = backend_class("http://worker", 3, profile="qwen3_5", capacity=1)
    await backend.close()
    backend.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://worker")
    assert await backend.identity() == "epoch"
    await backend.warmup("org/model", "epoch")
    async with backend.generate({"model": "org/model"}, "request-one") as response:
        assert response.headers["x-worker-epoch"] == "epoch"
    assert calls[-1].headers["x-request-id"] == "request-one"
    assert sum(r.headers.get("x-request-id") == "request-one" for r in calls) == 1
    await backend.close()
    assert backend.client.is_closed


@pytest.mark.parametrize("body,epoch", [({"choices": [{}]}, "epoch"),
    ({"choices": [{"finish_reason": "stop"}], "usage": {"completion_tokens": 4}}, "changed")])
@pytest.mark.parametrize("backend_class", [VLLMBackend, TransformersBackend])
async def test_warmup_rejects_incomplete_or_wrong_epoch(body, epoch, backend_class):
    backend = backend_class("http://worker", 3)
    await backend.close()
    backend.client = httpx.AsyncClient(base_url="http://worker", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, headers={"x-worker-epoch": epoch}, json=body)))
    with pytest.raises(RuntimeError):
        await backend.warmup("org/model", "epoch")
    await backend.close()


def test_explicit_backend_and_compose_selection(tmp_path):
    (tmp_path / "compose.env").write_text("BACKEND='transformers'\n")
    command = compose_command(tmp_path)
    assert command.count("-f") == 2 and command[-1].endswith("compose.transformers.yaml")
    (tmp_path / "compose.env").write_text("BACKEND='vllm'\n")
    assert compose_command(tmp_path).count("-f") == 1
    (tmp_path / "compose.env").write_text("BACKEND='unknown'\n")
    with pytest.raises(ValueError):
        compose_command(tmp_path)
    with pytest.raises(ValueError):
        create_backend("unknown")
    with pytest.raises(ValueError):
        capabilities("transformers", "text")


def test_runtime_and_uv_are_isolated():
    from pathlib import Path
    import tomllib
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "worker/transformers/pyproject.toml").read_text())
    assert "transformers==5.16.1" in project["project"]["dependencies"]
    assert not any(d.startswith("torch") for d in project["project"]["dependencies"])
    dockerfile = (root / "docker/transformers.Dockerfile").read_text()
    assert "pytorch/pytorch:2.13.0-cuda13.0-cudnn9-runtime@sha256:" in dockerfile
    assert "--system-site-packages" in dockerfile and "uv sync --locked" in dockerfile
    assert "FROM vllm/" in (root / "docker/worker.Dockerfile").read_text()
