---
status: current
---

# Operating the backend

This guide is for the Databricks workspace admin who deploys and maintains the
pr-quiz backend that one or more repos share. Adopters wire their repos to it
separately — see [adopting.md](adopting.md).

## What the backend is

One backend serves the quiz for every repo that points at it. It consists of:

- a Unity Catalog schema (default `workspace.pr_quiz`) with two Delta tables,
  `question_pool` and `quiz_results`;
- a serverless job (`pr-quiz-generator`) that generates questions from a PR
  diff;
- a Streamlit app (`pr-quiz`) that serves and grades quizzes and posts commit
  statuses;
- a **CI service principal** (`pr-quiz-ci`) that GitHub Actions uses to run the
  job and manage the app.

Before deploying, check the backend-operator column in
[adopting.md Prerequisites](adopting.md#prerequisites) for the workspace
rights and tools you need.

## Deploying

Use the installer's `backend` step (org admin, once per workspace):

```bash
python installer/pr_quiz_install.py doctor --profile <p>
python installer/pr_quiz_install.py backend --profile <p>
```

`backend` renders and deploys the bundle, creates the tables, stores the GitHub
token secret, creates the CI service principal, applies grants, and writes
`pr-quiz-backend.json` — the handoff file adopters need. `--dry-run` previews
the mutating commands; `--force` re-renders, overwrites secrets, and mints a
new SP secret. Re-runs are idempotent.

For this repo's own dogfood backend, the equivalent `just` recipes exist
(`just deploy`, `just init-tables`, `just put-github-secret`,
`just create-ci-sp`, `just grants`, `just protect`); the installer is the
canonical path for a fresh workspace.

## Serverless environment version

The generation job (`pr-quiz-generator`) runs on serverless job compute and pins
`environment_version: "4"` in
[`template/{{.project_name}}/resources/quiz_job.yml`](../template/%7B%7B.project_name%7D%7D/resources/quiz_job.yml).
That serverless base environment supplies the Python runtime plus the
third-party libraries the job imports at run time — `requests` and
`databricks-sdk` — alongside Spark. Bumping the version is a deliberate, tested
change: a new base image can shift library versions and behavior. After any
bump, re-run the test suite (`just test`) and a live generation
(`just run-job <pr_number> <head_sha> <repo>`).

## Grants

Both service principals need scoped access — the installer's `backend` applies
this; the `just grants` recipe is the reference:

- **App service principal**: `USE CATALOG` on the catalog; `USE SCHEMA`,
  `SELECT`, `MODIFY` on the schema; and `CAN_READ` on the deploying user's
  bundle folder (an app start fails with "no files found" without it).
- **CI service principal**: `USE CATALOG`; `USE SCHEMA`, `SELECT` on the schema;
  `CAN_USE` on the warehouse; `CAN_MANAGE_RUN` on the job; `CAN_MANAGE` on the
  app; and `CAN_READ` on the bundle folder.

Keep schema `SELECT` limited to these principals plus operators. Anyone with
`SELECT` can read quiz questions **and their correct answers** from
`question_pool` — the quiz is a good-faith review aid, not a secret exam.

## Secret and token rotation

- **GitHub token** (Databricks secret, default scope key `github_token`): used
  by the generation job to read PR diffs and post statuses. Rotate by storing a
  new token: `databricks secrets put-secret <scope> <key> --string-value <tok>`
  (or `just put-github-secret`, which scrapes it from your git credentials).
  The job falls back to anonymous diff access if the secret is missing, which
  is rate-limited and fails on private repos.
- **CI service-principal OAuth secret** (`DATABRICKS_CLIENT_SECRET` in each
  consumer repo): Databricks OAuth secrets for service principals **expire**.
  Track the expiry and rotate before it lapses — mint a new secret on the SP
  page (Settings > Identity and access > Service principals > `pr-quiz-ci` >
  Secrets) and update `DATABRICKS_CLIENT_SECRET` in every consumer repo. A
  lapsed secret breaks quiz generation and the gate for **all** repos on this
  backend at once.
- **GitHub token expiry**: if you used a fine-grained or classic PAT rather than
  a GitHub App installation token, it also expires — rotate it the same way.

## Warehouse cost

The gate query and the app's grading queries run on a SQL warehouse (on Free
Edition, the Serverless Starter Warehouse). Cost is driven by how often quizzes
are generated and taken, and by the warehouse auto-stop setting. Keep auto-stop
short. Question generation runs on serverless job compute and calls a
foundation-model endpoint; large diffs generate more questions and more model
calls (capped at 20 questions and 5 diff chunks).

## Teardown

To decommission a backend:

1. Remove branch protection and the caller workflows from every consumer repo
   (or run the installer's onboard with protection disabled), so repos stop
   depending on it.
2. Delete the bundle-deployed resources: `databricks bundle destroy` from the
   rendered bundle directory (removes the job, app, and schema resources).
3. Drop the schema/tables if `destroy` left them, revoke the grants, and delete
   the CI service principal.
4. Remove the GitHub token secret and its scope if unused elsewhere.
5. Delete `pr-quiz-backend.json` and any local `.ci-sp-client-id` file.
