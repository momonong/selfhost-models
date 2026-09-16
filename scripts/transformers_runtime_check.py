"""Offline CPU preflight inside the worker image; does not load model weights."""
import hashlib
import json
import sys
from pathlib import Path

import torch
import torchvision
import transformers
from transformers import AutoTokenizer, Qwen3_5Config, Qwen3_5ForConditionalGeneration

path = Path("/models/current")
config = Qwen3_5Config.from_pretrained(path, local_files_only=True)
tokenizer = AutoTokenizer.from_pretrained(path, local_files_only=True, trust_remote_code=False)
tokens = tokenizer.apply_chat_template([{"role": "user", "content": "Reply OK."}],
             enable_thinking=False, tokenize=True, add_generation_prompt=True, return_tensors="pt", return_dict=True)
print(json.dumps({"level": "offline-runtime-and-tokenizer-only-no-GPU", "python": sys.version.split()[0],
    "torch": torch.__version__, "torch_path": torch.__file__, "cuda": torch.version.cuda,
    "torchvision": torchvision.__version__, "transformers": transformers.__version__,
    "architecture": Qwen3_5ForConditionalGeneration.__name__, "model_type": config.model_type,
    "synthetic_prompt_tokens": tokens["input_ids"].shape[-1],
    "config_sha256": hashlib.sha256((path / "config.json").read_bytes()).hexdigest()}, indent=2))
