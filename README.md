# mega-nemo

Run **keystone**, **metaswarm** and **graphify** together inside a **NemoClaw**
sandbox driven by the **langchain-deepagents-code** (`dcode`) agent — with a real
triangular git workflow back to every source repo.

```
                  ┌─────────────────────────────────────────────┐
  provider ──────▶│  nemoclaw sandbox  (agent: dcode)           │
  model    ──────▶│                                             │
                  │   keystone   .charter/  + MCP server        │
                  │   metaswarm  skills / agents / commands     │
                  │   graphify   /graphify knowledge graph      │
                  │   workspaces uploaded, per-package tooling  │
                  └─────────────────────────────────────────────┘

  upstream ──fetch──▶ local clone ──push──▶ your fork ──PR──▶ upstream
   (read-only)          repos/<name>        dcsw/<name>
```

## Why this exists

Each of these tools has its own install story, its own idea of where config
lives, and its own upstream. Wiring them together by hand once is fine; doing it
again for a different provider/model, or keeping five forks in sync, is not.
`mega` makes both a single command.

## Quick start

```bash
uv venv && uv pip install -e ".[dev]"
```

```bash
mega doctor
```

```bash
mega up --provider build --model nvidia/nemotron-3-super-120b-a12b
```

`mega up` does three things in order: syncs every source repo and wires its
triangle, builds the sandbox for that provider/model, then provisions keystone +
metaswarm + graphify and uploads your workspaces.

Then shell in:

```bash
mega sandbox connect
```

## The two build inputs

Provider and model are the inputs that define the sandbox. Everything else has a
working default.

```bash
mega providers
```

`--provider` accepts either NemoClaw dialect — the installer key (`build`,
`openai`, `anthropic`, `gemini`, `openrouter`, `ollama`, `vllm`, `custom`, …) or
the OpenShell provider name (`nvidia-prod`, `openai-api`, …). They are
normalized for you, because `NEMOCLAW_PROVIDER` only accepts the former while
`nemoclaw inference set` only accepts the latter.

```bash
mega sandbox create --provider openai --model gpt-5.4 --name mega-oai
```

```bash
mega sandbox create --provider custom --model my-model --endpoint-url http://localhost:8000/v1
```

Swap the model on a live sandbox without a rebuild:

```bash
mega sandbox inference --provider anthropic --model claude-sonnet-4-6
```

Defaults live in [`mega.toml`](mega.toml); machine-specific overrides go in
`mega.local.toml` (gitignored). Precedence is
`mega.toml` → `mega.local.toml` → `MEGA_*` env → CLI flags. Existing
`NEMOCLAW_PROVIDER` / `NEMOCLAW_MODEL` / `NEMOCLAW_POLICY_TIER` env vars are
honored as a fallback, so a shell already set up for `nemoclaw onboard` works
here unchanged.

## Triangle workflows

Five source repos, each wired for the standard fork workflow that git supports
natively (`git help workflows`, "Triangular Workflows"):

| repo | upstream | role |
| --- | --- | --- |
| keystone | `tacoda/keystone` | `.charter/` agent-charter framework + MCP server |
| metaswarm | `dsifry/metaswarm` | multi-agent orchestration skills |
| graphify | `Graphify-Labs/graphify` | code knowledge graph skill (default branch is `v8`, not `main`) |
| nemoclaw | `NVIDIA/NemoClaw` | the sandbox runtime itself |
| deepagents | `langchain-ai/deepagents` | the Deep Agents SDK behind `dcode` |

`mega repos sync` clones each into `repos/` (gitignored) and sets:

```
remote.upstream.url           canonical repo
remote.upstream.pushurl       no-push://blocked-by-mega-nemo   ← push fails, loudly
remote.origin.url             your fork
remote.pushDefault            origin
branch.<b>.remote             upstream    ← bare `git pull` pulls from upstream
branch.<b>.pushRemote         origin      ← bare `git push` pushes to your fork
```

So even a plain `git push` inside `repos/keystone` cannot reach upstream. The
wiring is idempotent — rerun `mega repos sync` any time to repair it.

### The loop

```bash
mega triangle status
```

```bash
mega triangle start fix/charter-lint keystone
```

```bash
mega triangle push keystone
```

```bash
mega triangle pr keystone --title "fix: charter lint on nested globs"
```

Other verbs:

```bash
mega triangle pull --strategy rebase
```

```bash
mega triangle sync
```

