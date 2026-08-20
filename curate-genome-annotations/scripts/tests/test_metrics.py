from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

import dgr_telemetry  # noqa: E402
import generate_run_report as report  # noqa: E402
import run_annotation_workflow as workflow  # noqa: E402
import run_metrics as metrics  # noqa: E402


PRICING = {
    "schema": metrics.PRICING_SCHEMA,
    "currency": "USD",
    "models": {
        "deepseek-v4-pro": {
            "inputPerMillion": 1.0,
            "cachedInputPerMillion": 0.1,
            "outputPerMillion": 4.0,
        },
        "deepseek-v4-flash": {"inputPerMillion": 0.5, "outputPerMillion": 1.0},
    },
}

DGR_PHASE_USAGE = {
    "calls": 3,
    "phases": {
        "gene-llm-queries": {"calls": 1, "promptTokens": 1000, "completionTokens": 100, "totalTokens": 1100},
        "gene-llm-learnings": {"calls": 1, "promptTokens": 2000, "completionTokens": 200, "totalTokens": 2200},
        "gene-llm-report": {"calls": 1, "promptTokens": 3000, "completionTokens": 300, "totalTokens": 3300},
    },
}


def write_json(directory: Path, name: str, payload: object) -> Path:
    path = directory / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class PricingTests(unittest.TestCase):
    def test_quote_bills_cached_input_at_the_cached_rate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            book = metrics.PricingBook.load(write_json(Path(directory), "p.json", PRICING))
        cost, reason = book.quote("deepseek-v4-pro", prompt_tokens=1_000_000, completion_tokens=0, cached_prompt_tokens=500_000)
        self.assertIsNone(reason)
        # 500k fresh at 1.0/M plus 500k cached at 0.1/M.
        self.assertAlmostEqual(cost, 0.55, places=6)

    def test_cached_tokens_cannot_exceed_prompt_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            book = metrics.PricingBook.load(write_json(Path(directory), "p.json", PRICING))
        cost, _ = book.quote("deepseek-v4-pro", prompt_tokens=1000, completion_tokens=0, cached_prompt_tokens=99_999)
        self.assertAlmostEqual(cost, 1000 * 0.1 / 1_000_000, places=9)

    def test_missing_cached_rate_falls_back_to_the_input_rate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            book = metrics.PricingBook.load(write_json(Path(directory), "p.json", PRICING))
        cost, _ = book.quote("deepseek-v4-flash", prompt_tokens=1_000_000, completion_tokens=0, cached_prompt_tokens=1_000_000)
        self.assertAlmostEqual(cost, 0.5, places=6)

    def test_unknown_model_is_reported_not_priced_at_zero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            book = metrics.PricingBook.load(write_json(Path(directory), "p.json", PRICING))
        cost, reason = book.quote("some-other-model", 10, 10)
        self.assertIsNone(cost)
        self.assertIn("no pricing entry", reason or "")

    def test_no_pricing_file_reports_a_reason(self) -> None:
        cost, reason = metrics.PricingBook.empty().quote("deepseek-v4-pro", 10, 10)
        self.assertIsNone(cost)
        self.assertEqual(reason, "no pricing file configured")

    def test_incomplete_price_entry_fails_loudly(self) -> None:
        payload = {"schema": metrics.PRICING_SCHEMA, "models": {"m": {"inputPerMillion": None, "outputPerMillion": 1}}}
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                metrics.PricingBook.load(write_json(Path(directory), "p.json", payload))

    def test_shipped_template_refuses_to_load_until_prices_are_filled_in(self) -> None:
        template = SCRIPT_DIR.parent / "references" / "pricing.template.json"
        self.assertTrue(template.is_file())
        with self.assertRaises(ValueError):
            metrics.PricingBook.load(template)


