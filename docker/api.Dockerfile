FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254
WORKDIR /app
COPY pyproject.toml /app/
COPY requirements.lock /app/
COPY selfhost_models /app/selfhost_models
RUN pip install --no-cache-dir -r requirements.lock && pip install --no-cache-dir --no-deps . && useradd --uid 10001 --create-home service && mkdir /state && chown service /state
USER service
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "selfhost_models.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log", "--timeout-graceful-shutdown", "5"]
