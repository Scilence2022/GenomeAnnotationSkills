from __future__ import annotations

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

from mcp_http import McpError, McpHttpClient  # noqa: E402


class FakeMcpHandler(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        self.__class__.requests.append(payload)
        method = payload.get("method")
        if method == "notifications/initialized":
            self.send_response(204)
            self.end_headers()
            return
        if method == "initialize":
            result = {
                "protocolVersion": payload["params"]["protocolVersion"],
                "serverInfo": {"name": "fake", "version": "1"},
                "capabilities": {"tools": {}},
            }
            self._json({"jsonrpc": "2.0", "id": payload["id"], "result": result}, session=True)
            return
        if method == "tools/list":
            self._json(
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {"tools": [{"name": "echo", "description": "Echo JSON"}]},
                }
            )
            return
        if method == "tools/call" and payload.get("params", {}).get("name") == "fail":
            result = {"isError": True, "content": [{"type": "text", "text": "expected failure"}]}
            self._json({"jsonrpc": "2.0", "id": payload["id"], "result": result})
            return
        if method == "tools/call":
            result = {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"success": True, "value": payload["params"]["arguments"]}),
                    }
                ]
            }
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


class McpHttpClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        FakeMcpHandler.requests = []
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeMcpHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.endpoint = f"http://127.0.0.1:{cls.server.server_port}/mcp"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_initializes_lists_and_calls_sse_tool(self) -> None:
        with McpHttpClient(self.endpoint, token="not-logged", timeout=2) as client:
            self.assertEqual(client.session_id, "test-session")
            self.assertEqual([tool.name for tool in client.list_tools()], ["echo"])
            self.assertEqual(client.call_tool("echo", {"gene": "lysC"})["value"], {"gene": "lysC"})
        self.assertTrue(any(item.get("method") == "notifications/initialized" for item in FakeMcpHandler.requests))

    def test_tool_error_becomes_exception(self) -> None:
        with McpHttpClient(self.endpoint, timeout=2) as client:
            with self.assertRaisesRegex(McpError, "expected failure"):
                client.call_tool("fail")


if __name__ == "__main__":
    unittest.main()