class DgrUsageTests(unittest.TestCase):
    def test_phases_map_to_the_configured_thinking_and_task_models(self) -> None:
        entries, warnings = metrics.normalize_dgr_llm_usage(
            DGR_PHASE_USAGE, thinking_model="deepseek-v4-pro", task_model="cheap-task-model", gene="lysC"
        )
        self.assertEqual(warnings, [])
        by_phase = {entry.phase: entry for entry in entries}
        self.assertEqual(by_phase["gene-llm-queries"].model, "deepseek-v4-pro")
        self.assertEqual(by_phase["gene-llm-report"].model, "deepseek-v4-pro")
        self.assertEqual(by_phase["gene-llm-learnings"].model, "cheap-task-model")
        self.assertTrue(all(entry.role == metrics.ROLE_RESEARCH and entry.gene == "lysC" for entry in entries))

    def test_reported_model_ids_take_precedence_over_the_phase_map(self) -> None:
        payload = {
            "models": {
                "reported-model": {
                    "phases": {"gene-llm-report": {"calls": 2, "promptTokens": 10, "completionTokens": 5}}
                }
            }
        }
        entries, warnings = metrics.normalize_dgr_llm_usage(
            payload, thinking_model="deepseek-v4-pro", task_model="deepseek-v4-pro"
        )
        self.assertEqual(warnings, [])
        self.assertEqual([entry.model for entry in entries], ["reported-model"])
        self.assertEqual(entries[0].total_tokens, 15)

    def test_unconfigured_models_are_flagged_instead_of_guessed(self) -> None:
        entries, warnings = metrics.normalize_dgr_llm_usage(DGR_PHASE_USAGE, thinking_model=None, task_model=None)
        self.assertTrue(all(entry.model.startswith(metrics.UNKNOWN_MODEL_PREFIX) for entry in entries))
        self.assertTrue(warnings)

    def test_absent_usage_payload_produces_a_warning_not_zeros(self) -> None:
        entries, warnings = metrics.normalize_dgr_llm_usage(None, thinking_model="m", task_model="m")
        self.assertEqual(entries, [])
        self.assertTrue(warnings)


class LedgerTests(unittest.TestCase):
    def _ledger(self, directory: str) -> metrics.UsageLedger:
        book = metrics.PricingBook.load(write_json(Path(directory), "p.json", PRICING))
        return metrics.UsageLedger(book)

    def test_roles_and_models_stay_separate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = self._ledger(directory)
            ledger.extend(
                metrics.normalize_dgr_llm_usage(
                    DGR_PHASE_USAGE, thinking_model="deepseek-v4-pro", task_model="deepseek-v4-pro", gene="lysC"
                )[0]
            )
            ledger.add(
                metrics.UsageEntry(
                    role=metrics.ROLE_AGENT,
                    model="deepseek-v4-flash",
                    phase="orchestration",
                    calls=1,
                    prompt_tokens=1000,
                    completion_tokens=100,
                    gene="lysC",
                )
            )
            payload = ledger.to_dict()
        self.assertIn("research:deepseek-v4-pro", payload["byModel"])
        self.assertIn("agent:deepseek-v4-flash", payload["byModel"])
        self.assertEqual(payload["byRole"]["research"]["totalTokens"], 6600)
        self.assertEqual(payload["byRole"]["agent"]["totalTokens"], 1100)
        self.assertEqual(payload["totals"]["totalTokens"], 7700)
        self.assertTrue(payload["cost"]["complete"])

    def test_replayed_tokens_are_counted_but_not_billed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = self._ledger(directory)
            ledger.add(
                metrics.UsageEntry(
                    role=metrics.ROLE_RESEARCH,
                    model="deepseek-v4-pro",
                    phase="gene-llm-report",
                    calls=1,
                    prompt_tokens=1_000_000,
                    completion_tokens=0,
                    billable=False,
                )
            )
            payload = ledger.to_dict()
        self.assertEqual(payload["totals"]["totalTokens"], 1_000_000)
        self.assertEqual(payload["totals"]["replayedTotalTokens"], 1_000_000)
        self.assertEqual(payload["totals"]["billableTotalTokens"], 0)
        self.assertEqual(payload["cost"]["billedTotal"], 0.0)

    def test_an_unpriced_model_makes_the_cost_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = self._ledger(directory)
            ledger.add(
                metrics.UsageEntry(
                    role=metrics.ROLE_RESEARCH, model="mystery-model", phase="p", calls=1, prompt_tokens=10
                )
            )
            payload = ledger.to_dict()
        self.assertFalse(payload["cost"]["complete"])
        self.assertIsNone(payload["cost"]["total"])
        self.assertEqual(payload["cost"]["unpricedModels"], ["mystery-model"])


