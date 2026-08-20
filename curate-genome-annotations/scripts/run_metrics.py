#!/usr/bin/env python3
"""Token, cost, runtime, and evidence accounting for annotation curation runs.

Two different models bill for one annotated gene:

* the **research** model that Deep Gene Research (DGR) runs internally for query
  planning, per-batch literature learning, and report synthesis;
* the **agent** model that drives this skill (an external MCP agent such as
  Codex or Claude, or the CodeXomics ChatBox).

Provider-reported usage for the two is collected separately and never summed
into a single undifferentiated number, because they are usually different
models at different prices. Every number in this module is either a value a
service actually reported or an explicit "unavailable" record with a reason.
Nothing is estimated from character counts.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


PRICING_SCHEMA = "genome-annotation-skills.pricing.v1"
AGENT_USAGE_SCHEMA = "genome-annotation-skills.agent-usage.v1"
METRICS_SCHEMA = "genome-annotation-skills.run-metrics.v1"

ROLE_RESEARCH = "research"
ROLE_AGENT = "agent"
ROLES = (ROLE_RESEARCH, ROLE_AGENT)

# DGR selects one of two configured models per phase. `deep-research/index.ts`
# uses getThinkingModel() for planning/synthesis phases and getTaskModel() for
# per-source extraction phases. Newer DGR builds report the model id with the
# usage record; this map is the fallback for builds that do not.
DGR_PHASE_MODEL_SLOTS = {
    "report-plan": "thinking",
    "serp-query": "thinking",
    "final-report": "thinking",
    "gene-llm-queries": "thinking",
    "gene-llm-report": "thinking",
    "search-task": "task",
    "gene-llm-learnings": "task",
}

UNKNOWN_MODEL_PREFIX = "unknown:"

# A reference is "new" only when it carries a stable publication identifier.
# Qualifier values arrive as prefixed tokens ("PMID:12345678"), bare DOIs, or
# resolver URLs, so all three forms are recognised and normalised to the same
# identifier before two annotations are compared.
REFERENCE_PATTERNS = (
    re.compile(r"\b(?:PMID|PMCID|PMC|DOI)\s*[:\s]\s*(\S+)", re.IGNORECASE),
    re.compile(r"\b(10\.\d{4,9}/\S+)"),
    re.compile(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)", re.IGNORECASE),
    re.compile(r"(?:europepmc\.org/article/\w+/|ncbi\.nlm\.nih\.gov/pmc/articles/)(\w+)", re.IGNORECASE),
)
REFERENCE_PREFIX = re.compile(r"^(?:pmid|pmcid|pmc|doi)\s*[:\s]\s*", re.IGNORECASE)
# Dict keys whose value is already a bare identifier.
REFERENCE_ID_KEYS = ("pmid", "pmcid", "doi")
# Dict keys whose value must still be scanned for an identifier.
REFERENCE_TEXT_KEYS = ("id", "identifier", "url", "citation", "reference")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _non_negative_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def duration_seconds(started_at: Any, finished_at: Any) -> float | None:
    """Wall-clock seconds between two ISO-8601 stamps, or None if unusable."""
    start = parse_timestamp(started_at)
    end = parse_timestamp(finished_at)
    if start is None or end is None:
        return None
    elapsed = (end - start).total_seconds()
    return round(elapsed, 3) if elapsed >= 0 else None


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelPrice:
    """Per-million-token list price for one model id."""

    input_per_million: float
    output_per_million: float
    cached_input_per_million: float | None = None
    currency: str = "USD"
    note: str | None = None

    @classmethod
    def parse(cls, model: str, payload: Any, currency: str) -> "ModelPrice":
        if not isinstance(payload, dict):
            raise ValueError(f"Pricing entry for {model!r} must be an object")
        try:
            input_price = float(payload["inputPerMillion"])
            output_price = float(payload["outputPerMillion"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"Pricing entry for {model!r} needs numeric inputPerMillion and outputPerMillion"
            ) from exc
        cached_raw = payload.get("cachedInputPerMillion")
        cached = None
        if cached_raw is not None:
            try:
                cached = float(cached_raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Pricing entry for {model!r} has a non-numeric cachedInputPerMillion") from exc
        for label, value in (("inputPerMillion", input_price), ("outputPerMillion", output_price)):
            if value < 0:
                raise ValueError(f"Pricing entry for {model!r} has a negative {label}")
        if cached is not None and cached < 0:
            raise ValueError(f"Pricing entry for {model!r} has a negative cachedInputPerMillion")
        return cls(
            input_per_million=input_price,
            output_per_million=output_price,
            cached_input_per_million=cached,
            currency=str(payload.get("currency") or currency),
            note=str(payload["note"]) if payload.get("note") else None,
        )


@dataclass(frozen=True)
class PricingBook:
    """Model list prices supplied by the operator.

    Prices are never guessed. A model without an entry is reported as unpriced
    so a run report shows token counts with an explicit cost gap instead of a
    fabricated number.
    """

    models: dict[str, ModelPrice] = field(default_factory=dict)
    currency: str = "USD"
    source: str | None = None
    effectiveDate: str | None = None

    @classmethod
    def empty(cls) -> "PricingBook":
        return cls()

    @classmethod
    def load(cls, path: Path) -> "PricingBook":
        resolved = path.expanduser().resolve()
        try:
            payload = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Cannot read pricing file {resolved}: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"Pricing file {resolved} must contain a JSON object")
        schema = str(payload.get("schema") or PRICING_SCHEMA)
        if schema != PRICING_SCHEMA:
            raise ValueError(f"Pricing file {resolved} has unsupported schema {schema!r}")
        currency = str(payload.get("currency") or "USD")
        raw_models = payload.get("models")
        if not isinstance(raw_models, dict) or not raw_models:
            raise ValueError(f"Pricing file {resolved} has no models map")
        models: dict[str, ModelPrice] = {}
        for model, entry in raw_models.items():
            key = normalize_model(model)
            if not key:
                continue
            models[key] = ModelPrice.parse(model, entry, currency)
        if not models:
            raise ValueError(f"Pricing file {resolved} has no usable model entries")
        return cls(
            models=models,
            currency=currency,
            source=str(resolved),
            effectiveDate=str(payload["effectiveDate"]) if payload.get("effectiveDate") else None,
        )

    @classmethod
    def resolve(cls, explicit: Path | None) -> "PricingBook":
        """Load the operator's pricing file from --pricing-file or the environment."""
        if explicit is not None:
            return cls.load(explicit)
        from_env = os.environ.get("GENOME_ANNOTATION_PRICING_FILE", "").strip()
        if from_env:
            return cls.load(Path(from_env))
        return cls.empty()

    def quote(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cached_prompt_tokens: int = 0,
    ) -> tuple[float | None, str | None]:
        """Return (cost, unavailable_reason). Exactly one of the two is set."""
        key = normalize_model(model)
        if not self.models:
            return None, "no pricing file configured"
        price = self.models.get(key)
        if price is None:
            return None, f"no pricing entry for model {model!r}"
        billed_cached = min(max(cached_prompt_tokens, 0), max(prompt_tokens, 0))
        billed_fresh = max(prompt_tokens, 0) - billed_cached
        cached_rate = (
            price.cached_input_per_million
            if price.cached_input_per_million is not None
            else price.input_per_million
        )
        cost = (
            billed_fresh * price.input_per_million
            + billed_cached * cached_rate
            + max(completion_tokens, 0) * price.output_per_million
        ) / 1_000_000
        return round(cost, 6), None

    def to_dict(self) -> dict[str, Any]:
        return {
            "configured": bool(self.models),
            "source": self.source,
            "currency": self.currency,
            "effectiveDate": self.effectiveDate,
            "models": sorted(self.models),
        }


