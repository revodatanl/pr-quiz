---
status: current
---

# Adopting pr-quiz

This guide is for the repository maintainer who wants to block merges in a
GitHub repo until the PR author passes the quiz. It assumes that someone
deployed the Databricks backend already (see [operating.md](operating.md)). If
nobody deployed it, deploy it first, or ask your backend operator for the
handoff file.

## Prerequisites

Two roles install pr-quiz. The **backend operator** deploys the Databricks side
one time for each workspace (see [operating.md](operating.md)). The **repo
maintainer** connects each GitHub repo to that backend. This guide is for the
repo maintainer.

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

The table uses these terms:

- **SQL warehouse** — a Databricks SQL compute resource. The gate query and the
  grading queries of the app run on one.
- **Serverless jobs** — Databricks jobs that run on compute that Databricks
  manages. You provision nothing.
- **Serving endpoint** — the hosted foundation model (a large, general-purpose
  pretrained AI model) that the job calls to write quiz questions. The default
  is `databricks-gpt-oss-120b`. This endpoint does not exist in every workspace
  or region. If it is absent, set `serving_endpoint` to an endpoint that exists.
- **Service principal** — a machine identity that no human operates. CI uses one
  to authenticate to Databricks without a person.
- **Unity Catalog** — the governance layer of Databricks for data and
  permissions. If the sidebar of your workspace shows a catalog under
  **Catalog**, your workspace qualifies.
- **Reusable workflow** — a shared GitHub Actions workflow that other repos
  call. pr-quiz supplies two. A **caller workflow** is the small workflow in
  your repo that calls them. You own this workflow, and you edit it.

Get the handoff file from your backend operator. It holds these values:

- the workspace host
- the warehouse ID
- the app name and the job name
- the results table
- the status context
- the client ID of the CI service principal

