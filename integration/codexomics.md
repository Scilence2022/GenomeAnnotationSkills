# CodeXomics integration

CodeXomics tools mode owns exact-target resolution, research orchestration,
report archival, and ChangeSet creation. Keep the Electron genome window open.
The ChatBox uses the same governance boundary, and long-running direct DGR tasks
it starts can create a ChangeSet automatically after completion and validation.

- `start_annotation_research` captures the exact feature and a bounded
  `currentAnnotation` snapshot at the resolved revision. DGR uses this
  snapshot to suppress no-op proposals and conservatively refine generic products.
- `get_annotation_research_workflow` polls the compact DGR result. Once DGR
  completes, CodeXomics separately fetches the full artifact and verifies target,
  annotation snapshot, citation spans, and proposal binding. A caller with
  `annotation:propose` can materialize the result as a reviewable ChangeSet.
- The durable workflow and the archived attachment summary both carry
  `llmUsage`, `researchTimeMs`, and `cacheReplay` when DGR reports them.
- For verified full text, read
  `workflow.reportAttachment.citationValidation.verifiedFullTextSourceCount`
  with `verified: true`. `summary.fullTextSourceCount` counts parsed documents
  in the report and can include documents with no verified proposal fact.
- In **Annotation Review**, a reviewer can inspect the proposed changes and
  open the bound DGR report. ChangeSets record a stable `codexomics://` report
  reference. Preserve the attachment ID, hash, and task ID for the handoff.

The genome companion sidecar is bound to the canonical source path and the
hash of its uncompressed contents. Replacing the file at the same path can
open a separate history. Keep the source, its matching companion sidecar,
and report artifacts together when migrating or recovering work.

[Workflow implementation](https://github.com/Scilence2022/CodeXomics/blob/2d0c9a46a860f746d6e7b0fc9e23d4584ae255f3/src/renderer/modules/chat/services/AnnotationResearchWorkflowService.js)
 · [Archive validation](https://github.com/Scilence2022/CodeXomics/blob/2d0c9a46a860f746d6e7b0fc9e23d4584ae255f3/src/main/dgr-artifact-storage.js)
 · [Sidecar identity](https://github.com/Scilence2022/CodeXomics/commit/f2a5fa38)
