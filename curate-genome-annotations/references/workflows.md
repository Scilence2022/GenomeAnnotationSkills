# Annotation workflows

## External MCP agent: recommended path

Use CodeXomics MCP tools mode as the only client-facing orchestration endpoint. CodeXomics calls DGR internally and preserves genome identity and revision boundaries.

1. Call `tools/list` and require:
   - `list_genome_windows`
   - `load_genome_file`
   - `list_annotation_quality_candidates`
   - `list_annotation_changesets`
   - `resolve_annotation_target`
   - `start_annotation_research`
   - `get_annotation_research_workflow`
2. Call `load_genome_file` with an absolute path unless the intended file is already loaded.
   The target must be in the primary annotation set. For FASTA plus GFF, load
   or merge the features into that set before resolving targets.
3. Call `list_genome_windows`, identify the correct window, and attach its `windowId` and `expected_genome` to subsequent calls.
4. Call `resolve_annotation_target`. Require a supported gene-associated feature and a stable locus tag, protein identifier, or gene symbol. Co-located records resolve to CDS first, then a specific RNA/transcript feature, then generic gene.
5. Call `start_annotation_research`. The loaded genome supplies organism metadata when available; supply `organism` only as a fallback. For user PDFs, pass absolute paths in `researchDocumentPaths`; CodeXomics uploads them to DGR and binds the resulting content-addressed document IDs to the workflow.
6. Save the returned `workflow.taskId` immediately.
7. Poll `get_annotation_research_workflow` with bounded backoff. Terminal states are `completed`, `failed`, and `cancelled`. A completed DGR task may still produce no ChangeSet if evidence or target binding is insufficient.
8. Return `workflow.reportAttachment`, `workflow.proposalStatus`, `workflow.changeSetId`, and `workflow.changeSetStatus`.

Do not call DGR directly unless CodeXomics orchestration is unavailable and the user explicitly accepts the advanced recovery path. A direct DGR result must still be archived and rebound through CodeXomics before a ChangeSet can be created.

## User PDFs and full-text evidence

Use PDFs only for one explicitly resolved gene per invocation. This prevents a document from being silently treated as direct evidence for every member of a batch.

1. Validate each absolute path, PDF signature, 20 MiB size limit, and SHA-256. Deduplicate identical content and accept at most eight documents.
2. Send the paths only to CodeXomics. CodeXomics grants narrowly scoped file access, uploads the bytes over the authenticated DGR connection, stores the returned content-addressed IDs as gene-scoped attachments, and includes those IDs in workflow idempotency.
3. DGR parses text-bearing pages and continues database and web searches.
   Provider configuration controls open full-text retrieval. Private PDF and
   DOI-only excerpts may be preserved in `dgr.review-evidence.v1` for curator
   review, with hashes and offsets but without ChangeSet write eligibility.
4. For exact-target ChangeSet evidence, CodeXomics verifies the PMID citation,
   archive binding, document/text SHA-256 and UTF-16 offsets against the
   archived report. Keep image-only scans and target-negative PDFs visible as
   coverage gaps.
5. Read `reportAttachment.citationValidation.verifiedFullTextSourceCount`
   with `verified: true`. `summary.fullTextSourceCount` counts parsed documents,
   including those without a validated fact. Under `--full-text-policy require`,
   a zero or unavailable verified count fails the runner's outcome check even
   if CodeXomics already created a reviewable ChangeSet.

The current parsers handle text-bearing PDFs, PMC JATS XML, PubTator BioC, and GROBID TEI; every format is normalized to the same canonical text with SHA-256 hashes and UTF-16 offsets before evidence is accepted. Image-only scans or complex tables may require future OCR/table extraction; report that limitation explicitly rather than fabricating coverage.

## Internal CodeXomics ChatBox

Use a precise prompt such as:

> For the loaded genome, refine only the feature resolved as `lysC`. Prefer CDS if the locus also has a generic gene record. Use Deep Gene Research for a thorough evidence search, exclude unrelated lysozyme-C results, create an evidence-backed annotation ChangeSet with a concise citation-rich Note, archive the full report, and stop before approval or application.

