"""Bound a CLI client without waiting for descendants that inherit its output.

An expired client has an UNKNOWN external outcome, even if it was terminated.
This helper never retries commands or terminates descendant/app processes.
"""
from dataclasses import dataclass
from pathlib import Path
import subprocess
import time


@dataclass(frozen=True)
class CommandResult:
    pid: int
    returncode: int | None
    timed_out: bool
    client_cleanup_complete: bool
    elapsed_seconds: float
    stdout_path: Path
    stderr_path: Path

    @property
    def external_outcome(self):
        return "unknown" if self.timed_out else "client_completed"


def _stop_client(process, deadline):
    if process.poll() is not None:
        return True
    if time.monotonic() >= deadline:
        return False
    # Popen targets this process handle only, never its tree or the Docker daemon.
    try:
        process.kill()
    except ProcessLookupError:
        pass
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return process.poll() is not None
    try:
        process.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        return False
    return True


def run_bounded(argv, *, stdout_path, stderr_path, timeout_seconds, deadline,
                cleanup_seconds=2.0, cwd=None, env=None):
    """Use the earlier step/global deadline, reserving time for client cleanup.

    Output paths must be fresh. They are files, not PIPEs; a surviving descendant
    may continue writing them after return. Treat any read/hash as a timed snapshot.
    No wall-clock guarantee can cover an OS-stalled CreateProcess/kill call; a
    cleanup failure is explicit and must stop the caller's subsequent operations.
    """
    if not argv or timeout_seconds <= 0 or cleanup_seconds <= 0:
        raise ValueError("command and positive time limits required")
    started = time.monotonic()
    effective_deadline = min(deadline, started + timeout_seconds)
    budget = effective_deadline - started
    if budget <= 0:
        raise TimeoutError("deadline exhausted before process creation")
    reserve = min(cleanup_seconds, budget / 2)
    stdout_path, stderr_path = Path(stdout_path), Path(stderr_path)
    if stdout_path.resolve() == stderr_path.resolve():
        raise ValueError("separate stdout and stderr paths required")
    timed_out = False
    cleanup_complete = True
    with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=stdout,
            stderr=stderr, shell=False, close_fds=True, cwd=cwd, env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            remaining = effective_deadline - reserve - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, timeout_seconds)
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            timed_out = True
            cleanup_complete = _stop_client(process, effective_deadline)
        except BaseException:
            _stop_client(process, effective_deadline)
            raise
    return CommandResult(process.pid, process.poll(), timed_out, cleanup_complete,
                         time.monotonic() - started, stdout_path, stderr_path)