class AgentUsageTests(unittest.TestCase):
    def test_records_are_filtered_by_run_id_and_bad_lines_warn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent.jsonl"
            metrics.append_agent_usage(
                path, {"runId": "a", "model": "deepseek-v4-flash", "promptTokens": 10, "completionTokens": 5}
            )
            metrics.append_agent_usage(
                path, {"runId": "b", "model": "deepseek-v4-flash", "promptTokens": 90, "completionTokens": 90}
            )
            with path.open("a", encoding="utf-8") as handle:
                handle.write("{not json}\n")
            entries, warnings = metrics.read_agent_usage(path, run_id="a")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].total_tokens, 15)
        self.assertEqual(entries[0].role, metrics.ROLE_AGENT)
        self.assertTrue(any("not valid JSON" in warning for warning in warnings))

    def test_a_missing_sidecar_reports_unavailable_rather_than_zero(self) -> None:
        entries, warnings = metrics.read_agent_usage(Path("/nonexistent/agent.jsonl"))
        self.assertEqual(entries, [])
        self.assertTrue(any("does not exist" in warning for warning in warnings))


class ReferenceDeltaTests(unittest.TestCase):
    def test_reference_identifiers_are_extracted_from_mixed_values(self) -> None:
        found = metrics.extract_reference_ids(
            ["PMID:12345678", "doi: 10.1016/j.cell.2020.01.001", {"pmid": "999"}, "no identifier here"]
        )
        self.assertIn("12345678", found)
        self.assertIn("10.1016/j.cell.2020.01.001", found)
        self.assertIn("999", found)
        self.assertEqual(len(found), 3)

    def test_the_same_reference_in_url_and_prefixed_form_is_one_identifier(self) -> None:
        from_url = metrics.extract_reference_ids(
            {"url": "https://pubmed.ncbi.nlm.nih.gov/12345678/", "title": "irrelevant"}
        )
        from_token = metrics.extract_reference_ids("PMID:12345678")
        self.assertEqual(from_url, from_token)
        self.assertEqual(
            metrics.extract_reference_ids("https://doi.org/10.1016/j.cell.2020.01.001"),
            metrics.extract_reference_ids({"doi": "DOI: 10.1016/j.cell.2020.01.001"}),
        )

    def test_references_already_on_the_annotation_are_not_counted_as_new(self) -> None:
        changeset = {
            "id": "cs-1",
            "status": "draft",
            "operations": [
                {"op": "addDbxref", "field": "db_xref", "value": ["PMID:111", "PMID:222"]},
                {"op": "addQualifier", "field": "product", "value": "aspartate kinase III"},
            ],
            "evidence": [{"pmid": "333"}],
        }
        preview = [
            {"field": "db_xref", "before": ["PMID:111"], "after": ["PMID:111", "PMID:222"]},
            {"field": "product", "before": "hypothetical protein", "after": "aspartate kinase III"},
        ]
        delta = metrics.changeset_delta(changeset, preview)
        self.assertEqual(delta["newReferenceCount"], 2)
        self.assertEqual(sorted(delta["newReferences"]), ["222", "333"])
        self.assertEqual(delta["existingReferenceCount"], 1)
        self.assertEqual(delta["updatedFieldCount"], 2)

    def test_delta_without_a_preview_reports_every_proposed_reference(self) -> None:
        changeset = {"id": "cs", "operations": [{"op": "addDbxref", "value": "PMID:777"}], "evidence": []}
        delta = metrics.changeset_delta(changeset, None)
        self.assertEqual(delta["newReferenceCount"], 1)
        self.assertEqual(delta["updatedFields"][0]["field"], "db_xref")


class LiteratureStatisticsTests(unittest.TestCase):
    def test_unreported_counts_stay_none_rather_than_zero(self) -> None:
        stats = metrics.literature_statistics(None, None, None)
        self.assertIsNone(stats["surveyedRecords"])
        self.assertIsNone(stats["fullTextSources"])

    def test_coverage_and_attachment_summary_are_combined(self) -> None:
        stats = metrics.literature_statistics(
            {"pubmedTotalMatchCount": 812, "retainedAbstractCount": 300, "literatureBudget": 300},
            {"fullTextSourceCount": 12, "fullTextFindingCount": 41, "citationBoundFactCount": 18},
        )
        self.assertEqual(stats["surveyedRecords"], 812)
        self.assertEqual(stats["retainedReferences"], 300)
        self.assertEqual(stats["fullTextSources"], 12)
        self.assertEqual(stats["citationBoundFacts"], 18)