def normalize_model(value: Any) -> str:
    return str(value or "").strip().lower()


# ---------------------------------------------------------------------------
# Usage ledger
# ---------------------------------------------------------------------------


@dataclass
class UsageEntry:
    role: str
    model: str
    phase: str
    calls: int = 0
    prompt_tokens: int = 0
    cached_prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    billable: bool = True
    gene: str | None = None


class UsageLedger:
    """Accumulates provider-reported token usage split by role, model, and phase.

    `billable=False` records tokens that a service reported but did not charge
    for on this run — a DGR semantic-cache replay returns the original run's
    usage verbatim, so counting it as spend would inflate the daily cost.
    """

    def __init__(self, pricing: PricingBook | None = None) -> None:
        self.pricing = pricing or PricingBook.empty()
        self.entries: list[UsageEntry] = []

    def add(self, entry: UsageEntry) -> None:
        if entry.role not in ROLES:
            raise ValueError(f"Unknown usage role {entry.role!r}")
        if entry.total_tokens <= 0:
            entry.total_tokens = entry.prompt_tokens + entry.completion_tokens
        self.entries.append(entry)

    def extend(self, entries: Iterable[UsageEntry]) -> None:
        for entry in entries:
            self.add(entry)

    def filtered(self, *, gene: str | None = None, role: str | None = None) -> "UsageLedger":
        clone = UsageLedger(self.pricing)
        clone.entries = [
            entry
            for entry in self.entries
            if (gene is None or entry.gene == gene) and (role is None or entry.role == role)
        ]
        return clone

    @property
    def empty(self) -> bool:
        return not self.entries

    def _bucket(self) -> dict[str, Any]:
        return {
            "calls": 0,
            "promptTokens": 0,
            "cachedPromptTokens": 0,
            "completionTokens": 0,
            "totalTokens": 0,
            "billableTotalTokens": 0,
            "replayedTotalTokens": 0,
        }

    @staticmethod
    def _accumulate(bucket: dict[str, Any], entry: UsageEntry) -> None:
        bucket["calls"] += entry.calls
        bucket["promptTokens"] += entry.prompt_tokens
        bucket["cachedPromptTokens"] += entry.cached_prompt_tokens
        bucket["completionTokens"] += entry.completion_tokens
        bucket["totalTokens"] += entry.total_tokens
        if entry.billable:
            bucket["billableTotalTokens"] += entry.total_tokens
        else:
            bucket["replayedTotalTokens"] += entry.total_tokens

    def to_dict(self) -> dict[str, Any]:
        by_model: dict[str, dict[str, Any]] = {}
        by_role: dict[str, dict[str, Any]] = {}
        by_phase: dict[str, dict[str, Any]] = {}
        totals = self._bucket()
        unpriced: dict[str, str] = {}
        cost_by_model: dict[str, dict[str, Any]] = {}

        for entry in self.entries:
            model_key = f"{entry.role}:{entry.model}"
            model_bucket = by_model.setdefault(
                model_key, {**self._bucket(), "role": entry.role, "model": entry.model}
            )
            self._accumulate(model_bucket, entry)
            self._accumulate(by_role.setdefault(entry.role, self._bucket()), entry)
            self._accumulate(by_phase.setdefault(f"{entry.role}:{entry.phase}", self._bucket()), entry)
            self._accumulate(totals, entry)

            if not entry.billable:
                continue
            cost, reason = self.pricing.quote(
                entry.model, entry.prompt_tokens, entry.completion_tokens, entry.cached_prompt_tokens
            )
            bucket = cost_by_model.setdefault(
                model_key,
                {"role": entry.role, "model": entry.model, "cost": 0.0, "priced": True, "reason": None},
            )
            if cost is None:
                bucket["priced"] = False
                bucket["reason"] = reason
                unpriced[entry.model] = reason or "unpriced"
            else:
                bucket["cost"] = round(bucket["cost"] + cost, 6)

        total_cost = 0.0
        cost_complete = bool(self.entries) and bool(self.pricing.models)
        for bucket in cost_by_model.values():
            if bucket["priced"]:
                total_cost = round(total_cost + bucket["cost"], 6)
            else:
                cost_complete = False
                bucket["cost"] = None

        return {
            "totals": totals,
            "byRole": by_role,
            "byModel": by_model,
            "byPhase": by_phase,
            "cost": {
                "currency": self.pricing.currency,
                "complete": cost_complete,
                "total": round(total_cost, 6) if cost_complete else None,
                "billedTotal": round(total_cost, 6),
                "byModel": cost_by_model,
                "unpricedModels": sorted(unpriced),
                "unavailableReasons": sorted(set(unpriced.values())),
                "pricing": self.pricing.to_dict(),
            },
        }


