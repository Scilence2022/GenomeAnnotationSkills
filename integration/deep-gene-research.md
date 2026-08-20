# Deep Gene Research changes

Four changes. The first makes token accounting possible at all; the rest make
the reported statistics correct.

---

## 1. Keep the accounting fields in the `annotation` projection

**File:** `src/services/task-result-projection.ts`

`projectTaskResult(result, 'annotation')` currently keeps four metadata fields
and drops everything else. CodeXomics polls DGR with `resultMode: 'annotation'`
(`src/renderer/modules/MCPServerManager.js`, `checkTaskStatus`), so on that path
the following never reach any consumer:

- `metadata.llmUsage` — the provider-reported token counts;
- `metadata.llmSynthesis` — synthesis counters;
- `metadata.literatureMetrics` — the canonical retained-paper counts;
- `metadata.searchDiagnostics.literatureCoverage` — surveyed/retained coverage;
- the top-level `annotationNote` — the mutation-ready citation-bound `/note`.

This is also a live bug independent of token accounting.
`AnnotationResearchWorkflowService.getAnnotationResearchWorkflow` reads exactly
these three:

```js
workflow.literatureCoverage = this._clone(remoteResult?.metadata?.searchDiagnostics?.literatureCoverage ?? null);
workflow.llmSynthesis = this._clone(remoteResult?.metadata?.llmSynthesis ?? null);
workflow.annotationNote = this._clone(remoteResult?.annotationNote ?? null);
```

under a comment saying they are projected "so external agents (and the Skills
runner) can read them without re-fetching the full task result." They are
always `null`. The runner only sees these values because it falls back to the
archived report attachment's `summary`, which is built from a separate
`resultMode: 'full'` read.

### Change

```ts
export function projectTaskResult(result: any, mode: TaskResultMode): any {
  if (mode === 'full' || !result || typeof result !== 'object') return result;

  const diagnostics = result.metadata?.searchDiagnostics;
  const metadata = result.metadata && typeof result.metadata === 'object'
    ? {
        researchTime: result.metadata.researchTime,
        dataSources: result.metadata.dataSources,
        sourceCoverage: result.metadata.sourceCoverage,
        confidence: result.metadata.confidence,
        // Accounting and coverage fields. These are small, bounded counters;
        // consumers need them to report tokens, cost, and literature coverage
        // without re-reading a multi-megabyte report.
        llmUsage: result.metadata.llmUsage,
        llmSynthesis: result.metadata.llmSynthesis,
        literatureMetrics: result.metadata.literatureMetrics,
        cacheReplay: result.metadata.cacheReplay,
        searchDiagnostics: diagnostics?.literatureCoverage
          ? { literatureCoverage: diagnostics.literatureCoverage }
          : undefined,
      }
    : undefined;

  return {
    annotationProposal: result.annotationProposal,
    // The Note is bounded and is the annotation payload reviewers act on.
    annotationNote: result.annotationNote,
    artifactUri: result.artifactUri,
    download: result.download,
    title: result.title,
    metadata,
    qualityMetrics: result.qualityMetrics ?? result.geneResearch?.qualityMetrics,
  };
}
```

Only the `literatureCoverage` sub-object of `searchDiagnostics` is carried
through — `attempts` can be large and is not needed for a projection whose
purpose is to stay bounded.

### Test

Extend `src/services/task-result-projection.test.ts` to assert that a result
carrying `metadata.llmUsage`, `metadata.searchDiagnostics.literatureCoverage`,
and `annotationNote` still carries them after the `annotation` projection, and
that `searchDiagnostics.attempts` does not survive.

---

## 2. Tag token usage with the model that produced it

**File:** `src/utils/deep-research/index.ts`

`recordLlmUsage(phase, usage)` aggregates by phase only. DGR runs two configured
models — `getThinkingModel()` for planning and synthesis, `getTaskModel()` for
per-source extraction — and they can differ. Without the model id, a consumer
cannot price the run: it has to infer the model from a phase-name map, which
breaks silently whenever a phase is added or re-pointed.

The AI SDK also reports provider cache detail that the current recorder drops.
`@ai-sdk/deepseek` surfaces `providerMetadata.deepseek.promptCacheHitTokens`;
cached input is billed at roughly a tenth of the fresh input rate, so ignoring
it overstates cost.

### Change

