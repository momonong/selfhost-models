import json

import pytest

from selfhost_models.assets import Assets, repo_key


@pytest.fixture
def model(tmp_path):
    path = tmp_path / "weights"
    path.mkdir()
    for name in ("config.json", "tokenizer_config.json", "tokenizer.json"):
        (path / name).write_text("{}")
    (path / "model.safetensors").write_bytes(b"synthetic-test-weights")
    return path


def test_register_does_not_move_or_modify(tmp_path, model):
    before = {p.name: p.read_bytes() for p in model.iterdir()}
    assets = Assets(tmp_path / "state")
    r = assets.register("org/model", model, "a" * 40)
    assert r["path"] == str(model.resolve())
    assert assets.inspect("org/model") == r
    assert before == {p.name: p.read_bytes() for p in model.iterdir()}
    assert assets.fetch("org/model") == r  # No HF/network call needed.


def test_changed_or_missing_weights_fail(tmp_path, model):
    assets = Assets(tmp_path / "state")
    assets.register("org/model", model, "a" * 40)
    (model / "model.safetensors").write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        assets.inspect("org/model")
    (model / "model.safetensors").unlink()
    with pytest.raises(ValueError, match="missing"):
        assets.inspect("org/model")


def test_mixed_revision_rejected(tmp_path, model):
    metadata = model / ".cache/huggingface/download"
    metadata.mkdir(parents=True)
    for i, p in enumerate(model.iterdir()):
        if p.is_file():
            (metadata / (p.name + ".metadata")).write_text(("a" if i % 2 else "b") * 40 + "\netag\n0")
    with pytest.raises(ValueError, match="mixed"):
        Assets(tmp_path / "state").register("org/model", model)


@pytest.mark.parametrize("repo", ["../secret", "org/../../x", "org", "/x", "org/model/branch"])
def test_repo_traversal(repo):
    with pytest.raises(ValueError):
        repo_key(repo)


def test_index_missing_shard(tmp_path, model):
    (model / "model.safetensors.index.json").write_text(json.dumps({"weight_map": {"x": "absent.safetensors"}}))
    with pytest.raises(ValueError, match="missing/empty"):
        Assets(tmp_path / "state").register("org/model", model, "a" * 40)


def test_fetch_resolves_sha_once_and_serve_checks_local(tmp_path, model, monkeypatch):
    from types import SimpleNamespace
    import huggingface_hub
    import shutil
    calls = []

    class API:
        def model_info(self, repo, revision, token):
            calls.append((repo, revision))
            return SimpleNamespace(sha="f" * 40)

    def download(repo, revision, local_dir, token):
        assert revision == "f" * 40
        shutil.copytree(model, local_dir)

    monkeypatch.setattr(huggingface_hub, "HfApi", API)
    monkeypatch.setattr(huggingface_hub, "snapshot_download", download)
    assets = Assets(tmp_path / "state", tmp_path / "root")
    first = assets.fetch("org/model")
    assert first["revision"] == "f" * 40
    assert assets.fetch("org/model") == first
    assert calls == [("org/model", "main")]
