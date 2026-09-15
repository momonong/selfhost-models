FROM vllm/vllm-openai:v0.29.0@sha256:c2914767605584b6d8f45686b82de173ecc99e781897aa3d0a66dacd72c51ae1
# Keep the official CUDA/PyTorch/vLLM combination intact.
COPY worker/identity.py /opt/selfhost/identity.py
COPY worker/launch.py /opt/selfhost/launch.py
ENTRYPOINT ["python3", "/opt/selfhost/launch.py"]
ENV PYTHONPATH=/opt/selfhost HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 VLLM_NO_USAGE_STATS=1 DO_NOT_TRACK=1