```ts
export interface LlmPhaseUsage {
  calls: number;
  promptTokens: number;
  cachedPromptTokens: number;
  completionTokens: number;
  totalTokens: number;
}

export interface LlmModelUsage extends LlmPhaseUsage {
  phases: Record<string, LlmPhaseUsage>;
}

export interface LlmUsageSummary extends LlmPhaseUsage {
  phases: Record<string, LlmPhaseUsage>;
  /** Per-model breakdown, keyed by the provider's model id. */
  models: Record<string, LlmModelUsage>;
  provider?: string;
}
```

```ts
  private llmUsageByPhase = new Map<string, LlmPhaseUsage>();
  private llmUsageByModel = new Map<string, LlmModelUsage>();

  /** Cached input tokens as the provider reported them, if it did. */
  private cachedPromptTokens(providerMetadata?: any): number {
    const candidates = [
      providerMetadata?.deepseek?.promptCacheHitTokens,
      providerMetadata?.openai?.cachedPromptTokens,
      providerMetadata?.anthropic?.cacheReadInputTokens,
    ];
    for (const value of candidates) {
      if (typeof value === 'number' && Number.isFinite(value) && value >= 0) return value;
    }
    return 0;
  }

  /**
   * Accumulate one LLM call's provider-reported token usage under its phase
   * and under the model that produced it. The model id is what a cost report
   * prices against; a phase-to-model guess breaks whenever phases change.
   */
  protected recordLlmUsage(
    phase: string,
    usage: TokenUsageLike,
    model?: string,
    providerMetadata?: any,
  ) {
    if (!usage) return;
    const cached = this.cachedPromptTokens(providerMetadata);
    const add = (target: LlmPhaseUsage) => {
      target.calls += 1;
      target.promptTokens += usage.promptTokens ?? 0;
      target.cachedPromptTokens += cached;
      target.completionTokens += usage.completionTokens ?? 0;
      target.totalTokens += usage.totalTokens ?? 0;
    };
    const blank = (): LlmPhaseUsage => ({
      calls: 0, promptTokens: 0, cachedPromptTokens: 0, completionTokens: 0, totalTokens: 0,
    });

    const phaseUsage = this.llmUsageByPhase.get(phase) || blank();
    add(phaseUsage);
    this.llmUsageByPhase.set(phase, phaseUsage);

    const modelId = String(model || '').trim() || 'unknown';
    const modelUsage = this.llmUsageByModel.get(modelId) || { ...blank(), phases: {} };
    add(modelUsage);
    const modelPhase = modelUsage.phases[phase] || blank();
    add(modelPhase);
    modelUsage.phases[phase] = modelPhase;
    this.llmUsageByModel.set(modelId, modelUsage);
  }
```

`getLlmUsage()` gains `models` (from `llmUsageByModel`) and `provider`
(`this.options.AIProvider.provider`), and sums `cachedPromptTokens` alongside
the existing fields.

### Call sites

Each `recordLlmUsage` call passes the model it used. Current mapping:

| Line | Phase | Model |
| --- | --- | --- |
| ~205 | `report-plan` | `thinkingModel` |
| ~230 | `serp-query` | `thinkingModel` |
| ~376 | `search-task` | `taskModel` |
| ~505 | `final-report` | `thinkingModel` |
| ~997 | `gene-llm-queries` | `thinkingModel` |
| ~1078 | `gene-llm-learnings` | `taskModel` |
| ~1173 | `gene-llm-report` | `thinkingModel` |

For example:

```ts
// streaming call
} else if (part.type === "finish") {
  this.recordLlmUsage("gene-llm-report", part.usage, this.options.AIProvider.thinkingModel, part.providerMetadata);
}

// non-streaming call
const { text, usage, providerMetadata } = await generateText({ model: await this.getTaskModel(), ... });
this.recordLlmUsage("gene-llm-learnings", usage, this.options.AIProvider.taskModel, providerMetadata);
```

The skill reads `models` when present and falls back to the phase map
otherwise, so this change is backward compatible in both directions.

### Test

`src/utils/deep-research/llm-usage.test.ts` already covers phase aggregation.
Add cases for: two models kept separate; per-model phase breakdown; a missing
model id landing under `unknown`; and `promptCacheHitTokens` being recorded
without inflating `promptTokens`.

---

## 3. Mark cache replays