# ---------------------------------------------------------------------------
# DGR usage normalisation
# ---------------------------------------------------------------------------


def normalize_dgr_llm_usage(
    payload: Any,
    *,
    thinking_model: str | None,
    task_model: str | None,
    gene: str | None = None,
    billable: bool = True,
) -> tuple[list[UsageEntry], list[str]]:
    """Convert a DGR `metadata.llmUsage` payload into ledger entries.

    Two payload shapes are supported:

    * newer builds report `models: {<modelId>: {phases: {...}}}` — the model id
      is authoritative and used as-is;
    * older builds report only `phases: {<phase>: {...}}` — the phase is mapped
      to DGR's thinking/task model slot and the configured model id is applied.

    Returns the entries plus warnings describing anything that could not be
    attributed to a named model.
    """
    warnings: list[str] = []
    if not isinstance(payload, dict):
        return [], ["DGR reported no llmUsage payload"]

    entries: list[UsageEntry] = []
    models = payload.get("models")
    if isinstance(models, dict) and models:
        for model_id, model_usage in models.items():
            if not isinstance(model_usage, dict):
                continue
            phases = model_usage.get("phases")
            phase_map = phases if isinstance(phases, dict) and phases else {"aggregate": model_usage}
            for phase, usage in phase_map.items():
                if not isinstance(usage, dict):
                    continue
                entries.append(
                    _usage_entry(ROLE_RESEARCH, str(model_id), str(phase), usage, gene, billable)
                )
        if entries:
            return entries, warnings

    phases = payload.get("phases")
    if not isinstance(phases, dict) or not phases:
        return [], ["DGR llmUsage contains no per-phase records"]

    thinking = normalize_model(thinking_model)
    task = normalize_model(task_model)
    for phase, usage in phases.items():
        if not isinstance(usage, dict):
            continue
        slot = DGR_PHASE_MODEL_SLOTS.get(str(phase))
        if slot == "thinking" and thinking:
            model_id = thinking
        elif slot == "task" and task:
            model_id = task
        else:
            model_id = f"{UNKNOWN_MODEL_PREFIX}dgr-{slot or 'phase'}"
            warnings.append(
                f"DGR phase {phase!r} has no model attribution; set --dgr-thinking-model/--dgr-task-model "
                "or upgrade DGR to a build that reports model ids with usage"
            )
        entries.append(_usage_entry(ROLE_RESEARCH, model_id, str(phase), usage, gene, billable))
    return entries, sorted(set(warnings))