class TelemetryTests(unittest.TestCase):
    def test_only_accounting_fields_are_kept_from_a_full_task_payload(self) -> None:
        payload = {
            "taskId": "t-1",
            "status": "completed",
            "step": "final-report",
            "result": {
                "finalReport": "x" * 5000,
                "annotationNote": {"text": "note"},
                "metadata": {
                    "researchTime": 640200,
                    "llmUsage": DGR_PHASE_USAGE,
                    "literatureMetrics": {"totalPapers": 300},
                    "searchDiagnostics": {
                        "queryCount": 12,
                        "literatureCoverage": {"pubmedTotalMatchCount": 812},
                    },
                },
            },
        }
        summary = dgr_telemetry.summarize_task_telemetry(payload)
        self.assertEqual(summary["researchTimeMs"], 640200)
        self.assertEqual(summary["literatureCoverage"], {"pubmedTotalMatchCount": 812})
        self.assertEqual(summary["searchDiagnostics"]["queryCount"], 12)
        self.assertFalse(summary["cacheReplay"])
        self.assertNotIn("finalReport", json.dumps(summary))

    def test_a_cache_hit_step_marks_the_result_as_replayed(self) -> None:
        summary = dgr_telemetry.summarize_task_telemetry(
            {"taskId": "t", "status": "completed", "step": "cache-hit", "result": {"metadata": {}}}
        )
        self.assertTrue(summary["cacheReplay"])


class RunnerAccountingTests(unittest.TestCase):
    def test_sum_reported_separates_zero_from_unreported(self) -> None:
        self.assertEqual(
            workflow.sum_reported([3, 0, None, 5]),
            {"total": 8, "reportedFor": 3, "unreportedFor": 1},
        )

    def test_cached_research_is_recorded_as_replayed_and_warned_about(self) -> None:
        result: dict = {}
        entries, warnings = workflow.gene_usage_entries(
            result,
            {"llmUsage": DGR_PHASE_USAGE, "cacheReplay": True},
            None,
            gene="lysC",
            thinking_model="deepseek-v4-pro",
            task_model="deepseek-v4-pro",
        )
        self.assertTrue(entries)
        self.assertTrue(all(entry.billable is False for entry in entries))
        self.assertEqual(result["tokenUsageSource"], "dgr-telemetry")
        self.assertTrue(any("cached result" in warning for warning in warnings))

    def test_workflow_usage_is_preferred_over_the_telemetry_fallback(self) -> None:
        result: dict = {}
        entries, _ = workflow.gene_usage_entries(
            result,
            {"llmUsage": {"phases": {"gene-llm-report": {"promptTokens": 1}}}},
            DGR_PHASE_USAGE,
            gene="lysC",
            thinking_model="deepseek-v4-pro",
            task_model="deepseek-v4-pro",
        )
        self.assertEqual(result["tokenUsageSource"], "codexomics-workflow")
        self.assertEqual(sum(entry.total_tokens for entry in entries), 6600)

    def test_missing_usage_yields_an_explicit_gap(self) -> None:
        result: dict = {}
        entries, warnings = workflow.gene_usage_entries(
            result, None, None, gene="lysC", thinking_model="m", task_model="m"
        )
        self.assertEqual(entries, [])
        self.assertTrue(any("token usage" in warning for warning in warnings))
        self.assertNotIn("tokenUsageSource", result)

    def test_compact_workflow_carries_the_accounting_fields(self) -> None:
        compact = workflow.compact_workflow(
            {
                "taskId": "t",
                "status": "completed",
                "llmUsage": DGR_PHASE_USAGE,
                "researchTime": 1234,
                "cacheReplay": False,
            }
        )
        self.assertEqual(compact["llmUsage"], DGR_PHASE_USAGE)
        self.assertEqual(compact["researchTimeMs"], 1234)

    def test_reporting_tools_are_optional_not_required(self) -> None:
        self.assertNotIn("get_annotation_changeset", workflow.REQUIRED_TOOLS)
        self.assertIn("get_annotation_changeset", workflow.OPTIONAL_TOOLS)
        self.assertIn("list_annotation_research_history", workflow.OPTIONAL_TOOLS)