You also need repo secrets for the CI service principal. See [Configuration
reference](#configuration-reference) below.

## Installer walkthrough

The installer is one Python file that uses only the standard library. You
install no packages. Every step is **idempotent**: it is safe to run more than
once. A second run finds the work that is complete already and shows `[skip]`
for it. The `--dry-run` flag shows the commands that change your repo, but it
does not run them. The installer changes branch protection only when you ask
for it.

```bash
# 1. Make sure that the CLIs are on PATH and that authentication works.
python installer/pr_quiz_install.py doctor

# 2. Connect your repo. This opens a PR that adds the caller workflows, sets
#    the repo secrets and variables, and configures the quiz-gate protection.
python installer/pr_quiz_install.py onboard --repo owner/name
```

The `onboard` command pins the caller workflows to the released reusable
workflows (`--workflows-ref`, default `v1`). It reads the backend values from
`pr-quiz-backend.json` (`--handoff`). These flags are useful:

- `--target-branch <branch>` — the branch that the gate protects. The default is
  the default branch of the repo.
- `--no-protect` — do not change branch protection.
- `--protect-only` — apply branch protection only. Run this **after** the
  onboarding PR merges (see the next section).
- `--dry-run` — show the changes, but do not apply them.

Run any subcommand with `--help` for the full list.

## The most common mistake

**`/quiz` does nothing until the caller workflow is on your default branch.** A
comment on a PR or an issue fires the `issue_comment` event. GitHub runs that
workflow only from the version of the file on the default branch. It never runs
the version from the branch of the PR. The onboarding PR adds the caller
workflow. Until that PR merges, GitHub ignores `/quiz` on every PR, and it
gives no message.

Merge the onboarding PR first. Then use `/quiz`. The `--protect-only` flag
exists for this reason: it turns on the required status after the workflow is
live.

Two related facts:

- The `pull_request: branches:` filter selects which PRs need the gate. This
  filter is in **your caller workflow**, not in the reusable workflow. Set the
  filter to the branch that you protect, and give `QUIZ_TARGET_BRANCH` the same
  value.
- The quiz app sleeps when it is idle, and it needs about 2 minutes to start
  again. The `/quiz` workflow starts the app in advance. The first load after an
  idle period is still slow.

## Topologies

- **Shared multi-tenant backend.** This topology is the better choice for an
  organization. You deploy the backend one time, and many repos use it. The key
  of a quiz result is `(provider, repo, head_sha)`. The `provider` value names
  the code-hosting platform, for example GitHub. Two repos with the same
  `head_sha` therefore never read the results of each other. One CI service
  principal and one warehouse serve all repos, so you rotate the secrets in one
  place and you have one cost center.
- **One bundle for each team.** A **bundle** is the packaged unit of deployment
  in Databricks: the configuration and the code for a job, an app, and their
  resources. A team runs `databricks bundle init` from this template into its
  own workspace, and that team owns a separate backend. When teams need
  separate workspaces, separate catalogs, or separate cost, use this topology.
  An operator maintains each backend on its own, as [operating.md](operating.md)
  describes.

## Configuration reference

The caller workflows in your repo read these values. The reusable workflow
supplies the defaults, so most values are optional. `quiz-gate` is a **commit
status**: a pass or fail marker that GitHub keeps for each commit. A
branch-protection rule can require this status before GitHub allows a merge.

**Repo secrets** (required). These OAuth credentials let the CI service
principal authenticate to Databricks without a person. See
[Prerequisites](#prerequisites).

| Secret | Purpose |
| --- | --- |
| `DATABRICKS_HOST` | Workspace URL that the CI service principal authenticates to |
| `DATABRICKS_CLIENT_ID` | OAuth client ID of the CI service principal |
| `DATABRICKS_CLIENT_SECRET` | OAuth secret of the CI service principal. This secret expires, so rotate it |

**Repo variables**:

| Variable | Default | Purpose |
| --- | --- | --- |
| `QUIZ_WAREHOUSE_ID` | *(required for the gate)* | The SQL warehouse that runs the gate query |
| `QUIZ_APP_NAME` | `pr-quiz` | Name of the Databricks App that serves the quiz. A Databricks App is a web app that Databricks hosts. Here it is the Streamlit quiz UI |
| `QUIZ_JOB_NAME` | `pr-quiz-generator` | The job that generates the questions |
| `QUIZ_RESULTS_TABLE` | `workspace.pr_quiz.quiz_results` | Full name of the `quiz_results` table that the gate reads. When the backend uses a different catalog or schema, set this value. Set it also when the gate query fails with `TABLE_OR_VIEW_NOT_FOUND` |
| `QUIZ_STATUS_CONTEXT` | `quiz-gate` | Name of the commit status that the gate writes |
| `QUIZ_TARGET_BRANCH` | *(caller default)* | The base branch that `/quiz` generates for. Give `branches:` the same value |
| `QUIZ_WAIVE_AUTHORS` | `dependabot[bot]` | PR author logins, separated by commas and without spaces. The gate waives the PRs of these authors and writes a passing status. Use it for automated dependency bots. **Both** caller workflows read it: quiz-generate writes no quiz, and quiz-gate keeps `/quiz-check` from blocking these PRs |
| `QUIZ_GENERATED_GLOBS` | *(empty)* | Path patterns for machine-generated files to skip, separated by commas. They apply in addition to the built-in list and to your `.gitattributes`, for example `dist/*,*.pb.h`. See [Skipped files](#skipped-files) |

**Backend secret** (on the Databricks side). The workspace secret scope holds a
GitHub token (default key `github_token`). The generation job uses this token to
read PR diffs and to write commit statuses.

The deployment parameters on the Databricks side are the template inputs of
`databricks bundle init`.
[../databricks_template_schema.json](../databricks_template_schema.json) holds
them: the workspace host, the catalog, the schema, the warehouse ID, the serving
endpoint, the secret scope and key, and the commit-status context.

## Manual fallback

If you do not want to use the installer, configure the repo by hand:

1. Copy `templates/callers/quiz-generate.yml` and
   `templates/callers/quiz-gate.yml` into `.github/workflows/` in your repo.
2. In each file, replace `OWNER/REPO` in the `uses:` line with
   `revodatanl/pr-quiz` (the public pr-quiz repo). Pin it to `@v1` or to a
   commit SHA. Never pin it to a branch, because a branch moves.
3. Set the `branches:` filter and `QUIZ_TARGET_BRANCH` to the branch that you
   protect. See [The most common mistake](#the-most-common-mistake).
4. Add the repo secrets and variables from [Configuration
   reference](#configuration-reference) above.
5. Merge these workflows to your default branch.
6. Turn on branch protection for the protected branch, and require the
   `quiz-gate` status check.

## Skipped files

The quiz covers only the changes that a reviewer must read. This rule has three
consequences:

- **The quiz skips generated files.** These files do not count toward the
  question count. A lockfile of 4,000 lines next to a code change of 6 lines
  gives a quiz about the 6 lines. Binary files work the same way.
- **A deleted file gives one question.** The question asks about the results of
  the removal, and the code that remains holds the answer. The removed lines do
  not count either. A PR that only deletes files therefore gets a quiz with one
  question.
- **A PR with no reviewable change passes automatically.** The `quiz-gate`
  status then reads "Quiz waived: no reviewable changes in this PR", and it
  holds no quiz link. Without this rule, a PR that only updates `uv.lock` can
  never merge. A PR that edits `.gitattributes` never gets this waiver. That run
  fails instead, so a declaration of your own files as generated cannot become a
  way past the gate.

A file counts as generated in three cases:

- The file matches the built-in list: lock files, minified bundles, source maps,
  test snapshots, codegen output, `vendor/`, and `node_modules/`.
- Your `.gitattributes` marks the file `linguist-generated`.
- The file matches `QUIZ_GENERATED_GLOBS`.

A pattern applies at every depth, and it ignores case. `dist/*` therefore also
covers `web/dist/app.js`.

Git reads these patterns, so a directory needs the trailing slash. `dist/` and
`dist/*` select the contents of the directory. A bare `dist` selects a *file*
with the name `dist`, at every depth.

To add your own patterns, edit `.gitattributes`:

```gitattributes
# .gitattributes - GitHub also collapses the file
src/api/schema.json linguist-generated=true
# every path needs the attribute, and git reads a whole line as a comment only
# when it starts with "#" - a trailing "#" is part of the rule
dist/ linguist-generated=true
```

**The job reads `.gitattributes` from the base branch, not from the PR.** A PR
that adds or edits a declaration does not apply that declaration to its own
quiz. This rule stops a change under review from selecting which parts of itself
a reviewer sees. Merge the declaration first. It applies from the next PR.

## Limits and known problems

- The quality of the questions depends on the model. The job discards malformed
  output and asks again. Each round requests more questions than necessary,
  because some questions fail. A run fails only when too few valid questions
  remain after all rounds.
- **A very large PR gets a quiz about a part of the change.** The job splits the
  diff into a maximum of 5 chunks of 30,000 characters, so approximately 150,000
  characters reach the model. The job drops the remainder, but that remainder
  still counts toward the question count. When the PR deletes files, the context
  of those files uses the same budget, up to one third of it.
- **The job rebuilds a text diff that GitHub does not show.** GitHub returns no
  patch for a text diff that it considers too large. The job then reads the two
  versions of the file and builds the patch itself, with the same limits as any
  other patch. The change stays in the quiz, and it keeps its questions. The run
  fails only when the job cannot rebuild a *text* change: the file is more than
  4 MB, the file is not valid UTF-8, or GitHub does not serve it. The failure
  message then names the file, because a quiz without that file takes its
  changed lines out of the question count. Split the change, or declare the path
  generated.
- **A binary change is the opposite case.** There is nothing to ask about a
  binary change, so it leaves the corpus and the question count, like a
  generated file.
- **A PR with approximately 4,000 changed lines or more skips the difficulty
  judge.** The difficulty judge is a model call that rates how difficult a diff
  is to review, and it scales the question count. The skip is safe, because the
  size of the diff alone limits the count to 20.

## Troubleshooting

- **GitHub ignores `/quiz`.** In most cases, the caller workflow is not on your
  default branch yet — see [The most common
  mistake](#the-most-common-mistake). The other cause is the **author
  association** of the commenter. GitHub uses this label for the relation
  between a person and the repo, for example `OWNER`, `MEMBER`, or
  `COLLABORATOR`. The allowed list is `OWNER,MEMBER,COLLABORATOR` by default.
- **The gate step fails with an empty or unclear Databricks error.**
  `QUIZ_WAREHOUSE_ID` has no value, or its value names no warehouse.
- **The run reports "Could not resolve the quiz app URL".** The `app_name` input
  does not match a deployed app, or the CLI cannot see that app. Give the
  `app_url` input a value.
- **The quiz link returns 404, or the page does not load.** The app was asleep.
  Wait about 2 minutes, then load the page again.
- **A PR from a fork never gets a quiz automatically.** GitHub withholds the
  secrets from a workflow that a fork triggers, so a maintainer must write
  `/quiz` on the PR. See [SECURITY.md](../SECURITY.md).
- **A pass becomes a failure.** The gate reads the most recent result for each
  commit. A failed attempt after a passed attempt blocks the merge again. Take
  the quiz again and get 100%.
- **You must check the gate again, without a new commit and without a new
  quiz.** Write the comment `/quiz-check`. It evaluates `quiz-gate` against the
  quiz result that exists for the head commit, and it generates no new
  questions.
- **The gate passed, but there was no quiz.** The gate waived the PR. The status
  description names the reason: "no reviewable changes in this PR" (see [Skipped
  files](#skipped-files)) or "automated dependency update"
  (`QUIZ_WAIVE_AUTHORS`). `/quiz-check` applies both kinds of waiver again.
- **The quiz is shorter than the size of the diff suggests.** The difficulty
  judge rated the diff easy, or the job skipped a part of the diff — see
  [Skipped files](#skipped-files). The job log names every file that it skipped
  as generated.
- **A new `linguist-generated` line has no effect.** There are two usual causes.
  The job reads the line from the base branch, so the line applies from the next
  PR. A directory also needs its trailing slash: `dist/`, not `dist`. See
  [Skipped files](#skipped-files).
- **`/quiz-check` blocks the PRs of a bot.** Set `QUIZ_WAIVE_AUTHORS` on both
  caller workflows. The gate caller also applies the author waiver, so a value
  on the generate caller alone is not sufficient.
