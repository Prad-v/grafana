"""A stand-in for a LiteLLM proxy: the OpenAI chat API, answering with a fixed reply.

It records what it receives, so tests can check that the agent sent the right key, model
and path. Used by the agent tests and, mounted into its container, by the end-to-end test.
Standard library only.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODELS = ["gpt-fake", "team/claude-alias"]


class FakeOpenAI:
    def __init__(self, reply="Hello from the fake model.", required_key=None, host="127.0.0.1", port=0):
        self.reply = reply
        self.required_key = required_key
        self.requests = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _json(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authorized(self):
                if fake.required_key is None:
                    return True
                if self.headers.get("authorization") == f"Bearer {fake.required_key}":
                    return True
                self._json(401, {"error": {"message": "Incorrect API key provided.", "type": "invalid_request_error", "code": "invalid_api_key"}})
                return False

            def do_GET(self):
                fake.requests.append({"method": "GET", "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": None})
                if not self._authorized():
                    return
                if self.path.rstrip("/") == "/v1/models":
                    self._json(200, {"object": "list", "data": [{"id": m, "object": "model"} for m in MODELS]})
                else:
                    self._json(404, {"error": {"message": "not found"}})

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                fake.requests.append({"method": "POST", "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
                if not self._authorized():
                    return
                if self.path.rstrip("/") != "/v1/chat/completions":
                    return self._json(404, {"error": {"message": "not found"}})
                model = body.get("model", "unknown")
                usage = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
                base = {"id": "chatcmpl-fake", "created": int(time.time()), "model": model}
                if not body.get("stream"):
                    return self._json(
                        200,
                        {**base, "object": "chat.completion", "choices": [{"index": 0, "message": {"role": "assistant", "content": fake.reply}, "finish_reason": "stop"}], "usage": usage},
                    )
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                words = fake.reply.split(" ")
                chunks = [{"role": "assistant", "content": w + (" " if i < len(words) - 1 else "")} for i, w in enumerate(words)]
                for delta in chunks:
                    self._event({**base, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]})
                self._event({**base, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
                self._event({**base, "object": "chat.completion.chunk", "choices": [], "usage": usage})
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                self.close_connection = True

            def _event(self, payload):
                self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
                self.wfile.flush()

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def port(self):
        return self.server.server_address[1]

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    def posts(self):
        return [r for r in self.requests if r["method"] == "POST"]
