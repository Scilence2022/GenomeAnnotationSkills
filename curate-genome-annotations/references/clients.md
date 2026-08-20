# Client compatibility

This skill is a single portable folder. Its `SKILL.md` frontmatter uses only the
two fields every supported client understands — `name` and `description` — and
the `scripts/` helpers are plain Python programs that call CodeXomics and DGR
over MCP HTTP. They import no client-specific runtime, so the same files run
unchanged under Codex, Claude Code, TRAE Work, and WorkBuddy/CodeBuddy.

## Install locations

| Client | User-level skills dir | Project-level skills dir | Extra frontmatter |
| --- | --- | --- | --- |
| Codex | `$CODEX_HOME/skills` (default `~/.codex/skills`) | n/a | `metadata` (optional) |
| Claude Code | `~/.claude/skills` | `.claude/skills` | `allowed-tools`, `argument-hint`, `context` (optional) |
| TRAE Work | `~/.trae-cn/skills` | `.trae/skills` | none beyond `name` / `description` |
| WorkBuddy / CodeBuddy | import via Settings (or `~/.codebuddy/skills`) | `.codebuddy/skills` | `allowed-tools`, `disable` (optional) |

## Install

Copy or symlink the `curate-genome-annotations` folder into the target client's
skills directory. The bundled helper prints the plan first and writes only with
`--install`:

```bash
python3 curate-genome-annotations/scripts/install_for_clients.py --check
python3 curate-genome-annotations/scripts/install_for_clients.py --install --client codex
python3 curate-genome-annotations/scripts/install_for_clients.py --install --client claude-code
python3 curate-genome-annotations/scripts/install_for_clients.py --install --client trae-work
python3 curate-genome-annotations/scripts/install_for_clients.py --install --client workbuddy
```

For a project-scoped install, pass `--dest` (for example `.claude/skills`,
`.trae/skills`, or `.codebuddy/skills`). `--symlink` links the repository folder
instead of copying, so `git pull` updates the installed skill.

### Client detection

The helper detects which clients are installed and targets only those when
`--client` is omitted. Detection is best-effort: it checks the same markers the
clients themselves use (`CODEX_HOME` and `~/.codex` for Codex,
`CLAUDE_CODE_ENTRYPOINT` and `~/.claude` for Claude Code, `~/.trae-cn` for
TRAE Work, and `~/.codebuddy` / `~/.workbuddy` for WorkBuddy). Print what was
detected with `--detect`, or override the choice with `--client codex`,
`--client claude-code`, and so on. Use `--client all` to ignore detection and
target every supported client.

## Frontmatter notes

Keep the canonical `SKILL.md` frontmatter to `name` and `description`. Add
client-specific fields only in a client-scoped copy when that client needs them:

- Claude Code may add `allowed-tools: Bash, Read, Write` to grant those tools
  without per-use approval while the skill is active. Prefer least privilege.
- WorkBuddy/CodeBuddy supports the same `allowed-tools` plus `disable: false`.
- TRAE Work reads only `name` and `description` and ignores other keys.

## Invoking the skill

- Native skill discovery: name it `curate-genome-annotations` and ask the client
  to run it (`$curate-genome-annotations` in Codex, `/curate-genome-annotations`
  in TRAE Work, or plain language such as "use the curate-genome-annotations
  skill").
- Direct execution: any client that can run shell commands can invoke the
  scripts directly (`python3 scripts/run_annotation_workflow.py ...`) without
  skill discovery.
- MCP wiring: the scripts need network access to the CodeXomics MCP
  (`http://127.0.0.1:3002/mcp`) and DGR MCP (`http://127.0.0.1:3000/api/mcp`).
  Point them elsewhere with `--codexomics-url` / `--dgr-url` and the matching
  environment variables described in `references/configuration.md`.
