---
name: curate-genome-annotations
description: Load a GenBank, EMBL, or FASTA genome into CodeXomics and use Deep Gene Research (DGR) plus optional user-supplied PDF full text to produce evidence-backed, human-reviewable annotation ChangeSets for exact coding and non-coding gene annotation features. Use when asked to refine one named gene from web literature or PDFs; process a gene list; prioritize low-quality annotations; select a fixed daily batch; resume a run; prepare a recurring annotation job; report a batch's token consumption, per-model cost, runtime, references surveyed or newly added, full texts adopted, and newly incorporated information; or install, configure, start, or connect CodeXomics and DGR. Supports external MCP agents and the internal CodeXomics ChatBox. Never use it to silently approve or apply annotation changes.
---

# Curate Genome Annotations

Use CodeXomics as the genome authority and ChangeSet boundary. Use DGR as the evidence-research engine. Produce reviewable proposals; leave approval and application to a distinct human curator.

## Non-negotiable safety rules

1. Work only on supported gene-associated features resolved by CodeXomics, including `CDS`, `gene`, transcripts, coding/non-coding RNA features, and pseudogenes. When co-located records represent one locus, prefer `CDS`, then the specific RNA/transcript feature, then generic `gene`.
2. Require a stable `locus_tag`, `protein_id`, or gene symbol; never guess across ambiguous matches.
3. Treat the genome loaded in CodeXomics as authoritative for organism, coordinates, current qualifiers, and revision.
4. Prefer `start_annotation_research` followed by `get_annotation_research_workflow`. This path binds DGR to the live feature, archives the full report, validates citations, and creates a ChangeSet when the caller has `annotation:propose`.
5. Never call `request_annotation_approval`, `apply_annotation_changeset`, raw annotation-editing tools, or rollback tools from an unattended research workflow.
6. Never give an unattended worker a curator credential. Use a research key limited to `annotation:read`, `annotation:research`, and `annotation:propose`.
7. Report partial failures per gene. Do not claim an annotation was updated when only a proposal was created.
8. Treat a PDF as full text only when DGR parses its pages and CodeXomics verifies exact text spans, offsets, and hashes in the archived report. Do not relabel an abstract, snippet, OCR failure, or inaccessible document as full text.
9. Report only statistics a service actually reported. Never estimate tokens, cost, or runtime from message length, gene count, or elapsed time, and never present a missing measurement as zero. An unreported statistic is `unavailable` with its reason.

## Choose the execution path

The scripts are client-agnostic: they are plain Python programs that call the
CodeXomics and DGR MCP endpoints over HTTP, so they run unchanged under Codex,
Claude Code, TRAE Work, and WorkBuddy/CodeBuddy. For each client's install
location and optional frontmatter, read [references/clients.md](references/clients.md).

- For an external agent (Codex, Claude Code, TRAE Work, WorkBuddy/CodeBuddy,
  OpenClaw, or similar), use the scripts in `scripts/` or equivalent MCP calls
  against CodeXomics tools mode.
- For CodeXomics ChatBox, load the genome in the app, confirm DGR connectivity, and give the ChatBox an exact gene-associated target or gene list. Instruct it to stop after creating ChangeSets.
- If repositories or services are missing, read [references/setup.md](references/setup.md), then run `scripts/bootstrap_repositories.py` and `scripts/start_services.py`.
- If repositories and endpoints already exist, skip installation and startup. Validate endpoints with `scripts/start_services.py --check-only`.

## Execute the workflow

Read [references/workflows.md](references/workflows.md) before the first run in a new environment.

1. Confirm the CodeXomics MCP endpoint is in **tools mode** and exposes the required annotation research tools.
2. Load the user's absolute genome path. If it is already loaded in the intended window, reuse that window.
3. Pin every call with `windowId` and `expected_genome` when multiple windows exist.
4. Resolve every requested identifier and reject unsupported, identity-unsafe, or ambiguous targets.
5. Start DGR through CodeXomics with the user's research prompt, aspects, language, result limit, and literature/full-text budgets. Comprehensive analysis is the default: literature coverage is bounded by the literature budget, not by a fixed small ceiling.
   For one explicit gene, pass user PDFs with repeatable `--pdf` options. User PDFs are screened first, while web discovery still runs and open full text is retrieved when available.
