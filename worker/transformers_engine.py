"""One supported architecture on a fixed offline CUDA runtime, no model discovery."""
import json
from pathlib import Path


class QwenEngine:
    def __init__(self, path="/models/current", context=2048, memory_fraction=0.60):
        import torch
        import transformers
        from transformers import AutoTokenizer, Qwen3_5ForConditionalGeneration

        if torch.__version__ != "2.13.0+cu130" or torch.version.cuda != "13.0" or transformers.__version__ != "5.16.1":
            raise RuntimeError("unvalidated runtime")
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA required; no CPU fallback")
        config = json.loads((Path(path) / "config.json").read_text())
        if config.get("model_type") != "qwen3_5" or config.get("architectures") != ["Qwen3_5ForConditionalGeneration"]:
            raise ValueError("unsupported model architecture")
        if config.get("quantization_config"):
            raise ValueError("quantized assets have not been validated")
        torch.cuda.set_per_process_memory_fraction(memory_fraction, 0)
        self.context = context
        self.tokenizer = AutoTokenizer.from_pretrained(path, local_files_only=True, trust_remote_code=False)
        # Load the full official checkpoint, including its vision weights. This
        # deployment deliberately exposes only text; no partial-weight fallback.
        self.model = Qwen3_5ForConditionalGeneration.from_pretrained(
            path, local_files_only=True, trust_remote_code=False,
            dtype=torch.bfloat16, attn_implementation="sdpa").to("cuda:0").eval()
        torch.cuda.synchronize()

    def prepare(self, payload):
        messages = []
        for message in payload["messages"]:
            content = message["content"]
            if isinstance(content, list):
                content = "\n".join(part["text"] for part in content)
            messages.append({"role": message["role"], "content": content})
        inputs = self.tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=True,
                    enable_thinking=False, return_tensors="pt", return_dict=True)
        count = inputs["input_ids"].shape[-1]
        if count + payload["max_tokens"] > self.context:
            raise ValueError("context_limit")
        return inputs, count

    def run(self, prepared, payload, emit, warmup=False):
        import torch
        from transformers import GenerationConfig, TextStreamer

        class Streamer(TextStreamer):
            def on_finalized_text(self, text, stream_end=False):
                if text:
                    emit(text)
                # TextStreamer.end is NOT a GPU terminal acknowledgment.

        inputs, prompt_tokens = prepared
        inputs = inputs.to("cuda:0")
        eos = self.model.config.text_config.eos_token_id
        options = dict(max_new_tokens=payload["max_tokens"], do_sample=payload["temperature"] > 0,
                       eos_token_id=eos, pad_token_id=self.tokenizer.pad_token_id or eos,
                       use_cache=True)
        if options["do_sample"]:
            options.update(temperature=payload["temperature"], top_p=payload["top_p"], top_k=0)
        if warmup:
            options.update(min_new_tokens=4, max_new_tokens=4)
        if payload.get("seed") is not None:
            torch.manual_seed(payload["seed"])
        # A fresh config avoids unadvertised checkpoint sampling defaults.
        with torch.inference_mode():
            output = self.model.generate(**inputs, generation_config=GenerationConfig(**options),
                streamer=Streamer(self.tokenizer, skip_prompt=True, skip_special_tokens=True))
            tokens = output[0, prompt_tokens:].tolist()
            text = self.tokenizer.decode(tokens, skip_special_tokens=True)
            torch.cuda.synchronize()  # Terminal is published only after this returns.
        return {"text": text, "finish_reason": "stop" if tokens and tokens[-1] == eos else "length",
                "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": len(tokens),
                          "total_tokens": prompt_tokens + len(tokens)}}