`pull` fetches from upstream and rebases (or `--strategy merge|ff-only|reset`).
It refuses to touch a dirty tree unless you pass `--force`, which stashes and
restores instead. `sync` is `pull` followed by `push` — the full round trip for
every repo.

If a manifest `default_branch` does not exist on the upstream — because it was
renamed, or because the manifest is simply wrong — mega compares against the
remote's real `HEAD` and says so in the `note` column, rather than silently
reporting drift between two divergent branches.

Forks are created on demand:

```bash
mega repos sync --fork
```

Change the fork owner without editing the manifest with `--fork-owner` or
`MEGA_FORK_OWNER`.

## Monorepo support

A workspace is a *set of packages*. A single-package project is just the
degenerate case with one package at the root, so provisioning treats both the
same way: keystone scaffolds a charter per package, graphify prepares a graph
root per package.

Packages are discovered in this order:

1. explicit `packages:` globs in `repos.yaml`
2. workspace declarations the repo already makes for itself — pnpm/npm/yarn
   `workspaces`, `Cargo.toml` `[workspace] members`, `go.work`,
   `[tool.uv.workspace]`, `lerna.json`
3. marker files (`package.json`, `pyproject.toml`, `go.mod`, `Cargo.toml`, …)
   one level deep

```bash
mega workspace list
```

```bash
mega workspace add my-app ~/code/my-app --monorepo --package "packages/*" --package "apps/*"
```

## Provisioning

```bash
mega provision
```

Steps, all idempotent and independently runnable:

| step | what it does |
| --- | --- |
| `policies` | applies the NemoClaw policy presets the tools need (`github`, `npm`, `pypi`, plus `local-inference` for host providers) |
| `keystone` | installs the pinned release binary, arch chosen from the sandbox's `uname -m` |
| `metaswarm` | deploys all 14 `SKILL.md` trees via `nemoclaw skill install`, copies `agents/` and `commands/` into the runtime's config dir |
| `graphify` | installs the `graphifyy` package, assembles its skill into standard shape, deploys via `nemoclaw skill install` |
| `workspaces` | uploads each workspace, then `keystone init && index && lint` and `graphify build` per package |
| `mcp` | registers keystone's MCP server with the in-sandbox agent |

Two details worth knowing, because both are easy to get wrong by hand:

- **graphify is a package, not a skill tree.** It publishes to PyPI as
  `graphifyy` (the *console script* is `graphify`), and its own
  `graphify install <platform>` writes to that platform's hardcoded path —
  `.openclaw/`, `~/.claude/`, and so on. None of those is where the
  `langchain-deepagents-code` runtime looks. mega assembles
  `skill-<variant>.md` + `references/` into a normal skill directory and lets
  `nemoclaw skill install` place it.
- **Config dirs follow the agent.** dcode reads `/sandbox/.deepagents`,
  openclaw reads `/sandbox/.openclaw`. mega probes the sandbox for which one
  exists and falls back to the agent recorded in `sandboxes.json`, so
  metaswarm's `agents/` and `commands/` never land somewhere nothing reads.

```bash
mega provision --only keystone --only mcp
```

```bash
mega provision --workspace my-app
```

Provisioning also writes `TRIANGLE.md` — a short brief the sandboxed agent can
read to learn the push/pull rules it must follow.

## Command reference

```
mega up                     sync repos + build sandbox + provision
mega doctor [--fix]         check host, manifest, clones, sandbox
mega providers              the provider/agent vocabulary this build accepts
mega provision              install tools into the sandbox
mega agent "<prompt>"       one non-interactive dcode turn

mega repos list|sync|fork
mega triangle status|pull|push|start|pr|sync
mega sandbox create|rebuild|inference|list|status|connect|destroy
mega workspace list|add
```

Global flags: `--dry-run` (print mutating commands instead of running them),
`--verbose`, `--fork-owner`.

## Requirements

- WSL2 / Linux with Docker (NemoClaw's `openshell` driver)
- `nemoclaw` on `PATH` — [NVIDIA/NemoClaw](https://github.com/NVIDIA/NemoClaw)
- Python 3.11+, `uv`
- `gh` for `mega triangle pr` and `mega repos sync --fork`
- The credential env var for your provider (`mega providers` lists them);
  for `build` that is `NVIDIA_INFERENCE_API_KEY`

## Development

```bash
uv run pytest
```

```bash
uv run ruff check .
```

The triangle tests run against real local bare repos rather than mocks, so they
catch actual git config mistakes.
