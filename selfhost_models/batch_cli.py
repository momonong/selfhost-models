"""Nano5 offline local UI. Deliberately no host, login, live, or submit option."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import socket
import sys


def install_offline_guard():
    """Deny outbound sockets/DNS and process execution before serving uploads.

    Defense in depth for this closed application, not a sandbox for arbitrary Python.
    Uploaded programs are data and never imported or executed.
    """
    def audit(event, args):
        if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto", "socket.sendmsg",
                     "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn"}:
            raise PermissionError("Nano5 offline tool denies outbound networking and process execution")
    sys.addaudithook(audit)


def verify_offline_guard():
    for action in (lambda: socket.getaddrinfo("nano5.nchc.org.tw", 22),
                   lambda: socket.create_connection(("192.0.2.1", 22), timeout=0.01)):
        try:
            action()
        except PermissionError:
            continue
        raise RuntimeError("Offline guard failed")
    # Direct numeric connect tests the connect hook without getaddrinfo.
    with socket.socket() as sock:
        try:
            sock.connect(("192.0.2.1", 2222))
        except PermissionError:
            return
        raise RuntimeError("Offline connect guard failed")


@contextmanager
def single_process(state: Path):
    # flock/msvcrt is a process lifetime lock; DB transactions guard individual actions.
    lock_path = state / "ui.lock"
    if lock_path.is_symlink():
        raise RuntimeError("UI lock may not be a symlink")
    with lock_path.open("a+b") as f:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            f.write(b"0")
            f.flush()
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def main():
    parser = argparse.ArgumentParser(description="Nano5 批次工作本機離線介面；國網連線硬性停用。")
    parser.add_argument("--state", type=Path, default=Path(".state/nano5-batch"), help="獨立私有本機 state；不可與模型排程 state 共用")
    parser.add_argument("--port", type=int, default=18771, help="本機 loopback 埠（預設 18771）")
    parser.add_argument("--verify-offline", action="store_true", help="檢查 DNS 與 outbound socket 會在系統呼叫前被拒絕後退出")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535 or args.port == 18080:
        parser.error("port 須為 1024–65535，且不可占用現役模型 API 18080")
    install_offline_guard()
    verify_offline_guard()
    if args.verify_offline:
        print("offline guard: DNS and outbound connect denied before network calls")
        return
    from .batch_app import BatchApp
    import uvicorn
    app = BatchApp(args.state, args.port)
    try:
        with single_process(app.store.path.parent):
            # Bind a numeric loopback address ourselves; no resolver or external interface.
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind(("127.0.0.1", args.port))
                listener.listen(32)
                config = uvicorn.Config(app, host="127.0.0.1", port=args.port, workers=1,
                                        access_log=False, limit_concurrency=16, timeout_keep_alive=5,
                                        server_header=False, proxy_headers=False, log_level="warning")
                print(f"Nano5 offline UI: http://127.0.0.1:{args.port}  PID={os.getpid()}  state={app.store.path.parent}", flush=True)
                uvicorn.Server(config).run(sockets=[listener])
    except (OSError, RuntimeError) as exc:
        parser.exit(1, f"無法啟動本機 UI（port 被使用、state 已開啟或權限不足）：{type(exc).__name__}\n")


if __name__ == "__main__":
    main()
