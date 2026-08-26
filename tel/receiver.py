"""Dependency-free OTLP receiver, used when Docker is unavailable.

Accepts OTLP over **HTTP/JSON only** (`OTEL_EXPORTER_OTLP_PROTOCOL=http/json`)
and appends each request verbatim to the same newline-delimited JSON files the
collector writes, so the analysis layer cannot tell the difference.

This is a fallback. The OpenTelemetry Collector is the supported path: it also
speaks gRPC and http/protobuf, handles batching and file rotation, and is far
better tested.
"""
from __future__ import annotations

import datetime as _dt
import gzip
import json
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config

_LOCK = threading.Lock()

PATHS = {
    "/v1/logs": ("logs", "resourceLogs"),
    "/v1/metrics": ("metrics", "resourceMetrics"),
    "/v1/traces": ("traces", "resourceSpans"),
}


def _append(signal_name: str, payload: dict) -> None:
    out = Path(config.RAW_DIR) / f"{signal_name}.jsonl"
    with _LOCK:
        with out.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, separators=(",", ":")) + "\n")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "claude-telemetry-receiver"

    def log_message(self, fmt, *args):  # keep stdout useful
        pass

    def _reply(self, code: int, body: bytes = b"{}") -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") in ("/health", ""):
            self._reply(200, b'{"status":"Server available"}')
        else:
            self._reply(404, b'{"error":"not found"}')

    def do_POST(self):
        route = PATHS.get(self.path.split("?")[0])
        if not route:
            self._reply(404, b'{"error":"unsupported signal path"}')
            return
        signal_name, root_key = route
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if (self.headers.get("Content-Encoding") or "").lower() == "gzip":
                raw = gzip.decompress(raw)
            ctype = (self.headers.get("Content-Type") or "").lower()
            if "json" not in ctype:
                self._reply(415, json.dumps({
                    "error": "this fallback receiver accepts OTLP/JSON only; "
                             "set OTEL_EXPORTER_OTLP_PROTOCOL=http/json, or run "
                             "the collector with `telemetry start`"}).encode())
                return
            payload = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            self._reply(400, json.dumps({"error": str(exc)}).encode())
            return

        if root_key not in payload:
            payload = {root_key: payload.get(root_key, [])}
        _append(signal_name, payload)
        self._reply(200)


def serve(port: int | None = None) -> None:
    config.ensure_dirs()
    port = port or config.OTLP_HTTP_PORT
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)

    def _stop(*_):
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    started = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    print(f"[{started}] fallback OTLP/JSON receiver on http://127.0.0.1:{port} "
          f"-> {config.RAW_DIR}", flush=True)
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        httpd.server_close()
        print("receiver stopped", flush=True)


if __name__ == "__main__":
    serve(int(sys.argv[1]) if len(sys.argv) > 1 else None)
