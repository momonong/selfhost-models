"""Short isolated Windows process fixture; no services, Docker, or network."""
import json
from pathlib import Path
import subprocess
import sys
import time

role, folder = sys.argv[1], Path(sys.argv[2])
if role == "child":
    (folder / "child-ready").write_text("ready")
    print("child owns inherited stdout", flush=True)
    end = time.monotonic() + 6
    while time.monotonic() < end and not (folder / "child-stop").exists():
        time.sleep(.02)
    (folder / "child-finished").write_text("finished")
else:
    child = subprocess.Popen([sys.executable, __file__, "child", str(folder)],
        stdin=subprocess.DEVNULL, stdout=sys.stdout, stderr=sys.stderr,
        close_fds=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    (folder / "child.json").write_text(json.dumps({"pid": child.pid}))
    print("parent started child", flush=True)
    if role == "timeout":
        time.sleep(10)
