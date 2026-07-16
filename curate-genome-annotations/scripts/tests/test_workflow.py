from __future__ import annotations

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
        if name == "list_annotations":
            chromosome = arguments["chromosome"]
            if chromosome == "chrB":
                return {
                    "annotations": [
                        {"id": "f3", "type": "CDS", "start": 5, "locus_tag": "b3"},
                        {"id": "skip", "type": "CDS", "start": 1, "locus_tag": None},
                    ]
                }
            return {
                "annotations": [
                    {"id": "f2", "type": "CDS", "start": 20, "locus_tag": "a2"},
                    {"id": "f1", "type": "CDS", "start": 10, "locus_tag": "a1"},
                ]
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

    def test_daily_cds_selection_is_deterministic_and_requires_locus(self) -> None:
        result = workflow.enumerate_cds(
            FakeClient(),
            {"windowId": "w", "expected_genome": "g"},
            {"chromosomes": ["chrB", "chrA"]},
            None,
        )
        self.assertEqual(
            [(item.chromosome, item.identifier) for item in result],
            [("chrA", "a1"), ("chrA", "a2"), ("chrB", "b3")],
        )

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
