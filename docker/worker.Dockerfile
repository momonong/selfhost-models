FROM vllm/vllm-openai:v0.29.0@sha256:c2914767605584b6d8f45686b82de173ecc99e781897aa3d0a66dacd72c51ae1
ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.version="0.2.0" \
      org.opencontainers.image.revision=$SOURCE_REVISION \
      org.opencontainers.image.source="https://github.com/momonong/selfhost-models"
# Keep the official CUDA/PyTorch/vLLM combination intact.
COPY worker/identity.py /opt/selfhost/identity.py
COPY worker/launch.py /opt/selfhost/launch.py
COPY worker/relay.py /opt/selfhost/worker/relay.py
ENTRYPOINT ["python3", "/opt/selfhost/launch.py"]
ENV PYTHONPATH=/opt/selfhost HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 VLLM_NO_USAGE_STATS=1 DO_NOT_TRACK=1