6. Poll the durable workflow until it reaches a terminal state. Do not infer completion from elapsed time.
7. Record the archived report attachment, proposal status, ChangeSet ID, and failure reason for each gene.
8. Direct the human reviewer to CodeXomics **Annotation Review Center** for individual or batch review.

Use the production runner for repeatable work:

```bash
python3 scripts/run_annotation_workflow.py \
  --genome /absolute/path/to/genome.gbk \
  --gene lysC \
  --pdf /absolute/path/to/primary-study.pdf \
  --full-text-policy require \
  --user-prompt "Refine function, regulation, pathway role, complexes, and phenotype with precise citations"
```

The runner supports exactly one selector per invocation:

- `--gene lysC` for one exact gene annotation feature.
- Repeat `--pdf /absolute/paper.pdf` up to eight times for a single `--gene`. The runner validates PDF magic, size, and SHA-256 before CodeXomics uploads and registers each file as a gene-scoped DGR research-source attachment.
- Use `--full-text-policy prefer` (default), `require`, or `abstract-allowed`. `require` returns a failed run outcome when the archived report has no verified full-text source; it never promotes abstract-only evidence.
- `--genes lysC,thrB,talB` for an explicit list.
- `--gene-file /absolute/path/genes.txt` for newline, comma, or tab-separated identifiers.
- `--daily-count 10` for a deterministic batch ranked by lowest annotation quality first.
- Add `--maximum-quality-score 70` to set the low-quality threshold, `--feature-types CDS,tRNA,rRNA,ncRNA,gene` to restrict types, or `--selection-policy coordinate` to retain coordinate-order coverage.
- Use `--selection-policy random` for an unbiased sample of the eligible pool. It still honours `--maximum-quality-score`, so pass `--maximum-quality-score 100` to sample the whole genome rather than only poorly annotated features. Seed it with `--random-seed` or a stable `--run-id`; otherwise the seed comes from the genome and the UTC date and a rerun after midnight samples a different batch. The resolved seed is recorded in the run summary, so any batch can be reproduced exactly.
- `--max-result N` (1..100, default 10) caps results per search query; `--literature-budget N` (10..2000, default 300) bounds the total PubMed abstracts DGR reads per gene; `--full-text-budget N` (1..100, default 25) bounds the open-access full texts attempted. Comprehensive runs use the budgets, not the per-query cap, to control depth. These require a matching DGR version; older DGR builds reject values above their documented limits.
- Daily mode uses the CodeXomics per-genome research ledger to exclude active and durably completed DGR targets across agents and state directories. Use `--research-refresh-days N` for scheduled refresh or `--include-researched` only for an intentional repeat campaign.

Run with `--dry-run` before a new batch policy. The runner never approves or applies ChangeSets.

## Report the run

Read [references/reporting.md](references/reporting.md) before promising any statistic.

Two different models bill for one annotated gene: the **research** model DGR runs internally, and the **agent** model driving this skill. They are usually different models at different prices, so they are counted in separate ledgers and never summed into one number.

1. Give the batch a stable `--run-id` and write both artifacts:

   ```bash
   python3 scripts/run_annotation_workflow.py --genome /absolute/genome.gbk --daily-count 10 \
     --run-id daily-2026-08-20 \
     --pricing-file /protected/path/pricing.json \
     --agent-usage-file /protected/path/agent-usage.jsonl \
     --dgr-thinking-model "$MCP_THINKING_MODEL" --dgr-task-model "$MCP_TASK_MODEL" \
     --output /reports/2026-08-20-summary.json --metrics-output /reports/2026-08-20-metrics.json
   ```