For a list, include exact identifiers and require an independent target-resolution check for each. Ask the ChatBox to summarize task ID, report attachment, and ChangeSet ID per target.

## Gene selection

### Single gene or explicit list

Preserve user order, trim whitespace, and remove exact duplicate identifiers case-insensitively. Resolve every identifier independently. If a gene symbol is ambiguous, ask for chromosome or locus tag rather than choosing a match.

### Daily count

1. Call `list_annotation_quality_candidates` for supported gene-associated features.
2. Collapse records at the same chromosome, coordinates, strand, and stable identity. Prefer `CDS`, then specific RNA/transcript types, then generic `gene`.
3. By default retain scores at or below `--maximum-quality-score` and sort lowest quality first. Use chromosome, coordinate, and identifier as deterministic tie-breakers.
4. Ask CodeXomics to exclude targets with active research or a durably archived completed DGR report. This genome-sidecar ledger is authoritative across agents, machines sharing the sidecar, and runner state directories.
5. Use the local state only as a resumable execution checkpoint. Treat completed and coverage-skipped records as covered; failed or cancelled records remain retryable.
6. Exclude targets with an existing active, approved, or committed ChangeSet unless `--include-existing-changesets` was explicitly selected.
7. Select the first requested number, add each candidate's recommended research focus to its DGR request, and submit sequentially with `repeatPolicy=skip-covered` as a race-condition guard.

The default `low-quality` policy prioritizes missing or generic products, missing functional Notes and cross-references, identity gaps, and CDS translation defects. Quality scoring is triage rather than a biological truth claim. Use `--selection-policy coordinate` for reproducible coverage independent of quality.

Use `--selection-policy random` when the batch should be an unbiased sample rather than a worklist — for benchmarking annotation quality across a genome, or for spreading curation over regions the quality score would never reach. It applies after every coverage exclusion, so it samples only genes that are actually curatable, and it stays inside `--maximum-quality-score` (pass 100 to sample the whole genome).

Random selection is reproducible, not merely repeatable. Each candidate is ranked by a hash of the seed and its identifier rather than by shuffling the list, which has two consequences worth relying on:

- a rerun with the same seed picks the same genes, so an interrupted batch resumes instead of abandoning the targets it already submitted to DGR;
- when covered genes drop out of the pool, the survivors keep their relative order, so the next batch continues where the last one stopped instead of resampling from scratch.

The seed resolves from `--random-seed`, then an explicit `--run-id`, then the genome plus the UTC date. Only the last of those changes on its own, and the runner records a data-gap warning when it is used. The resolved seed and its source appear in `selection.randomSeed` and `selection.randomSeedSource`.

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

CodeXomics derives a semantic idempotency key when none is supplied. The runner supplies a stable key derived from genome path, exact target, and research intent. Idempotency handles identical requests; the separate research-coverage ledger prevents a changed prompt or a different agent from unintentionally repeating a completed target.

Persist task IDs before polling. On interruption, rerun with the same state directory and inputs. Preserve the DGR ledger, its artifact directory, and the matching CodeXomics sidecar. The CodeXomics sidecar binds to both the canonical path and genome content, so a replacement at the same path can open a separate history. Use `--force-refresh` only when intentionally bypassing DGR's semantic result cache.

When polling or proposal materialization fails after a task has started, retain the task ID and record the failure as retryable. Failed, cancelled, and completed-but-unarchived runs do not count as durable coverage. Correct the underlying service, validation, or evidence problem and rerun; CodeXomics resumes exact active work and skips only safely covered targets.

Use `--research-refresh-days N` when the curation policy requires periodic re-research. Use `--include-researched` only when the operator explicitly intends to bypass coverage for a new campaign. `--force-refresh` bypasses the DGR cache but does not by itself authorize repeated target selection.

## Human review

Research completion is not annotation application. The reviewer opens CodeXomics Annotation Review, filters the queue, examines current versus proposed qualifiers and citations, selects eligible ChangeSets, and uses batch approval/application only under the configured governance policy. The creator must not self-approve.
