"""本地 webhook 接收器：记录收到的 POST 请求体到日志文件（端到端验证 notify 真实外发）。"""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer

LOG = "/tmp/webhook_received.log"


class Handler(BaseHTTPRequestHandler):
    def _record(self) -> None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length).decode("utf-8", "replace")
        entry = {
            "method": self.command,
            "path": self.path,
            "headers": dict(self.headers),
            "body": body,
        }
        with open(LOG, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    do_POST = _record  # noqa: E501, N815 - HTTP 方法名，BaseHTTPRequestHandler 协议要求
    do_GET = _record  # noqa: E501, N815 - HTTP 方法名，BaseHTTPRequestHandler 协议要求

    def log_message(self, *args: object) -> None:
        pass


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", 18765), Handler)
    print("webhook receiver listening on :18765", flush=True)
    server.serve_forever()
