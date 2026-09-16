import json

import httpx

from selfhost_models.backend import VLLMBackend


async def test_warmup_covers_decode_batch_and_vision():
    calls = []
    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        assert payload["max_tokens"] >= 2 and payload["min_tokens"] >= 2
        assert payload["ignore_eos"] is True
        return httpx.Response(200, headers={"x-worker-epoch": "epoch"}, json={
            "choices": [{"finish_reason": "length"}], "usage": {"completion_tokens": 4}})
    backend = VLLMBackend("http://worker", 4, profile="qwen3_5", capacity=2)
    await backend.client.aclose()
    backend.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://worker")
    await backend.warmup("org/model", "epoch")
    await backend.close()
    assert len(calls) == 4
    assert calls[-1]["messages"][0]["content"][1]["type"] == "image_url"


async def test_video_warmup_covers_capacity_at_full_pixel_budget():
    calls = []
    async def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        return httpx.Response(200, headers={"x-worker-epoch": "epoch"}, json={
            "choices": [{"finish_reason": "length"}], "usage": {"completion_tokens": 4}})
    backend = VLLMBackend("http://worker", 4, profile="qwen3_5", capacity=2, video_enabled=True)
    await backend.close()
    backend.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://worker")
    await backend.warmup("org/model", "epoch")
    await backend.close()
    videos = [p for p in calls if "media_io_kwargs" in p]
    assert len(videos) == 2
    assert videos[0]["messages"] != videos[1]["messages"]
    for p in videos:
        assert p["media_io_kwargs"]["video"]["num_frames"] == 120
        assert p["mm_processor_kwargs"] == {"max_pixels": 7864320, "do_sample_frames": False}
    assert calls[3]["mm_processor_kwargs"] == {"max_pixels": 1048576}
