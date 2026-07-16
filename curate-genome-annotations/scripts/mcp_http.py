#!/usr/bin/env python3
"""Small dependency-free MCP Streamable HTTP client used by this skill."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


DEFAULT_PROTOCOL_VERSION = "2025-03-26"
DEFAULT_MAX_RESPONSE_BYTES = 64 * 1024 * 1024


class McpError(RuntimeError):
    """Raised for transport, JSON-RPC, or tool-level MCP failures."""


@dataclass(frozen=True)
class ToolInfo:
    name: str
    description: str = ""


class McpHttpClient:
    """Minimal synchronous MCP client with session and SSE response support."""

    def __init__(
        self,
        endpoint: str,
        token: str | None = None,
        timeout: float = 30.0,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        client_name: str = "genome-annotation-skills",
        client_version: str = "1.0.0",
    ) -> None:
        endpoint = endpoint.strip()
        if not endpoint.startswith(("http://", "https://")):
            raise ValueError("MCP endpoint must use http:// or https://")
        self.endpoint = endpoint
        self.token = token.strip() if token else None
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self.client_name = client_name
        self.client_version = client_version
        self.session_id: str | None = None
        self._next_id = 1
        self.initialized = False
        self.server_info: dict[str, Any] = {}

    def __enter__(self) -> "McpHttpClient":
        self.initialize()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "User-Agent": f"{self.client_name}/{self.client_version}",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    def _decode_response(self, raw: bytes, content_type: str) -> Any:
        if not raw:
            return None
        text = raw.decode("utf-8", errors="strict")
        if "text/event-stream" not in content_type and not text.lstrip().startswith(("event:", "data:")):
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                raise McpError(f"MCP endpoint returned invalid JSON: {exc}") from exc

        events: list[Any] = []
        data_lines: list[str] = []
        for line in text.splitlines() + [""]:
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
            elif line == "" and data_lines:
                data = "\n".join(data_lines)
                data_lines = []
                if data == "[DONE]":
                    continue
                try:
                    events.append(json.loads(data))
                except json.JSONDecodeError as exc:
                    raise McpError(f"MCP endpoint returned invalid SSE JSON: {exc}") from exc
        if not events:
            raise McpError("MCP endpoint returned an empty event stream")
        return events[-1]

    def _post(self, payload: dict[str, Any], *, expect_response: bool = True) -> Any:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.endpoint, data=body, headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                session_id = response.headers.get("Mcp-Session-Id")
                if session_id:
                    self.session_id = session_id
                raw = response.read(self.max_response_bytes + 1)
                if len(raw) > self.max_response_bytes:
                    raise McpError(
                        f"MCP response exceeded {self.max_response_bytes} bytes; use the archived report attachment"
                    )
                if not expect_response or response.status == 204:
                    return None
                return self._decode_response(raw, response.headers.get("Content-Type", ""))
        except urllib.error.HTTPError as exc:
            raw = exc.read(16 * 1024)
            detail = raw.decode("utf-8", errors="replace").strip()
            try:
                parsed = json.loads(detail)
                detail = parsed.get("error") or parsed.get("message") or detail
            except (json.JSONDecodeError, AttributeError):
                pass
            suffix = f": {detail}" if detail else ""
            raise McpError(f"MCP HTTP {exc.code} at {self.endpoint}{suffix}") from exc
        except urllib.error.URLError as exc:
            raise McpError(f"Cannot reach MCP endpoint {self.endpoint}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise McpError(f"MCP request timed out after {self.timeout:g}s at {self.endpoint}") from exc

    def _request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        request_id = self._next_id
        self._next_id += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        response = self._post(payload)
        if not isinstance(response, dict):
            raise McpError(f"MCP {method} returned no JSON-RPC response")
        if response.get("id") not in (request_id, None):
            raise McpError(f"MCP {method} returned a mismatched response id")
        if "error" in response:
            error = response["error"]
            message = error.get("message", str(error)) if isinstance(error, dict) else str(error)
            raise McpError(f"MCP {method} failed: {message}")
        if "result" not in response:
            raise McpError(f"MCP {method} returned neither result nor error")
        return response["result"]

    def _notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._post(payload, expect_response=False)

    def initialize(self) -> dict[str, Any]:
        if self.initialized:
            return self.server_info
        result = self._request(
            "initialize",
            {
                "protocolVersion": DEFAULT_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": self.client_name, "version": self.client_version},
            },
        )
        if not isinstance(result, dict):
            raise McpError("MCP initialize returned an invalid result")
        self.server_info = result
        self._notification("notifications/initialized")
        self.initialized = True
        return result

    def close(self) -> None:
        # CodeXomics and DGR both tolerate stateless clients. There is no
        # portable close notification in the MCP revisions they support.
        self.initialized = False

    def list_tools(self) -> list[ToolInfo]:
        self.initialize()
        result = self._request("tools/list", {})
        tools = result.get("tools", []) if isinstance(result, dict) else []
        if not isinstance(tools, list):
            raise McpError("MCP tools/list returned an invalid tools collection")
        return [
            ToolInfo(name=str(item.get("name", "")), description=str(item.get("description", "")))
            for item in tools
            if isinstance(item, dict) and item.get("name")
        ]

    @staticmethod
    def _tool_content(result: dict[str, Any], tool_name: str) -> Any:
        if result.get("isError") is True:
            messages = [
                str(item.get("text", ""))
                for item in result.get("content", [])
                if isinstance(item, dict) and item.get("text")
            ]
            raise McpError(f"MCP tool {tool_name} failed: {' '.join(messages) or 'unknown tool error'}")
        if "structuredContent" in result:
            payload: Any = result["structuredContent"]
        else:
            texts = [
                str(item["text"])
                for item in result.get("content", [])
                if isinstance(item, dict) and item.get("type") == "text" and "text" in item
            ]
            if not texts:
                payload = result
            else:
                joined = "\n".join(texts)
                try:
                    payload = json.loads(joined)
                except json.JSONDecodeError:
                    payload = joined

        # CodeXomics may add routing metadata around the renderer's tool result.
        if (
            isinstance(payload, dict)
            and payload.get("success") is True
            and "result" in payload
            and any(key in payload for key in ("executedVia", "windowId", "routing"))
            and isinstance(payload["result"], (dict, list))
        ):
            payload = payload["result"]
        if isinstance(payload, dict) and payload.get("success") is False:
            raise McpError(f"MCP tool {tool_name} failed: {payload.get('error') or payload.get('message') or 'unknown'}")
        return payload

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        self.initialize()
        result = self._request("tools/call", {"name": name, "arguments": arguments or {}})
        if not isinstance(result, dict):
            raise McpError(f"MCP tool {name} returned an invalid result envelope")
        return self._tool_content(result, name)


def require_tools(client: McpHttpClient, names: set[str]) -> list[str]:
    available = {tool.name for tool in client.list_tools()}
    return sorted(names - available)


def _main() -> int:
    parser = argparse.ArgumentParser(description="Validate an MCP endpoint and list its tools")
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--token", default=None)
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args()
    token = args.token or os.environ.get("MCP_API_KEY")
    try:
        with McpHttpClient(args.endpoint, token=token, timeout=args.timeout) as client:
            tools = client.list_tools()
            server = client.server_info.get("serverInfo", {})
            print(json.dumps({"endpoint": args.endpoint, "serverInfo": server, "tools": [t.name for t in tools]}, indent=2))
        return 0
    except (McpError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
