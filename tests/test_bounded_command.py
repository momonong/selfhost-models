import ctypes
import json
import os
from pathlib import Path
import sys
import time

import pytest

from scripts.bounded_command import run_bounded


FIXTURE = Path(__file__).parent / "fixtures/inherited_output.py"


def run(tmp_path, argv, **kwargs):
    return run_bounded(argv, stdout_path=tmp_path / "stdout.log",
        stderr_path=tmp_path / "stderr.log", timeout_seconds=3,
        deadline=time.monotonic()+10, cleanup_seconds=.5, **kwargs)


def test_completed_nonzero_is_not_a_timeout(tmp_path):
    result = run(tmp_path, [sys.executable, "-c", "print('diagnostic');raise SystemExit(7)"])
    assert result.returncode == 7 and not result.timed_out
    assert result.external_outcome == "client_completed"
    assert result.stdout_path.read_text().strip() == "diagnostic"


def test_expired_deadline_never_starts_client(tmp_path):
    with pytest.raises(TimeoutError, match="before process creation"):
        run_bounded([sys.executable, "-c", "raise Exception('must not run')"],
            stdout_path=tmp_path/'out', stderr_path=tmp_path/'err',
            timeout_seconds=1, deadline=time.monotonic()-1)
    assert list(tmp_path.iterdir()) == []


def test_global_deadline_includes_client_cleanup(tmp_path):
    result = run_bounded([sys.executable, "-c", "import time;time.sleep(10)"],
        stdout_path=tmp_path/'out', stderr_path=tmp_path/'err',
        timeout_seconds=10, deadline=time.monotonic()+1, cleanup_seconds=.25)
    assert result.timed_out and result.external_outcome == "unknown"
    assert result.client_cleanup_complete and result.returncode is not None
    assert result.elapsed_seconds < 1.5  # Small allowance for OS scheduling, not another wait budget.


@pytest.mark.skipif(os.name != "nt", reason="actual Windows inherited-handle regression")
@pytest.mark.parametrize("mode", ["parent-exit", "timeout"])
def test_windows_inherited_output_never_waits_for_descendant(tmp_path, mode):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.WaitForSingleObject.restype = ctypes.c_ulong
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    child_handle = None
    try:
        result = run(tmp_path, [sys.executable, str(FIXTURE), mode, str(tmp_path)])
        pid = json.loads((tmp_path/'child.json').read_text())["pid"]
        child_handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE only.
        assert child_handle
        assert kernel.WaitForSingleObject(child_handle, 0) == 258  # Still running; helper did not kill it.
        assert result.elapsed_seconds < 3.5
        assert result.client_cleanup_complete
        if mode == "parent-exit":
            assert result.returncode == 0 and not result.timed_out
            assert result.elapsed_seconds < 2
        else:
            assert result.timed_out and result.external_outcome == "unknown"
            assert result.returncode is not None
    finally:
        # Only this isolated fixture child cooperatively exits; no process-tree kill.
        (tmp_path/'child-stop').write_text("stop")
        if child_handle:
            try:
                assert kernel.WaitForSingleObject(child_handle, 3000) == 0
            finally:
                kernel.CloseHandle(child_handle)