**File:** `src/services/task-queue.ts`

On a cache hit the queue returns the stored result verbatim:

```ts
const policyCompliantCachedResult = enforceTaskMediaPolicy(task.parameters, cachedResult);
const cachedResultWithProposal = this.ensureCodeXomicsAnnotationProposal(task, policyCompliantCachedResult);
const taskResult = this.prepareResultForTask(task, cachedResultWithProposal);
```

That result still carries the original run's `metadata.llmUsage` and
`metadata.researchTime`. Nothing distinguishes it from work actually performed,
so a repeat run reports full token spend and full research time for an
inference that never happened. The only current signal is the transient
progress step `cache-hit`, which is not part of the result.

### Change

```ts
const policyCompliantCachedResult = enforceTaskMediaPolicy(task.parameters, cachedResult);
const cachedResultWithProposal = this.ensureCodeXomicsAnnotationProposal(task, policyCompliantCachedResult);
// The replayed usage and researchTime describe the original run. Mark them so
// consumers count the tokens without billing for them a second time.
const replayed = {
  ...cachedResultWithProposal,
  metadata: {
    ...(cachedResultWithProposal?.metadata || {}),
    cacheReplay: true,
    cacheReplayedAt: new Date().toISOString(),
  },
};
const taskResult = this.prepareResultForTask(task, replayed);
```

The skill already treats `metadata.cacheReplay` (and a terminal `cache-hit`
step) as non-billable and reports those tokens under `replayedTotalTokens`.

### Test

Assert that a cache-hit task's result carries `metadata.cacheReplay === true`
and that a freshly computed result does not.

---

## 4. Fix `pubmedTotalMatchCount`

**File:** `src/utils/gene-research/index.ts` (~line 412)

This is the field a run report uses for "references surveyed". It is currently
computed as:

```ts
pubmedTotalMatchCount: Math.max(
  0,
  ...this.searchAttempts.map(attempt => attempt.totalMatchCount ?? 0),
) || null,
```

Two defects:

1. **`Math.max` reports one query, not the survey.** A gene run issues on the
   order of sixteen queries; this returns the single largest query's match
   count and calls it the total. The surveyed set is systematically
   under-reported, and the number moves with query phrasing rather than with
   coverage.
2. **`|| null` erases a real zero.** A gene with genuinely no PubMed matches is
   indistinguishable from a run where no attempt reported a count. Downstream
   consumers must treat `null` as "unmeasured", so a true negative result is
   reported as a data gap.

Measured on the current task ledger (169 completed tasks): 67 carry a count, 29
report `null`, and 73 predate the coverage block entirely.

### Change

Report the honest quantities separately rather than collapsing them into one
ambiguous number. PubMed match counts across different queries overlap, so a
sum is an upper bound, not a distinct-record count — label it as such.

```ts
const matchCounts = this.searchAttempts
  .map(attempt => attempt.totalMatchCount)
  .filter((value): value is number => typeof value === 'number' && Number.isFinite(value) && value >= 0);

literatureCoverage: {
  literatureBudget: this.literatureBudget,
  // Null only when no search attempt reported a count at all. Zero is a
  // real result and must stay distinguishable from "not measured".
  pubmedTotalMatchCount: matchCounts.length ? Math.max(...matchCounts) : null,
  // Queries overlap, so this is an upper bound on distinct records seen,
  // not a deduplicated count. Named so no consumer mistakes it for one.
  pubmedMatchCountUpperBound: matchCounts.length
    ? matchCounts.reduce((total, value) => total + value, 0)
    : null,
  pubmedQueriesWithCounts: matchCounts.length,
  pubmedQueryCount: this.searchAttempts.length,
  retainedAbstractCount: coverage.literatureSourceCount,
  linkedBibliographyRequested: coverage.linkedBibliographyRequested,
  linkedBibliographyRetrieved: coverage.linkedBibliographyRetrieved,
  linkedBibliographyComplete: coverage.linkedBibliographyComplete,
},
```

`pubmedTotalMatchCount` keeps its existing meaning (largest single query) so
current consumers do not silently change behaviour, but it now reports `0`
honestly instead of `null`.

### Test

Assert that: no attempts with counts yields `null`; attempts all reporting `0`
yields `0`, not `null`; and the upper bound is the sum while
`pubmedTotalMatchCount` is the maximum.
