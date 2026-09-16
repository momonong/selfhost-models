"""Explicit deployment capabilities; never infer or fall back to another engine."""


def capabilities(backend, profile, video_enabled=False):
    if backend not in ("vllm", "transformers"):
        raise ValueError("unsupported backend")
    if backend == "transformers" and profile != "qwen3_5":
        raise ValueError("Transformers supports only the qwen3_5 profile")
    advanced = backend == "vllm" and profile in ("qwen3_5", "gemma4")
    if video_enabled and (backend != "vllm" or profile != "qwen3_5"):
        raise ValueError("video requires vLLM qwen3_5")
    from .video import LIMITS
    return {"text": True, "stream": True, "images": advanced, "tools": advanced,
            "videos": video_enabled, "video_limits": LIMITS if video_enabled else None,
            "thinking": backend == "vllm" and profile == "qwen3_5",
            "cancellation": "drain_to_terminal", "queue_capacity": 0,
            "max_inflight_limit": 1 if backend == "transformers" else (2 if video_enabled else 16),
            "unsupported_parameters": (["stop", "presence_penalty", "frequency_penalty"]
                                       if backend == "transformers" else [])}


def validate_capabilities(payload, backend, profile, video_enabled=False):
    caps = capabilities(backend, profile, video_enabled)
    images = any(isinstance(m.get("content"), list) and any(p["type"] == "image_url" for p in m["content"])
                 for m in payload["messages"])
    tools = payload.get("tools") or any(m.get("tool_calls") or m["role"] == "tool" for m in payload["messages"])
    from .video import find_video
    if (images and not caps["images"]) or (tools and not caps["tools"]) or (find_video(payload) and not caps["videos"]):
        raise ValueError("unsupported_model_capability")
    options = payload.get("chat_template_kwargs")
    if options is not None and not caps["thinking"]:
        if backend != "transformers" or options.get("enable_thinking") is not False:
            raise ValueError("unsupported_model_capability")
    if backend == "transformers":
        if payload.get("stop") is not None or any(payload.get(k, 0) != 0 for k in ("presence_penalty", "frequency_penalty")):
            raise ValueError("unsupported_backend_parameter")
        if payload["temperature"] == 0 and payload["top_p"] != 1:
            raise ValueError("top_p_requires_sampling")
