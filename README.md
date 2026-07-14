# pr-quiz — an AI quiz merge gate for pull requests

[![CI](https://github.com/revodatanl/pr-quiz/actions/workflows/ci.yml/badge.svg)](https://github.com/revodatanl/pr-quiz/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

pr-quiz makes a pull request prove its author (or a reviewer) understood the
change before it can merge. When a PR opens, a Databricks job reads the diff and
uses a foundation model to generate a multiple-choice quiz about it; a web app
serves the quiz, and the PR can merge to the protected branch only when the
latest published quiz result for its head commit scored 100%.

<!-- Hero screenshot: docs/img/quiz-attempt.png (add once captured) -->

## How it works

1. A PR opens or reopens (or someone comments `/quiz`). A reusable GitHub
   workflow sets the `quiz-gate` commit status to pending, wakes the quiz app,
   and starts a Databricks job. PRs by a waived author (see `QUIZ_WAIVE_AUTHORS`)
   skip the quiz and get an automatic passing status.
2. The job fetches the diff, scales the question count with diff size and a
   model-judged difficulty factor, and writes a pool of questions (with correct
   answers) to a table in Databricks.
3. A bot comment posts the quiz link. A reviewer takes the quiz in the app;
   each attempt samples fresh questions and is graded instantly.
4. On a 100% score the app flips the `quiz-gate` commit status to green and
   posts the results on the PR. Branch protection requires that status, so the
   merge unlocks. Pushing a new commit sets the status back to pending; comment
   `/quiz` for a fresh quiz, or `/quiz-check` to re-check a commit that passed
   before.

<!-- Screenshot: docs/img/quiz-attempt.png — the quiz app mid-attempt (add once captured) -->

Design diagram: [quiz-merge-gate.mermaid](quiz-merge-gate.mermaid).

## Prerequisites

Two roles set up pr-quiz. The **backend operator** deploys the Databricks side
once per workspace. The **repo maintainer** wires each GitHub repo to that
backend. See the [Quickstart](#quickstart) for the commands each runs.

| Requirement | Backend operator | Repo maintainer |
| --- | --- | --- |
| Unity Catalog workspace | Yes | — |
| Serverless jobs enabled | Yes | — |
| An existing SQL warehouse | Yes | — |
| A foundation-model serving endpoint in the region | Yes | — |
| Rights to create a service principal | Yes | — |
| [Databricks CLI](https://docs.databricks.com/dev-tools/cli/) | Yes | — |
| Python 3.10+ | Yes | — |
| Admin on the target GitHub repo | — | Yes |
| [GitHub CLI](https://cli.github.com/) (`gh`) | — | Yes |
| `pr-quiz-backend.json` handoff file | — | Yes |

A few terms above:

- **Serverless jobs** — Databricks jobs that run on compute Databricks manages
  for you, with nothing to provision.
- **Serving endpoint** — the hosted foundation model the job calls to write
  quiz questions. The default is `databricks-gpt-oss-120b`; it may not exist in
  every workspace or region, so set `serving_endpoint` to one that does.
- **Service principal** — a non-interactive machine identity. CI uses one to
  authenticate to Databricks with no human in the loop.
- **Unity Catalog** — Databricks' governance layer for data and permissions. If
  your workspace lists a catalog under **Catalog** in the sidebar, it qualifies.
- **Reusable workflow** — a shared GitHub Actions workflow that other repos
  call. pr-quiz ships two. A **caller workflow** is the small workflow in your
  repo that calls them; you own and edit it.

The maintainer gets the handoff file from the backend operator; it carries the
workspace host, warehouse ID, app/job names, results table, and the CI
service-principal client ID.

## Quickstart

pr-quiz has two sides: a **backend** on Databricks (deployed once per
workspace) and a **repo wiring** step (once per consumer repo). The guided
installer does both. It is a single Python file with no packages to install.
Check the
[Prerequisites](#prerequisites) first — you need the Databricks CLI, `gh`, and
Python 3.10+ on `PATH`.

```bash
# Preflight: CLIs present and authenticated.
python installer/pr_quiz_install.py doctor

# Backend (workspace admin, once): deploy the bundle + tables, store the
# GitHub token secret, create the CI service principal, apply grants.
# Writes pr-quiz-backend.json for adopters to consume.
python installer/pr_quiz_install.py backend --profile <profile>

# Onboard a repo (maintainer, per repo): open a PR adding the caller
# workflows, set secrets/variables, configure quiz-gate branch protection.
python installer/pr_quiz_install.py onboard --repo owner/name
```

Every step is safe to re-run (re-runs report `[skip]` and change nothing),
`--dry-run` prints mutating commands without executing, and nothing destructive
happens without `--force`.
See [docs/adopting.md](docs/adopting.md) and [docs/operating.md](docs/operating.md)
for full walkthroughs, manual fallbacks, and troubleshooting.

<!-- Screenshot: docs/img/results-comment.png — the results comment posted on the PR after a 100% pass (add once captured) -->

## Read this before you wire a repo

- **`/quiz` is dead until the caller workflow reaches your default branch.**
  GitHub runs `issue_comment` workflows only from the version on the default
  branch. Merge the onboarding PR first; only then does `/quiz` do anything.
- **The `pull_request: branches:` filter lives in your caller workflow**, not in
  the reusable workflow (it can't be parameterized there). Set it — and keep
  `QUIZ_TARGET_BRANCH` in sync — to the branch your gate protects.
- **The app sleeps and takes about 2 minutes to wake.** The `/quiz` workflow
  pre-starts it; the first load after idle is slow.
- **Pin the reusable workflows to `@v1` or a commit SHA, never a moving
  branch.** See [SECURITY.md](SECURITY.md).

<!-- Screenshot: docs/img/blocked-merge.png — the PR merge box blocked by the pending quiz-gate check (add once captured) -->

## Topologies

- **Shared multi-tenant backend** (recommended for an org). Deploy the backend
  once; many repos point their caller workflows at it. Quiz results are keyed by
  `(provider, repo, head_sha)`, so repos sharing a `head_sha` never cross
  results. One CI service principal and one warehouse serve all repos — one
  place to rotate secrets, one cost center.
- **Per-team bundle init.** A team runs `databricks bundle init` from this
  template into their own workspace and owns an isolated backend. Use this when
  teams need separate workspaces, catalogs, or cost isolation. Each backend is
  operated independently per [docs/operating.md](docs/operating.md).

## Configuration reference

The consumer caller workflows read these. Defaults come from the reusable
workflow, so most are optional.

**Repo secrets** (required):

| Secret | Purpose |
| --- | --- |
| `DATABRICKS_HOST` | Workspace URL the CI service principal authenticates to |
| `DATABRICKS_CLIENT_ID` | CI service-principal OAuth client id |
| `DATABRICKS_CLIENT_SECRET` | CI service-principal OAuth secret (expires — rotate) |

**Repo variables**:

| Variable | Default | Purpose |
| --- | --- | --- |
| `QUIZ_WAREHOUSE_ID` | *(required for the gate)* | SQL warehouse the gate query runs on |
| `QUIZ_APP_NAME` | `pr-quiz` | Databricks App that serves the quiz |
| `QUIZ_JOB_NAME` | `pr-quiz-generator` | Generation job |
| `QUIZ_RESULTS_TABLE` | `workspace.pr_quiz.quiz_results` | Fully qualified quiz_results table the gate reads; override when the backend uses a non-default catalog or schema |
| `QUIZ_STATUS_CONTEXT` | `quiz-gate` | Commit-status name the gate uses |
| `QUIZ_TARGET_BRANCH` | *(caller default)* | Base branch `/quiz` will generate for; keep in sync with `branches:` |
| `QUIZ_WAIVE_AUTHORS` | `dependabot[bot]` | Comma-separated PR author logins (no spaces) whose PRs bypass the quiz with an automatic passing status (for automated dependency bots) |

**Backend secret** (Databricks side): a GitHub token in the workspace secret
scope (default key `github_token`) that the generation job uses to read PR
diffs and post statuses.

The Databricks-side deployment parameters (workspace host, catalog, schema,
warehouse ID, serving endpoint, secret scope/key, commit-status context) are
the `databricks bundle init` template inputs in
[databricks_template_schema.json](databricks_template_schema.json).

## Repo layout

Everything Databricks-side ships as a `databricks bundle init` template:
`databricks_template_schema.json` plus the `template/{{.project_name}}/` tree
(`bundle init` renders the directory name to the chosen project name).

```text
databricks_template_schema.json  Init-template parameters (project name, host, catalog, warehouse, ...)
template/{{.project_name}}/      Template payload; paths below are relative to it
  databricks.yml.tmpl     Bundle config: deploy target, variables (model endpoint, table names)
  resources/              Bundle resources: schema, generation job, app
  src/job/                Generation job (quiz_logic.py pure + unit-tested; prompts.py; github_diff.py)
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

## Documentation

- [docs/adopting.md](docs/adopting.md) — consumer guide: prerequisites,
  installer walkthrough, manual fallback, troubleshooting.
- [docs/operating.md](docs/operating.md) — backend operator: grants, secret and
  token rotation, warehouse cost, teardown.
- [docs/threat-model.md](docs/threat-model.md) — assets, actors, trust
  boundaries, accepted risks, v2 mitigations.
- [SECURITY.md](SECURITY.md) — reporting and the v1 trust model.
- [CONTRIBUTING.md](CONTRIBUTING.md) · [RELEASING.md](RELEASING.md) ·
  [CHANGELOG.md](CHANGELOG.md).

## Development

This repo runs the gate on its own PRs to `main`. Local recipes run
through [`just`](https://just.systems/) (v1.46+; `winget install Casey.Just` /
`brew install just`). You also need the Databricks CLI and `gh` on `PATH`, and
Python 3.12 with `pytest` for the tests.

**Python versions**: adopters and the installer need Python 3.10+ (see
[Prerequisites](#prerequisites)). Contributors use Python 3.12 — the version CI
tests against.

```bash
just bootstrap      # generate .just/shell.justfile for your OS
just setup          # verify tooling (databricks CLI, gh, python, just) + auth
just test           # run the unit tests (alias: just t)
just deploy         # databricks bundle deploy (renders template into .build/)
just deploy-app     # push app source to app compute and restart it
just run-job 1 <sha>   # trigger the generation job directly, skipping the workflow
just gate-check owner/repo <sha>  # evaluate the merge gate locally
just quiz 1         # comment /quiz on PR 1
just protect        # enable branch protection (required quiz-gate status)
```

Recipes authenticate with the Databricks CLI profile `free` by default (override
with `DATABRICKS_PROFILE=<name>`); the GitHub recipes read the token that
`git credential fill` returns for `github.com`. See
[CONTRIBUTING.md](CONTRIBUTING.md) for test conventions.

### Testing with fixture PRs

`just fixture-prs [small medium large]` (re)creates deterministic PRs that
exercise the pipeline end to end. Content comes from
[scripts/gen_fixture_pr.py](scripts/gen_fixture_pr.py), written to
`fixtures/sandbox/` on `fixture/*` branches (never on `main`). Needs a clean
working tree and the same GitHub token as the recipes above.

```bash
just fixture-prs              # (re)create all three: small, medium, large
just fixture-clean            # close open fixture/* PRs, delete their branches
```

- **small** — ~12-line docs change; exercises a low difficulty factor.
- **medium** — ~280-line Python change; the judge visibly swings the question count.
- **large** — ~4,470 lines across 8 files; exercises large-diff handling (the
  difficulty judge is skipped and the diff is split into chunks, see
  [Limits and gotchas](#limits-and-gotchas)).

## Limits and gotchas

- The gate reads the **latest** result per commit: a passing attempt followed by
  a failing one blocks again.
- Fork PRs don't run the gate automatically (GitHub withholds secrets from fork
  workflows); a maintainer `/quiz` comment generates for them.
- The model endpoint is a bundle variable — swap it without code changes.
- Question quality depends on the model; malformed output is dropped and
  retried, and generation over-provisions rounds. A run fails only if fewer
  valid questions than needed remain after all rounds.
- PRs at roughly 4,000+ changed lines skip the difficulty judge (diff size
  alone already caps the count at 20). Diffs split into at most 5 chunks of up
  to 30,000 characters; files that don't fit are skipped for question
  generation but still count toward the diff size.
