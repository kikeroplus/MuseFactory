"""テスト用の偽の LM Studio / ComfyUI サーバー。

- GET /v1/models → models
- POST /v1/chat/completions → replies を先頭から 1 つずつ返す (なくなったら最後のものを繰り返す)
- GET /queue → queue_running (ComfyUI)
受け取ったリクエストは requests に残す。delay で返事を遅らせる。

手で動かすこともできる (画面の接続テストを試すとき):
    py -3.11 -m tests.fake_server 1234
"""

import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeServer:
    def __init__(self, models=None, replies=None, queue_running=None, delay: float = 0.0, port: int = 0,
                 responder=None):
        self.models = list(models or ["qwen-9b-instruct"])
        self.replies = list(replies or ["OK"])
        self.queue_running = list(queue_running or [])
        self.delay = delay
        # responder(payload) -> 返事の本文。あれば replies より優先 (頼まれた内容で返事を変えるとき)
        self.responder = responder
        self.requests: list[dict] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # 静かにする
                pass

            def _send(self, data, status=200):
                body = json.dumps(data).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if owner.delay:
                    time.sleep(owner.delay)
                if self.path.rstrip("/").endswith("/models"):
                    self._send({"object": "list", "data": [{"id": m} for m in owner.models]})
                elif self.path.startswith("/queue"):
                    self._send({"queue_running": owner.queue_running, "queue_pending": []})
                else:
                    self._send({"error": "not found"}, 404)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                owner.requests.append(payload)
                if owner.delay:
                    time.sleep(owner.delay)
                if not self.path.endswith("/chat/completions"):
                    self._send({"error": "not found"}, 404)
                    return
                if owner.responder is not None:
                    reply = owner.responder(payload)
                else:
                    index = min(len(owner.requests_chat()) - 1, len(owner.replies) - 1)
                    reply = owner.replies[index]
                self._send({
                    "model": payload.get("model"),
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": reply}}],
                })

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def requests_chat(self) -> list[dict]:
        return [r for r in self.requests if "messages" in r]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def endpoint(self) -> str:
        return self.url + "/v1"

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def dead_url() -> str:
    """何も待っていないポートの URL (つながらない接続先)。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}/v1"


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 1234
    with FakeServer(models=["qwen-9b-instruct", "qwen-14b-instruct", "gemma-vision"],
                    replies=["<think>考え中</think>OK"], port=port) as server:
        print(f"偽の LM Studio: {server.endpoint}  (Ctrl+C で終わる)")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
