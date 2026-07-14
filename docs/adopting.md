---
status: current
---

# Adopting pr-quiz

This guide is for a repository maintainer who wants to gate merges in their
GitHub repo behind the quiz. It assumes someone has already stood up the
Databricks backend (see [operating.md](operating.md)); if not, do that first or
ask your backend operator for the handoff file.

## Prerequisites

See [README Prerequisites](../README.md#prerequisites) for the full list across
both roles. As the repo maintainer you need:

- Admin on the GitHub repo, to add secrets/variables and branch protection.
- The [GitHub CLI](https://cli.github.com/) (`gh`) on your `PATH`.
- The backend handoff file `pr-quiz-backend.json` from your backend operator.
- Databricks credentials for the CI service principal, stored as repo secrets:
  `DATABRICKS_HOST`, `DATABRICKS_CLIENT_ID`, `DATABRICKS_CLIENT_SECRET`.

## Installer walkthrough

The guided installer is a single stdlib-only file. Every step is idempotent
(re-runs report `[skip]`), `--dry-run` prints the mutating commands without
running them, and nothing runs branch-protection changes you didn't ask for.

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
branch.** GitHub runs `issue_comment` workflows only from the version on the
repository's default branch. The onboarding PR adds the caller workflow, but
until that PR merges, commenting `/quiz` on any PR is silently ignored. Merge
the onboarding PR first, then use `/quiz`. This is why `--protect-only` exists:
turn on the required status only once the workflow is live.

Two related points:

- The `pull_request: branches:` filter that decides which PRs require the gate
  lives in **your caller workflow**, not in the reusable workflow. Edit it (and
  keep `QUIZ_TARGET_BRANCH` in sync) to match the branch you protect.
- The quiz app sleeps when idle and takes about 2 minutes to wake. The `/quiz`
  workflow pre-starts it, but the first load after idle is slow.

## Manual fallback

If you'd rather not run the installer, wire the repo by hand:

1. Copy `templates/callers/quiz-generate.yml` and
   `templates/callers/quiz-gate.yml` into your repo's `.github/workflows/`.
2. In each, replace `OWNER/REPO` in the `uses:` line with the public
   pr-quiz repo and pin it to `@v1` (or a commit SHA) — never a moving branch.
3. Adjust the `branches:` filter and `QUIZ_TARGET_BRANCH` to the branch you
   protect.
4. Add repo **secrets**: `DATABRICKS_HOST`, `DATABRICKS_CLIENT_ID`,
   `DATABRICKS_CLIENT_SECRET`.
5. Add repo **variable** `QUIZ_WAREHOUSE_ID` (required by the gate caller), plus
   any optional overrides (`QUIZ_APP_NAME`, `QUIZ_JOB_NAME`,
   `QUIZ_RESULTS_TABLE`, `QUIZ_STATUS_CONTEXT`, `QUIZ_TARGET_BRANCH`, and
   `QUIZ_WAIVE_AUTHORS` — comma-separated author logins, no spaces, whose PRs
   bypass the quiz; default `dependabot[bot]`).
   `QUIZ_RESULTS_TABLE` defaults to `workspace.pr_quiz.quiz_results` — set it
   whenever the backend was deployed with a non-default catalog or schema, or
   the gate query fails with `TABLE_OR_VIEW_NOT_FOUND`.
6. Merge those workflows to your default branch.
7. Enable branch protection on the protected branch requiring the `quiz-gate`
   status check.

## Troubleshooting

- **`/quiz` is ignored.** The caller workflow isn't on your default branch yet
  (merge it), or your comment's author association isn't in the allowed list
  (`OWNER,MEMBER,COLLABORATOR` by default).
- **Gate step fails with an empty/cryptic Databricks error.** `QUIZ_WAREHOUSE_ID`
  is unset — the gate caller validates it and tells you to set the variable.
- **"Could not resolve the quiz app URL".** The `app_name` input doesn't match a
  deployed app, or the CLI can't see it; pass `app_url` explicitly.
- **Quiz link 404s or hangs.** The app was asleep; wait ~2 minutes and reload.
- **Fork PRs never get a quiz automatically.** By design — a maintainer must
  post `/quiz`. See [SECURITY.md](../SECURITY.md).
- **Passing then failing.** The gate reads the **latest** result per commit; a
  later failing attempt re-blocks. Retake to 100%.
