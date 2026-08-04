# mega-nemo user guide

How to get working, bring a repo in, and run a feature from idea to pull
request — with the debugging moves for when each stage misbehaves.

If you only read one thing: [the feature loop](#4-the-feature-development-workflow).

---

**Contents**

1. [Getting working](#1-getting-working)
2. [Cloning mega-nemo onto a machine](#2-cloning-mega-nemo-onto-a-machine)
3. [Initializing a repo to work on](#3-initializing-a-repo-to-work-on)
4. [The feature development workflow](#4-the-feature-development-workflow)
5. [Debugging](#5-debugging)
6. [Extending mega-nemo itself](#6-extending-mega-nemo-itself)
7. [Command cheat sheet](#7-command-cheat-sheet)

---

## 1. Getting working

### What you are operating

Three layers, and most confusion comes from mixing them up:

| Layer | Lives on | You touch it with |
| --- | --- | --- |
| **mega-nemo** | your WSL filesystem | `mega ...` |
| **the sandbox** | a Docker container run by nemoclaw | `mega sandbox ...`, `nemoclaw <name> ...` |
| **the agent (dcode)** | inside the sandbox | `mega agent "..."`, or interactively after `mega sandbox connect` |

keystone, metaswarm and graphify are installed **inside the sandbox**. The
clones under `repos/` on your host are for *reading and contributing to* those
tools, not for running them. Editing `repos/graphify/` does not change the
graphify the agent uses until you reinstall it.

### Prerequisites

- WSL2 or Linux with Docker running (nemoclaw's `openshell` driver)
- `nemoclaw` on `PATH`
- Python 3.11+ and `uv`
- `gh`, authenticated (`gh auth login`) — needed for PRs and fork creation
- An API key for your provider

Check all of it at once:

```bash
mega doctor
```

`doctor` is the first thing to run whenever something feels wrong. It checks the
host binaries, the manifest, every clone's triangle wiring, and the sandbox.

### Credentials

Each provider wants a different environment variable. List them:

```bash
mega providers
```

For the default NVIDIA provider that is `NVIDIA_INFERENCE_API_KEY`. Export it in
your shell profile, or keep it in a file you source — **not** in `mega.toml`,
which is committed:

```bash
export NVIDIA_INFERENCE_API_KEY=nvapi-...
```

If the key is missing, `mega sandbox create` warns before starting rather than
letting a ten-minute image build fail at the credential prompt.

---

## 2. Cloning mega-nemo onto a machine

```bash
git clone https://github.com/dcsw/mega-nemo.git ~/mega-nemo
cd ~/mega-nemo
```

Keep it on the Linux filesystem. On a `/mnt/c` path, git and Docker bind mounts
are slow and permissions do not survive the crossing.

Install the CLI:

```bash
uv venv && uv pip install -e ".[dev]"
```

Every command below assumes `uv run mega`, or a shell where `.venv/bin` is on
`PATH`. Optional, and worth it:

```bash
mega --install-completion
```

### Point it at your provider and model

Defaults live in `mega.toml` (committed). Machine-specific overrides go in
`mega.local.toml` (gitignored). Precedence, lowest to highest:

```
mega.toml  <  mega.local.toml  <  MEGA_* env  <  CLI flags
```

```toml
# mega.local.toml
[sandbox]
sandbox = "mega-nemo"
provider = "build"
model = "nvidia/nemotron-3-super-120b-a12b"
agent = "dcode"
```

`--provider` accepts either NemoClaw dialect — the installer key (`build`,
`openai`, `anthropic`, `gemini`, `openrouter`, `ollama`, `vllm`, `custom`) or
the OpenShell name (`nvidia-prod`, `openai-api`, …). Both are normalized for
you. `--agent dcode` is an alias for `langchain-deepagents-code`.

### Bring everything up

```bash
mega up --provider build --model nvidia/nemotron-3-super-120b-a12b
```

Three phases, printed as they run:

1. **sync repos** — clone the five sources into `repos/`, wire each triangle
2. **sandbox** — `nemoclaw onboard` with your provider/model, then a rebuild to
   apply dcode auto-approval
3. **provision** — install keystone, metaswarm and graphify; upload workspaces;
   scaffold a charter and build a graph per package

Rehearse it first if you want to see the exact commands without running them:

```bash
mega --dry-run up --provider build --model nvidia/nemotron-3-super-120b-a12b
```

### Create your forks

Until the forks exist, `mega triangle status` shows *"branch not on fork yet"*
and pushes fail.

```bash
mega repos sync --fork
```

### Confirm it works

```bash
mega doctor
```

```bash
mega agent "List the skills you have available and the tools you can call."
```

---

## 3. Initializing a repo to work on

A **workspace** is a project the sandboxed agent works on. Adding one uploads it
into the sandbox and gives it a keystone charter and a graphify graph. Three
starting points.

### A. A brand-new project

Create and initialize it on the host first, so it has a git history from day
one:

```bash
mkdir -p ~/code/my-service && cd ~/code/my-service && git init -b main
```

Give it something to describe — a `pyproject.toml`, `package.json`, `go.mod` —
otherwise package discovery has nothing to anchor on. Then register it and push
it into the sandbox:

```bash
mega workspace add my-service ~/code/my-service
```

```bash
mega provision --workspace my-service
```

That appends an entry to `repos.yaml`, uploads the tree, and per package runs
`keystone init && keystone index && keystone lint`, then
`graphify extract . --code-only`.

Your charter now exists at `.charter/` inside the sandbox copy. On a fresh
keystone install you may see a migration warning — clear it once:

```bash
mega agent "Run 'keystone migrate up' in the my-service workspace and show me the result."
```

### B. An existing repo

Same two commands, pointed at the checkout:

```bash
mega workspace add legacy-api ~/code/legacy-api
```

```bash
mega provision --workspace legacy-api
```

For a large repo the first graph build is the slow part. It is deterministic AST
parsing — no LLM calls, no key needed — but it walks every file.

### C. A monorepo

Declare it, and mega expands it into packages:

```bash
mega workspace add platform ~/code/platform --monorepo --package "packages/*" --package "apps/*"
```

If the repo already declares its own workspaces you can skip the globs entirely
— `--monorepo` alone is enough. mega reads pnpm/npm/yarn `workspaces`,
`Cargo.toml` `[workspace] members`, `go.work`, `[tool.uv.workspace]` and
`lerna.json`. Failing that it falls back to marker files one level deep.

Check what it found before provisioning:

```bash
mega workspace list platform
```

```
┏━━━━━━━━━━━┳━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━┓
┃ package   ┃ path          ┃ language ┃ found via ┃
┡━━━━━━━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━┩
│ api       │ apps/api      │ python   │ manifest  │
│ web       │ apps/web      │ javascript│ manifest │
│ shared    │ packages/…    │ javascript│ manifest │
└───────────┴───────────────┴──────────┴───────────┘
```

`found via` tells you which rule matched: `manifest` (your globs), `native` (the
repo's own declaration), `marker` (a `package.json` etc.), or `root` (single
package). If a package is missing, that column tells you which rule to fix.

Each package gets **its own** charter and **its own** `graphify-out/graph.json`.
That is the point: a query inside `apps/api` does not drag in the whole
platform.

Tune per workspace in `repos.yaml`:

```yaml
workspaces:
  - name: platform
    path: ~/code/platform
    monorepo:
      enabled: true
      packages: ["packages/*", "apps/*"]
    charter: true   # false to skip keystone for this workspace
    graph: true     # false to skip graphify
```

### Re-syncing after host-side edits

`mega provision` uploads; it is not a live mount. After editing on the host:

```bash
mega provision --workspace my-service --only workspaces
```

For a genuinely live view in both directions, mount instead:

```bash
mkdir -p ~/mnt/my-service && nemoclaw mega-nemo share mount /home/agent/workspace/my-service ~/mnt/my-service
```

---

## 4. The feature development workflow

```
  orient          plan            branch          build           verify          ship
  ──────          ────            ──────          ─────           ──────          ────
  graphify        metaswarm       mega triangle   dcode +         tests +         mega triangle
  query/explain   start           start           keystone new    keystone lint   push → pr
       │              │               │               │               │               │
       └──────────────┴───────────────┴───────────────┴───────────────┴───────────────┘
                        each stage has a debug move in §5
```

Two different things get developed here, and the loop differs:

- **your own project** (a workspace) — steps 1–5 below, shipped however that
  project ships
- **one of the five source tools** (keystone, metaswarm, graphify, nemoclaw,
  deepagents) — same steps, plus the triangle in step 6

### Step 1 — Orient: ask the graph, don't grep

Before reading code, ask the knowledge graph. This is the whole reason graphify
is installed: the agent stops grepping and starts traversing.

```bash
mega agent "Using graphify, explain how request authentication flows through my-service. Cite the nodes."
```

Directly, inside the sandbox:

```bash
mega sandbox connect
```

```bash
cd ~/workspace/my-service && graphify query "where is the retry policy configured?"
```

The three orientation verbs:

| Command | Answers |
| --- | --- |
| `graphify query "<question>"` | open-ended — "how does X work?" |
| `graphify explain "<node>"` | what one symbol is, and what touches it |
| `graphify path "<source>" "<target>"` | how two things connect, edge by edge |

Add `--budget N` to cap tokens returned, `--dfs` for depth-first traversal.

If the graph is stale (you changed code since provisioning):

```bash
graphify update
```

### Step 2 — Plan: let metaswarm structure the work

metaswarm supplies 19 agents and 14 skills. The entry point is `start`, which
takes a rough intent and turns it into a plan with review gates.

```bash
mega agent "/start Add rate limiting to the public API, 100 req/min per key, with a bypass for internal callers."
```

The skills you will actually reach for:

| Skill | Use it for |
| --- | --- |
| `start` | turning an intent into a structured plan |
| `brainstorming-extension` | exploring approaches before committing |
| `plan-review-gate` | adversarial review of the plan before code |
| `design-review-gate` | design review before implementation |
| `orchestrated-execution` | multi-agent implementation with TDD enforcement |
| `create-issue` | filing the work as a tracked issue |
| `handoff` | passing context to a later session |
| `status` | where the current work stands |
| `pr-shepherd` | driving a PR to merge |
| `handling-pr-comments` | working through review feedback |

Named agents worth invoking directly: `architect-agent`, `security-auditor-agent`,
`test-automator-agent`, `code-review-agent`, `product-manager-agent`.

### Step 3 — Branch: start from fresh upstream

**For a source tool**, always start the branch through mega. It fetches upstream
first and sets the branch's tracking so `git pull` follows upstream while
`git push` goes to your fork:

```bash
mega triangle start feat/rate-limit graphify
```

Omit the repo name to start the same branch across every triangle repo — useful
for a change that spans tools.

**For your own project**, branch normally inside the workspace.

### Step 4 — Build: keystone constrains, dcode writes

keystone's job is to make the rules explicit so the agent stays inside them. A
new feature usually wants a new primitive or two:

```bash
keystone new rule api/rate-limiting
```

```bash
keystone new document adr-rate-limiting
```

Available kinds: `rule`, `command`, `skill`, `agent`, `pattern`, `posture`,
`tool`, `document`, `corpus`, `eval`, `adapter`, `policy`. Each scaffolds at the
conventional path under `.charter/` with correct frontmatter.

After editing any primitive:

```bash
keystone index && keystone lint
```

`index` regenerates `.charter/INDEX.json`, which is what the agent actually
reads. `lint` validates frontmatter, unique ids, and dependency links. Skipping
`index` is the most common reason a new rule appears to be ignored.

Then do the work. Interactively:

```bash
mega sandbox connect
```

Or scripted, one turn at a time:

```bash
mega agent "Implement the rate limiter per .charter/rules/api/rate-limiting.md. Write the tests first."
```

Useful while writing:

```bash
keystone watch
```

That re-runs index + project + lint on every change to `.charter/`.

```bash
keystone charter
```

Shows coverage — which files no guide governs yet. A useful "what did I forget"
check.

### Step 5 — Verify

Run the project's own tests first. Then the three tool-level checks:

```bash
keystone lint && keystone verify
```

`verify` checks vendored policies for drift and the strict cascade for
violations.

```bash
keystone eval run
```

Runs charter evals — static and sensor level — against known scenarios.

```bash
graphify affected "RateLimiter" --depth 3
```

Blast radius: everything downstream of the thing you changed. This is the check
that catches the caller you forgot about.

For mega-nemo itself:

```bash
uv run pytest -q && uv run ruff check .
```

### Step 6 — Ship: close the triangle

Only for the source tools. Push goes to **your fork** — pushing to upstream is
blocked by config and will fail loudly.

```bash
mega triangle status
```

```bash
mega triangle push graphify
```

```bash
mega triangle pr graphify --title "feat: per-package graph roots" --ready
```

`pr` opens a draft by default; `--ready` skips that, `--web` opens the form in a
browser instead. The base branch is the upstream's real default — which for
graphify is `v8`, not `main`, and mega resolves that for you.

Keeping up with upstream mid-feature:

```bash
mega triangle pull graphify
```

That fetches upstream and rebases your branch onto it. If you have uncommitted
work it refuses; `--force` stashes and restores around the rebase instead.

The full round trip for every repo at once:

```bash
mega triangle sync
```

---

## 5. Debugging

### The first three commands

```bash
mega doctor
```

```bash
mega --dry-run --verbose <the command that failed>
```

```bash
nemoclaw mega-nemo logs --follow
```

`--dry-run` prints the exact commands mega would run without running the
mutating ones — read-only probes still execute so the output is realistic. This
is the fastest way to find out whether mega is building the wrong command or the
command itself is failing.

### Debugging your code

```bash
graphify explain "<symbol>"
```

```bash
graphify path "HttpHandler" "Database"
```

```bash
graphify affected "<symbol>" --depth 3 --relation calls
```

```bash
graphify diagnose multigraph
```

If answers look wrong or stale, the graph is behind the code:

```bash
graphify update
```

If `update` is not enough, rebuild from scratch:

```bash
rm -rf graphify-out && graphify extract . --code-only
```

In a monorepo, run these **inside the package directory** — each package has its
own graph. Running from the repo root queries the root graph, which may not
exist.

### Debugging the sandbox

```bash
mega sandbox status
```

```bash
nemoclaw mega-nemo doctor --fix
```

```bash
nemoclaw mega-nemo logs -n 200
```

```bash
mega sandbox connect
```

mega has no `exec` wrapper — drop to nemoclaw for one-off commands:

```bash
nemoclaw mega-nemo exec --no-tty -- bash -lc "keystone version && graphify --version"
```

A stopped sandbox usually just needs starting, not rebuilding:

```bash
nemoclaw mega-nemo start
```

### Debugging provisioning

Re-run one step at a time instead of the whole thing:

```bash
mega provision --only keystone
```

```bash
mega provision --only graphify --only mcp
```

Steps: `policies`, `keystone`, `metaswarm`, `graphify`, `workspaces`, `mcp`. All
are idempotent — re-running is always safe.

The provision table's `detail` column is the diagnosis. Common readings:

| Detail | Means | Fix |
| --- | --- | --- |
| `not cloned; run mega repos sync <name>` | the source clone is missing | `mega repos sync <name>` |
| `no SKILL.md found under [...]` | `install.skill_globs` no longer matches upstream's layout | inspect `repos/<name>/`, update the glob in `repos.yaml` |
| `graphify not installed` | the package step failed before the graph step | `mega provision --only graphify`, read the pip output |
| `0/N graph(s)` | extract failed in the packages | connect and run `graphify extract . --code-only` by hand to see the error |
| `0/N charters` | keystone failed | check for pending migrations: `keystone migrate up` |

If a network fetch fails inside the sandbox, it is usually policy:

```bash
nemoclaw mega-nemo policy-list
```

```bash
mega provision --only policies --policy huggingface
```

### Debugging the triangle

```bash
mega triangle status
```

Read the columns: `behind upstream` is work you have not pulled, `ahead of fork`
is work you have not pushed, `wired` is whether the triangle config is intact.

| Symptom | Cause | Fix |
| --- | --- | --- |
| `wired: partial` | remotes or push-block drifted | `mega repos sync` — it repairs idempotently |
| `manifest says 'main', upstream default is 'v8'` | the manifest's `default_branch` is stale | mega already used the real HEAD; correct `repos.yaml` when convenient |
| `branch not on fork yet` | fork missing or never pushed | `mega repos sync --fork`, then `mega triangle push <repo>` |
| `no upstream/<branch> (fetch first)` | clone has no upstream refs | `mega repos sync` |
| push rejected after a rebase | fork has the pre-rebase history | `mega triangle push <repo> --force-with-lease` |
| `N uncommitted change(s)` on pull | dirty tree | commit, or `mega triangle pull --force` to stash around it |

If a push to upstream ever fails with:

```
git: 'remote-upstream-is-read-only-use-mega-triangle-push' is not a git command.
```

that is the guard working as designed. Push to your fork instead.

### Debugging provider and model problems

```bash
mega providers
```

```bash
mega sandbox list
```

Swap the model without a rebuild:

```bash
mega sandbox inference --provider anthropic --model claude-sonnet-4-6
```

Errors you will actually hit:

- **`no provider`** — nothing set anywhere. Pass `--provider` or set it in
  `mega.local.toml`.
- **`needs an explicit endpoint`** — `custom`, `anthropicCompatible`, `ollama`
  and `vllm` have no default URL. Pass `--endpoint-url`.
- **`<VAR> is not set`** — export the credential from `mega providers`.
- **`no model for provider X and it has no default`** — pass `--model`.

All four are raised before any image build starts.

---

## 6. Extending mega-nemo itself

### Adding a source repo to the triangle

Append to `sources:` in `repos.yaml`:

```yaml
  - name: mytool
    upstream: https://github.com/someone/mytool.git
    fork: https://github.com/{fork_owner}/mytool.git
    default_branch: main
    triangle: true
    role: whatever-it-does
    install:
      kind: skills          # skills | python-package | binary | none
      skill_globs: ["skills/*"]
```

```bash
mega repos sync mytool --fork
```

```bash
mega triangle status mytool
```

`{fork_owner}` is templated from `defaults.fork_owner`, overridable with
`--fork-owner` or `MEGA_FORK_OWNER`. Set `triangle: false` for a repo you only
ever pull from.

Before trusting a new `install:` block, rehearse it:

```bash
mega --dry-run provision --only mytool
```

### Changing the remote names

If you prefer `parent` over `upstream`:

```yaml
defaults:
  remotes:
    upstream: parent
    fork: origin
```

```bash
mega repos sync
```

### Working on mega-nemo's own code

```bash
uv run pytest -q
```

```bash
uv run ruff check --fix .
```

The triangle tests build real local bare repos rather than mocking git, so they
catch actual config mistakes. When you change wiring in `src/mega_nemo/gitx.py`,
they are the tests that matter.

---

## 7. Command cheat sheet

**Setup**

```bash
mega doctor
mega providers
mega up --provider build --model nvidia/nemotron-3-super-120b-a12b
```

**Repos and triangle**

```bash
mega repos list
mega repos sync [<repo>...] [--fork]
mega triangle status
mega triangle start <branch> [<repo>...]
mega triangle pull [<repo>...] [--strategy rebase|merge|ff-only|reset] [--force]
mega triangle push [<repo>...] [--force-with-lease]
mega triangle pr <repo> [--title T] [--ready] [--web]
mega triangle sync
```

**Sandbox**

```bash
mega sandbox create --provider P --model M [--name N]
mega sandbox rebuild [--dcode-auto-approval thread-opt-in]
mega sandbox inference --provider P --model M
mega sandbox list | status | connect | destroy
mega agent "<prompt>"
```

**Workspaces and provisioning**

```bash
mega workspace list [<name>]
mega workspace add <name> <path> [--monorepo] [--package GLOB]
mega provision [--only STEP] [--workspace W] [--policy P]
```

**Inside the sandbox**

```bash
keystone init | index | lint | verify | charter | watch
keystone new <kind> <id>
keystone eval run
keystone migrate up

graphify extract . --code-only
graphify update
graphify query "<question>" [--budget N] [--dfs]
graphify explain "<node>"
graphify path "<source>" "<target>"
graphify affected "<node>" [--depth N] [--relation R]
graphify diagnose multigraph
```

**Global flags**

```
--dry-run          print mutating commands instead of running them
--verbose / -v     echo every command
--fork-owner       override defaults.fork_owner
```
