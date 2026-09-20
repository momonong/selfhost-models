from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from selfhost_models import cli


@pytest.mark.parametrize("backend", ["vllm", "transformers"])
@pytest.mark.parametrize("no_build", [False, True])
def test_serve_release_mode_preserves_host_config(tmp_path, monkeypatch, backend, no_build):
    model = tmp_path / "model"
    model.mkdir()
    (model / "config.json").write_text('{"model_type":"qwen3_5"}')
    assets = SimpleNamespace(state=tmp_path / "state", inspect=lambda _: {
        "repo_id": "Qwen/Qwen3.5-4B", "path": str(model), "revision": "a" * 40})
    monkeypatch.setattr(cli.socket, "socket", lambda: nullcontext(Mock()))
    monkeypatch.setattr(cli.subprocess, "check_output", lambda *a, **k: "")
    calls = []
    monkeypatch.setattr(cli, "GPUOwnership", Mock())
    monkeypatch.setattr(cli, "compose", lambda state, args: calls.append(args))
    args = SimpleNamespace(repo="Qwen/Qwen3.5-4B", backend=backend, video=False,
                           context=2048, max_inflight=None, port=18080, gpu_memory=.6,
                           deadline=30, drain=120, no_build=no_build)
    cli.serve(assets, args)
    assert calls == [["up", "-d", *(["--no-build", "--pull", "never"] if no_build else ["--build"])]]
    settings = (assets.state / "compose.env").read_text()
    assert f"BACKEND='{backend}'" in settings
    assert "VIDEO_ENABLED='0'" in settings
    assert str(model.as_posix()) in settings
    assert (assets.state / "api-key").is_file()
