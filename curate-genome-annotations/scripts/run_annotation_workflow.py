#!/usr/bin/env python3
"""Run safe, resumable CodeXomics-managed DGR annotation research workflows."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from mcp_http import McpError, McpHttpClient, require_tools


REQUIRED_TOOLS = {
    "get_genome_info",
    "get_annotation_research_workflow",
    "list_annotation_quality_candidates",
    "list_annotation_changesets",
    "list_annotation_research_history",
    "list_genome_windows",
    "load_genome_file",
    "resolve_annotation_target",
    "start_annotation_research",
}
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "canceled"}
DAILY_COVERED_STATUSES = {"completed", "skipped"}
RETRYABLE_WORKFLOW_STATUSES = {"failed", "cancelled", "canceled"}
EXISTING_CHANGESET_TERMINAL_REUSABLE = {"rejected", "stale", "rolled_back", "cancelled"}
DEFAULT_PROMPT = (
    "Refine this gene annotation feature using organism-specific evidence. Exclude lexical gene-name collisions and "
    "unrelated organisms, label homolog-only evidence, synthesize a concise citation-rich Note, and preserve uncertainty."
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: object, length: int = 32) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()[:length]


def normalized(value: object) -> str:
    return str(value or "").strip().lower()


def positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be at least 1")
    return parsed


def quality_score(value: str) -> float:
    parsed = float(value)
    if parsed < 0 or parsed > 100:
        raise argparse.ArgumentTypeError("value must be from 0 to 100")
    return parsed


def research_refresh_days(value: str) -> int:
    parsed = int(value)
    if parsed < 1 or parsed > 3650:
        raise argparse.ArgumentTypeError("value must be from 1 to 3650")
    return parsed


def parse_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in re.split(r"[,\t\r\n]+", value) if part.strip()]


def unique_identifiers(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = normalized(value)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(value.strip())
    return result


def read_gene_file(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    retained: list[str] = []
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if stripped:
            retained.extend(parse_list(stripped))
    return unique_identifiers(retained)


def atomic_json_write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(stat.S_IRWXU)
    except OSError:
        pass
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


class GenomeLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Any = None

    def __enter__(self) -> "GenomeLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        try:
            import fcntl

            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (ImportError, BlockingIOError) as exc:
            self.handle.close()
            self.handle = None
            raise RuntimeError(f"Another annotation run holds the lock {self.path}") from exc
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(json.dumps({"pid": os.getpid(), "startedAt": utc_now()}))
        self.handle.flush()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.handle is None:
            return
        with contextlib.suppress(Exception):
            import fcntl

            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()


@dataclass(frozen=True)
class Candidate:
    identifier: str
    chromosome: str | None = None
    start: int | None = None
    feature_id: str | None = None
    feature_type: str | None = None
    quality_score: float | None = None
    quality_band: str | None = None
    quality_reasons: tuple[str, ...] = ()
    recommended_research_focus: tuple[str, ...] = ()
    quality_policy_version: str | None = None


@dataclass(frozen=True)
class CandidateSelection:
    candidates: tuple[Candidate, ...]
    quality_matching_features: int
    excluded_by_research_history: int
    research_history_policy: str


def routing(window_id: str, expected_genome: str) -> dict[str, str]:
    result = {"windowId": window_id}
    if expected_genome:
        result["expected_genome"] = expected_genome
    return result


def extract_windows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("windows"), list):
        raise RuntimeError("CodeXomics returned an invalid genome window list")
    return [item for item in payload["windows"] if isinstance(item, dict) and item.get("windowId")]


def info_for_window(client: McpHttpClient, window_id: str) -> dict[str, Any] | None:
    try:
        payload = client.call_tool("get_genome_info", {"windowId": window_id})
    except McpError:
        return None
    if not isinstance(payload, dict):
        return None
    genome_info = payload.get("genomeInfo")
    return genome_info if isinstance(genome_info, dict) else None


def contains_exact_path(value: object, genome_path: Path) -> bool:
    target = str(genome_path)
    if isinstance(value, str):
        with contextlib.suppress(OSError):
            if str(Path(value).expanduser().resolve()) == target:
                return True
        return value == target
    if isinstance(value, dict):
        return any(contains_exact_path(item, genome_path) for item in value.values())
    if isinstance(value, list):
        return any(contains_exact_path(item, genome_path) for item in value)
    return False


def prepare_genome(
    client: McpHttpClient,
    genome_path: Path,
    requested_window_id: str | None,
    requested_expected_genome: str | None,
) -> tuple[str, str, dict[str, Any]]:
    windows = extract_windows(client.call_tool("list_genome_windows", {}))
    if not windows:
        raise RuntimeError("No connected CodeXomics genome window; start the app with its MCP server")

    selected: dict[str, Any] | None = None
    already_loaded = False
    if requested_window_id:
        selected = next((item for item in windows if str(item["windowId"]) == requested_window_id), None)
        if selected is None:
            available = ", ".join(str(item["windowId"]) for item in windows)
            raise RuntimeError(f"Unknown window {requested_window_id!r}; available windows: {available}")
        info = info_for_window(client, requested_window_id)
        already_loaded = bool(info and contains_exact_path(info.get("loadedFiles", []), genome_path))
    else:
        exact: list[dict[str, Any]] = []
        for item in windows:
            info = info_for_window(client, str(item["windowId"]))
            if info and contains_exact_path(info.get("loadedFiles", []), genome_path):
                exact.append(item)
        if len(exact) == 1:
            selected = exact[0]
            already_loaded = True
        elif len(exact) > 1:
            raise RuntimeError("The same genome path is reported in multiple windows; provide --window-id")
        elif len(windows) == 1:
            selected = windows[0]
        else:
            raise RuntimeError("Multiple CodeXomics windows are open and the genome is not loaded; provide --window-id")

    window_id = str(selected["windowId"])
    if not already_loaded:
        client.call_tool(
            "load_genome_file",
            {"filePath": str(genome_path), "fileType": "auto", "showFileDialog": False, "windowId": window_id},
        )

    info_payload = client.call_tool("get_genome_info", {"windowId": window_id})
    if not isinstance(info_payload, dict) or not isinstance(info_payload.get("genomeInfo"), dict):
        raise RuntimeError("CodeXomics did not return genome metadata after loading")
    genome_info = info_payload["genomeInfo"]
    windows_after = extract_windows(client.call_tool("list_genome_windows", {}))
    updated = next((item for item in windows_after if str(item["windowId"]) == window_id), selected)
    expected = str(updated.get("genomeName") or genome_info.get("name") or genome_path.name)
    if requested_expected_genome and normalized(requested_expected_genome) != normalized(expected):
        raise RuntimeError(
            f"Loaded genome name {expected!r} does not match requested --expected-genome {requested_expected_genome!r}"
        )
    return window_id, expected, genome_info


def feature_identity(target: dict[str, Any]) -> str:
    return normalized(
        target.get("locusTag") or target.get("proteinId") or target.get("geneSymbol") or target.get("featureId")
    )


def select_workflow_attempt(workflows: dict[str, Any], base_key: str) -> tuple[str, dict[str, Any]]:
    attempts: list[tuple[int, str, dict[str, Any]]] = []
    for key, value in workflows.items():
        if not isinstance(value, dict):
            continue
        if key == base_key:
            attempts.append((0, key, value))
            continue
        prefix = f"{base_key}:retry:"
        if not key.startswith(prefix):
            continue
        with contextlib.suppress(ValueError):
            attempts.append((int(key[len(prefix) :]), key, value))
    if not attempts:
        return base_key, {}
    attempt, key, record = max(attempts, key=lambda item: item[0])
    if normalized(record.get("status")) in RETRYABLE_WORKFLOW_STATUSES:
        return f"{base_key}:retry:{attempt + 1}", {}
    return key, record


def changeset_identities(client: McpHttpClient, route: dict[str, str]) -> set[str]:
    offset = 0
    identities: set[str] = set()
    while True:
        payload = client.call_tool("list_annotation_changesets", {**route, "limit": 1000, "offset": offset})
        if not isinstance(payload, dict) or not isinstance(payload.get("changeSets"), list):
            raise RuntimeError("CodeXomics returned an invalid ChangeSet list")
        page = payload["changeSets"]
        for item in page:
            if not isinstance(item, dict):
                continue
            if normalized(item.get("status")) in EXISTING_CHANGESET_TERMINAL_REUSABLE:
                continue
            target_value = item.get("target")
            target: dict[str, Any] = target_value if isinstance(target_value, dict) else {}
            for field in ("locusTag", "proteinId", "featureId", "geneSymbol"):
                if target.get(field):
                    identities.add(normalized(target[field]))
        offset += len(page)
        if not page or offset >= int(payload.get("total", 0)):
            return identities


def enumerate_annotation_candidates(
    client: McpHttpClient,
    route: dict[str, str],
    genome_info: dict[str, Any],
    chromosome_filter: str | None,
    selection_policy: str,
    maximum_quality_score: float,
    feature_types: list[str] | None,
    research_history_policy: str = "include",
    research_refresh_days_value: int | None = None,
) -> CandidateSelection:
    chromosomes = genome_info.get("chromosomes") or []
    if chromosome_filter and chromosome_filter not in chromosomes:
        raise RuntimeError(f"CodeXomics did not report chromosome {chromosome_filter!r}")
    candidates: list[Candidate] = []
    arguments: dict[str, Any] = {
        **route,
        "sortBy": "quality" if selection_policy == "low-quality" else "coordinate",
        "maximumQualityScore": maximum_quality_score if selection_policy == "low-quality" else 100,
        "limit": 0,
        "offset": 0,
        "researchHistoryPolicy": research_history_policy,
    }
    if research_refresh_days_value is not None:
        arguments["researchRefreshDays"] = research_refresh_days_value
    if chromosome_filter:
        arguments["chromosome"] = chromosome_filter
    if feature_types:
        arguments["featureTypes"] = feature_types
    payload = client.call_tool("list_annotation_quality_candidates", arguments)
    if not isinstance(payload, dict) or not isinstance(payload.get("candidates"), list):
        raise RuntimeError("CodeXomics returned an invalid annotation quality candidate list")
    for item in payload["candidates"]:
        if not isinstance(item, dict) or not isinstance(item.get("feature"), dict):
            continue
        feature = item["feature"]
        identifier = str(
            feature.get("locusTag") or feature.get("proteinId") or feature.get("gene") or ""
        ).strip()
        if not identifier:
            continue
        reasons = tuple(
            str(reason.get("code"))
            for reason in item.get("reasons", [])
            if isinstance(reason, dict) and reason.get("code")
        )
        focus = tuple(str(value) for value in item.get("recommendedResearchFocus", []) if str(value).strip())
        candidates.append(
            Candidate(
                identifier=identifier,
                chromosome=str(item.get("chromosome") or chromosome_filter or "") or None,
                start=int(feature.get("start") or 0),
                feature_id=str(feature.get("id") or "") or None,
                feature_type=str(feature.get("featureType") or "") or None,
                quality_score=float(item.get("qualityScore")) if item.get("qualityScore") is not None else None,
                quality_band=str(item.get("qualityBand") or "") or None,
                quality_reasons=reasons,
                recommended_research_focus=focus,
                quality_policy_version=str(item.get("policyVersion") or payload.get("policyVersion") or "") or None,
            )
        )
    excluded_by_history = int(payload.get("excludedByResearchHistory") or 0)
    return CandidateSelection(
        candidates=tuple(candidates),
        quality_matching_features=len(candidates) + excluded_by_history,
        excluded_by_research_history=excluded_by_history,
        research_history_policy=str(payload.get("researchHistoryPolicy") or research_history_policy),
    )


def compact_workflow(workflow: dict[str, Any]) -> dict[str, Any]:
    attachment = workflow.get("reportAttachment") if isinstance(workflow.get("reportAttachment"), dict) else None
    target_value = workflow.get("target")
    target: dict[str, Any] = target_value if isinstance(target_value, dict) else {}
    return {
        "status": workflow.get("status"),
        "taskId": workflow.get("taskId"),
        "target": {
            key: target.get(key)
            for key in ("featureType", "featureId", "chromosome", "locusTag", "geneSymbol", "proteinId", "organism")
            if target.get(key) is not None
        },
        "progress": workflow.get("progress"),
        "step": workflow.get("step"),
        "error": workflow.get("error"),
        "proposalStatus": workflow.get("proposalStatus"),
        "proposalReason": workflow.get("proposalReason"),
        "changeSetId": workflow.get("changeSetId"),
        "changeSetStatus": workflow.get("changeSetStatus"),
        "reportAttachment": {
            key: attachment.get(key)
            for key in ("attachmentId", "geneId", "fileName", "size", "sha256", "storedAt")
            if attachment and attachment.get(key) is not None
        }
        if attachment
        else None,
    }


def poll_workflow(
    client: McpHttpClient,
    task_id: str,
    route: dict[str, str],
    timeout: float,
    initial_interval: float,
    max_interval: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    interval = max(1.0, initial_interval)
    last_workflow: dict[str, Any] = {"taskId": task_id, "status": "unknown"}
    while time.monotonic() < deadline:
        payload = client.call_tool("get_annotation_research_workflow", {**route, "taskId": task_id})
        if not isinstance(payload, dict) or not isinstance(payload.get("workflow"), dict):
            raise RuntimeError(f"CodeXomics returned an invalid workflow status for {task_id}")
        last_workflow = payload["workflow"]
        status = normalized(last_workflow.get("status"))
        if status in TERMINAL_STATUSES:
            return last_workflow
        time.sleep(interval)
        interval = min(max_interval, interval * 1.35)
    compact = compact_workflow(last_workflow)
    raise TimeoutError(f"Research workflow {task_id} did not finish within {timeout:g}s; last state: {compact}")


def load_state(path: Path, genome_path: Path, genome_key: str) -> dict[str, Any]:
    if not path.exists():
        return {
            "version": 1,
            "genomePath": str(genome_path),
            "genomeKey": genome_key,
            "createdAt": utc_now(),
            "updatedAt": utc_now(),
            "workflows": {},
        }
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read state file {path}: {exc}") from exc
    if state.get("genomeKey") != genome_key or state.get("genomePath") != str(genome_path):
        raise RuntimeError(f"State file {path} belongs to a different genome")
    if not isinstance(state.get("workflows"), dict):
        raise RuntimeError(f"State file {path} has an invalid workflows map")
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    state["updatedAt"] = utc_now()
    atomic_json_write(path, state)


def persist_failed_workflow(
    path: Path,
    state: dict[str, Any],
    key: str | None,
    *,
    task_id: str | None,
    selection_mode: str,
    requested_identifier: str,
    resolved_identity: str | None,
    error: Exception,
) -> None:
    if not key:
        return
    existing = state["workflows"].get(key)
    effective_task_id = task_id or (existing.get("taskId") if isinstance(existing, dict) else None)
    if not effective_task_id:
        return
    record = state["workflows"].setdefault(key, {})
    finished_at = utc_now()
    record.update(
        {
            "taskId": effective_task_id,
            "selectionMode": selection_mode,
            "requestedIdentifier": requested_identifier,
            "resolvedIdentity": resolved_identity or record.get("resolvedIdentity"),
            "status": "failed",
            "error": str(error),
            "errorType": type(error).__name__,
            "retryable": task_id is not None,
            "failureCount": int(record.get("failureCount") or 0) + 1,
            "finishedAt": finished_at,
        }
    )
    save_state(path, state)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create evidence-backed, human-reviewable CodeXomics annotation ChangeSets"
    )
    parser.add_argument("--genome", type=Path, required=True, help="Absolute GenBank, EMBL, or FASTA path")
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--gene")
    selector.add_argument("--genes")
    selector.add_argument("--gene-file", type=Path)
    selector.add_argument("--daily-count", type=positive_integer)
    parser.add_argument(
        "--selection-policy",
        choices=("low-quality", "coordinate"),
        default="low-quality",
        help="Daily candidate ranking policy (default: low-quality)",
    )
    parser.add_argument(
        "--maximum-quality-score",
        type=quality_score,
        default=70,
        help="Maximum CodeXomics quality score selected by low-quality policy (default: 70)",
    )
    parser.add_argument(
        "--feature-types",
        help="Optional comma-separated gene annotation feature types (for example CDS,tRNA,rRNA,ncRNA,gene)",
    )
    parser.add_argument("--chromosome")
    parser.add_argument("--window-id")
    parser.add_argument("--expected-genome")
    parser.add_argument(
        "--codexomics-url",
        default=os.environ.get("CODEXOMICS_MCP_URL", "http://127.0.0.1:3002/mcp"),
    )
    parser.add_argument("--codexomics-token", default=None)
    parser.add_argument("--organism", help="Fallback only when loaded genome metadata has no organism")
    parser.add_argument("--user-prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--research-focus", action="append", default=[])
    parser.add_argument("--specific-aspect", action="append", default=[])
    parser.add_argument("--language", default="English")
    parser.add_argument("--max-result", type=int, choices=range(1, 21), default=10, metavar="1..20")
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument(
        "--include-researched",
        action="store_true",
        help="Allow daily selection to repeat targets with active or durably completed CodeXomics research",
    )
    parser.add_argument(
        "--research-refresh-days",
        type=research_refresh_days,
        help="Allow archived completed research older than this many days to become eligible again",
    )
    parser.add_argument("--include-existing-changesets", action="store_true")
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--max-poll-interval", type=float, default=30.0)
    parser.add_argument("--workflow-timeout", type=float, default=3600.0)
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(os.environ.get("GENOME_ANNOTATION_STATE_DIR", "~/.local/state/genome-annotation-skills")),
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="Load/inspect the genome but do not start research")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    genome_path = args.genome.expanduser()
    if not genome_path.is_absolute():
        print("ERROR: --genome must be an absolute path", file=sys.stderr)
        return 2
    genome_path = genome_path.resolve()
    if not genome_path.is_file():
        print(f"ERROR: genome file does not exist: {genome_path}", file=sys.stderr)
        return 2
    if args.gene_file:
        args.gene_file = args.gene_file.expanduser().resolve()
        if not args.gene_file.is_file():
            print(f"ERROR: gene file does not exist: {args.gene_file}", file=sys.stderr)
            return 2

    stat_info = genome_path.stat()
    genome_key = digest(
        {"path": str(genome_path), "size": stat_info.st_size, "mtimeNs": stat_info.st_mtime_ns},
        20,
    )
    state_dir = args.state_dir.expanduser().resolve()
    state_path = state_dir / f"{genome_key}.json"
    lock_path = state_dir / f"{genome_key}.lock"
    token = args.codexomics_token or os.environ.get("CODEXOMICS_MCP_API_KEY") or os.environ.get(
        "CODEXOMICS_MCP_MASTER_KEY"
    )
    summary: dict[str, Any] = {
        "startedAt": utc_now(),
        "genomePath": str(genome_path),
        "selector": "daily-count" if args.daily_count else "explicit",
        "dryRun": args.dry_run,
        "results": [],
        "automatedApprovalOrApplication": False,
    }

    try:
        with GenomeLock(lock_path), McpHttpClient(args.codexomics_url, token=token, timeout=35.0) as client:
            missing = require_tools(client, REQUIRED_TOOLS)
            if missing:
                raise RuntimeError(
                    "CodeXomics MCP is missing required tools or permissions: " + ", ".join(missing)
                )
            window_id, expected_genome, genome_info = prepare_genome(
                client,
                genome_path,
                args.window_id,
                args.expected_genome,
            )
            route = routing(window_id, expected_genome)
            summary["windowId"] = window_id
            summary["expectedGenome"] = expected_genome
            summary["genomeInfo"] = {
                key: genome_info.get(key)
                for key in ("name", "length", "chromosomes", "annotations")
                if genome_info.get(key) is not None
            }
            state = load_state(state_path, genome_path, genome_key)

            if args.daily_count:
                requested_feature_types = parse_list(args.feature_types)
                selection = enumerate_annotation_candidates(
                    client,
                    route,
                    genome_info,
                    args.chromosome,
                    args.selection_policy,
                    args.maximum_quality_score,
                    requested_feature_types or None,
                    "include" if args.include_researched else "exclude-covered",
                    args.research_refresh_days,
                )
                all_candidates = list(selection.candidates)
                excluded_changesets = (
                    set() if args.include_existing_changesets else changeset_identities(client, route)
                )
                completed_daily = {
                    normalized(record.get("resolvedIdentity"))
                    for record in state["workflows"].values()
                    if isinstance(record, dict)
                    and record.get("selectionMode") == "daily-count"
                    and normalized(record.get("status")) in DAILY_COVERED_STATUSES
                }
                candidates = [
                    candidate
                    for candidate in all_candidates
                    if normalized(candidate.identifier) not in excluded_changesets
                    and normalized(candidate.identifier) not in completed_daily
                ][: args.daily_count]
                summary["selection"] = {
                    "requested": args.daily_count,
                    "qualityMatchingFeatures": selection.quality_matching_features,
                    "availableFeatures": len(all_candidates),
                    "selected": len(candidates),
                    "excludedByExistingChangeSet": len(excluded_changesets),
                    "excludedByResearchHistory": selection.excluded_by_research_history,
                    "policy": args.selection_policy,
                    "researchHistoryPolicy": selection.research_history_policy,
                    "researchRefreshDays": args.research_refresh_days,
                    "maximumQualityScore": args.maximum_quality_score,
                    "featureTypes": requested_feature_types or "CodeXomics defaults",
                }
            else:
                identifiers = (
                    [args.gene]
                    if args.gene
                    else parse_list(args.genes)
                    if args.genes
                    else read_gene_file(args.gene_file)
                )
                identifiers = unique_identifiers([item for item in identifiers if item])
                if not identifiers:
                    raise RuntimeError("The explicit gene selector did not contain any identifiers")
                candidates = [Candidate(identifier=item, chromosome=args.chromosome) for item in identifiers]
                summary["selection"] = {"requested": len(identifiers), "selected": len(candidates)}

            if args.dry_run:
                validated_candidates: list[dict[str, Any]] = []
                for item in candidates:
                    candidate_summary: dict[str, Any] = {
                        "identifier": item.identifier,
                        "chromosome": item.chromosome,
                        "start": item.start,
                    }
                    try:
                        resolved = client.call_tool(
                            "resolve_annotation_target",
                            {
                                **route,
                                "identifier": item.identifier,
                                **({"chromosome": item.chromosome} if item.chromosome else {}),
                            },
                        )
                        target = resolved.get("target") if isinstance(resolved, dict) else None
                        if not isinstance(target, dict):
                            raise RuntimeError("CodeXomics returned an invalid resolved target")
                        if not (target.get("locusTag") or target.get("proteinId") or target.get("geneSymbol")):
                            raise RuntimeError(
                                "Resolved annotation target lacks a locus tag, protein identifier, or gene symbol"
                            )
                        candidate_summary["eligible"] = True
                        candidate_summary["resolvedTarget"] = compact_workflow({"target": target})["target"]
                    except (McpError, RuntimeError, ValueError) as exc:
                        candidate_summary.update({"eligible": False, "error": str(exc)})
                    validated_candidates.append(candidate_summary)
                summary["candidates"] = validated_candidates
            else:
                intent = {
                    "userPrompt": args.user_prompt,
                    "researchFocus": args.research_focus,
                    "specificAspects": args.specific_aspect,
                    "language": args.language,
                    "maxResult": args.max_result,
                    "forceRefresh": args.force_refresh,
                }
                for candidate in candidates:
                    result: dict[str, Any] = {
                        "requestedIdentifier": candidate.identifier,
                        "requestedChromosome": candidate.chromosome,
                        "startedAt": utc_now(),
                        "candidateFeatureType": candidate.feature_type,
                        "candidateQualityScore": candidate.quality_score,
                        "candidateQualityBand": candidate.quality_band,
                        "candidateQualityReasons": list(candidate.quality_reasons),
                    }
                    key: str | None = None
                    task_id: str | None = None
                    resolved_identity: str | None = None
                    try:
                        resolved = client.call_tool(
                            "resolve_annotation_target",
                            {
                                **route,
                                "identifier": candidate.identifier,
                                **({"chromosome": candidate.chromosome} if candidate.chromosome else {}),
                            },
                        )
                        if not isinstance(resolved, dict) or not isinstance(resolved.get("target"), dict):
                            raise RuntimeError("CodeXomics returned an invalid resolved target")
                        target = resolved["target"]
                        stable_identifier = (
                            target.get("locusTag")
                            or target.get("proteinId")
                            or target.get("geneSymbol")
                        )
                        if not stable_identifier:
                            raise RuntimeError(
                                "Resolved annotation target lacks a locus tag, protein identifier, or gene symbol"
                            )
                        resolved_identity = feature_identity(target)
                        candidate_intent = {
                            **intent,
                            "selectionPolicy": args.selection_policy if args.daily_count else "explicit",
                            "qualityPolicyVersion": candidate.quality_policy_version,
                            "recommendedResearchFocus": list(candidate.recommended_research_focus),
                        }
                        base_key = (
                            f"gas:v1:{digest({'genome': genome_key, 'target': target, 'intent': candidate_intent}, 40)}"
                        )
                        key, existing = select_workflow_attempt(state["workflows"], base_key)
                        result["resolvedTarget"] = compact_workflow({"target": target})["target"]
                        result["idempotencyKey"] = key
                        task_id = existing.get("taskId") if isinstance(existing, dict) else None
                        start_payload: dict[str, Any] | None = None
                        if not task_id:
                            candidate_research_focus = unique_identifiers(
                                [*args.research_focus, *candidate.recommended_research_focus]
                            )[:20]
                            start_arguments: dict[str, Any] = {
                                **route,
                                "identifier": str(stable_identifier),
                                "geneSymbol": target.get("geneSymbol") or None,
                                "researchFocus": candidate_research_focus,
                                "specificAspects": args.specific_aspect,
                                "userPrompt": args.user_prompt,
                                "language": args.language,
                                "maxResult": args.max_result,
                                "forceRefresh": args.force_refresh,
                                "repeatPolicy":
                                    "skip-covered"
                                    if args.daily_count and not args.include_researched
                                    else "allow",
                                "idempotencyKey": key,
                                "correlationId": f"gas:{digest({'key': key}, 32)}",
                            }
                            if candidate.chromosome or target.get("chromosome"):
                                start_arguments["chromosome"] = candidate.chromosome or target.get("chromosome")
                            if args.organism:
                                start_arguments["organism"] = args.organism
                            if args.research_refresh_days is not None:
                                start_arguments["researchRefreshDays"] = args.research_refresh_days
                            start_payload = client.call_tool("start_annotation_research", start_arguments)
                            workflow = (
                                start_payload.get("workflow") if isinstance(start_payload, dict) else None
                            )
                            if not isinstance(workflow, dict) or not workflow.get("taskId"):
                                raise RuntimeError("CodeXomics did not return a DGR task ID")
                            task_id = str(workflow["taskId"])
                            state["workflows"][key] = {
                                "taskId": task_id,
                                "selectionMode": summary["selector"],
                                "requestedIdentifier": candidate.identifier,
                                "resolvedIdentity": resolved_identity,
                                "status": workflow.get("status"),
                                "createdAt": utc_now(),
                            }
                            save_state(state_path, state)
                        result["taskId"] = task_id
                        if isinstance(start_payload, dict) and start_payload.get("skipped") is True:
                            compact = compact_workflow(workflow)
                            compact["status"] = "skipped"
                            result.update(compact)
                            result["researchDisposition"] = start_payload.get("researchDisposition")
                            result["researchCoverage"] = start_payload.get("coverage")
                            result["curationOutcome"] = "research_already_covered"
                        else:
                            workflow = poll_workflow(
                                client,
                                str(task_id),
                                route,
                                args.workflow_timeout,
                                args.poll_interval,
                                args.max_poll_interval,
                            )
                            compact = compact_workflow(workflow)
                            result.update(compact)
                            if normalized(compact.get("status")) == "completed":
                                if compact.get("changeSetId"):
                                    result["curationOutcome"] = "changeset_created"
                                else:
                                    result["curationOutcome"] = "no_changeset"
                                    result["curationIssue"] = (
                                        compact.get("proposalReason")
                                        or "Research completed without a reviewable annotation ChangeSet"
                                    )
                        result["finishedAt"] = utc_now()
                        record = state["workflows"].setdefault(key, {})
                        record.update(
                            {
                                "taskId": task_id,
                                "selectionMode": summary["selector"],
                                "requestedIdentifier": candidate.identifier,
                                "resolvedIdentity": resolved_identity,
                                "status": compact.get("status"),
                                "changeSetId": compact.get("changeSetId"),
                                "changeSetStatus": compact.get("changeSetStatus"),
                                "reportAttachment": compact.get("reportAttachment"),
                                "finishedAt": utc_now(),
                            }
                        )
                        for stale_field in ("error", "errorType", "retryable", "failureCount"):
                            record.pop(stale_field, None)
                        save_state(state_path, state)
                    except (McpError, RuntimeError, TimeoutError, ValueError) as exc:
                        result.update({"status": "failed", "error": str(exc), "finishedAt": utc_now()})
                        persist_failed_workflow(
                            state_path,
                            state,
                            key,
                            task_id=task_id,
                            selection_mode=summary["selector"],
                            requested_identifier=candidate.identifier,
                            resolved_identity=resolved_identity,
                            error=exc,
                        )
                    summary["results"].append(result)

            summary["stateFile"] = str(state_path)
        summary["finishedAt"] = utc_now()
        summary["counts"] = {
            "selected": summary.get("selection", {}).get("selected", 0),
            "completed": sum(
                1 for item in summary["results"] if normalized(item.get("status")) == "completed"
            ),
            "failed": sum(1 for item in summary["results"] if normalized(item.get("status")) == "failed"),
            "skippedPreviouslyResearched": sum(
                1 for item in summary["results"] if item.get("curationOutcome") == "research_already_covered"
            ),
            "changeSetsCreated": sum(1 for item in summary["results"] if item.get("changeSetId")),
            "completedWithoutChangeSet": sum(
                1 for item in summary["results"] if item.get("curationOutcome") == "no_changeset"
            ),
            "dryRunEligible": sum(
                1 for item in summary.get("candidates", []) if item.get("eligible") is True
            ),
            "dryRunIneligible": sum(
                1 for item in summary.get("candidates", []) if item.get("eligible") is False
            ),
        }
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        if args.output:
            atomic_json_write(args.output.expanduser().resolve(), summary)
        if args.dry_run:
            return 1 if summary["counts"]["dryRunIneligible"] else 0
        unsuccessful = sum(
            1 for item in summary["results"] if normalized(item.get("status")) != "completed"
        )
        return 1 if unsuccessful or summary["counts"]["completedWithoutChangeSet"] else 0
    except (McpError, RuntimeError, OSError, ValueError) as exc:
        summary.update({"finishedAt": utc_now(), "fatalError": str(exc)})
        print(json.dumps(summary, indent=2, ensure_ascii=False), file=sys.stderr)
        if args.output:
            with contextlib.suppress(OSError):
                atomic_json_write(args.output.expanduser().resolve(), summary)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
