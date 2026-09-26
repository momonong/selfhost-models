FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254
ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.version="0.3.0rc1" \
      org.opencontainers.image.revision=$SOURCE_REVISION \
      org.selfhost-models.release-channel="candidate" \
      org.opencontainers.image.source="https://github.com/momonong/selfhost-models"
COPY --from=ghcr.io/astral-sh/uv:0.12.15@sha256:62f8c047d0a0e9ece6b53fc63df902585a67a47a7f318ddec4a37db586edc8e3 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH"
COPY pyproject.toml uv.lock /app/
RUN uv sync --locked --no-dev --no-install-project --no-cache
COPY selfhost_models /app/selfhost_models
RUN uv sync --locked --no-dev --no-editable --no-cache && useradd --uid 10001 --create-home service && mkdir /state && chown service /state
USER service
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "selfhost_models.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log", "--timeout-graceful-shutdown", "5"]
