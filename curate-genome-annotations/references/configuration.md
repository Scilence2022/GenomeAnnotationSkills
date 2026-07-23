# Configuration and security

## CodeXomics MCP authentication

CodeXomics fails closed when authentication is enabled without credentials.

For scoped production keys, configure JSON like:

```bash
export CODEXOMICS_MCP_API_KEYS_JSON='{
  "research-agent": {
    "apiKey": "replace-with-a-long-random-secret",
    "permissions": ["annotation:read", "annotation:research", "annotation:propose"]
  },
  "curator": {
    "apiKey": "use-a-different-secret",
    "permissions": ["annotation:read", "annotation:approve", "annotation:commit"]
  }
}'
```

Pass only the research-agent key to this skill using `CODEXOMICS_MCP_API_KEY` or `--codexomics-token`. Keep the curator key in a separate human review session. A master key is supported through `CODEXOMICS_MCP_MASTER_KEY`, but it is over-privileged for unattended annotation research.

## DGR authentication and durability

Set a strong `ACCESS_PASSWORD` of at least 16 characters. CodeXomics uses the same value in `DGR_MCP_TOKEN` when proxying DGR calls.

For resumable local or server operation:

```bash
export MCP_TASK_STORAGE_FILE=/durable/private/path/dgr-tasks.json
export DGR_WORKER_COUNT=1
export DGR_MAX_CONCURRENT_RESEARCH=2
export MCP_SERVER_BASE_URL=http://127.0.0.1:3000
```

Use one DGR process per JSON ledger. Do not point multiple processes at the same task file. The task file can contain research inputs and outputs; protect it accordingly.

User research PDFs are stored by DGR as content-addressed, mode-`0600` documents. Set `MCP_RESEARCH_DOCUMENT_STORAGE_DIR` to a protected persistent directory if the default `.dgr-research-documents` location is unsuitable. Do not place this directory in a web-served tree or shared source checkout.

## Model provider

Select a provider and appropriate models using DGR variables such as:

```bash
export MCP_AI_PROVIDER=openai
export MCP_THINKING_MODEL=<provider-model>
export MCP_TASK_MODEL=<provider-model>
export OPENAI_API_KEY=<secret>
```

DGR also supports other providers exposed by its current `env.tpl`. Check that file in the installed DGR revision before choosing variable names. Use a model capable of structured outputs and long-context evidence synthesis.

## Search provider

Configure a real search provider. For local SearXNG:

```bash
export MCP_SEARCH_PROVIDER=searxng
export SEARXNG_API_BASE_URL=http://127.0.0.1:8888
export MCP_SEARXNG_SCOPE=academic
```

The URL must be the reachable SearXNG base URL, not the DGR URL. Confirm SearXNG returns JSON search results. DGR also supports provider-specific variables for Tavily, Firecrawl, Exa, Bocha, and model-based search; consult the installed `env.tpl`.

Optionally set `NCBI_API_KEY` for higher NCBI rate limits. Search credentials improve retrieval but do not replace identity filtering: every retained source must refer to the exact gene/protein and organism or provide explicitly relevant homolog evidence.

## MCP client variables

The bundled scripts recognize:

```bash
export CODEXOMICS_MCP_URL=http://127.0.0.1:3002/mcp
export CODEXOMICS_MCP_API_KEY=<research-agent-key>
export DGR_MCP_URL=http://127.0.0.1:3000/api/mcp
export DGR_MCP_TOKEN=<ACCESS_PASSWORD>
export GENOME_ANNOTATION_STATE_DIR=$HOME/.local/state/genome-annotation-skills
```

Command-line token options override environment variables but are discouraged on shared systems because process listings and shell history may expose them.

## Credential hygiene

- Never print tokens in logs or summaries.
- Keep research and curator principals distinct.
- Bind services to loopback unless remote access is deliberately secured.
- Rotate any secret accidentally committed or displayed.
- Do not copy provider keys into CodeXomics genome sidecars or DGR report attachments.
