"""Tests for start_services endpoint readiness probes."""

from __future__ import annotations

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

from start_services import check_endpoint  # noqa: E402


class ProbeHandler(BaseHTTPRequestHandler):
    window_ready = True
    calls: list[dict] = []

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        self.__class__.calls.append(payload)
        method = payload.get("method")
        if method == "notifications/initialized":
            self.send_response(204)
            self.end_headers()
            return
        if method == "initialize":
            result = {
                "protocolVersion": payload["params"]["protocolVersion"],
                "serverInfo": {"name": "codexomics", "version": "1.0.0"},
                "capabilities": {"tools": {}},
            }
            self._json({"jsonrpc": "2.0", "id": payload["id"], "result": result}, session=True)
            return
        if method == "tools/list":
            self._json(
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {"tools": [{"name": "list_genome_windows"}]},
                }
            )
            return
        if method == "tools/call":
            if not self.__class__.window_ready:
                self._json(
                    {
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "error": {"code": -32000, "message": "window not ready"},
                    }
                )
                return
            result = {"content": [{"type": "text", "text": json.dumps({"windows": []})}]}
            encoded = json.dumps({"jsonrpc": "2.0", "id": payload["id"], "result": result})
            body = f"event: message\ndata: {encoded}\n\n".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._json(
            {
                "jsonrpc": "2.0",
                "id": payload.get("id"),
                "error": {"code": -32601, "message": "not found"},
            }
        )

    def _json(self, payload: dict, session: bool = False) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if session:
            self.send_header("Mcp-Session-Id", "test-session")
        self.end_headers()
        self.wfile.write(body)


class CheckEndpointProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), ProbeHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.endpoint = f"http://127.0.0.1:{cls.server.server_port}/mcp"
        cls.required = {"list_genome_windows"}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_probe_failure_means_not_ready(self) -> None:
        ProbeHandler.window_ready = False
        ProbeHandler.calls = []
        status = check_endpoint(
            self.endpoint,
            token=None,
            required=self.required,
            probe_tool="list_genome_windows",
        )
        self.assertTrue(status["reachable"])
        self.assertFalse(status["ready"])
        self.assertIsNotNone(status["error"])
        self.assertTrue(any(call.get("method") == "tools/call" for call in ProbeHandler.calls))

    def test_probe_success_means_ready(self) -> None:
        ProbeHandler.window_ready = True
        ProbeHandler.calls = []
        status = check_endpoint(
            self.endpoint,
            token=None,
            required=self.required,
            probe_tool="list_genome_windows",
        )
        self.assertTrue(status["reachable"])
        self.assertTrue(status["ready"])
        self.assertIsNone(status["error"])


if __name__ == "__main__":
    unittest.main()
