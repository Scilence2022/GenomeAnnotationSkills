from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

import run_annotation_workflow as workflow  # noqa: E402


class FakeClient:
    def call_tool(self, name: str, arguments: dict):
        if name == "list_annotation_quality_candidates":
            return {
                "policyVersion": "codexomics.annotation-quality-policy.v1",
                "candidates": [
                    {
                        "chromosome": "chrA",
                        "qualityScore": 10,
                        "qualityBand": "critical",
                        "feature": {
                            "id": "f1",
                            "featureType": "CDS",
                            "start": 10,
                            "locusTag": "a1",
                        },
                        "reasons": [{"code": "generic_product"}],
                        "recommendedResearchFocus": ["molecular function"],
                    },
                    {
                        "chromosome": "chrB",
                        "qualityScore": 20,
                        "qualityBand": "low",
                        "feature": {
                            "id": "f3",
                            "featureType": "tRNA",
                            "start": 5,
                            "locusTag": "b3",
                        },
                        "reasons": [{"code": "missing_functional_note"}],
                        "recommendedResearchFocus": ["RNA function"],
                    },
                    {
                        "chromosome": "chrA",
                        "qualityScore": 30,
                        "qualityBand": "low",
                        "feature": {
                            "id": "f2",
                            "featureType": "gene",
                            "start": 20,
                            "gene": "a2",
                        },
                        "reasons": [],
                        "recommendedResearchFocus": [],
                    },
                    {"chromosome": "chrA", "qualityScore": 5, "feature": {"id": "skip"}},
                ],
            }
        if name == "list_annotation_changesets":
            return {
                "total": 3,
                "changeSets": [
                    {"status": "awaiting_approval", "target": {"locusTag": "a1"}},
                    {"status": "committed", "target": {"geneSymbol": "lysC"}},
                    {"status": "rejected", "target": {"locusTag": "retry-me"}},
                ],
            }
        raise AssertionError(name)


class WorkflowHelpersTests(unittest.TestCase):
    def test_parse_and_deduplicate_explicit_identifiers(self) -> None:
        values = workflow.parse_list("lysC, thrB\ntalB\tLYSC")
        self.assertEqual(workflow.unique_identifiers(values), ["lysC", "thrB", "talB"])

    def test_gene_file_ignores_comments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "genes.txt"
            path.write_text("lysC # primary\nthrB,talB\n", encoding="utf-8")
            self.assertEqual(workflow.read_gene_file(path), ["lysC", "thrB", "talB"])

    def test_daily_quality_selection_supports_multiple_gene_feature_types(self) -> None:
        result = workflow.enumerate_annotation_candidates(
            FakeClient(),
            {"windowId": "w", "expected_genome": "g"},
            {"chromosomes": ["chrB", "chrA"]},
            None,
            "low-quality",
            70,
            None,
        )
        self.assertEqual(
            [(item.chromosome, item.identifier, item.feature_type) for item in result],
            [("chrA", "a1", "CDS"), ("chrB", "b3", "tRNA"), ("chrA", "a2", "gene")],
        )
        self.assertEqual(result[0].quality_reasons, ("generic_product",))
        self.assertEqual(result[1].recommended_research_focus, ("RNA function",))

    def test_quality_score_argument_is_bounded(self) -> None:
        self.assertEqual(workflow.quality_score("70"), 70)
        with self.assertRaises(argparse.ArgumentTypeError):
            workflow.quality_score("101")

    def test_existing_changesets_exclude_active_but_not_rejected(self) -> None:
        identities = workflow.changeset_identities(FakeClient(), {"windowId": "w"})
        self.assertEqual(identities, {"a1", "lysc"})

    def test_state_round_trip_and_genome_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            genome = Path(directory) / "genome.gbk"
            genome.write_text("LOCUS test\n", encoding="utf-8")
            path = Path(directory) / "state.json"
            state = workflow.load_state(path, genome, "abc")
            state["workflows"]["key"] = {"status": "completed"}
            workflow.save_state(path, state)
            loaded = workflow.load_state(path, genome, "abc")
            self.assertEqual(loaded["workflows"]["key"]["status"], "completed")
            with self.assertRaisesRegex(RuntimeError, "different genome"):
                workflow.load_state(path, genome, "different")

    def test_failed_workflow_is_persisted_as_retryable_when_a_task_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            genome = Path(directory) / "genome.gbk"
            genome.write_text("LOCUS test\n", encoding="utf-8")
            path = Path(directory) / "state.json"
            state = workflow.load_state(path, genome, "abc")

            workflow.persist_failed_workflow(
                path,
                state,
                "gas:v1:test",
                task_id="task-28751",
                selection_mode="daily-count",
                requested_identifier="b0002",
                resolved_identity="b0002",
                error=RuntimeError("Evidence record contains an invalid PMID identifier"),
            )

            record = workflow.load_state(path, genome, "abc")["workflows"]["gas:v1:test"]
            self.assertEqual(record["status"], "failed")
            self.assertEqual(record["taskId"], "task-28751")
            self.assertTrue(record["retryable"])
            self.assertEqual(record["failureCount"], 1)
            self.assertEqual(record["errorType"], "RuntimeError")

    def test_failed_workflow_without_a_started_task_is_not_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            genome = Path(directory) / "genome.gbk"
            genome.write_text("LOCUS test\n", encoding="utf-8")
            path = Path(directory) / "state.json"
            state = workflow.load_state(path, genome, "abc")

            workflow.persist_failed_workflow(
                path,
                state,
                "gas:v1:not-started",
                task_id=None,
                selection_mode="explicit",
                requested_identifier="ambiguous",
                resolved_identity=None,
                error=RuntimeError("Target is ambiguous"),
            )

            self.assertFalse(path.exists())

    def test_compact_workflow_drops_large_result(self) -> None:
        large = {"workflow": "not used", "fullReport": "x" * 10000}
        compact = workflow.compact_workflow(
            {
                "taskId": "task-1",
                "status": "completed",
                "target": {"featureType": "CDS", "locusTag": "b0001"},
                "reportAttachment": {"attachmentId": "a-1", "fileName": "report.json"},
                "result": large,
            }
        )
        self.assertEqual(compact["reportAttachment"]["attachmentId"], "a-1")
        self.assertNotIn("result", compact)
        json.dumps(compact)

    def test_unattended_required_tools_exclude_governance_mutations(self) -> None:
        self.assertNotIn("request_annotation_approval", workflow.REQUIRED_TOOLS)
        self.assertNotIn("apply_annotation_changeset", workflow.REQUIRED_TOOLS)


if __name__ == "__main__":
    unittest.main()
