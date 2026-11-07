# Upstream compatibility

The skill uses the annotation workflow and reporting contracts on CodeXomics
`main` at `2d0c9a46` (package 0.723.0) and Deep Gene Research (DGR) `main`
at `b0d767e` (package 0.22.0). These are the source revisions checked for this
alignment. The runner also accepts older workflow records with missing
accounting fields and can recover usage from the archived attachment or DGR.

- [CodeXomics contract](codexomics.md) covers durable workflow data and archive validation.
- [DGR contract](deep-gene-research.md) covers result modes, cache replay, and workspace access.

Use the [setup guide](../curate-genome-annotations/references/setup.md) to select
the runtime for each checkout. Use the [reporting guide](../curate-genome-annotations/references/reporting.md)
to diagnose missing usage or interpret archived results.
