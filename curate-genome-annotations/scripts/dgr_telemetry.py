#!/usr/bin/env python3
"""Read-only DGR telemetry lookup for token, runtime, and coverage accounting.

CodeXomics remains the only orchestration path: research is always started and
polled through it. This module never starts, cancels, or mutates anything. It
only re-reads a task CodeXomics already created. Current DGR builds include
accounting fields in the annotation projection, while older builds may
require a full result read.

Use it when the workflow record has no usage block. When DGR is unreachable or
the build predates usage reporting, callers get an explicit warning rather than
an estimated number.
"""

from __future__ import annotations

import os
from typing import Any

from mcp_http import McpError, McpHttpClient


DEFAULT_DGR_URL = "http://127.0.0.1:3000/api/mcp"
TELEMETRY_TOOL = "get-task-status"
# One archived gene report can be several megabytes. Cap the telemetry read so
# an unexpectedly large result degrades to a warning instead of exhausting
# memory in an unattended nightly run.
MAX_TELEMETRY_BYTES = 32 * 1024 * 1024
CACHE_REPLAY_STEPS = {"cache-hit"}


class DgrTelemetryClient:
    """Bounded, read-only accessor for one DGR task's accounting fields."""

    def __init__(self, url: str, token: str | None, timeout: float = 60.0) -> None:
        self.url = url
        self.token = token
        self.timeout = timeout
        self._client: McpHttpClient | None = None
        self.available: bool | None = None
        self.unavailable_reason: str | None = None

    @classmethod
    def from_environment(
        cls, url: str | None = None, token: str | None = None, timeout: float = 60.0
    ) -> "DgrTelemetryClient":
        resolved_url = (url or os.environ.get("DGR_MCP_URL") or DEFAULT_DGR_URL).strip()
        resolved_token = token or os.environ.get("DGR_MCP_TOKEN") or os.environ.get("ACCESS_PASSWORD")
        return cls(resolved_url, resolved_token, timeout)

    def __enter__(self) -> "DgrTelemetryClient":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _connect(self) -> McpHttpClient | None:
        if self._client is not None:
            return self._client
        if self.available is False:
            return None
        try:
            client = McpHttpClient(
                self.url,
                token=self.token,
                timeout=self.timeout,
                max_response_bytes=MAX_TELEMETRY_BYTES,
                client_name="genome-annotation-skills-telemetry",
            )
            client.initialize()
            names = {tool.name for tool in client.list_tools()}
            if TELEMETRY_TOOL not in names:
                self.available = False
                self.unavailable_reason = (
                    f"DGR MCP at {self.url} does not expose {TELEMETRY_TOOL}; token accounting is unavailable"
                )
                client.close()
                return None
        except (McpError, ValueError, OSError) as exc:
            self.available = False
            self.unavailable_reason = f"DGR telemetry endpoint {self.url} is unavailable: {exc}"
            return None
        self.available = True
        self._client = client
        return client

    def fetch(self, task_id: str) -> tuple[dict[str, Any] | None, str | None]:
        """Return (telemetry, warning). Telemetry is None when unavailable."""
        client = self._connect()
        if client is None:
            return None, self.unavailable_reason
        try:
            payload = client.call_tool(TELEMETRY_TOOL, {"taskId": task_id, "resultMode": "annotation"})
        except McpError as exc:
            return None, f"DGR telemetry read for task {task_id} failed: {exc}"
        if not isinstance(payload, dict):
            return None, f"DGR returned a non-object telemetry payload for task {task_id}"
        if str(payload.get("taskId") or payload.get("id") or "") not in ("", task_id):
            return None, f"DGR returned telemetry for a different task than {task_id}"
        telemetry = summarize_task_telemetry(payload)
        if telemetry["status"] == "completed" and not telemetry["llmUsage"]:
            try:
                full = client.call_tool(TELEMETRY_TOOL, {"taskId": task_id, "resultMode": "full"})
            except McpError as exc:
                return telemetry, f"DGR legacy full-result telemetry read for task {task_id} failed: {exc}"
            if not isinstance(full, dict) or str(full.get("taskId") or full.get("id") or "") not in ("", task_id):
                return telemetry, f"DGR returned invalid full-result telemetry for task {task_id}"
            telemetry = summarize_task_telemetry(full)
        return telemetry, None


def summarize_task_telemetry(task: dict[str, Any]) -> dict[str, Any]:
    """Extract only the accounting fields from a full DGR task payload."""
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    diagnostics = (
        metadata.get("searchDiagnostics") if isinstance(metadata.get("searchDiagnostics"), dict) else {}
    )
    step = str(task.get("step") or "").strip().lower()
    cache_replay = metadata.get("cacheReplay") is True or step in CACHE_REPLAY_STEPS
    return {
        "taskId": task.get("taskId") or task.get("id"),
        "status": task.get("status"),
        "step": task.get("step"),
        "createdAt": task.get("createdAt"),
        "updatedAt": task.get("updatedAt"),
        # Milliseconds DGR spent on the run, as DGR measured it.
        "researchTimeMs": metadata.get("researchTime"),
        "llmUsage": metadata.get("llmUsage"),
        "llmSynthesis": metadata.get("llmSynthesis"),
        "literatureMetrics": metadata.get("literatureMetrics"),
        "literatureCoverage": diagnostics.get("literatureCoverage"),
        "searchDiagnostics": {
            key: diagnostics.get(key)
            for key in (
                "queryCount",
                "followUpQueryCount",
                "attemptedSearches",
                "successfulSearches",
                "sourceCount",
                "uniqueSourceCount",
                "literatureSourceCount",
                "authoritativeSourceCount",
            )
            if diagnostics.get(key) is not None
        }
        or None,
        "annotationNote": result.get("annotationNote"),
        # A semantic-cache replay returns the original run's usage verbatim, so
        # its tokens were reported but not charged again on this run.
        "cacheReplay": cache_replay,
    }


__all__ = [
    "CACHE_REPLAY_STEPS",
    "DEFAULT_DGR_URL",
    "DgrTelemetryClient",
    "MAX_TELEMETRY_BYTES",
    "summarize_task_telemetry",
]
