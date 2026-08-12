---
status: current
---

# Adopting pr-quiz

This guide is for a repository maintainer who wants to gate merges in their
GitHub repo behind the quiz. It assumes someone has already stood up the
Databricks backend (see [operating.md](operating.md)); if not, do that first or
ask your backend operator for the handoff file.

## Prerequisites

Two roles set up pr-quiz. The **backend operator** deploys the Databricks side
once per workspace (see [operating.md](operating.md)). The **repo
maintainer** — the role this guide is for — wires each GitHub repo to that
backend.

| Requirement | Backend operator | Repo maintainer |
| --- | --- | --- |
| Unity Catalog workspace | Yes | — |
| Serverless jobs enabled | Yes | — |
| An existing SQL warehouse | Yes | — |
| A foundation-model serving endpoint in the region | Yes | — |
| Rights to create a service principal | Yes | — |
| [Databricks CLI](https://docs.databricks.com/dev-tools/cli/) | Yes | — |
| Python 3.10+ | Yes | Yes |
| Admin on the target GitHub repo | — | Yes |
| [GitHub CLI](https://cli.github.com/) (`gh`) | — | Yes |
| `pr-quiz-backend.json` handoff file | — | Yes |

A few terms above:

- **SQL warehouse** — a Databricks SQL compute resource. The gate query and
  the app's grading queries run on one.
- **Serverless jobs** — Databricks jobs that run on compute Databricks manages
  for you, with nothing to provision.
- **Serving endpoint** — the hosted foundation model (a large, general-purpose
  pretrained AI model) the job calls to write quiz questions. The default is
  `databricks-gpt-oss-120b`; it may not exist in every workspace or region, so
  set `serving_endpoint` to one that does.
- **Service principal** — a non-interactive machine identity. CI uses one to
  authenticate to Databricks with no human in the loop.
- **Unity Catalog** — Databricks' governance layer for data and permissions. If
  your workspace lists a catalog under **Catalog** in the sidebar, it
  qualifies.
- **Reusable workflow** — a shared GitHub Actions workflow that other repos
  call. pr-quiz ships two. A **caller workflow** is the small workflow in your
  repo that calls them; you own and edit it.

The handoff file carries the workspace host, warehouse ID, app/job names,
results table, status context, and the CI service-principal client ID — get
it from your backend operator. You'll also need repo secrets for the CI
service principal — see [Configuration reference](#configuration-reference)
below.

## Installer walkthrough

The guided installer is a single Python file that uses only the standard
library — no packages to install. Every step is **idempotent**: safe to run
more than once, because a re-run detects work already done and reports
`[skip]` instead of repeating it. `--dry-run` prints the mutating commands
without running them, and nothing runs branch-protection changes you didn't
ask for.

```bash
# 1. Preflight: CLIs on PATH, auth working.
python installer/pr_quiz_install.py doctor

# 2. Wire your repo: opens a PR adding the caller workflows, sets repo
#    secrets and variables, and configures the quiz-gate branch protection.
python installer/pr_quiz_install.py onboard --repo owner/name
```

`onboard` pins the caller workflows to the released reusable workflows
(`--workflows-ref`, default `v1`) and reads the backend values from
`pr-quiz-backend.json` (`--handoff`). Useful flags:

- `--target-branch <branch>` — the branch the gate protects (default: the
  repo's default branch).
- `--no-protect` — skip branch-protection changes.
- `--protect-only` — apply only branch protection; run this **after** the
  onboarding PR merges (see the gotcha below).
- `--dry-run` — preview without changing anything.

Run any subcommand with `--help` for the full list.

## The one gotcha that catches everyone

**`/quiz` does nothing until the caller workflow is merged to your default
branch.** GitHub only runs workflows triggered by `issue_comment` (the event
a PR/issue comment fires) from the version of the workflow file on the
repository's default branch — never from a PR's own branch. The onboarding
PR adds the caller workflow, but
until that PR merges, commenting `/quiz` on any PR is silently ignored. Merge
the onboarding PR first, then use `/quiz`. This is why `--protect-only` exists:
turn on the required status only once the workflow is live.

Two related points:

- The `pull_request: branches:` filter that decides which PRs require the gate
  lives in **your caller workflow**, not in the reusable workflow. Edit it (and
  keep `QUIZ_TARGET_BRANCH` in sync) to match the branch you protect.
- The quiz app sleeps when idle and takes about 2 minutes to wake. The `/quiz`
  workflow pre-starts it, but the first load after idle is slow.

## Topologies

- **Shared multi-tenant backend** (recommended for an org). Deploy the backend
  once; many repos point their caller workflows at it. Quiz results are keyed by
  `(provider, repo, head_sha)` — `provider` identifies the code-hosting
  platform, e.g. GitHub — so repos sharing a `head_sha` never cross results.
  One CI service principal and one warehouse serve all repos — one place to
  rotate secrets, one cost center.
- **Per-team bundle init.** A **bundle** is Databricks' packaged unit of
  deploy — config plus code for a job, an app, and their resources. A team
  runs `databricks bundle init` from this template into their own workspace
  and owns an isolated backend. Use this when teams need separate workspaces,
  catalogs, or cost isolation. Each backend is operated independently per
  [operating.md](operating.md).

## Configuration reference

The consumer caller workflows read these. Defaults come from the reusable
workflow, so most are optional. `quiz-gate` itself is a **commit status** —
GitHub's per-commit pass/fail marker that a PR's branch-protection rule can
require before allowing merge.

**Repo secrets** (required): OAuth credentials that let the CI service
principal — see [Prerequisites](#prerequisites) — authenticate to Databricks
without a human signing in.

| Secret | Purpose |
| --- | --- |
| `DATABRICKS_HOST` | Workspace URL the CI service principal authenticates to |
| `DATABRICKS_CLIENT_ID` | CI service-principal OAuth client id |
| `DATABRICKS_CLIENT_SECRET` | CI service-principal OAuth secret (expires — rotate) |

**Repo variables**:

| Variable | Default | Purpose |
| --- | --- | --- |
| `QUIZ_WAREHOUSE_ID` | *(required for the gate)* | SQL warehouse the gate query runs on |
| `QUIZ_APP_NAME` | `pr-quiz` | Name of the Databricks App (a Databricks-hosted web app; here, the Streamlit quiz UI) that serves the quiz |
| `QUIZ_JOB_NAME` | `pr-quiz-generator` | Generation job |
| `QUIZ_RESULTS_TABLE` | `workspace.pr_quiz.quiz_results` | Fully qualified quiz_results table the gate reads; override when the backend uses a non-default catalog or schema, or the gate query fails with `TABLE_OR_VIEW_NOT_FOUND` |
| `QUIZ_STATUS_CONTEXT` | `quiz-gate` | Commit-status name the gate uses |
| `QUIZ_TARGET_BRANCH` | *(caller default)* | Base branch `/quiz` will generate for; keep in sync with `branches:` |
| `QUIZ_WAIVE_AUTHORS` | `dependabot[bot]` | Comma-separated PR author logins (no spaces) whose PRs bypass the quiz with an automatic passing status (for automated dependency bots). Used by **both** caller workflows — quiz-generate skips generation, quiz-gate keeps `/quiz-check` from blocking those PRs |
| `QUIZ_GENERATED_GLOBS` | *(empty)* | Comma-separated path patterns for machine-generated files to skip, on top of the built-in list and your `.gitattributes` — e.g. `dist/*,*.pb.h`. See [Skipped files](#skipped-files) |

**Backend secret** (Databricks side): a GitHub token in the workspace secret
scope (default key `github_token`) that the generation job uses to read PR
diffs and post statuses.

The Databricks-side deployment parameters (workspace host, catalog, schema,
warehouse ID, serving endpoint, secret scope/key, commit-status context) are
the `databricks bundle init` template inputs in
[../databricks_template_schema.json](../databricks_template_schema.json).

## Manual fallback

If you'd rather not run the installer, wire the repo by hand:

1. Copy `templates/callers/quiz-generate.yml` and
   `templates/callers/quiz-gate.yml` into your repo's `.github/workflows/`.
2. In each, replace `OWNER/REPO` in the `uses:` line with `revodatanl/pr-quiz`
   (the public pr-quiz repo) and pin it to `@v1` (or a commit SHA) — never a
   moving branch.
3. Set the `branches:` filter and `QUIZ_TARGET_BRANCH` to the branch you
   protect (see the note above).
4. Add the repo secrets and variables listed in [Configuration
   reference](#configuration-reference) above.
5. Merge those workflows to your default branch.
6. Enable branch protection on the protected branch requiring the `quiz-gate`
   status check.

## Skipped files

The quiz only covers changes a reviewer is expected to read. Three consequences:

- **Generated files are skipped**, and don't count toward the question count. A
  4,000-line lockfile next to a 6-line code change gives you a quiz about the 6
  lines. Same for binary files.
- **A deleted file is worth one question**, about the consequences of removing
  it, answered from the code that remains. Its removed lines don't count either,
  so a delete-only PR gets a 1-question quiz.
- **A PR with nothing left to quiz passes automatically**, with a `quiz-gate`
  status reading "Quiz waived: no reviewable changes in this PR" and no quiz
  link. Otherwise a PR that only bumps `uv.lock` could never merge. A PR editing
  `.gitattributes` never gets that waive — it fails the run instead, so declaring
  your own files generated cannot become a way past the gate.

A file counts as generated if it matches the built-in list (lock files, minified
bundles, source maps, test snapshots, codegen output, `vendor/`,
`node_modules/`), if your `.gitattributes` marks it `linguist-generated`, or if
it matches `QUIZ_GENERATED_GLOBS`. Patterns apply at any depth and ignore case,
so `dist/*` also covers `web/dist/app.js`.

Patterns follow git's own reading, so a directory needs the trailing slash:
`dist/` (or `dist/*`) is the directory's contents, while a bare `dist` matches a
*file* called `dist` at any depth.

To add your own:

```gitattributes
# .gitattributes - also makes GitHub collapse the file
src/api/schema.json linguist-generated=true
dist/                                        # the whole directory
```

**`.gitattributes` is read from the base branch, not from the PR.** A PR that
adds or edits declarations does not get them applied to its own quiz — otherwise
the change under review would be choosing which parts of itself get reviewed.
Merge the declaration first; it applies from the next PR on.

## Limits and gotchas

- Question quality depends on the model. Malformed output is dropped and
  retried, and generation over-provisions rounds (it requests more questions
  than needed, expecting some to fail); a run fails only if fewer valid
  questions than needed remain after all rounds.
- **A very large PR is only partly quizzed.** The diff is split into at most 5
  chunks of 30,000 characters, so roughly 150,000 characters reach the model;
  anything past that is dropped but still counts toward the question count. When
  the PR deletes files, their context is carved out of that same budget, up to a
  third of it.
- **A text diff GitHub won't show is rebuilt, not dropped.** GitHub returns no
  patch for a text diff it considers too large; the job fetches the file's two
  versions and rebuilds the patch itself, capped like any other. The change stays
  in the quiz and keeps counting. Binary changes are the opposite case — there is
  nothing to ask about, so they leave the corpus and the count, like generated
  files. The run fails only when a *text* change can't be rebuilt either: the
  file is over 4 MB, isn't valid UTF-8, or GitHub wouldn't serve it. Then it
  fails naming the file rather than quizzing around it, because dropping it would
  take its changed lines out of the question count. Split the change, or declare
  the path generated.
- PRs at roughly 4,000+ changed lines skip the difficulty judge — the model call
  that rates how hard a diff is to review and scales the question count.
  Skipping it is safe because diff size alone already caps the count at 20.

## Troubleshooting

- **`/quiz` is ignored.** See [the gotcha above](#the-one-gotcha-that-catches-everyone) —
  most likely the caller workflow isn't on your default branch yet — or your
  commenter's **author association** (GitHub's label for their relationship
  to the repo, e.g. `OWNER`, `MEMBER`, `COLLABORATOR`) isn't in the allowed
  list (`OWNER,MEMBER,COLLABORATOR` by default).
- **Gate step fails with an empty/cryptic Databricks error.** `QUIZ_WAREHOUSE_ID`
  is unset — the gate caller validates it and tells you to set the variable.
- **"Could not resolve the quiz app URL".** The `app_name` input doesn't match a
  deployed app, or the CLI can't see it; pass `app_url` explicitly.
- **Quiz link 404s or hangs.** The app was asleep (see the wake-up note
  above); wait ~2 minutes and reload.
- **Fork PRs never get a quiz automatically.** GitHub withholds secrets from
  fork-triggered workflows, so a maintainer must post `/quiz` instead. See
  [SECURITY.md](../SECURITY.md).
- **Passing then failing.** The gate reads the **latest** result per commit; a
  passing attempt followed by a failing one blocks the merge again. Retake to
  100%.
- **Need to re-check the gate without a new commit or a new quiz.** Comment
  `/quiz-check` — it re-evaluates `quiz-gate` against the existing quiz
  result for the head commit, without regenerating questions.
- **The gate went green with no quiz.** The PR was waived — the status
  description says which kind: "no reviewable changes in this PR" (see [Skipped
  files](#skipped-files)) or "automated dependency update"
  (`QUIZ_WAIVE_AUTHORS`). `/quiz-check` re-confirms either one.
- **The quiz is shorter than the diff suggests.** Either the difficulty judge
  rated the diff easy, or part of it was skipped — see [Skipped
  files](#skipped-files). The job log names every file it skipped as generated,
  so that list tells you which.
- **My new `linguist-generated` line had no effect.** Two usual causes: it is
  read from the base branch, so it only applies from the next PR onward, and a
  directory needs its trailing slash (`dist/`, not `dist`). See [Skipped
  files](#skipped-files).
- **Set `QUIZ_WAIVE_AUTHORS` on both caller workflows.** The gate caller applies
  the author waive too; setting it only on the generate caller leaves
  `/quiz-check` blocking bot PRs.
