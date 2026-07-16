#!/usr/bin/env python3
"""Validate, start, and reuse CodeXomics plus Deep Gene Research services."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from mcp_http import McpError, McpHttpClient, require_tools


CODEXOMICS_TOOLS = {
    "list_genome_windows",
    "load_genome_file",
    "list_annotations",
    "list_annotation_quality_candidates",
    "list_annotation_changesets",
    "resolve_annotation_target",
    "start_annotation_research",
    "get_annotation_research_workflow",
}
DGR_TOOLS = {"deep-gene-research", "get-task-status", "cancel-research-run"}


def default_state_dir() -> Path:
    configured = os.environ.get("GENOME_ANNOTATION_STATE_DIR")
    return Path(configured or "~/.local/state/genome-annotation-skills").expanduser()


def scoped_codexomics_token(environment: dict[str, str]) -> str | None:
    explicit = environment.get("CODEXOMICS_MCP_API_KEY") or environment.get("CODEXOMICS_MCP_MASTER_KEY")
    if explicit:
        return explicit
    raw = environment.get("CODEXOMICS_MCP_API_KEYS_JSON")
    if not raw:
        return None
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError:
        return None
    candidates = entries.values() if isinstance(entries, dict) else entries if isinstance(entries, list) else []
    for entry in candidates:
        if not isinstance(entry, dict) or not entry.get("apiKey"):
            continue
        permissions = set(entry.get("permissions") or [])
        if {"annotation:read", "annotation:research"}.issubset(permissions):
            return str(entry["apiKey"])
    return None


def check_endpoint(endpoint: str, token: str | None, required: set[str]) -> dict[str, Any]:
    try:
        with McpHttpClient(endpoint, token=token, timeout=8.0) as client:
            missing = require_tools(client, required)
            server_info = client.server_info.get("serverInfo", {})
            return {
                "reachable": True,
                "ready": not missing,
                "missingTools": missing,
                "serverInfo": server_info,
                "error": None,
            }
    except (McpError, ValueError) as exc:
        return {
            "reachable": False,
            "ready": False,
            "missingTools": sorted(required),
            "serverInfo": {},
            "error": str(exc),
        }


def ensure_repo(directory: Path, expected_package: str) -> None:
    package_file = directory / "package.json"
    if not package_file.is_file():
        raise RuntimeError(f"Missing package.json in {directory}; run bootstrap_repositories.py first")
    try:
        name = json.loads(package_file.read_text(encoding="utf-8")).get("name")
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read {package_file}: {exc}") from exc
    if name != expected_package:
        raise RuntimeError(f"Unexpected package {name!r} in {directory}; expected {expected_package!r}")


def pnpm_command() -> list[str]:
    if shutil.which("pnpm"):
        return ["pnpm"]
    if shutil.which("corepack"):
        return ["corepack", "pnpm"]
    raise RuntimeError("pnpm is required for DGR; install it or enable Corepack")


def endpoint_origin(endpoint: str, default_port: int) -> tuple[str, int, str]:
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError(f"Invalid service endpoint: {endpoint}")
    port = parsed.port or (443 if parsed.scheme == "https" else default_port)
    origin = f"{parsed.scheme}://{parsed.hostname}:{port}"
    return origin, port, parsed.path or "/"


def secure_state_dir(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.chmod(stat.S_IRWXU)
    except OSError:
        pass


def launch(
    name: str,
    command: list[str],
    cwd: Path,
    environment: dict[str, str],
    state_dir: Path,
) -> dict[str, Any]:
    log_path = state_dir / f"{name}.log"
    log_handle = log_path.open("ab", buffering=0)
    try:
        os.chmod(log_path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log_handle.close()
    metadata = {
        "name": name,
        "pid": process.pid,
        "command": command,
        "cwd": str(cwd),
        "log": str(log_path),
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    metadata_path = state_dir / f"{name}.pid.json"
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(metadata_path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return metadata


def wait_until_ready(
    name: str,
    endpoint: str,
    token: str | None,
    required: set[str],
    timeout: float,
    process_metadata: dict[str, Any],
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last = check_endpoint(endpoint, token, required)
    while time.monotonic() < deadline:
        if last["ready"]:
            return last
        pid = int(process_metadata["pid"])
        try:
            os.kill(pid, 0)
        except ProcessLookupError as exc:
            raise RuntimeError(f"{name} exited during startup; inspect {process_metadata['log']}") from exc
        time.sleep(1.0)
        last = check_endpoint(endpoint, token, required)
    raise RuntimeError(
        f"{name} did not become ready at {endpoint} within {timeout:g}s: {last.get('error') or last.get('missingTools')}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start or validate DGR and CodeXomics MCP services")
    parser.add_argument("--codexomics-dir", type=Path)
    parser.add_argument("--dgr-dir", type=Path)
    parser.add_argument(
        "--codexomics-url",
        default=os.environ.get("CODEXOMICS_MCP_URL", "http://127.0.0.1:3002/mcp"),
    )
    parser.add_argument("--dgr-url", default=os.environ.get("DGR_MCP_URL", "http://127.0.0.1:3000/api/mcp"))
    parser.add_argument("--codexomics-token", default=None)
    parser.add_argument("--dgr-token", default=None)
    parser.add_argument("--state-dir", type=Path, default=default_state_dir())
    parser.add_argument("--dgr-mode", choices=("development", "production"), default="development")
    parser.add_argument("--startup-timeout", type=float, default=120.0)
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--allow-insecure-local-bypass", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    environment = dict(os.environ)
    if args.allow_insecure_local_bypass:
        environment.setdefault("CODEXOMICS_MCP_ENABLE_LOCAL_BYPASS", "true")
        environment.setdefault("DGR_ALLOW_UNAUTHENTICATED_DEV", "true")

    codexomics_token = args.codexomics_token or scoped_codexomics_token(environment)
    dgr_token = args.dgr_token or environment.get("DGR_MCP_TOKEN") or environment.get("ACCESS_PASSWORD")
    statuses = {
        "dgr": check_endpoint(args.dgr_url, dgr_token, DGR_TOOLS),
        "codexomics": check_endpoint(args.codexomics_url, codexomics_token, CODEXOMICS_TOOLS),
    }
    if args.check_only:
        print(json.dumps(statuses, indent=2))
        return 0 if all(status["ready"] for status in statuses.values()) else 1

    state_dir = args.state_dir.expanduser().resolve()
    secure_state_dir(state_dir)
    started: dict[str, Any] = {}
    try:
        if statuses["dgr"]["reachable"] and not statuses["dgr"]["ready"]:
            raise RuntimeError(
                f"DGR endpoint is occupied but missing required tools: {statuses['dgr']['missingTools']}"
            )
        if not statuses["dgr"]["ready"]:
            if args.dgr_dir is None:
                raise RuntimeError("DGR is not ready; provide --dgr-dir to start it")
            dgr_dir = args.dgr_dir.expanduser().resolve()
            ensure_repo(dgr_dir, "deep-gene-research")
            if not (environment.get("ACCESS_PASSWORD") or environment.get("DGR_ALLOW_UNAUTHENTICATED_DEV") == "true"):
                raise RuntimeError(
                    "DGR requires ACCESS_PASSWORD, or explicit --allow-insecure-local-bypass for isolated development"
                )
            dgr_origin, dgr_port, dgr_path = endpoint_origin(args.dgr_url, 3000)
            if dgr_path.rstrip("/") != "/api/mcp":
                raise RuntimeError("A locally started DGR endpoint must use the /api/mcp path")
            environment["PORT"] = str(dgr_port)
            environment.setdefault("MCP_SERVER_BASE_URL", dgr_origin)
            environment.setdefault("DGR_MCP_URL", args.dgr_url)
            if dgr_token:
                environment.setdefault("DGR_MCP_TOKEN", dgr_token)
            pnpm = pnpm_command()
            if args.dgr_mode == "production":
                subprocess.run(pnpm + ["build"], cwd=dgr_dir, env=environment, check=True)
                command = pnpm + ["start"]
            else:
                command = pnpm + ["dev"]
            started["dgr"] = launch("dgr", command, dgr_dir, environment, state_dir)
            statuses["dgr"] = wait_until_ready(
                "DGR",
                args.dgr_url,
                dgr_token,
                DGR_TOOLS,
                args.startup_timeout,
                started["dgr"],
            )

        if statuses["codexomics"]["reachable"] and not statuses["codexomics"]["ready"]:
            raise RuntimeError(
                "CodeXomics endpoint is occupied but missing required tools: "
                f"{statuses['codexomics']['missingTools']}. Confirm tools mode and scoped permissions."
            )
        if not statuses["codexomics"]["ready"]:
            if args.codexomics_dir is None:
                raise RuntimeError("CodeXomics is not ready; provide --codexomics-dir to start it")
            codexomics_dir = args.codexomics_dir.expanduser().resolve()
            ensure_repo(codexomics_dir, "codexomics")
            _, codexomics_port, codexomics_path = endpoint_origin(args.codexomics_url, 3002)
            if codexomics_port != 3002 or codexomics_path.rstrip("/") != "/mcp":
                raise RuntimeError(
                    "The bundled CodeXomics start-with-mcp command currently binds http://127.0.0.1:3002/mcp; "
                    "use that endpoint or start a separately configured service before running this script"
                )
            has_auth = any(
                environment.get(name)
                for name in ("CODEXOMICS_MCP_MASTER_KEY", "CODEXOMICS_MCP_API_KEYS_JSON")
            ) or environment.get("CODEXOMICS_MCP_ENABLE_LOCAL_BYPASS") == "true"
            if not has_auth:
                raise RuntimeError(
                    "CodeXomics MCP requires scoped API keys or a master key, or explicit local bypass for development"
                )
            environment["DGR_MCP_URL"] = args.dgr_url
            if dgr_token:
                environment["DGR_MCP_TOKEN"] = dgr_token
            started["codexomics"] = launch(
                "codexomics",
                ["npm", "run", "start-with-mcp"],
                codexomics_dir,
                environment,
                state_dir,
            )
            statuses["codexomics"] = wait_until_ready(
                "CodeXomics",
                args.codexomics_url,
                codexomics_token,
                CODEXOMICS_TOOLS,
                args.startup_timeout,
                started["codexomics"],
            )

        print(json.dumps({"ready": True, "statuses": statuses, "started": started}, indent=2))
        return 0
    except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        if started:
            print(
                "One or more processes were started before the failure. Inspect their PID metadata and logs; "
                "they were not terminated automatically.",
                file=sys.stderr,
            )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
