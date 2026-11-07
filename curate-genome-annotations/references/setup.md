# Setup and startup

## Supported topology

Run DGR as a long-lived service and start CodeXomics together with its MCP server. The recommended local topology is:

| Component | Default endpoint | Purpose |
| --- | --- | --- |
| DGR MCP | `http://127.0.0.1:3000/api/mcp` | Evidence research and proposal generation |
| CodeXomics MCP tools mode | `http://127.0.0.1:3002/mcp` | Genome loading, exact target resolution, workflow orchestration, ChangeSets |

CodeXomics and its MCP server should be started together with `npm run start-with-mcp`. The MCP listener delegates genome operations into the live Electron renderer, so starting a detached MCP server without the app is insufficient for genome work.

## Repository bootstrap

Default repositories:

- CodeXomics: `https://github.com/Scilence2022/CodeXomics.git`
- Deep Gene Research: `https://github.com/Scilence2022/DeepGeneResearch.git`

Use explicit local directories. The bootstrap script skips an existing valid checkout and never pulls or overwrites it automatically.

```bash
python3 scripts/bootstrap_repositories.py \
  --codexomics-dir /absolute/path/CodeXomics \
  --dgr-dir /absolute/path/deep-gene-research \
  --skip-install
```

The checked upstream revisions use different Node versions:

- CodeXomics source build uses Node.js 22.12+ in the 22.x line and npm 10+.
- DGR uses Node.js 24 LTS (see its `.node-version`) and pnpm 10.15.1.

For these versions, run the bootstrap command with `--skip-install`. Then run
`npm ci` in CodeXomics and `pnpm install --frozen-lockfile` in DGR using the
respective Node version in each checkout. The bootstrap script uses one PATH
for both installs. Use `--dry-run` to inspect commands.

## Environment-first configuration

Keep secrets in the service manager, shell environment, or a protected environment file outside the skill repository. Never place API keys in commands recorded by shared shell history, committed files, run summaries, or logs.

Minimum relationship between services:

```bash
export DGR_MCP_URL=http://127.0.0.1:3000/api/mcp
export DGR_MCP_TOKEN="$ACCESS_PASSWORD"
```

Set CodeXomics MCP auth and DGR provider/search variables as described in `configuration.md`.

## Start or reuse services

Start DGR under Node.js 24 first, from its checkout, using `pnpm dev` for local
development or `pnpm build && pnpm start` for a long-lived production Node
service. Use a persistent task/document directory. Static Cloudflare Pages and
ephemeral deployments need a separate long-lived DGR worker for queued
research, PDF uploads, and artifacts.

Switch to Node.js 22 for CodeXomics and run the startup helper. It reuses the
running DGR worker and starts the Electron app with its tools-mode listener.

```bash
python3 scripts/start_services.py \
  --codexomics-dir /absolute/path/CodeXomics
```

The helper stores PID metadata and logs for the process it starts. For inspection only:

```bash
python3 scripts/start_services.py --check-only
```

If both upstream checkouts use a compatible shared PATH, the helper can also
start DGR with `--dgr-dir`. In that mode `--dgr-mode production` builds DGR
before starting it.

## Local bypasses

Two development bypasses exist:

```bash
export CODEXOMICS_MCP_ENABLE_LOCAL_BYPASS=true
export DGR_ALLOW_UNAUTHENTICATED_DEV=true
```

Use them only on an isolated local development machine. DGR's bypass is not restricted to loopback by itself. Never use either bypass on shared networks, remote hosts, or production deployments. `start_services.py` refuses to enable them unless `--allow-insecure-local-bypass` is passed explicitly.

## Shutdown

The startup script writes only the PID of the process it starts. Stop a managed service with a normal `TERM` signal after confirming the PID metadata points to the intended command. Do not kill unrelated processes merely because they occupy the default port; inspect them first and select different endpoints when appropriate.
