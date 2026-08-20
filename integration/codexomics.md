# CodeXomics changes

Two changes, both additive. Neither is required for curation to work; both are
required for a run to account for itself without a second read of the DGR task.

Apply the [DGR changes](deep-gene-research.md) first — until DGR stops
projecting the accounting fields away, `remoteResult.metadata.llmUsage` is
`undefined` on the polling path and these changes have nothing to persist.

---

## 1. Persist token usage and research time on the workflow record

**File:** `src/renderer/modules/chat/services/AnnotationResearchWorkflowService.js`
**Method:** `getAnnotationResearchWorkflow`

The method already projects three DGR result fields onto the durable workflow:

```js
const remoteResult = this._isPlainRecord(status.result) ? status.result : null;
if (remoteResult) {
  workflow.literatureCoverage = this._clone(
    remoteResult?.metadata?.searchDiagnostics?.literatureCoverage ?? null
  );
  workflow.llmSynthesis = this._clone(remoteResult?.metadata?.llmSynthesis ?? null);
  workflow.annotationNote = this._clone(remoteResult?.annotationNote ?? null);
}
```

Add the accounting fields alongside them:

```js
const remoteResult = this._isPlainRecord(status.result) ? status.result : null;
if (remoteResult) {
  workflow.literatureCoverage = this._clone(
    remoteResult?.metadata?.searchDiagnostics?.literatureCoverage ?? null
  );
  workflow.llmSynthesis = this._clone(remoteResult?.metadata?.llmSynthesis ?? null);
  workflow.annotationNote = this._clone(remoteResult?.annotationNote ?? null);
  // Provider-reported token usage and elapsed research time for this task.
  // Without these on the durable record every consumer has to re-read the
  // full DGR result to answer what a gene cost.
  workflow.llmUsage = this._clone(remoteResult?.metadata?.llmUsage ?? null);
  workflow.researchTimeMs = Number.isFinite(Number(remoteResult?.metadata?.researchTime))
    ? Number(remoteResult.metadata.researchTime)
    : null;
  // A DGR semantic-cache replay returns the original run's usage verbatim.
  workflow.cacheReplay = remoteResult?.metadata?.cacheReplay === true;
}
```

The guard is deliberate: these are only written when the remote status
actually carries a result, so an intermediate poll cannot wipe values already
persisted.

### Effect

`get_annotation_research_workflow` starts returning `workflow.llmUsage`,
`workflow.researchTimeMs`, and `workflow.cacheReplay`. The skill prefers these
over its read-only DGR fallback and reports
`tokenUsageSource: "codexomics-workflow"`. The ChatBox path, which has no
runner and therefore no fallback, gets token accounting for the first time.

### Note on `resultMode`

`src/renderer/modules/MCPServerManager.js` (`checkTaskStatus`) polls DGR with
`resultMode: 'annotation'`. Keep it. The bounded projection is the right choice
for a polling loop; the DGR change makes that projection retain the accounting
fields. Switching this call to `'full'` would pull a multi-megabyte report on
every poll and is not the fix.

---

## 2. Record usage and timing in the archived report attachment

**File:** `src/main/dgr-artifact-storage.js`
**Function:** `archiveDgrTaskResult`

The archived attachment is the durable audit artifact for a gene, and its
`summary` is what reviewers and the run report read without opening the full
JSON. It already carries literature counts, full-text counts, coverage, and the
Note, but nothing about what the run consumed. This read already uses
`resultMode: 'full'`, so the values are present in `task.result` and only need
to be summarised.

Add to the returned `summary` object:

```js
    summary: {
      title: ...,
      // ... existing fields ...
      literatureCoverage: task.result?.metadata?.searchDiagnostics?.literatureCoverage ?? null,
      llmSynthesis: task.result?.metadata?.llmSynthesis ?? null,
      annotationNote: task.result?.annotationNote ?? null,
      // What this gene's research consumed, so the archived artifact can
      // answer a cost question on its own.
      llmUsage: task.result?.metadata?.llmUsage ?? null,
      researchTimeMs: Number.isFinite(Number(task.result?.metadata?.researchTime))
        ? Number(task.result.metadata.researchTime)
        : null,
      cacheReplay: task.result?.metadata?.cacheReplay === true,
    },
```

`llmUsage` is a small counter object (a handful of integers per phase and
model), so this does not meaningfully affect `MAX_DGR_ARTIFACT_BYTES`.

### Effect

The skill's per-gene fallback chain becomes: workflow record → archived
attachment summary → read-only DGR lookup. A batch whose reports were archived
can be re-costed later from the attachments alone, with no live services.

---

## Optional: expose research history timing

`list_annotation_research_history` returns `createdAt`, `updatedAt`, and
`completedAt` per entry. Adding `researchTimeMs`, `llmUsage`, and
`changeSetId` to those entries would let a reviewer produce a monthly cost
rollup from one call instead of replaying every daily run summary. Not needed
for the daily report; worth it if cost reporting moves into the CodeXomics UI.

---

## Verification

After both changes:

```bash
# workflow record now carries usage
python3 curate-genome-annotations/scripts/mcp_http.py --endpoint "$CODEXOMICS_MCP_URL" --token "$CODEXOMICS_MCP_API_KEY"
# then call get_annotation_research_workflow for a completed task and look for llmUsage

# and the runner no longer needs its fallback
python3 curate-genome-annotations/scripts/run_annotation_workflow.py \
  --genome /absolute/genome.gbk --gene lysC \
  --dgr-telemetry off --pricing-file /protected/pricing.json \
  --output /tmp/run.json
```

The result's `tokenUsageSource` should read `codexomics-workflow` and the
metrics block should contain a complete cost.
