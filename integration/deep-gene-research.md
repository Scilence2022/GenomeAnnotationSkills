# Deep Gene Research integration

`get-task-status` with `resultMode: "annotation"` returns the bounded proposal,
annotation Note, review evidence, usage and cache metadata, and literature
coverage. CodeXomics uses this projection for polling and fetches
`resultMode: "full"` for report archival. The skill's read-only telemetry fallback
also tries the compact result first and can read the full result for older builds.

DGR can publish per-model usage, cache replays, queue/retry and attempt timings,
and an optional cost estimate from `DGR_LLM_RATE_TABLE`. A cache replay carries
the original result's usage and research time, plus separate replay metadata.
The skill estimates cost from its own pricing file for research and agent usage.
Keep that estimate separate from any DGR rate-table estimate or provider invoice.

DGR 0.22 supports named-workspace bearer tokens. The token's workspace must
equal `target.workspaceId` for exact-target research. The checked CodeXomics
revision emits `ws_…` identifiers and DGR's configured workspace IDs allow only
lowercase ASCII letters, digits, and hyphens. Use the legacy `ACCESS_PASSWORD`
as `DGR_MCP_TOKEN` for this pairing. Use the same token for a telemetry lookup
as CodeXomics used to submit the task.

The `dgr.review-evidence.v1` artifact carries review-only excerpts such as
DOI-only literature and private PDF passages. Keep them available to the human
reviewer and use CodeXomics's archive receipt to decide which full-text sources
actually support citation-bound facts.

[Result projection](https://github.com/Scilence2022/DeepGeneResearch/blob/b0d767e0bcf81cef6c77900e4f74f3be6f5950b8/src/services/task-result-projection.ts)
 · [Deployment, storage, and telemetry](https://github.com/Scilence2022/DeepGeneResearch/blob/b0d767e0bcf81cef6c77900e4f74f3be6f5950b8/docs/research-operations.md)
