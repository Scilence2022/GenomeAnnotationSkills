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

Use locus tag or protein ID plus chromosome. Do not use coordinates copied from a different genome build. CodeXomics intentionally rejects multiple CDS matches and non-CDS features.

## DGR completed but no ChangeSet exists

Check:

- caller has `annotation:propose` as well as read/research permissions;
- `workflow.proposalStatus` and `workflow.proposalReason`;
- DGR returned evidence-backed claims;
- current annotation and target binding still match;
- report archive/citation validation succeeded.

Do not manufacture a minimal ChangeSet to hide missing evidence.

## ChangeSet is stale

The target feature or annotation revision changed after proposal creation. Start a new research workflow against the live annotation. Do not apply the stale proposal or edit its stored hashes.

## DGR ledger problems

Run one DGR process per task file. On corruption, DGR may quarantine the ledger with a `.corrupt.*` suffix and lock further work. Preserve the files, inspect logs and filesystem permissions, then recover deliberately. Never delete the ledger blindly; it is part of the research audit trail.

## Port already in use

Inspect the owning process before terminating anything. If it is the intended healthy service, reuse it. Otherwise stop it through its supervisor or configure a different URL/port. The bundled startup script does not kill port owners.

## Large full report

CodeXomics stores the verified DGR JSON as a genome-scoped gene attachment and exposes it through the Resources/JSON viewer. Return the attachment identifier and file name. Avoid pasting a multi-megabyte report into chat unless the user explicitly requests it.
