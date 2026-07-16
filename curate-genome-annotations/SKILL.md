---
name: curate-genome-annotations
description: Load a GenBank, EMBL, or FASTA genome into CodeXomics and use Deep Gene Research (DGR) to produce evidence-backed, human-reviewable annotation ChangeSets for exact CDS features. Use when asked to install, configure, start, or connect CodeXomics and DGR; refine one named gene; process a gene list; select a fixed number of CDS genes; resume a previous run; or prepare a recurring daily annotation job. Supports external MCP agents and the internal CodeXomics ChatBox. Never use it to silently approve or apply annotation changes.
---

# Curate Genome Annotations

Use CodeXomics as the genome authority and ChangeSet boundary. Use DGR as the evidence-research engine. Produce reviewable proposals; leave approval and application to a distinct human curator.

## Non-negotiable safety rules

1. Work only on features resolved by CodeXomics as `CDS`.
2. Require a stable `locus_tag` or `protein_id`; never guess across ambiguous matches.
3. Treat the genome loaded in CodeXomics as authoritative for organism, coordinates, current qualifiers, and revision.
4. Prefer `start_annotation_research` followed by `get_annotation_research_workflow`. This path binds DGR to the live CDS, archives the full report, validates citations, and creates a ChangeSet when the caller has `annotation:propose`.
5. Never call `request_annotation_approval`, `apply_annotation_changeset`, raw annotation-editing tools, or rollback tools from an unattended research workflow.
6. Never give an unattended worker a curator credential. Use a research key limited to `annotation:read`, `annotation:research`, and `annotation:propose`.
7. Report partial failures per gene. Do not claim an annotation was updated when only a proposal was created.

## Choose the execution path

- For an external agent such as Codex, Claude, or OpenClaw, use the scripts in `scripts/` or equivalent MCP calls against CodeXomics tools mode.
- For CodeXomics ChatBox, load the genome in the app, confirm DGR connectivity, and give the ChatBox an exact CDS target or gene list. Instruct it to stop after creating ChangeSets.
- If repositories or services are missing, read [references/setup.md](references/setup.md), then run `scripts/bootstrap_repositories.py` and `scripts/start_services.py`.
- If repositories and endpoints already exist, skip installation and startup. Validate endpoints with `scripts/start_services.py --check-only`.

## Execute the workflow

Read [references/workflows.md](references/workflows.md) before the first run in a new environment.

1. Confirm the CodeXomics MCP endpoint is in **tools mode** and exposes the required annotation research tools.
2. Load the user's absolute genome path. If it is already loaded in the intended window, reuse that window.
3. Pin every call with `windowId` and `expected_genome` when multiple windows exist.
4. Resolve every requested identifier and reject non-CDS or ambiguous targets.
5. Start DGR through CodeXomics with the user's research prompt, aspects, language, and result limit.
6. Poll the durable workflow until it reaches a terminal state. Do not infer completion from elapsed time.
7. Record the archived report attachment, proposal status, ChangeSet ID, and failure reason for each gene.
8. Direct the human reviewer to CodeXomics **Annotation Review Center** for individual or batch review.

Use the production runner for repeatable work:

```bash
python3 scripts/run_annotation_workflow.py \
  --genome /absolute/path/to/genome.gbk \
  --gene lysC \
  --user-prompt "Refine function, regulation, pathway role, complexes, and phenotype with precise citations"
```

The runner supports exactly one selector per invocation:

- `--gene lysC` for one CDS.
- `--genes lysC,thrB,talB` for an explicit list.
- `--gene-file /absolute/path/genes.txt` for newline, comma, or tab-separated identifiers.
- `--daily-count 10` for a deterministic batch of unresolved CDS features.

Run with `--dry-run` before a new batch policy. The runner never approves or applies ChangeSets.

## Recurring daily jobs

Read [references/automation.md](references/automation.md) before creating a schedule. Ask for the daily time, timezone, genome path, count, selection policy, and research prompt if the user has not supplied them. Do not install or modify a schedule without explicit authorization.

Keep scheduling separate from curation logic: the scheduler invokes `run_annotation_workflow.py --daily-count N`; the runner uses a per-genome lock and resumable state. Prefer one active run per genome and sequential DGR submissions unless capacity was explicitly validated.

## Configuration and recovery

- Read [references/configuration.md](references/configuration.md) when setting credentials, provider models, SearXNG, task storage, or scoped permissions.
- Read [references/troubleshooting.md](references/troubleshooting.md) when an endpoint is unavailable, DGR finishes implausibly fast, a task stalls, a target is ambiguous, or a ChangeSet is not created.
- Preserve DGR's task ledger and the CodeXomics sidecar. Do not delete either to “unstick” a run.
- Treat `completed` research with `changeSetStatus: validation_failed` as a recoverable proposal-processing failure. Preserve the task ID and rerun the same explicit target after correcting the validator or configuration.

## Completion contract

Return a concise run summary containing:

- genome path and CodeXomics window;
- each exact CDS identifier and resolved locus;
- DGR task status and task ID;
- archived full-report attachment ID/file name when present;
- annotation proposal status;
- ChangeSet ID and review status;
- failures or skipped reasons;
- explicit statement that no ChangeSet was approved or applied automatically.

If the user requests the full DGR report or proposal, return the stored artifact or structured result without truncating it silently. For large JSON, point to the CodeXomics attachment/JSON viewer and optionally save an explicit output file requested by the user.
