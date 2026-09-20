"""The approved fixed Whisper checkpoint; no loading or inference fallback."""
import hashlib
import json
from pathlib import Path

MODEL = "openai/whisper-small"
REVISION = "973afd24965f72e36ca33b3055d56a652f456b4d"
WEIGHT_SHA256 = "1d7734884874f1a1513ed9aa760a4f8e97aaa02fd6d93a3a85d27b2ae9ca596b"


def validate_identity(path, model, revision):
    if model != MODEL or revision != REVISION:
        raise ValueError("unsupported Whisper identity")
    config = json.loads((Path(path) / "config.json").read_text())
    if (config.get("model_type") != "whisper" or config.get("architectures") != ["WhisperForConditionalGeneration"] or
            config.get("quantization_config") or config.get("d_model") != 768):
        raise ValueError("unsupported Whisper checkpoint")
    sha = hashlib.sha256()
    with (Path(path) / "model.safetensors").open("rb") as f:
        for block in iter(lambda: f.read(1024**2), b""):
            sha.update(block)
    if sha.hexdigest() != WEIGHT_SHA256:
        raise ValueError("Whisper weights hash mismatch")


class WhisperEngine:
    def __init__(self, path, model, revision, memory_fraction=0.17):
        validate_identity(path, model, revision)
        import torch
        import transformers
        from transformers import WhisperForConditionalGeneration, WhisperProcessor
        if torch.__version__ != "2.13.0+cu130" or torch.version.cuda != "13.0" or transformers.__version__ != "5.16.1":
            raise RuntimeError("unvalidated runtime")
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA required")
        torch.cuda.set_per_process_memory_fraction(memory_fraction, 0)
        self.processor = WhisperProcessor.from_pretrained(path, local_files_only=True, trust_remote_code=False)
        self.model = WhisperForConditionalGeneration.from_pretrained(path, local_files_only=True,
            trust_remote_code=False, use_safetensors=True, dtype=torch.float16, attn_implementation="sdpa").to("cuda:0").eval()
        torch.cuda.synchronize()

    def run(self, raw, language, max_tokens, warmup=False):
        import numpy as np
        import torch
        from selfhost_models.wav import validate_wav
        meta = validate_wav(raw)
        audio = np.frombuffer(raw, dtype="<i2", offset=44).astype(np.float32) / 32768
        features = self.processor(audio=audio, sampling_rate=16000, return_tensors="pt", return_attention_mask=True)
        kwargs = dict(task="transcribe", language=language, return_timestamps=False, return_dict_in_generate=True, do_sample=False,
                      num_beams=1, max_new_tokens=max_tokens, temperature=0.0)
        if warmup:
            kwargs.update(min_new_tokens=4, max_new_tokens=4)
        with torch.inference_mode():
            output = self.model.generate(input_features=features.input_features.to(device="cuda:0", dtype=torch.float16),
                attention_mask=features.attention_mask.to("cuda:0"), **kwargs)
            ids = output.sequences[0].tolist()
            text = self.processor.batch_decode(output.sequences, skip_special_tokens=True)[0]
            torch.cuda.synchronize()
        reason = "stop" if ids and ids[-1] == self.model.generation_config.eos_token_id else "length"
        return {"text": text, "finish_reason": reason, "truncated": reason == "length", "terminal": True,
                "duration_seconds": meta["duration_seconds"], "model": MODEL, "revision": REVISION}
