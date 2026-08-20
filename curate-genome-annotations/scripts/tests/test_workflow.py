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
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def call_tool(self, name: str, arguments: dict):
        self.calls.append((name, arguments))
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

    def test_research_pdfs_are_validated_and_content_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "paper-a.pdf"
            duplicate = root / "paper-b.pdf"
            payload = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n"
            first.write_bytes(payload)
            duplicate.write_bytes(payload)

            documents = workflow.validate_research_pdfs([first, duplicate])

            self.assertEqual(len(documents), 1)
            self.assertEqual(documents[0]["path"], str(first.resolve()))
            self.assertEqual(documents[0]["name"], first.name)
            self.assertRegex(documents[0]["sha256"], r"^[a-f0-9]{64}$")

    def test_research_pdf_rejects_non_pdf_and_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            invalid = Path(directory) / "paper.pdf"
            invalid.write_text("not a pdf", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not a PDF"):
                workflow.validate_research_pdfs([invalid])
        with self.assertRaisesRegex(ValueError, "must be absolute"):
            workflow.validate_research_pdfs([Path("paper.pdf")])

    def test_daily_quality_selection_supports_multiple_gene_feature_types(self) -> None:
        client = FakeClient()
        result = workflow.enumerate_annotation_candidates(
            client,
            {"windowId": "w", "expected_genome": "g"},
            {"chromosomes": ["chrB", "chrA"]},
            None,
            "low-quality",
            70,
            None,
        )
        self.assertEqual(
            [(item.chromosome, item.identifier, item.feature_type) for item in result.candidates],
            [("chrA", "a1", "CDS"), ("chrB", "b3", "tRNA"), ("chrA", "a2", "gene")],
        )
        self.assertEqual(result.candidates[0].quality_reasons, ("generic_product",))
        self.assertEqual(result.candidates[1].recommended_research_focus, ("RNA function",))
        self.assertEqual(result.research_history_policy, "include")
        quality_call = next(arguments for name, arguments in client.calls if name == "list_annotation_quality_candidates")
        self.assertEqual(quality_call["researchHistoryPolicy"], "include")

    def test_random_selection_asks_for_deterministic_order_within_the_quality_pool(self) -> None:
        client = FakeClient()
        workflow.enumerate_annotation_candidates(
            client,
            {"windowId": "w", "expected_genome": "g"},
            {"chromosomes": ["chrA", "chrB"]},
            None,
            "random",
            70,
            None,
        )
        call = next(arguments for name, arguments in client.calls if name == "list_annotation_quality_candidates")
        # Coordinate order is the deterministic base the local shuffle needs.
        self.assertEqual(call["sortBy"], "coordinate")
        # Random samples inside the threshold; coordinate coverage ignores it.
        self.assertEqual(call["maximumQualityScore"], 70)

    def test_coordinate_selection_still_ignores_the_quality_threshold(self) -> None:
        client = FakeClient()
        workflow.enumerate_annotation_candidates(
            client,
            {"windowId": "w", "expected_genome": "g"},
            {"chromosomes": ["chrA", "chrB"]},
            None,
            "coordinate",
            70,
            None,
        )
        call = next(arguments for name, arguments in client.calls if name == "list_annotation_quality_candidates")
        self.assertEqual(call["maximumQualityScore"], 100)

    def test_random_ranking_is_reproducible_and_seed_dependent(self) -> None:
        candidates = [workflow.Candidate(identifier=f"gene{index}") for index in range(30)]
        first = workflow.rank_candidates(candidates, "random", "seed-a")
        again = workflow.rank_candidates(candidates, "random", "seed-a")
        other = workflow.rank_candidates(candidates, "random", "seed-b")

        self.assertEqual([item.identifier for item in first], [item.identifier for item in again])
        self.assertNotEqual([item.identifier for item in first], [item.identifier for item in other])
        self.assertCountEqual([item.identifier for item in first], [item.identifier for item in candidates])

    def test_random_ranking_keeps_its_order_when_covered_genes_drop_out(self) -> None:
        candidates = [workflow.Candidate(identifier=f"gene{index}") for index in range(30)]
        ranked = [item.identifier for item in workflow.rank_candidates(candidates, "random", "seed-a")]

        # Yesterday's batch completed and is now excluded by coverage.
        covered = set(ranked[:10])
        remaining = [item for item in candidates if item.identifier not in covered]
        continued = [item.identifier for item in workflow.rank_candidates(remaining, "random", "seed-a")]

        # The next batch continues where the last one stopped instead of
        # reshuffling the whole pool.
        self.assertEqual(continued[:10], ranked[10:20])

    def test_random_ranking_is_case_insensitive_on_identifiers(self) -> None:
        lower = workflow.rank_candidates([workflow.Candidate(identifier="lysc")], "random", "s")
        upper = workflow.rank_candidates([workflow.Candidate(identifier="LysC")], "random", "s")
        self.assertEqual(
            workflow.random_selection_key("s", lower[0].identifier),
            workflow.random_selection_key("s", upper[0].identifier),
        )

    def test_non_random_policies_preserve_the_codexomics_order(self) -> None:
        candidates = [workflow.Candidate(identifier=f"gene{index}") for index in range(5)]
        for policy in ("low-quality", "coordinate"):
            self.assertEqual(workflow.rank_candidates(candidates, policy, None), candidates)

    def test_random_policy_refuses_to_run_without_a_seed(self) -> None:
        with self.assertRaises(RuntimeError):
            workflow.rank_candidates([workflow.Candidate(identifier="a")], "random", None)

    def test_random_seed_prefers_the_most_stable_source(self) -> None:
        self.assertEqual(
            workflow.resolve_random_seed("pinned", "run-7", "genome-key", "2026-08-20"),
            ("pinned", "explicit"),
        )
        self.assertEqual(
            workflow.resolve_random_seed(None, "run-7", "genome-key", "2026-08-20"),
            ("run-7", "run-id"),
        )
        # No stable identifier: same genome and same UTC day resolve alike, so a
        # rerun that day continues the same batch.
        self.assertEqual(
            workflow.resolve_random_seed(None, None, "genome-key", "2026-08-20"),
            ("genome-key:2026-08-20", "genome-and-utc-date"),
        )
        self.assertNotEqual(
            workflow.resolve_random_seed(None, None, "genome-key", "2026-08-21")[0],
            workflow.resolve_random_seed(None, None, "genome-key", "2026-08-20")[0],
        )

    def test_daily_selection_requests_authoritative_coverage_exclusion(self) -> None:
        client = FakeClient()
        workflow.enumerate_annotation_candidates(
            client,
            {"windowId": "w", "expected_genome": "g"},
            {"chromosomes": ["chrA", "chrB"]},
            None,
            "low-quality",
            70,
            None,
            "exclude-covered",
            365,
        )
        quality_call = next(arguments for name, arguments in client.calls if name == "list_annotation_quality_candidates")
        self.assertEqual(quality_call["researchHistoryPolicy"], "exclude-covered")
        self.assertEqual(quality_call["researchRefreshDays"], 365)

    def test_quality_score_argument_is_bounded(self) -> None:
        self.assertEqual(workflow.quality_score("70"), 70)
        with self.assertRaises(argparse.ArgumentTypeError):
            workflow.quality_score("101")

    def test_research_refresh_days_is_bounded(self) -> None:
        self.assertEqual(workflow.research_refresh_days("365"), 365)
        with self.assertRaises(argparse.ArgumentTypeError):
            workflow.research_refresh_days("0")

    def test_failed_daily_work_is_retryable_not_covered(self) -> None:
        self.assertNotIn("failed", workflow.DAILY_COVERED_STATUSES)
        self.assertIn("completed", workflow.DAILY_COVERED_STATUSES)

    def test_failed_workflow_uses_a_fresh_retry_idempotency_key(self) -> None:
        base = "gas:v1:abc"
        key, existing = workflow.select_workflow_attempt(
            {
                base: {"status": "failed", "taskId": "failed-1"},
                f"{base}:retry:1": {"status": "cancelled", "taskId": "failed-2"},
            },
            base,
        )
        self.assertEqual(key, f"{base}:retry:2")
        self.assertEqual(existing, {})

        key, existing = workflow.select_workflow_attempt(
            {base: {"status": "completed", "taskId": "completed-1"}},
            base,
        )
        self.assertEqual(key, base)
        self.assertEqual(existing["taskId"], "completed-1")

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
                "reportAttachment": {
                    "attachmentId": "a-1",
                    "fileName": "report.json",
                    "summary": {"fullTextSourceCount": 2, "fullTextFindingCount": 5},
                },
                "result": large,
            }
        )
        self.assertEqual(compact["reportAttachment"]["attachmentId"], "a-1")
        self.assertEqual(compact["reportAttachment"]["summary"]["fullTextSourceCount"], 2)
        self.assertNotIn("result", compact)
        json.dumps(compact)

    def test_unattended_required_tools_exclude_governance_mutations(self) -> None:
        self.assertNotIn("request_annotation_approval", workflow.REQUIRED_TOOLS)
        self.assertNotIn("apply_annotation_changeset", workflow.REQUIRED_TOOLS)


if __name__ == "__main__":
    unittest.main()
