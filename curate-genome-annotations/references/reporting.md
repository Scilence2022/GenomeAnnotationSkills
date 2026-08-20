# Run statistics and reporting

A daily batch is expected to report what it consumed and what it produced:
tokens per model, runtime per gene and in total, references surveyed and newly
added, full texts adopted, newly incorporated information, and the actual cost.

Every number in a run report comes from something a service reported. Nothing
is estimated from character counts or elapsed time. A statistic that no service
reported appears as `unavailable` with the reason, so a gap in instrumentation
never masquerades as a measurement.

## Who reports what

| Statistic | Source | Notes |
| --- | --- | --- |
| Research-model tokens | DGR `metadata.llmUsage`, via CodeXomics or a read-only DGR status lookup | Provider-reported per LLM phase |
| Agent-model tokens | The agent usage sidecar | Only the orchestrating agent can observe its own spend |
| DGR research time | DGR `metadata.researchTime` | Milliseconds DGR measured for the run |
| Per-gene wall clock | The runner | Includes CodeXomics resolution, polling, and archival |
| References surveyed | DGR `literatureCoverage.pubmedTotalMatchCount` | Everything PubMed matched, before budgeting |
| References retained | `literatureCoverage.retainedAbstractCount` / `literatureMetrics.totalPapers` | Abstracts fed to synthesis |
| Full texts adopted | Archived report `summary.fullTextSourceCount` / `fullTextFindingCount` | Verified spans only |
| Newly added references | ChangeSet `db_xref` / evidence operations, minus references the annotation already carried | Needs `get_annotation_changeset` |
| Newly incorporated information | ChangeSet qualifier operations and the Genome Annotation Note coverage | |
| Cost | The operator's price list × reported tokens | Never guessed |

## Two models, two ledgers

DGR and the orchestrating agent are usually different models at different
prices — for example DGR on a `pro` tier and the agent on a `flash` tier. The
runner keeps them in separate ledgers under the roles `research` and `agent`,
and reports them on separate rows. They are never summed into one
undifferentiated token count.

Token attribution needs the model id. Newer DGR builds report it with each
usage record. Older builds report only the phase, so the runner maps each phase
to DGR's thinking/task model slot and applies the configured model id:

```bash
python3 scripts/run_annotation_workflow.py ... \
  --dgr-thinking-model deepseek-v4-pro \
  --dgr-task-model deepseek-v4-pro
```

These default to `$MCP_THINKING_MODEL` and `$MCP_TASK_MODEL`. Without them,
tokens are still counted but land under `unknown:dgr-thinking` /
`unknown:dgr-task` and cost is reported as unavailable.

## Price list

Cost is computed from a price list you supply. Copy
[`pricing.template.json`](pricing.template.json) outside the repository, fill in
what your provider actually charges, and point the runner at it:

```bash
export GENOME_ANNOTATION_PRICING_FILE=/protected/path/pricing.json
```

The template ships with `null` prices on purpose. An unfilled file fails at
startup, before any research is submitted, rather than reporting a confident
cost of zero. A model with no entry is listed under `unpricedModels` and its
tokens are excluded from the cost total instead of being priced at zero.

Set `cachedInputPerMillion` when the provider discounts prompt-cache hits.
Cached input tokens are billed at that rate and the rest at the full input
rate.

## Agent usage sidecar

Nothing in CodeXomics or DGR can see the orchestrating agent's own token spend.
The agent must record it. The sidecar is JSON Lines, one record per LLM turn:

```json
{"schema":"genome-annotation-skills.agent-usage.v1","runId":"genome-annotation-daily-2026-08-20","gene":"lysC","model":"deepseek-v4-flash","phase":"orchestration","calls":1,"promptTokens":8123,"cachedPromptTokens":2048,"completionTokens":914}
```

- `runId` must match the runner's `--run-id`. Records with a different run id
  are ignored, so several runs can share one file.
- `gene` is optional; supply it to get per-gene agent costs.
- `cachedPromptTokens` is optional and must not exceed `promptTokens`.
- Set `billable: false` for a turn the provider did not charge for.

Point the runner at it with `--agent-usage-file` or
`GENOME_ANNOTATION_AGENT_USAGE_FILE`. Without it, the report states plainly
that agent-model tokens and cost are unavailable.

Agents that expose their own usage totals should append one record per gene.
Agents that cannot report usage should say so; do not fabricate the numbers.

## Where research telemetry comes from

The runner prefers `workflow.llmUsage` on the CodeXomics workflow record. Some
CodeXomics/DGR builds project the DGR task result down to the annotation
proposal, which drops `metadata.llmUsage`, `metadata.researchTime`, and the
coverage fields. When the workflow record has no usage block, the runner falls
back to a **read-only** DGR status lookup for the task CodeXomics already
created:

```bash
--dgr-telemetry auto      # default: use the fallback when needed
--dgr-telemetry require   # fail the gene when telemetry cannot be read
--dgr-telemetry off       # never contact DGR; accept unavailable token counts
```

This is not an alternative orchestration path. It never starts, cancels, or
mutates research; it re-reads a task by id. Research is still started and
polled exclusively through CodeXomics.

The fallback exists because of a specific upstream gap, not by design. The
repository's `integration/` directory documents the CodeXomics and DGR changes
that make the workflow record self-sufficient; after they land, a run reports
`tokenUsageSource: "codexomics-workflow"` and `--dgr-telemetry off` still
produces complete statistics.

## Cached research

DGR serves a matching request from its semantic cache and replays the original
run's result verbatim, including its token counts and research time. Those
tokens were reported but not charged again. The runner marks them
`billable: false`, reports them under `replayedTotalTokens`, and excludes them
from cost. Treat a batch with many replays as a coverage question, not a
cheap day.

## Generating the report

```bash
python3 scripts/generate_run_report.py \
  /reports/2026-08-20-summary.json \
  --title "ECOLI daily annotation run 2026-08-20" \
  --markdown-output /reports/2026-08-20-report.md \
  --json-output /reports/2026-08-20-report.json
```

Several summaries can be combined into one report — useful when a daily batch
was split across retries. Add `--fail-on-gaps` when a monitored job should
alert on missing statistics.

The report has seven sections: token consumption by model, runtime, literature
coverage, newly incorporated information, per-gene detail, data gaps, and the
review handoff. It always states that nothing was approved or applied.

## Reading the data-gaps section

Common entries and what to do:

- *No pricing file configured* — set `GENOME_ANNOTATION_PRICING_FILE`.
- *No agent-side token usage was recorded* — the agent did not write the
  sidecar. Token totals cover DGR only.
- *no LLM token usage was reported by CodeXomics or DGR* — the DGR build
  predates usage reporting, or telemetry is `off` and CodeXomics does not
  surface `llmUsage`. See [troubleshooting.md](troubleshooting.md).
- *has no model attribution* — pass `--dgr-thinking-model` / `--dgr-task-model`.
- *does not expose get_annotation_changeset* — newly-added-reference and
  updated-field counts are unavailable on this CodeXomics build.
- *DGR served a cached result* — expected after a repeat; those tokens are not
  billed again.
