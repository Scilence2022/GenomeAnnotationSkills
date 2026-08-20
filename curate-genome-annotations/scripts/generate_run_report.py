#!/usr/bin/env python3
"""Render a comprehensive run report from one or more annotation run summaries.

Input is whatever `run_annotation_workflow.py` wrote with `--output` (a run
summary containing a `metrics` block) or `--metrics-output` (the metrics block
on its own). Several files can be combined into one report, which is what a
daily batch split across retries produces.

Every statistic is either a reported value or an explicit "unavailable" cell.
The report never estimates tokens, cost, or runtime from anything other than
what CodeXomics, DGR, and the agent usage sidecar actually reported.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from run_metrics import METRICS_SCHEMA, ROLE_AGENT, ROLE_RESEARCH, utc_now


ROLE_LABELS = {
    ROLE_RESEARCH: "Deep Gene Research (research model)",
    ROLE_AGENT: "Orchestrating agent (agent model)",
}
UNAVAILABLE = "unavailable"


def load_documents(paths: list[Path]) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for path in paths:
        resolved = path.expanduser().resolve()
        try:
            payload = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Cannot read run summary {resolved}: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"Run summary {resolved} must contain a JSON object")
        metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else None
        if metrics is None and payload.get("schema") == METRICS_SCHEMA and "runtime" in payload:
            metrics = payload
            payload = {"runId": payload.get("runId"), "genomePath": payload.get("genome")}
        if metrics is None:
            raise ValueError(
                f"{resolved} has no metrics block. Re-run the workflow with a build that writes "
                "metrics, or pass the file produced by --metrics-output."
            )
        documents.append({"path": str(resolved), "summary": payload, "metrics": metrics})
    if not documents:
        raise ValueError("No run summaries were supplied")
    return documents


def _bucket() -> dict[str, int]:
    return {
        "calls": 0,
        "promptTokens": 0,
        "cachedPromptTokens": 0,
        "completionTokens": 0,
        "totalTokens": 0,
        "billableTotalTokens": 0,
        "replayedTotalTokens": 0,
    }


def _merge_bucket(target: dict[str, int], source: Any) -> None:
    if not isinstance(source, dict):
        return
    for key in target:
        value = source.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            target[key] += int(value)


def _merge_reported(target: dict[str, Any], source: Any) -> None:
    if not isinstance(source, dict):
        return
    for key in ("total", "reportedFor", "unreportedFor"):
        value = source.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            target[key] = target.get(key, 0) + value


def combine(documents: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge several run-metrics blocks into one batch view."""
    per_gene: list[dict[str, Any]] = []
    warnings: list[str] = []
    by_model: dict[str, dict[str, Any]] = {}
    by_role: dict[str, dict[str, int]] = {}
    cost_by_model: dict[str, dict[str, Any]] = {}
    totals = _bucket()
    references: dict[str, dict[str, Any]] = {}
    new_information: dict[str, Any] = {"changeSetsCreated": 0, "mutationReadyNotes": 0, "fieldsUpdated": {}}
    updated_fields: dict[str, Any] = {}
    note_facts: dict[str, Any] = {}
    runtime: dict[str, Any] = {"genesAttempted": 0, "genesCompleted": 0, "runSeconds": 0.0}
    wall_clock: dict[str, Any] = {}
    research_time: dict[str, Any] = {}
    currency = "USD"
    pricing_sources: set[str] = set()
    cost_complete = True
    unpriced: set[str] = set()

    for document in documents:
        metrics = document["metrics"]
        per_gene.extend(item for item in metrics.get("perGene", []) if isinstance(item, dict))
        warnings.extend(str(item) for item in metrics.get("warnings", []))

        block = metrics.get("runtime") if isinstance(metrics.get("runtime"), dict) else {}
        runtime["genesAttempted"] += int(block.get("genesAttempted") or 0)
        runtime["genesCompleted"] += int(block.get("genesCompleted") or 0)
        if isinstance(block.get("runSeconds"), (int, float)):
            runtime["runSeconds"] += float(block["runSeconds"])
        _merge_reported(wall_clock, block.get("perGeneWallClockSeconds"))
        _merge_reported(research_time, block.get("dgrResearchSeconds"))

        tokens = metrics.get("tokens") if isinstance(metrics.get("tokens"), dict) else {}
        _merge_bucket(totals, tokens.get("totals"))
        for key, value in (tokens.get("byModel") or {}).items():
            entry = by_model.setdefault(
                key, {**_bucket(), "role": value.get("role"), "model": value.get("model")}
            )
            _merge_bucket(entry, value)
        for key, value in (tokens.get("byRole") or {}).items():
            _merge_bucket(by_role.setdefault(key, _bucket()), value)

        cost = tokens.get("cost") if isinstance(tokens.get("cost"), dict) else {}
        currency = str(cost.get("currency") or currency)
        if cost.get("complete") is False:
            cost_complete = False
        unpriced.update(str(item) for item in cost.get("unpricedModels") or [])
        pricing = cost.get("pricing") if isinstance(cost.get("pricing"), dict) else {}
        if pricing.get("source"):
            pricing_sources.add(str(pricing["source"]))
        for key, value in (cost.get("byModel") or {}).items():
            entry = cost_by_model.setdefault(
                key,
                {"role": value.get("role"), "model": value.get("model"), "cost": 0.0, "priced": True, "reason": None},
            )
            if value.get("priced") is False or value.get("cost") is None:
                entry["priced"] = False
                entry["reason"] = value.get("reason")
            elif entry["priced"]:
                entry["cost"] = round(entry["cost"] + float(value["cost"]), 6)

        for key, value in (metrics.get("references") or {}).items():
            _merge_reported(references.setdefault(key, {}), value)

        info = metrics.get("newInformation") if isinstance(metrics.get("newInformation"), dict) else {}
        new_information["changeSetsCreated"] += int(info.get("changeSetsCreated") or 0)
        new_information["mutationReadyNotes"] += int(info.get("mutationReadyNotes") or 0)
        for field_name, count in (info.get("fieldsUpdated") or {}).items():
            new_information["fieldsUpdated"][field_name] = (
                new_information["fieldsUpdated"].get(field_name, 0) + int(count or 0)
            )
        _merge_reported(updated_fields, info.get("updatedFields"))
        _merge_reported(note_facts, info.get("noteIncludedFacts"))

    total_cost = 0.0
    for entry in cost_by_model.values():
        if entry["priced"]:
            total_cost = round(total_cost + entry["cost"], 6)
        else:
            entry["cost"] = None
            cost_complete = False

    durations = sorted(
        float(item["durationSeconds"])
        for item in per_gene
        if isinstance(item.get("durationSeconds"), (int, float))
    )
    runtime.update(
        {
            "runSeconds": round(runtime["runSeconds"], 3),
            "perGeneWallClockSeconds": wall_clock,
            "dgrResearchSeconds": research_time,
            "medianGeneSeconds": durations[len(durations) // 2] if durations else None,
            "fastestGeneSeconds": durations[0] if durations else None,
            "slowestGeneSeconds": durations[-1] if durations else None,
            "meanGeneSeconds": round(sum(durations) / len(durations), 3) if durations else None,
        }
    )
    new_information["updatedFields"] = updated_fields
    new_information["noteIncludedFacts"] = note_facts

    return {
        "schema": METRICS_SCHEMA,
        "generatedAt": utc_now(),
        "runIds": [document["metrics"].get("runId") or document["summary"].get("runId") for document in documents],
        "genomes": sorted(
            {str(document["summary"].get("genomePath") or document["metrics"].get("genome") or "") for document in documents}
            - {""}
        ),
        "sources": [document["path"] for document in documents],
        "runtime": runtime,
        "tokens": {
            "totals": totals,
            "byRole": by_role,
            "byModel": by_model,
            "cost": {
                "currency": currency,
                "complete": cost_complete,
                "total": round(total_cost, 6) if cost_complete else None,
                "billedTotal": round(total_cost, 6),
                "byModel": cost_by_model,
                "unpricedModels": sorted(unpriced),
                "pricingSources": sorted(pricing_sources),
            },
        },
        "references": references,
        "newInformation": new_information,
        "perGene": per_gene,
        "warnings": sorted(set(warnings)),
    }


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def cell(value: Any, suffix: str = "") -> str:
    if value is None or value == "":
        return UNAVAILABLE
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{f'{value:,.3f}'.rstrip('0').rstrip('.')}{suffix}"
    if isinstance(value, int):
        return f"{value:,}{suffix}"
    return f"{value}{suffix}"


def reported_cell(block: Any) -> str:
    if not isinstance(block, dict) or "total" not in block:
        return UNAVAILABLE
    text = cell(block.get("total"))
    unreported = block.get("unreportedFor") or 0
    if unreported:
        text += f" (not reported for {unreported} gene{'s' if unreported != 1 else ''})"
    return text


def table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "_No rows._\n"
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines) + "\n"