2. **Record your own token usage.** Nothing in CodeXomics or DGR can observe the orchestrating agent's spend. Append one JSON Lines record per gene to the `--agent-usage-file` with the same `runId`, your model id, and the prompt/completion tokens your runtime reports. If your runtime does not expose usage, say so; do not estimate it.
3. Research-model tokens come from DGR's provider-reported `llmUsage`. The runner reads it from the CodeXomics workflow record, and falls back to a **read-only** DGR task-status lookup when that build does not surface it. This fallback never starts, cancels, or mutates research.
4. Cost needs a price list. Copy `references/pricing.template.json`, fill in what the provider actually charges, and pass `--pricing-file`. Without it, tokens are reported and cost is `unavailable` — never zero.
5. Generate the report:

   ```bash
   python3 scripts/generate_run_report.py /reports/2026-08-20-summary.json \
     --markdown-output /reports/2026-08-20-report.md --json-output /reports/2026-08-20-report.json
   ```

6. Read the report's data-gaps section back to the user. A missing statistic is a finding, not something to quietly omit.

DGR serves repeat requests from a semantic cache and replays the original run's token counts. Those are reported as `replayedTotalTokens` and excluded from cost.

## Recurring daily jobs

Read [references/automation.md](references/automation.md) before creating a schedule. Ask for the daily time, timezone, genome path, count, selection policy, and research prompt if the user has not supplied them. Do not install or modify a schedule without explicit authorization.

`scripts/install_schedule.py` prints a wrapper script and a launchd/systemd/cron unit that runs the batch and then the run report. It writes and activates nothing unless `--install` is passed, and keeps credentials in an environment file rather than on the command line.

Keep scheduling separate from curation logic: the scheduler invokes `run_annotation_workflow.py --daily-count N`; CodeXomics owns research coverage in its genome sidecar, while the runner state remains a local execution checkpoint. The runner also uses a per-genome lock and a start-time repeat guard. Prefer sequential DGR submissions unless capacity was explicitly validated.

## Configuration and recovery

- Read [references/configuration.md](references/configuration.md) when setting credentials, provider models, SearXNG, task storage, or scoped permissions.
- Read [references/reporting.md](references/reporting.md) when setting up token, cost, runtime, or evidence statistics.
- Read [references/troubleshooting.md](references/troubleshooting.md) when an endpoint is unavailable, DGR finishes implausibly fast, a task stalls, a target is ambiguous, a ChangeSet is not created, or a run statistic is unavailable.
- Preserve DGR's task ledger and the CodeXomics sidecar. Do not delete either to “unstick” a run.
- Treat `completed` research with `changeSetStatus: validation_failed` as a recoverable proposal-processing failure. Preserve the task ID and rerun the same explicit target after correcting the validator or configuration.

## Completion contract

Return a concise run summary containing:

- genome path and CodeXomics window;
- each exact feature identifier, type, resolved locus, and pre-research quality score when available;
- DGR task status and task ID;
- archived full-report attachment ID/file name when present;
- annotation proposal status;
- ChangeSet ID and review status;
- Genome Annotation Note summary: whether the report carries a mutation-ready citation-bound `/note` text or only an informational summary, and its included/cited fact counts when available;
- failures or skipped reasons;
- explicit statement that no ChangeSet was approved or applied automatically.

For a batch run, also return the statistics block:

- total tokens across every step, split by role (research model versus agent model) and by model id, with cached-input and cache-replayed tokens shown separately;
- actual cost per model and in total, in the price list's currency, or `unavailable` with the reason;
- runtime per gene and for the batch, separating DGR research time from wall clock;
- references surveyed, references retained, and references newly added to annotations;
- full texts adopted as verified evidence, and the evidence spans bound to facts;
- newly incorporated information: qualifier fields updated per gene and citation-bound facts included in Notes;
- every data gap, naming the statistic and why it is unavailable.

If the user requests the full DGR report or proposal, return the stored artifact or structured result without truncating it silently. For large JSON, point to the CodeXomics attachment/JSON viewer and optionally save an explicit output file requested by the user.
