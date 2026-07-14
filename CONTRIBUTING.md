# Contributing

Thanks for your interest in pr-quiz. This guide covers local setup, how the
tests are organized, and what to expect when you open a pull request.

## Development setup

You need Python 3.12, [`just`](https://just.systems/) (v1.46+), and `pytest`.
The Databricks CLI and GitHub CLI (`gh`) are needed only for the recipes that
talk to a live workspace or repo — not for the tests.

```bash
just bootstrap                                  # generate .just/shell.justfile for your OS
python -m pip install -r requirements-dev.txt   # pinned dev dependencies
just test                                       # run the unit suite (alias: just t)
```

`just setup` additionally verifies that the `databricks`, `gh`, `python`, and
`just` CLIs are installed and that your Databricks profile authenticates — run
it before the recipes that deploy or operate the backend. Run `just` with no
arguments to list every recipe.

Those recipes also need workspace values: copy `.dev/init-config.example.json`
to `.dev/init-config.json` and fill in your own. The file is git-ignored, so
your values stay local. The tests do not need it.

The tests import the template source directly (`tests/conftest.py` adds the
template's `src/job`, `src/app`, and the `actions/gate-check` directory to the
path), so no build or bundle render is needed to run them. Running the suite
imports `streamlit`, `requests`, `databricks-sdk`, and `pyyaml`; install those
if a test module fails to import.

```bash
python -m pytest tests/ -q            # what CI runs
```

## Test conventions

- **Pure logic is tested; effectful code is not.** The generation and app
  modules are split so that the decision logic (question counts, difficulty
  scaling, grading, gate evaluation, comment/URL formatting) is pure and
  covered by unit tests. Code that shells out, talks to Spark, opens a network
  connection, or renders Streamlit is kept thin and left untested.
- **Don't import modules that connect on import.** For example
  `quiz_store.py` builds a Databricks `Config` at import time; its drift guard
  (`test_quiz_store_sql.py`) parses the source with `ast` instead of importing
  it. Follow that pattern when a module has import-time side effects.
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
- CI runs the tests, `actionlint`, `zizmor`, and repo-hygiene grep gates (see
  [`scripts/grep_gates.sh`](scripts/grep_gates.sh)). Don't hardcode
  workspace-specific values (app URLs, warehouse IDs) anywhere in tracked files
  — keep your own workspace values in the untracked `.dev/init-config.json`. The
  repo's own CI workflows read their values from repo variables and secrets, not
  from that file.
- Changes under `.github/workflows/**`, `actions/**`, `templates/callers/**`,
  and `installer/**` are the consumer attack surface and require review from a
  code owner (see [`.github/CODEOWNERS`](.github/CODEOWNERS)).
- Update the prose docs when behavior changes. Don't hand-edit `CHANGELOG.md`
  version sections — commitizen generates them from Conventional-Commit history
  at release time (from v1.0.1 on), so a clear, correctly-typed PR title is what
  lands in the changelog.

## Security

Please report vulnerabilities privately — see [SECURITY.md](SECURITY.md). Do not
open a public issue for a security report.