class RunReportTests(unittest.TestCase):
    def _summary(self, ledger: metrics.UsageLedger) -> dict:
        summary = {
            "runId": "run-1",
            "startedAt": "2026-08-20T01:00:00+00:00",
            "finishedAt": "2026-08-20T01:20:00+00:00",
            "genomePath": "/genomes/ECOLI.gbk",
            "results": [
                {
                    "requestedIdentifier": "lysC",
                    "status": "completed",
                    "taskId": "t-1",
                    "changeSetId": "cs-1",
                    "startedAt": "2026-08-20T01:00:00+00:00",
                    "finishedAt": "2026-08-20T01:10:00+00:00",
                    "durationSeconds": 600.0,
                    "researchSeconds": 580.0,
                    "references": {"surveyedRecords": 812, "fullTextSources": 12},
                    "annotationNote": {"mutationReady": True, "includedFactCount": 18},
                    "annotationDelta": {
                        "newReferenceCount": 14,
                        "updatedFieldCount": 2,
                        "updatedFields": [{"field": "product"}, {"field": "note"}],
                    },
                },
                {
                    "requestedIdentifier": "thrB",
                    "status": "failed",
                    "startedAt": "2026-08-20T01:10:00+00:00",
                    "finishedAt": "2026-08-20T01:20:00+00:00",
                    "durationSeconds": 600.0,
                },
            ],
        }
        summary["metrics"] = workflow.build_metrics(summary, ledger, [], agent_usage_file=None)
        return summary

    def test_report_states_gaps_instead_of_inventing_numbers(self) -> None:
        ledger = metrics.UsageLedger(metrics.PricingBook.empty())
        ledger.extend(
            metrics.normalize_dgr_llm_usage(
                DGR_PHASE_USAGE, thinking_model="deepseek-v4-pro", task_model="deepseek-v4-pro", gene="lysC"
            )[0]
        )
        with tempfile.TemporaryDirectory() as directory:
            path = write_json(Path(directory), "summary.json", self._summary(ledger))
            combined = report.combine(report.load_documents([path]))
            markdown = report.render_markdown(combined, "Test run")
        self.assertFalse(combined["tokens"]["cost"]["complete"])
        self.assertIn("Actual cost: partially unavailable", markdown)
        self.assertIn("agent-side token usage", markdown)
        # thrB failed before any reference statistic existed.
        self.assertIn("not reported for 1 gene", markdown)
        self.assertIn("`deepseek-v4-pro`", markdown)
        self.assertIn("No ChangeSet was approved or applied", markdown)

    def test_priced_report_totals_every_requested_statistic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            book = metrics.PricingBook.load(write_json(Path(directory), "p.json", PRICING))
            ledger = metrics.UsageLedger(book)
            ledger.extend(
                metrics.normalize_dgr_llm_usage(
                    DGR_PHASE_USAGE, thinking_model="deepseek-v4-pro", task_model="deepseek-v4-pro", gene="lysC"
                )[0]
            )
            ledger.add(
                metrics.UsageEntry(
                    role=metrics.ROLE_AGENT,
                    model="deepseek-v4-flash",
                    phase="orchestration",
                    calls=4,
                    prompt_tokens=20_000,
                    completion_tokens=2_000,
                    gene="lysC",
                )
            )
            path = write_json(Path(directory), "summary.json", self._summary(ledger))
            combined = report.combine(report.load_documents([path]))
            markdown = report.render_markdown(combined, "Test run")
        self.assertTrue(combined["tokens"]["cost"]["complete"])
        self.assertEqual(combined["references"]["newlyAdded"]["total"], 14)
        self.assertEqual(combined["references"]["fullTextsAdopted"]["total"], 12)
        self.assertEqual(combined["newInformation"]["changeSetsCreated"], 1)
        self.assertEqual(combined["runtime"]["genesCompleted"], 1)
        self.assertEqual(combined["runtime"]["perGeneWallClockSeconds"]["total"], 1200)
        self.assertIn("Actual cost:", markdown)
        self.assertIn("deepseek-v4-flash", markdown)

    def test_two_summaries_combine_into_one_batch_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = metrics.UsageLedger(metrics.PricingBook.empty())
            first = write_json(Path(directory), "a.json", self._summary(ledger))
            second = write_json(Path(directory), "b.json", self._summary(ledger))
            combined = report.combine(report.load_documents([first, second]))
        self.assertEqual(combined["runtime"]["genesAttempted"], 4)
        self.assertEqual(len(combined["perGene"]), 4)
        self.assertEqual(combined["references"]["newlyAdded"]["total"], 28)

    def test_a_file_without_metrics_is_rejected_with_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = write_json(Path(directory), "old.json", {"results": []})
            with self.assertRaises(ValueError) as caught:
                report.load_documents([path])
        self.assertIn("--metrics-output", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
