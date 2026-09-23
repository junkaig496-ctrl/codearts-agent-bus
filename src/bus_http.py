"""
agent-bus 的 StreamableHTTP / SSE 传输（给码道 Space、网页版、远程场景用）
=====================================================================
码道官方支持三类 MCP 服务器：stdio（本地）、SSE（本地/远程）、Streamable HTTP（本地/远程）。
stdio 版（bus_mcp.py）够 IDE 用；但 Space 模式/网页版拿不到本地进程，所以这里再开一个
HTTP 端点，一套 bus_core 状态，两种接法同时可用。

用法：
    python bus_http.py --host 127.0.0.1 --port 8765
    码道 MCP 配置：{{"url": "http://127.0.0.1:8765/mcp?token=<TOKEN>", "type": "streamableHttp"}}

安全：默认只监听 127.0.0.1；除 /health 外都要求 token；token 首次运行自动生成并落盘。
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bus_core as core          # noqa: E402
import bus_mcp as mcp            # noqa: E402
import bus_dashboard             # noqa: E402

TOKEN_FILE = core.home() / "http-token.txt"


def ensure_token() -> str:
    import os
    tok = os.environ.get("AGENT_BUS_TOKEN")
    if tok:
        return tok.strip()
    if TOKEN_FILE.exists():
        t = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if t:
            return t
    tok = secrets.token_urlsafe(24)
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(tok, encoding="utf-8")
    return tok


def dispatch_rpc(msg: dict) -> list[dict]:
    """复用 bus_mcp 的协议层：把 _send 截流，收集响应而不写 stdout。"""
    captured: list[dict] = []
    original = mcp._send
    mcp._send = lambda obj: captured.append(obj)
    try:
        mcp.handle(msg)
    finally:
        mcp._send = original
    return captured


class Handler(BaseHTTPRequestHandler):
    server_version = f"agent-bus/{core.BUS_VERSION}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):            # 静音默认日志，避免刷屏
        pass

    # ---------------------------------------------------------- helpers
    def _authorized(self, q) -> bool:
        if self.path.startswith("/health"):
            return True
        supplied = (q.get("token") or [""])[0]
        if not supplied:
            hdr = self.headers.get("Authorization", "")
            if hdr.lower().startswith("bearer "):
                supplied = hdr[7:].strip()
        return secrets.compare_digest(supplied, self.server.token)   # type: ignore[attr-defined]

    def _json(self, code: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _text(self, code: int, text: str, ctype="text/plain; charset=utf-8") -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    # ---------------------------------------------------------- verbs
    def do_OPTIONS(self):                                    # CORS 预检
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, Mcp-Session-Id")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/health":
            self._json(200, {"ok": True, "service": "agent-bus",
                             "version": core.BUS_VERSION, "stats": core.stats()})
            return
        if not self._authorized(q):
            self._json(401, {"error": "invalid or missing token"})
            return
        if u.path in ("/dashboard", "/dashboard.html"):
            self._text(200, bus_dashboard.build().read_text(encoding="utf-8"), "text/html; charset=utf-8")
            return
        if u.path == "/ps":
            self._json(200, core.ps())
            return
        if u.path in ("/mcp", "/sse"):
            # 简版 SSE：一次性把"服务器就绪"事件推给客户端（码道 SSE 模式可连上）
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            with self.server.lock:                            # type: ignore[attr-defined]
                self.server.clients.add(self)                 # type: ignore[attr-defined]
            return
        self._json(404, {"error": "not found", "hint": "POST /mcp 调用工具；GET /dashboard 看看板"})

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if not self._authorized(q):
            self._json(401, {"error": "invalid or missing token"})
            return
        if u.path not in ("/mcp", "/message", "/sse"):
            self._json(404, {"error": "not found"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:                                     # noqa: BLE001
            self._json(400, {"error": "invalid JSON body"})
            return

        if isinstance(payload, list):
            out = []
            for one in payload:
                if isinstance(one, dict):
                    out.extend(dispatch_rpc(one))
            self._json(200, out if out else {"ok": True})
            return

        if not isinstance(payload, dict):
            self._json(400, {"error": "body must be a JSON-RPC object"})
            return

        out = dispatch_rpc(payload)
        if not out:                                           # 通知类消息，无响应
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._json(200, out[0] if len(out) == 1 else out)


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    core._ensure_dirs()
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.token = ensure_token()          # type: ignore[attr-defined]
    httpd.clients = set()                 # type: ignore[attr-defined]
    httpd.lock = threading.Lock()         # type: ignore[attr-defined]

    print(json.dumps({
        "serving": f"http://{host}:{port}",
        "mcp_endpoint": f"http://{host}:{port}/mcp?token={httpd.token}",
        "streamable_http_config": {
            "agent-bus": {"url": f"http://{host}:{port}/mcp?token={httpd.token}",
                          "type": "streamableHttp"}
        },
        "dashboard": f"http://{host}:{port}/dashboard?token={httpd.token}",
        "bus_home": str(core.home()),
    }, ensure_ascii=False, indent=2), flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="agent-bus HTTP 传输（StreamableHTTP/SSE MCP）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    try:
        serve(a.host, a.port)
    except KeyboardInterrupt:
        print("\nbye")
