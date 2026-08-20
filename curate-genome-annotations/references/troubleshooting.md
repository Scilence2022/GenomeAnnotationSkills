# Troubleshooting

## CodeXomics MCP reports missing credentials

Set a master key or scoped API keys, or explicitly enable the isolated local bypass. For automated research use a scoped research key. Do not solve this by disabling authentication on a reachable shared service.

## Endpoint is reachable but tools are missing

- Confirm CodeXomics is in tools mode, not agent mode.
- Start the app and MCP together with `npm run start-with-mcp`.
- Confirm the Electron client is connected to the MCP listener.
- Re-run `scripts/start_services.py --check-only` and inspect the missing-tool list.

## `SEARXNG_API_BASE_URL is empty`

Set `MCP_SEARCH_PROVIDER=searxng` and `SEARXNG_API_BASE_URL` to the reachable SearXNG base URL before starting DGR. Restart DGR so it receives the environment. Test SearXNG's JSON response independently.

## DGR completes implausibly fast

Inspect the full report and task events, not only terminal status. Verify:

- a real search provider is configured and returning results;
- model provider credentials and models are valid;
- retained sources match organism, gene/locus/protein identity, and functional context;
- query count, fetched records, deduplication, and rejection reasons are non-zero and plausible;
- PubMed term collisions such as `lysC` versus “lysozyme C” are rejected;
- task cache was not reused unexpectedly (`--force-refresh` only when scientifically justified).

Treat a completed task with insufficient evidence as a failed curation outcome even if the transport succeeded.

## Target is missing or ambiguous

Use locus tag, protein ID, or an unambiguous gene symbol plus chromosome. Do not use coordinates copied from a different genome build. CodeXomics accepts supported coding and non-coding gene features, prefers CDS at co-located duplicate records, and still rejects multiple distinct matches.

## DGR completed but no ChangeSet exists

Check:

- caller has `annotation:propose` as well as read/research permissions;
- `workflow.proposalStatus` and `workflow.proposalReason`;
- DGR returned evidence-backed claims;
- current annotation and target binding still match;
- report archive/citation validation succeeded.

Do not manufacture a minimal ChangeSet to hide missing evidence.

If `workflow.status` is `completed` while `changeSetStatus` is `validation_failed`, DGR and report archival succeeded but CodeXomics rejected proposal materialization. Inspect `proposalMaterializationError` or `proposalReason`. Keep the task and attachment; after correcting the validator or configuration, rerun the same explicit target so CodeXomics retries the stored proposal.

The runner persists a started task failure with `status: failed`, `retryable`, `failureCount`, and the original task ID. Daily selection treats that failed record as handled to prevent an unattended loop. Retry it explicitly with `--gene`, `--genes`, or `--gene-file`; the stable idempotency key resumes the existing task instead of starting unrelated research.

## ChangeSet is stale

The target feature or annotation revision changed after proposal creation. Start a new research workflow against the live annotation. Do not apply the stale proposal or edit its stored hashes.

## DGR ledger problems

Run one DGR process per task file. On corruption, DGR may quarantine the ledger with a `.corrupt.*` suffix and lock further work. Preserve the files, inspect logs and filesystem permissions, then recover deliberately. Never delete the ledger blindly; it is part of the research audit trail.

## Port already in use

Inspect the owning process before terminating anything. If it is the intended healthy service, reuse it. Otherwise stop it through its supervisor or configure a different URL/port. The bundled startup script does not kill port owners.

## Token counts are missing from the run report

Work through the chain in order; each step names what to check.

1. **Is a price list configured?** Without `--pricing-file` or
   `GENOME_ANNOTATION_PRICING_FILE`, tokens are still counted but cost is
   `unavailable`. That is the intended behaviour, not a failure.
2. **Did DGR report usage at all?** Query the task directly:

   ```bash
   python3 scripts/mcp_http.py --endpoint "$DGR_MCP_URL" --token "$DGR_MCP_TOKEN"
   ```

   then read the task with `get-task-status` and `resultMode: "full"`. If
   `result.metadata.llmUsage` is absent, the DGR build predates usage
   reporting; upgrade DGR. If it is present, continue.
3. **Does the CodeXomics workflow record carry it?** Call
   `get_annotation_research_workflow` and look for `workflow.llmUsage`. Several
   CodeXomics/DGR builds poll DGR with `resultMode: "annotation"`, and that
   projection keeps only `researchTime`, `dataSources`, `sourceCoverage`, and
   `confidence` — it drops `llmUsage`, `llmSynthesis`, `literatureMetrics`,
   `searchDiagnostics`, and the top-level `annotationNote`. On such a build the
   workflow record cannot show token counts no matter how the runner asks.
4. **Is the read-only DGR fallback enabled?** It is the default. Confirm
   `DGR_MCP_URL` and `DGR_MCP_TOKEN` are exported for the runner's process —
   a scheduled job often has a narrower environment than an interactive shell.
   Run with `--dgr-telemetry require` to turn a silent gap into a hard failure
   while diagnosing.
5. **Are the model ids known?** Tokens filed under `unknown:dgr-thinking` or
   `unknown:dgr-task` mean DGR reported usage per phase without a model id and
   neither `--dgr-thinking-model`/`--dgr-task-model` nor
   `$MCP_THINKING_MODEL`/`$MCP_TASK_MODEL` was set.

## Agent-model tokens are always unavailable

Only the orchestrating agent can observe its own spend; CodeXomics and DGR
never see it. Set `--agent-usage-file` (or
`GENOME_ANNOTATION_AGENT_USAGE_FILE`) and have the agent append one record per
gene with the run's `--run-id`. Records whose `runId` does not match the run
are ignored by design, so a mismatched or absent run id looks exactly like a
missing sidecar. See [reporting.md](reporting.md) for the record format.

## Token counts look implausibly high for a fast run

Check `replayedTotalTokens` and the per-gene `cacheReplay` flag. DGR serves a
matching request from its semantic cache and replays the original run's result
verbatim, including its token counts and `researchTime`. Those tokens are
reported but were not charged again, so they are excluded from cost. A batch
that is mostly replays means the selection is re-covering researched targets;
review `--research-refresh-days` and the coverage ledger rather than treating
it as a cheap day.

## Newly-added-reference or updated-field counts are unavailable

These are derived from the created ChangeSet. Confirm the CodeXomics build
exposes `get_annotation_changeset` and that the research key has
`annotation:read`. Without the before/after preview from
`list_annotation_changesets`, every proposed reference is reported as new
because there is nothing to subtract the annotation's existing citations
against.

## Large full report

CodeXomics stores the verified DGR JSON as a genome-scoped gene attachment and exposes it through the Resources/JSON viewer. Return the attachment identifier and file name. Avoid pasting a multi-megabyte report into chat unless the user explicitly requests it.
