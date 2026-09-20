# Scheduler CPU reproduction

These helpers use synthetic assets and a real local HTTP subprocess. They do not
load models or use Docker/GPU. `CPUProvider.command` rejects Docker operations;
`prepare`, `preflight`, `load`, and `warmup` are explicit CPU fixture overrides.
`DockerProvider.execute`, the Linux decoder, Store, Controller, Gateway, and CLI
remain production code. This does not verify Docker lifecycle, GPU, or quality.

Requirements: Linux or WSL, Python 3.12, project-compatible uv on PATH, local
ext4/xfs/btrfs. Windows can
run the HTTP client only. Never put SQLite on DrvFs, 9p, NAS, or a Windows share.

## Snapshot and dependencies

Pass actual paths; no host-specific paths or credentials are embedded:

```bash
python3 /path/to/reproduce/snapshot.py --repo /path/to/repo --scratch-parent /tmp
# Read the returned "scratch" value, then set it explicitly:
scratch=/tmp/selfhost-scheduler-cpu-RETURNED_ID
cd "$scratch"
export UV_PROJECT_ENVIRONMENT="$scratch/.venv" UV_CACHE_DIR="$scratch/uv-cache"
export OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 MKL_NUM_THREADS=2 PYTHONDONTWRITEBYTECODE=1
export UV_CONCURRENT_INSTALLS=1 UV_CONCURRENT_BUILDS=1
ulimit -v 8388608
taskset -c 0,1 uv sync --locked --python python3.12
```

Select two available CPUs if 0 and 1 are unavailable. The 8 GiB virtual-memory
limit applies per process, not a cgroup aggregate guarantee; these fixtures are
small CPU workloads and import no Torch/CUDA. Snapshot creation copies only
source, tests, worker files, lockfiles, and relevant docs. No .state, secret,
model weights, Docker data, Git metadata, or existing venv is copied.

## Tests and documented management commands

```bash
taskset -c 0,1 uv run --locked pytest -q \
  tests/test_video_preparation.py tests/test_video_decoder.py tests/test_video_gateway.py \
  tests/test_scheduler.py tests/test_scheduler_process.py tests/test_scheduler_boundaries.py \
  --basetemp="$scratch/pytest-temp" --junitxml="$scratch/pytest.xml"
taskset -c 0,1 uv run --locked python reproduce/manage_docs.py --scratch "$scratch"
```

`manage_docs.py` runs actual `uv run --locked modelctl` entry points against new
native scratch state: init, asset-register, asset-verify, both JSON deployment
examples from docs/scheduler.md with synthetic ref/image substitutions, status,
events, collect, and resume-deployment. Controller/api/unload are checked using
`--help` only. `management-evidence-final.json` records commands and results.

## Real decoder, TCP worker, and client

```bash
taskset -c 0,1 uv run --locked python reproduce/integration.py --scratch "$scratch" --window-seconds 240
```

The helper verifies real decoder success, timeout, and cancellation; each child
must be reaped and its temporary files removed. It then verifies inherited
production execute converts a synthetic H.264 MP4 into JPEG frames and retains
video metadata. It starts the real Gateway on a free loopback port and writes
`endpoint.json` with non-secret connection information. The fixture exits after
the window or after its `stop_file` is created, and terminates/reaps its own
worker. Keep this process running while executing the client.

On Windows, read `endpoint.json` and the public key via a secure file source,
such as the distro's UNC path. Pass that key file explicitly; never print or
copy its contents into code. Use the project uv environment:

```powershell
uv run --locked python /path/to/reproduce/windows_client.py `
  --repo /path/to/repo --scratch-parent $env:TEMP `
  --endpoint-file <mapped-endpoint.json> --api-key-file <mapped-public-key-file> `
  --stop-file <mapped-stop-file>
```

Read the public key and stop-file **paths** from endpoint.json and map them to
the actual WSL distro's UNC namespace. No distro name is assumed. The client
can also run on Linux using native paths; only a Windows invocation proves the
Windows-to-WSL path. It checks catalog, submit, get, result, idempotency, upload,
readiness/models, and worker rejection of missing/public credentials.
Every client CLI child replaces `sqlite3.connect` with a failing function.

## Evidence and cleanup

Keep `source-sha256-final.txt`, its SHA256, pytest XML, management evidence,
integration-evidence.json, and the client's result file. These contain local
paths; sanitize them before committing. Source may change concurrently: compare
the manifest against the checkout before attributing results to a later commit.

The helpers never remove the scratch directory, source, model files, containers,
or shared state. Only their own decoder temporary directories and CPU processes
are cleaned. Scratch retains synthetic DBs and random fixture key files; keep
it private. Remove the exact scratch directory only under an explicit cleanup
authorization after confirming the fixture has stopped.
