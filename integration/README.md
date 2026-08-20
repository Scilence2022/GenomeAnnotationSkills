# Upstream changes for CodeXomics and Deep Gene Research

The skill's run reporting works against current CodeXomics and DGR builds: when
the CodeXomics workflow record carries no token usage, the runner falls back to
a read-only DGR task-status lookup. That fallback is a workaround, not the
design.

These documents describe the upstream changes that remove the workaround and
close gaps the skill cannot fix from outside:

- [`deep-gene-research.md`](deep-gene-research.md) — stop projecting away the
  accounting fields, tag token usage with the model that produced it, and mark
  cache replays.
- [`codexomics.md`](codexomics.md) — persist DGR's reported token usage and
  research time on the durable workflow record and in the archived report
  attachment.

## Why it matters

| Problem | Symptom today | Fixed by |
| --- | --- | --- |
| `resultMode: "annotation"` drops `metadata.llmUsage` | Token counts unobtainable through CodeXomics | DGR change 1 |
| The same projection drops `annotationNote`, `llmSynthesis`, `searchDiagnostics` | `workflow.literatureCoverage`, `workflow.llmSynthesis`, and `workflow.annotationNote` are always `null` on the poll path, despite the code comment saying they are projected for external agents | DGR change 1 |
| Usage is recorded per phase but not per model | Research and agent models cannot be billed separately when DGR runs different thinking/task models | DGR change 2 |
| Prompt-cache hits are not distinguished | Cost is overstated on providers that discount cached input | DGR change 2 |
| A semantic-cache hit replays the original run's usage verbatim with nothing marking it | A repeat run reports full token spend and full research time for work that was not redone | DGR change 3 |
| `pubmedTotalMatchCount` is `Math.max(...) \|\| null` over per-query counts | "References surveyed" reports one query instead of the survey, and a genuine zero is reported as unmeasured | DGR change 4 |
| The archived report attachment has no usage or timing | The durable audit artifact cannot answer "what did this gene cost" | CodeXomics change 2 |
| The workflow record has no usage or timing | Every consumer must re-read the full DGR task | CodeXomics change 1 |

## Status

Both changes are applied on branches, committed but not pushed or merged:

| Repository | Branch | Verification |
| --- | --- | --- |
| Deep Gene Research | `feature/run-accounting-telemetry` | 231/231 vitest pass, `tsc --noEmit` clean |
| CodeXomics | `feature/annotation-run-accounting` | Annotation/DGR suites 54/54 pass; full suite unchanged against `main` (8 files / 65 tests fail before and after — all in TrackRenderer, advanced search, LLM config, and genome-window startup, none touched here) |

DGR's `annotation` projection was confirmed live against the running service:
`get-task-status` with `resultMode: "annotation"` now returns `llmUsage`,
`llmSynthesis`, `literatureMetrics`, `searchDiagnostics.literatureCoverage`,
and `annotationNote`, where it previously returned none of them.

CodeXomics runs in Electron, so its change takes effect after the app is
restarted.

## Running the tests

Both projects need a Node newer than the shell default here — the repo pins
23.11.1 and vitest fails to load its config under Node 22.6:

```bash
export PATH=/opt/homebrew/opt/node@23/bin:$PATH
cd /path/to/deep-gene-research && npx vitest run && npx tsc --noEmit
cd /path/to/CodeXomics && npx vitest run test/unit/annotation-research-workflow-service.test.js test/unit/dgr-artifact-storage.test.js
```

## Confirming the end-to-end effect

After the CodeXomics app restarts, a run should report
`tokenUsageSource: "codexomics-workflow"` instead of `"dgr-telemetry"`, and
`--dgr-telemetry off` should still produce complete token statistics:

```bash
python3 curate-genome-annotations/scripts/run_annotation_workflow.py \
  --genome /absolute/genome.gbk --gene lysC \
  --dgr-telemetry off --pricing-file /protected/pricing.json \
  --output /tmp/run.json
```
