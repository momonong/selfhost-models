"""Fixed profile-to-engine flags, no dynamic plugin loading or fallback."""
import os
import sys

profile = os.environ.get("MODEL_PROFILE", "text")
flags = []
if profile == "qwen3_5":
    flags = ["--reasoning-parser", "qwen3", "--enable-auto-tool-choice", "--tool-call-parser", "qwen3_coder",
             "--limit-mm-per-prompt", '{"image":1,"video":0}', "--mm-processor-kwargs", '{"max_pixels":1048576}']
elif profile == "gemma4":
    flags = ["--enable-auto-tool-choice", "--tool-call-parser", "gemma4",
             "--limit-mm-per-prompt", '{"image":1,"audio":0,"video":0}']
elif profile != "text":
    raise SystemExit("unsupported model profile")
os.execvp("vllm", ["vllm", "serve", *sys.argv[1:], *flags])