def _usage_entry(
    role: str, model: str, phase: str, usage: dict[str, Any], gene: str | None, billable: bool
) -> UsageEntry:
    prompt = _non_negative_int(usage.get("promptTokens") or usage.get("inputTokens"))
    completion = _non_negative_int(usage.get("completionTokens") or usage.get("outputTokens"))
    return UsageEntry(
        role=role,
        model=normalize_model(model),
        phase=phase,
        calls=_non_negative_int(usage.get("calls")) or 1,
        prompt_tokens=prompt,
        cached_prompt_tokens=_non_negative_int(
            usage.get("cachedPromptTokens") or usage.get("promptCacheHitTokens")
        ),
        completion_tokens=completion,
        total_tokens=_non_negative_int(usage.get("totalTokens")) or (prompt + completion),
        billable=billable,
        gene=gene,
    )


# ---------------------------------------------------------------------------
# Agent-side usage sidecar
# ---------------------------------------------------------------------------


def append_agent_usage(path: Path, record: dict[str, Any]) -> None:
    """Append one agent-side usage record as JSON Lines."""
    resolved = path.expanduser()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema": AGENT_USAGE_SCHEMA, "timestamp": utc_now(), **record}
    with resolved.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        handle.write("\n")


def read_agent_usage(
    path: Path, *, run_id: str | None = None, since: str | None = None
) -> tuple[list[UsageEntry], list[str]]:
    """Read the agent-side JSON Lines usage sidecar.

    The orchestrating agent is the only party that knows its own token spend;
    nothing in CodeXomics or DGR can observe it. The agent appends one record
    per LLM turn and this reads them back, optionally narrowed to one run id or
    to records at/after an ISO-8601 instant.
    """
    resolved = path.expanduser()
    if not resolved.is_file():
        return [], [f"agent usage file {resolved} does not exist; agent-side tokens are unavailable"]
    warnings: list[str] = []
    entries: list[UsageEntry] = []
    since_stamp = parse_timestamp(since) if since else None
    for line_number, line in enumerate(resolved.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            warnings.append(f"{resolved}:{line_number} is not valid JSON and was skipped")
            continue
        if not isinstance(record, dict):
            warnings.append(f"{resolved}:{line_number} is not a JSON object and was skipped")
            continue
        if run_id and str(record.get("runId") or "") != run_id:
            continue
        if since_stamp is not None:
            stamp = parse_timestamp(record.get("timestamp"))
            if stamp is not None and stamp < since_stamp:
                continue
        model = normalize_model(record.get("model"))
        if not model:
            warnings.append(f"{resolved}:{line_number} has no model id and was skipped")
            continue
        entries.append(
            _usage_entry(
                ROLE_AGENT,
                model,
                str(record.get("phase") or "orchestration"),
                record,
                str(record.get("gene")) if record.get("gene") else None,
                record.get("billable") is not False,
            )
        )
    if not entries and not warnings:
        warnings.append(f"agent usage file {resolved} contained no matching records")
    return entries, warnings


# ---------------------------------------------------------------------------
# Literature and annotation-delta statistics
# ---------------------------------------------------------------------------


def literature_statistics(
    literature_coverage: Any, attachment_summary: Any, dgr_metadata: Any = None
) -> dict[str, Any]:
    """Reference counts for one gene, taken only from reported fields."""
    coverage = literature_coverage if isinstance(literature_coverage, dict) else {}
    summary = attachment_summary if isinstance(attachment_summary, dict) else {}
    metadata = dgr_metadata if isinstance(dgr_metadata, dict) else {}
    metrics = summary.get("literatureMetrics")
    if not isinstance(metrics, dict):
        metrics = metadata.get("literatureMetrics") if isinstance(metadata.get("literatureMetrics"), dict) else {}

    surveyed = coverage.get("pubmedTotalMatchCount")
    retained = coverage.get("retainedAbstractCount")
    if retained is None:
        retained = metrics.get("totalPapers")
    if retained is None:
        retained = summary.get("literatureCount")

    return {
        # Everything PubMed matched for this gene's queries, before budgeting.
        "surveyedRecords": _optional_int(surveyed),
        # Abstracts DGR actually retained and fed to synthesis.
        "retainedReferences": _optional_int(retained),
        "directReferences": _optional_int(summary.get("directLiteratureCount") or metrics.get("directPapers")),
        "geneLinkedReferences": _optional_int(
            summary.get("geneLinkedContextCount") or metrics.get("geneLinkedPapers")
        ),
        "preprintReferences": _optional_int(summary.get("preprintCount") or metrics.get("preprintPapers")),
        "userDocumentReferences": _optional_int(
            summary.get("userDocumentCount") or metrics.get("userDocumentPapers")
        ),
        # Full texts DGR parsed and bound as verifiable evidence spans.
        "fullTextSources": _optional_int(summary.get("fullTextSourceCount")),
        "fullTextFindings": _optional_int(summary.get("fullTextFindingCount")),
        "citationBoundFacts": _optional_int(summary.get("citationBoundFactCount")),
        "literatureBudget": _optional_int(coverage.get("literatureBudget")),
        "bibliographyComplete": coverage.get("linkedBibliographyComplete"),
    }


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


REFERENCE_FIELDS = ("db_xref", "codexomics_research_evidence")


def _clean_reference_token(value: Any) -> str:
    token = REFERENCE_PREFIX.sub("", str(value or "").strip()).strip().strip(".,;)]}\"'")
    return token.lower()


def extract_reference_ids(value: Any) -> set[str]:
    """Pull stable publication identifiers out of a qualifier or evidence value."""
    found: set[str] = set()
    if value is None or isinstance(value, bool):
        return found
    if isinstance(value, (list, tuple, set)):
        for item in value:
            found |= extract_reference_ids(item)
        return found
    if isinstance(value, dict):
        for key in REFERENCE_ID_KEYS:
            raw = value.get(key)
            if raw in (None, "", [], {}):
                continue
            for item in raw if isinstance(raw, (list, tuple, set)) else [raw]:
                token = _clean_reference_token(item)
                if token:
                    found.add(token)
        for key in REFERENCE_TEXT_KEYS:
            if value.get(key):
                found |= extract_reference_ids(value[key])
        return found
    text = str(value).strip()
    if not text:
        return found
    for pattern in REFERENCE_PATTERNS:
        for match in pattern.finditer(text):
            token = _clean_reference_token(match.group(1))
            if token:
                found.add(token)
    return found


def changeset_delta(changeset: Any, preview: Any = None) -> dict[str, Any]:
    """What a proposed ChangeSet would newly add to the current annotation.

    `changeset` is the full record from `get_annotation_changeset`; `preview`
    is the per-field before/after list from `list_annotation_changesets`, used
    to subtract references the annotation already carried.
    """
    record = changeset if isinstance(changeset, dict) else {}
    operations = record.get("operations")
    operations = operations if isinstance(operations, list) else []
    preview_items = preview if isinstance(preview, list) else []

    existing_references: set[str] = set()
    before_by_field: dict[str, Any] = {}
    for item in preview_items:
        if not isinstance(item, dict):
            continue
        field_name = str(item.get("field") or "")
        if field_name:
            before_by_field[field_name] = item.get("before")
        existing_references |= extract_reference_ids(item.get("before"))

    proposed_references: set[str] = set()
    updated_fields: list[dict[str, Any]] = []
    seen_fields: set[str] = set()
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        op = str(operation.get("op") or "")
        field_name = str(
            operation.get("field")
            or ("db_xref" if op == "addDbxref" else "")
            or ("codexomics_research_evidence" if op == "addEvidenceLink" else "")
        )
        value = operation.get("value")
        if field_name in REFERENCE_FIELDS or op in ("addDbxref", "addEvidenceLink"):
            proposed_references |= extract_reference_ids(value)
        if field_name and field_name not in seen_fields:
            seen_fields.add(field_name)
            updated_fields.append(
                {
                    "field": field_name,
                    "op": op,
                    "hadPreviousValue": bool(before_by_field.get(field_name)),
                }
            )

    evidence = record.get("evidence")
    evidence_records = evidence if isinstance(evidence, list) else []
    for item in evidence_records:
        proposed_references |= extract_reference_ids(item)

    new_references = sorted(proposed_references - existing_references)
    return {
        "changeSetId": record.get("id"),
        "status": record.get("status"),
        "operationCount": len(operations),
        "updatedFields": updated_fields,
        "updatedFieldCount": len(updated_fields),
        "evidenceCount": len(evidence_records),
        "proposedReferenceCount": len(proposed_references),
        "existingReferenceCount": len(existing_references),
        "newReferenceCount": len(new_references),
        "newReferences": new_references[:200],
        "newReferencesTruncated": len(new_references) > 200,
    }


def note_delta(annotation_note: Any) -> dict[str, Any]:
    """Newly incorporated information carried by the Genome Annotation Note."""
    note = annotation_note if isinstance(annotation_note, dict) else {}
    segments = note.get("segments")
    segments = segments if isinstance(segments, list) else []
    coverage = note.get("coverage") if isinstance(note.get("coverage"), dict) else {}
    cited = sum(1 for segment in segments if isinstance(segment, dict) and segment.get("citations"))
    return {
        "mutationReady": bool(note.get("text")),
        "textLength": len(str(note.get("text") or "")),
        "segmentCount": len(segments),
        "citedSegmentCount": cited,
        "includedFactCount": _optional_int(coverage.get("includedFactCount")),
        "availableFactCount": _optional_int(coverage.get("availableFactCount")),
    }


__all__ = [
    "AGENT_USAGE_SCHEMA",
    "DGR_PHASE_MODEL_SLOTS",
    "METRICS_SCHEMA",
    "PRICING_SCHEMA",
    "ROLE_AGENT",
    "ROLE_RESEARCH",
    "ModelPrice",
    "PricingBook",
    "UsageEntry",
    "UsageLedger",
    "append_agent_usage",
    "changeset_delta",
    "duration_seconds",
    "extract_reference_ids",
    "literature_statistics",
    "normalize_dgr_llm_usage",
    "normalize_model",
    "note_delta",
    "parse_timestamp",
    "read_agent_usage",
    "utc_now",
]
