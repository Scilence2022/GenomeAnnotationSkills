# Annotation workflows

## External MCP agent: recommended path

Use CodeXomics MCP tools mode as the only client-facing orchestration endpoint. CodeXomics calls DGR internally and preserves genome identity and revision boundaries.

1. Call `tools/list` and require:
   - `list_genome_windows`
   - `load_genome_file`
   - `list_annotations`
   - `list_annotation_changesets`
   - `resolve_annotation_target`
   - `start_annotation_research`
   - `get_annotation_research_workflow`
2. Call `load_genome_file` with an absolute path unless the intended file is already loaded.
3. Call `list_genome_windows`, identify the correct window, and attach its `windowId` and `expected_genome` to subsequent calls.
4. Call `resolve_annotation_target`. Require `target.featureType == CDS` and a stable locus or protein identifier.
5. Call `start_annotation_research`. The loaded genome supplies organism metadata when available; supply `organism` only as a fallback.
6. Save the returned `workflow.taskId` immediately.
7. Poll `get_annotation_research_workflow` with bounded backoff. Terminal states are `completed`, `failed`, and `cancelled`. A completed DGR task may still produce no ChangeSet if evidence or target binding is insufficient.
8. Return `workflow.reportAttachment`, `workflow.proposalStatus`, `workflow.changeSetId`, and `workflow.changeSetStatus`.

Do not call DGR directly unless CodeXomics orchestration is unavailable and the user explicitly accepts the advanced recovery path. A direct DGR result must still be archived and rebound through CodeXomics before a ChangeSet can be created.

## Internal CodeXomics ChatBox

Use a precise prompt such as:

> For the loaded genome, refine only the CDS resolved as `lysC`. Use Deep Gene Research for a thorough evidence search, exclude unrelated lysozyme-C results, create an evidence-backed annotation ChangeSet with a concise citation-rich Note, archive the full report, and stop before approval or application.

For a list, include exact identifiers and require an independent target-resolution check for each. Ask the ChatBox to summarize task ID, report attachment, and ChangeSet ID per target.

## Gene selection

### Single gene or explicit list

Preserve user order, trim whitespace, and remove exact duplicate identifiers case-insensitively. Resolve every identifier independently. If a gene symbol is ambiguous, ask for chromosome or locus tag rather than choosing a match.

### Daily count

1. Enumerate `CDS` features with `list_annotations`.
2. Sort deterministically by chromosome, start coordinate, locus tag, then feature ID.
3. Prefer `locus_tag`, then gene name, then stable feature ID.
4. Exclude targets already recorded as successfully submitted in the per-genome state file.
5. Exclude targets with an existing active, approved, or committed ChangeSet unless `--include-existing-changesets` was explicitly selected.
6. Select the first requested number and submit sequentially.

This is a reproducible coverage policy, not a claim that coordinate order equals biological priority. If the user specifies a priority strategy—hypothetical proteins, low-confidence annotations, pathway membership, or a curated list—honor it and record the policy.

## Proposal quality

The annotation proposal should be concise but information-rich:

- standardized product and function;
- catalytic activity and EC number when supported;
- pathway and physiological role;
- subunit/complex and localization when supported;
- regulation and phenotype where relevant;
- synonyms and database cross-references;
- a compact `Note` synthesizing the report with precise citations;
- explicit uncertainty and conflicts;
- evidence records traceable to stable URLs, identifiers, and publication metadata.

Exclude lexical overmatches such as `lysC` interpreted as “lysozyme C”. Organism, locus, protein identity, and functional context must agree. Homolog evidence must be labeled as such and not presented as direct evidence for the target organism.

## Idempotency and resumption

CodeXomics derives a semantic idempotency key when none is supplied. The runner supplies a stable key derived from genome path, exact target, and research intent. Repeating the same request resumes or returns the same workflow; changing the research prompt creates a distinct intent.

Persist task IDs before polling. On interruption, rerun with the same state directory and inputs. Never discard the DGR ledger or CodeXomics sidecar to force a retry. Use `--force-refresh` only when intentionally bypassing DGR's semantic result cache; it does not remove identity checks.

When polling or proposal materialization fails after a task has started, retain the task ID and record the failure as retryable. Unattended daily selection skips that failed target to avoid repeatedly consuming the batch; retry the exact target explicitly after correcting the underlying service, validation, or evidence problem.

## Human review

Research completion is not annotation application. The reviewer opens CodeXomics Annotation Review Center, filters the queue, examines current versus proposed qualifiers and citations, selects eligible ChangeSets, and uses batch approval/application only under the configured governance policy. The creator must not self-approve.
