FROM pytorch/pytorch:2.13.0-cuda13.0-cudnn9-runtime@sha256:db80a41f8428644cebcb3d75b0b62df334ab6c0e75785951eb25f48bfbd42407
ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.version="scheduler-v1" \
      org.opencontainers.image.revision=$SOURCE_REVISION \
      org.opencontainers.image.source="https://github.com/momonong/selfhost-models"
COPY --from=ghcr.io/astral-sh/uv:0.12.15@sha256:62f8c047d0a0e9ece6b53fc63df902585a67a47a7f318ddec4a37db586edc8e3 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH"
# Same locked Transformers dependencies, independent runtime image/worker.
COPY worker/transformers/pyproject.toml worker/transformers/uv.lock /app/
RUN uv venv --system-site-packages --python python /app/.venv && uv sync --locked --no-dev --no-cache \
    && python -c "import torch,transformers; assert torch.__version__ == '2.13.0+cu130'; assert torch.version.cuda == '13.0'; assert transformers.__version__ == '5.16.1'"
COPY selfhost_models /app/selfhost_models
COPY worker /app/worker
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 DO_NOT_TRACK=1 TOKENIZERS_PARALLELISM=false HF_HOME=/tmp/hf PYTHONUNBUFFERED=1
RUN useradd --uid 10001 --create-home service
USER service
CMD ["python", "-m", "uvicorn", "worker.whisper_app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log", "--timeout-graceful-shutdown", "5"]
