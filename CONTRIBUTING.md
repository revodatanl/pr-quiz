# Contributing

This guide is for contributors working on pr-quiz itself — its code, tests,
and release process.

## Development setup

Contributors need Python 3.12 — the version CI tests against — plus
[`just`](https://just.systems/) (v1.46+; `winget install Casey.Just` /
`brew install just`) and `pytest`. The Databricks CLI and GitHub CLI (`gh`)
are needed only for the recipes that talk to a live workspace or repo — not
for the tests. (Adopters and the installer need only Python 3.10+ — see
[docs/adopting.md](docs/adopting.md#prerequisites).)

```bash
just bootstrap                                  # generate .just/shell.justfile for your OS; also enables .githooks
python -m pip install -r requirements-dev.txt   # pinned dev dependencies
just test                                       # run the unit suite (alias: just t; same command CI runs: python -m pytest tests/ -q)
```

`just setup` additionally verifies that the `databricks`, `gh`, `python`, and
`just` CLIs are installed and that your Databricks profile authenticates — run
it before the recipes that deploy or operate the backend. Run `just` with no
arguments to list every recipe.

Those recipes also need workspace values: copy `.dev/init-config.example.json`
to `.dev/init-config.json` and fill in your own. The file is git-ignored, so
your values stay local. The tests do not need it.

Recipes authenticate with the Databricks CLI **profile** `free` by default (a
profile is a named set of Databricks credentials the CLI stores; override
with `DATABRICKS_PROFILE=<name>`); the GitHub recipes read the token that
`git credential fill` returns for `github.com`.

Common recipes:

```bash
just deploy         # databricks bundle deploy (renders template into .build/)
just deploy-app     # push app source to app compute and restart it
just run-job 1 <sha>   # trigger the generation job directly, skipping the workflow
just gate-check owner/repo <sha>  # evaluate the merge gate locally
just quiz 1         # comment /quiz on PR 1
just protect        # enable branch protection (required quiz-gate status)
```

The tests import the template source directly (`tests/conftest.py` adds the
template's `src/job`, `src/app`, and the `actions/gate-check` directory to the
path), so no build or bundle render is needed to run them. Running the suite
imports `streamlit`, `requests`, `databricks-sdk`, and `pyyaml`; install those
if a test module fails to import.

## Repo layout

Everything Databricks-side ships as a `databricks bundle init` template. A
**bundle** is Databricks' packaged unit of deploy (config plus code for a
job, an app, and their resources); `databricks bundle init` renders one from
`databricks_template_schema.json` plus the `template/{{.project_name}}/` tree
(the directory name is rendered to the chosen project name).

```text
databricks_template_schema.json  Init-template parameters (project name, host, catalog, warehouse, ...)
template/{{.project_name}}/      Template payload; paths below are relative to it
  databricks.yml.tmpl     Bundle config: deploy target, variables (model endpoint, table names)
  resources/              Bundle resources: schema, generation job, app
  src/job/                Generation job (quiz_logic.py + diff_corpus.py pure + unit-tested; prompts.py; github_diff.py)
  src/app/                Streamlit app (app_logic.py pure + unit-tested)
  sql/init_tables.sql.tmpl  Table definitions (question_pool, quiz_results)
actions/gate-check/       Composite action wrapping gate_check.py (local or cross-repo)
installer/                pr_quiz_install.py — the guided installer
scripts/                  gen_fixture_pr.py (fixtures); grep_gates.sh (CI hygiene gates)
tests/                    pytest suite for the pure logic
.github/workflows/        Reusable quiz-generate.yml + quiz-gate.yml (on: workflow_call),
                          this repo's own caller-quiz-*.yml, and project ci.yml
templates/callers/        Caller-workflow templates consumers copy into their own repos
.just/                    just recipes: setup, databricks, github, quality
docs/                     Adopter, operator, and threat-model guides
```

`actions/gate-check` is a **composite action** — a GitHub Action defined as a
sequence of steps (as opposed to a Docker image or a JS runtime) that other
workflows can call with one `uses:` line. The workflows under
`.github/workflows/` marked "Reusable" use `on: workflow_call`, the trigger
that lets another repo's workflow invoke them directly.

## Test conventions

- **Pure logic is tested; effectful code is not.** The generation and app
  modules are split so that the decision logic (question counts, difficulty
  scaling, grading, gate evaluation, comment/URL formatting) is pure and
  covered by unit tests. Code that shells out, talks to Spark, opens a network
  connection, or renders Streamlit is kept thin and left untested.
- **Don't import modules that connect on import.** For example,
  `quiz_store.py` builds a Databricks `Config` at import time, so it can't be
  imported directly in a test. Its **drift guard**, `test_quiz_store_sql.py`,
  parses the source with `ast` instead of importing it, and checks the SQL
  hasn't drifted from what the code expects. Follow that pattern when a
  module has import-time side effects.
- **Mock only real boundaries.** Stub the network/CLI/Spark edge, not the logic
  under test.
- New behavior in the pure layer needs a test. If a change forces logic behind
  an untestable boundary, restructure so the decision stays testable.

The suite is fast (about a second) and must stay green — a red test is a signal
to act, never to skip or weaken the assertion.

## Pull request expectations

- Keep PRs focused and explain the "why" in the description.
- Write the PR title as a Conventional Commit. PRs are squash-merged, so the
  title becomes the commit subject on `main`, and `pr-title.yml` lints it with
  `cz check` (commitizen). A title that isn't a valid Conventional Commit fails
  the PR-title check. Format is `type(scope): summary` — `scope` is optional.
  Examples:
  - `feat(app): show per-question difficulty`
  - `fix(gate): handle empty warehouse id`
  - `docs: clarify adopter prerequisites`
  - `chore(release): v1.2.0`

  Allowed types include `feat`, `fix`, `docs`, `chore`, `refactor`, `test`,
  `ci`, `build`, and `perf`.
- Run `just test` (and `just fmt-check` if you touched a justfile) before
  pushing.
- CI runs the tests, `actionlint` (a GitHub Actions workflow linter),
  `zizmor` (a GitHub Actions security scanner), and repo-hygiene grep gates
  (see [`scripts/grep_gates.sh`](scripts/grep_gates.sh)). Don't hardcode
  workspace-specific values (app URLs, Databricks SQL warehouse IDs) anywhere
  in tracked files — use `.dev/init-config.json` (see Development setup)
  instead. The repo's own CI workflows read their values from repo variables
  and secrets, not from that file.
- Changes under `.github/workflows/**`, `actions/**`, `templates/callers/**`,
  and `installer/**` are the consumer attack surface and require review from a
  code owner (see [`.github/CODEOWNERS`](.github/CODEOWNERS)).
- Update the prose docs when behavior changes. Don't hand-edit `CHANGELOG.md`
  version sections — commitizen generates them from Conventional-Commit history
  at release time (from v1.0.1 on), so a clear, correctly-typed PR title is what
  lands in the changelog.

## Testing with fixture PRs

`just fixture-prs [small medium large generated deleted waived]` (re)creates
deterministic PRs that exercise the pipeline end to end. Content comes from
[scripts/gen_fixture_pr.py](scripts/gen_fixture_pr.py), written to
`fixtures/sandbox/` on `fixture/*` branches (never on `main`). Needs a clean
working tree and the same GitHub token as the recipes above.

```bash
just fixture-prs              # (re)create all six, in dependency order
just fixture-clean            # close open fixture/* PRs, delete their branches
```

- **small** — ~12-line docs change; exercises a low difficulty factor (the
  score a model call — the "difficulty judge" — gives a diff, which scales
  the question count).
- **medium** — ~280-line Python change; the difficulty factor visibly swings
  the question count.
- **large** — ~4,470 lines across 8 files; exercises large-diff handling (the
  difficulty judge is skipped and the diff is split into chunks, see
  [Limits and known problems](docs/adopting.md#limits-and-known-problems)).
- **generated** — a ~3,500-line fake `uv.lock` plus a 12-line docs change;
  expect `N=1`, not the `N=20` the lock file's line count alone would force.
- **deleted** — deletes two of `medium`'s modules and rewrites `orders.py` to
  stop importing them; expect at most one impact question per deleted file, and
  an N sized from the `orders.py` rewrite alone — deletions weigh zero. Branches off
  `fixture/medium` (a diff only marks a file deleted if it exists on the base),
  so build `medium` first and drive this one with `just run-job` — its base is
  not `main`, so `/quiz` skips it.
- **waived** — the lock file alone; expect a passing gate and no quiz.

## Security

Please report vulnerabilities privately — see [SECURITY.md](SECURITY.md). Do not
open a public issue for a security report.
