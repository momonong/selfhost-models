"""CPU-only lifecycle fixture. No model libraries, Docker, or arbitrary commands."""
import argparse
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

p = argparse.ArgumentParser()
p.add_argument("--port", type=int, required=True)
p.add_argument("--key", required=True)
args = p.parse_args()
epoch = uuid.uuid4().hex
finish = threading.Event()
calls = 0


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, value):
        raw = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Worker-Epoch", epoch)
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_GET(self):
        if self.headers.get("x-selfhost-worker-key") != args.key:
            self.reply(401, {"error": "unauthorized"})
        elif self.path == "/health":
            self.reply(200, {"calls": calls, "epoch": epoch})
        else:
            self.reply(404, {})

    def do_POST(self):
        global calls
        if self.headers.get("x-selfhost-worker-key") != args.key:
            self.reply(401, {"error": "unauthorized"})
            return
        size = int(self.headers.get("content-length", "0"))
        if size > 2097152:
            self.reply(413, {})
            return
        data = json.loads(self.rfile.read(size) or "{}")
        if self.path == "/internal/complete":
            finish.set()
            self.reply(200, {})
        elif self.path == "/internal/warmup":
            self.reply(200, {"terminal": True})
        elif self.path == "/v1/chat/completions":
            calls += 1
            if "urgent" in data:
                self.reply(400, {"error": "metadata_leaked"})
                return
            finish.wait(30)
            self.reply(200, {"choices": [{"message": {"role": "assistant", "content": "synthetic"}, "finish_reason": "stop"}]})
        else:
            self.reply(404, {})


server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
print("ready", flush=True)
server.serve_forever()