def render_markdown(combined: dict[str, Any], title: str) -> str:
    tokens = combined["tokens"]
    cost = tokens["cost"]
    runtime = combined["runtime"]
    references = combined["references"]
    information = combined["newInformation"]
    per_gene = combined["perGene"]

    out: list[str] = [f"# {title}", ""]
    out.append(f"- Generated: `{combined['generatedAt']}`")
    for genome in combined["genomes"]:
        out.append(f"- Genome: `{genome}`")
    out.append("- Run IDs: " + ", ".join(f"`{value}`" for value in combined["runIds"] if value))
    out.append(f"- Genes attempted: {runtime['genesAttempted']} · completed: {runtime['genesCompleted']}")
    out.append("- ChangeSets created: " + str(information["changeSetsCreated"]))
    out.append("")
    out.append(
        "> No ChangeSet was approved or applied by this run. Every proposal below is queued for human "
        "review in the CodeXomics Annotation Review Center."
    )
    out.append("")

    out.append("## 1. Token consumption by model")
    out.append("")
    if not tokens["byModel"]:
        out.append(
            "_No token usage was reported._ See the data gaps section — the run completed but no service "
            "returned provider usage."
        )
        out.append("")
    else:
        rows = []
        for key in sorted(tokens["byModel"]):
            entry = tokens["byModel"][key]
            cost_entry = cost["byModel"].get(key, {})
            cost_text = (
                f"{cost_entry['cost']:.4f} {cost['currency']}"
                if cost_entry.get("priced") and cost_entry.get("cost") is not None
                else f"{UNAVAILABLE} ({cost_entry.get('reason') or 'no price'})"
            )
            rows.append(
                [
                    ROLE_LABELS.get(str(entry.get("role")), str(entry.get("role"))),
                    f"`{entry.get('model')}`",
                    cell(entry["calls"]),
                    cell(entry["promptTokens"]),
                    cell(entry["cachedPromptTokens"]),
                    cell(entry["completionTokens"]),
                    cell(entry["totalTokens"]),
                    cell(entry["replayedTotalTokens"]),
                    cost_text,
                ]
            )
        out.append(
            table(
                [
                    "Role",
                    "Model",
                    "Calls",
                    "Input tokens",
                    "of which cached",
                    "Output tokens",
                    "Total tokens",
                    "Replayed (uncharged)",
                    "Cost",
                ],
                rows,
            )
        )
        out.append("")
        out.append(f"**Total tokens across all steps: {cell(tokens['totals']['totalTokens'])}**")
        if tokens["totals"]["replayedTotalTokens"]:
            out.append(
                f"Of these, {cell(tokens['totals']['replayedTotalTokens'])} were replayed from DGR's "
                "semantic cache and were not charged again."
            )
        if cost["complete"]:
            out.append(f"**Actual cost: {cost['total']:.4f} {cost['currency']}**")
        else:
            out.append(
                f"**Actual cost: partially unavailable.** Billed for priced models: "
                f"{cost['billedTotal']:.4f} {cost['currency']}. Unpriced models: "
                + (", ".join(f"`{model}`" for model in cost["unpricedModels"]) or "none")
                + "."
            )
        if cost["pricingSources"]:
            out.append("Price list: " + ", ".join(f"`{item}`" for item in cost["pricingSources"]))
        out.append("")

    out.append("## 2. Runtime")
    out.append("")
    out.append(f"- Total batch wall clock: {cell(runtime.get('runSeconds'), ' s')}")
    out.append(f"- Summed per-gene wall clock: {reported_cell(runtime.get('perGeneWallClockSeconds'))} s")
    out.append(f"- Summed DGR research time: {reported_cell(runtime.get('dgrResearchSeconds'))} s")
    out.append(f"- Mean / median per gene: {cell(runtime.get('meanGeneSeconds'))} s / {cell(runtime.get('medianGeneSeconds'))} s")
    out.append(f"- Fastest / slowest gene: {cell(runtime.get('fastestGeneSeconds'))} s / {cell(runtime.get('slowestGeneSeconds'))} s")
    out.append("")

    out.append("## 3. Literature coverage")
    out.append("")
    out.append(f"- References surveyed (PubMed matches for this batch's queries): {reported_cell(references.get('surveyed'))}")
    out.append(f"- References retained for synthesis: {reported_cell(references.get('retained'))}")
    out.append(f"- Full texts adopted as verifiable evidence: {reported_cell(references.get('fullTextsAdopted'))}")
    out.append(f"- Full-text evidence spans bound to facts: {reported_cell(references.get('fullTextFindings'))}")
    out.append(f"- **References newly added to annotations: {reported_cell(references.get('newlyAdded'))}**")
    out.append("")

    out.append("## 4. Newly incorporated information")
    out.append("")
    out.append(f"- ChangeSets created: {information['changeSetsCreated']}")
    out.append(f"- Qualifier fields updated: {reported_cell(information.get('updatedFields'))}")
    out.append(f"- Genes with a mutation-ready citation-bound Note: {information['mutationReadyNotes']}")
    out.append(f"- Citation-bound facts included in Notes: {reported_cell(information.get('noteIncludedFacts'))}")
    out.append("")
    if information["fieldsUpdated"]:
        out.append(
            table(
                ["Qualifier field", "Genes updated"],
                [[f"`{name}`", str(count)] for name, count in sorted(information["fieldsUpdated"].items())],
            )
        )
        out.append("")

    out.append("## 5. Per-gene detail")
    out.append("")
    rows = []
    for item in per_gene:
        rows.append(
            [
                f"`{item.get('gene')}`",
                cell(item.get("locusTag")),
                cell(item.get("status")),
                cell(item.get("durationSeconds"), " s"),
                cell(item.get("researchSeconds"), " s"),
                cell((item.get("tokens") or {}).get("totalTokens")),
                cell(item.get("referencesSurveyed")),
                cell(item.get("fullTextSources")),
                cell(item.get("newReferenceCount")),
                cell(item.get("updatedFieldCount")),
                cell(item.get("changeSetId")),
            ]
        )
    out.append(
        table(
            [
                "Gene",
                "Locus tag",
                "Status",
                "Wall clock",
                "DGR time",
                "Tokens",
                "Refs surveyed",
                "Full texts",
                "New refs",
                "Fields updated",
                "ChangeSet",
            ],
            rows,
        )
    )
    out.append("")

    out.append("## 6. Data gaps and warnings")
    out.append("")
    if combined["warnings"]:
        out.extend(f"- {warning}" for warning in combined["warnings"])
    else:
        out.append("- None. Every requested statistic was reported by a service.")
    out.append("")
    out.append("## 7. Review handoff")
    out.append("")
    out.append(
        "Open the CodeXomics **Annotation Review Center**, filter for the ChangeSets listed above, and "
        "compare current versus proposed qualifiers and citations before approving anything. The research "
        "principal that created these proposals cannot approve or apply them."
    )
    out.append("")
    return "\n".join(out)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render a comprehensive annotation run report from run summaries"
    )
    parser.add_argument(
        "summaries",
        nargs="+",
        type=Path,
        help="Run summary JSON files (--output) or metrics JSON files (--metrics-output)",
    )
    parser.add_argument("--title", default="Genome Annotation Daily Run Report")
    parser.add_argument("--markdown-output", type=Path, help="Write the Markdown report here")
    parser.add_argument("--json-output", type=Path, help="Write the combined metrics JSON here")
    parser.add_argument(
        "--fail-on-gaps",
        action="store_true",
        help="Exit non-zero when any requested statistic could not be reported",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        documents = load_documents(args.summaries)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    combined = combine(documents)
    markdown = render_markdown(combined, args.title)

    if args.markdown_output:
        target = args.markdown_output.expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(markdown, encoding="utf-8")
    else:
        print(markdown)
    if args.json_output:
        target = args.json_output.expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(combined, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    if args.fail_on_gaps and (combined["warnings"] or not combined["tokens"]["cost"]["complete"]):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
