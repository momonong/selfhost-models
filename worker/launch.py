"""Fixed profile-to-engine flags, no dynamic plugin loading or fallback."""
import os
import sys
import json

profile = os.environ.get("MODEL_PROFILE", "text")
flags = []
if profile == "qwen3_5":
    flags = ["--reasoning-parser", "qwen3", "--enable-auto-tool-choice", "--tool-call-parser", "qwen3_coder",
             "--limit-mm-per-prompt", '{"image":1,"video":0}', "--mm-processor-kwargs", '{"max_pixels":1048576}']
    if os.getenv("VIDEO_ENABLED", "0") == "1":
        flags[flags.index("--limit-mm-per-prompt") + 1] = json.dumps({
            "image": {"count": 1, "width": 1024, "height": 1024},
            "video": {"count": 1, "num_frames": 120, "width": 256, "height": 256}})
        flags[flags.index("--mm-processor-kwargs") + 1] = '{"max_pixels":7864320}'
        flags += ["--media-io-kwargs", '{"video":{"num_frames":120}}', "--max-num-batched-tokens", "8192"]
elif profile == "gemma4":
    flags = ["--enable-auto-tool-choice", "--tool-call-parser", "gemma4",
             "--limit-mm-per-prompt", '{"image":1,"audio":0,"video":0}']
elif profile != "text":
    raise SystemExit("unsupported model profile")
os.execvp("vllm", ["vllm", "serve", *sys.argv[1:], *flags])
