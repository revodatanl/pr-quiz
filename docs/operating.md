---
status: current
---

# Operating the backend

This guide is for the Databricks workspace administrator who deploys and
maintains the pr-quiz backend. One or more repos share this backend. The repo
maintainers connect their repos to it separately — see
[adopting.md](adopting.md).

## What the backend is

One backend serves the quiz for every repo that uses it. It holds these parts:

- a Unity Catalog schema (default `workspace.pr_quiz`) with two Delta tables,
  `question_pool` and `quiz_results`
- a serverless job (`pr-quiz-generator`) that generates questions from the diff
  of a PR
- a Streamlit app (`pr-quiz`) that serves the quizzes, grades them, and writes
  commit statuses
- a **CI service principal** (`pr-quiz-ci`) that GitHub Actions uses to run the
  job and to manage the app

Before you deploy, read the backend-operator column in [adopting.md
Prerequisites](adopting.md#prerequisites). It lists the workspace rights and the
tools that you need.

## Deploying

The organization administrator runs the `backend` step of the installer one time
for each workspace:

```bash
python installer/pr_quiz_install.py doctor --profile <p>
python installer/pr_quiz_install.py backend --profile <p>
```

The `backend` step does this work:

- It renders the bundle and deploys it.
- It creates the tables.
- It stores the GitHub token secret.
- It creates the CI service principal and applies the grants.
- It writes `pr-quiz-backend.json`, the handoff file that repo maintainers need.

The `--dry-run` flag shows the commands that change the workspace, but it does
not run them. The `--force` flag renders the bundle again, overwrites the
secrets, and creates a new secret for the service principal. A second run is
idempotent: it makes no duplicate resources.

This repo also has its own backend, which runs the quiz on the PRs of pr-quiz
itself. The equivalent `just` recipes are `just deploy`, `just init-tables`,
`just put-github-secret`, `just create-ci-sp`, `just grants`, and
`just protect`. For a new workspace, use the installer.

## Serverless environment version

The generation job (`pr-quiz-generator`) runs on serverless job compute. It pins
`environment_version: "4"` in
[`template/{{.project_name}}/resources/quiz_job.yml`](../template/%7B%7B.project_name%7D%7D/resources/quiz_job.yml).
That serverless base environment supplies Spark, the Python runtime, and the
third-party libraries that the job imports at run time: `requests` and
`databricks-sdk`.

A new version is a deliberate change that you must test, because a new base
image can change library versions and behavior. After a change of the version,
run the test suite again (`just test`). Then run a live generation
(`just run-job <pr_number> <head_sha> <repo>`).

## Grants

Both service principals need limited access. The `backend` step of the installer
applies these grants, and the `just grants` recipe is the reference:

- **App service principal**: `USE CATALOG` on the catalog. `USE SCHEMA`,
  `SELECT`, and `MODIFY` on the schema. `CAN_READ` on the bundle folder of the
  user that deploys. Without `CAN_READ`, the app does not start, and it reports
  "no files found".
- **CI service principal**: `USE CATALOG` on the catalog. `USE SCHEMA` and
  `SELECT` on the schema. `CAN_USE` on the warehouse. `CAN_MANAGE_RUN` on the
  job. `CAN_MANAGE` on the app. `CAN_READ` on the bundle folder.

Give `SELECT` on the schema only to these principals and to the operators. A
user with `SELECT` can read the quiz questions **and their correct answers**
from `question_pool`. The quiz is an aid for review in good faith, not a secret
examination.

## Secret and token rotation

- **GitHub token** (a Databricks secret, default scope key `github_token`). The
  generation job uses this token to read PR diffs and to write commit statuses.
  To rotate the token, store a new one with
  `databricks secrets put-secret <scope> <key> --string-value <tok>`. The recipe
  `just put-github-secret` does the same, and it takes the token from your git
  credentials. If the secret is absent, the job reads the diffs anonymously.
  GitHub limits the rate of anonymous requests, and anonymous access fails on a
  private repo.
- **OAuth secret of the CI service principal** (`DATABRICKS_CLIENT_SECRET` in
  each consumer repo). A Databricks OAuth secret for a service principal
  **expires**. Record the expiry date, and replace the secret before that date.
  Create a new secret on the page of the service principal (Settings > Identity
  and access > Service principals > `pr-quiz-ci` > Secrets). Then update
  `DATABRICKS_CLIENT_SECRET` in every consumer repo. An expired secret stops
  quiz generation and the gate for **all** repos on this backend at the same
  time.
- **Expiry of the GitHub token**. If you stored a fine-grained PAT or a classic
  PAT instead of an installation token of a GitHub App, that token also expires.
  Rotate it in the same way.

## Warehouse cost

The gate query and the grading queries of the app run on a SQL warehouse. On
Free Edition, this warehouse is the Serverless Starter Warehouse. Two factors
drive the cost: the number of quizzes that users generate and take, and the
auto-stop setting of the warehouse. A short auto-stop time keeps the cost low.

Question generation runs on serverless job compute, and it calls a
foundation-model endpoint. A large diff gives more questions and more model
calls. The limits are 20 questions and 5 diff chunks. Generated files and the
contents of deleted files do not count toward the question count. A PR that is
mostly lockfile changes therefore costs much less than its line count suggests.
See [Skipped files](adopting.md#skipped-files).

## Teardown

To remove a backend:

1. Remove the branch protection and the caller workflows from every consumer
   repo, so that no repo depends on the backend. Alternatively, run the
   `onboard` step of the installer with branch protection disabled.
2. Remove the resources that the bundle deployed. Run
   `databricks bundle destroy` from the rendered bundle directory. This command
   removes the job, the app, and the schema resources.
3. If `destroy` left the schema or the tables, remove them. Revoke the grants.
   Remove the CI service principal.
4. Remove the GitHub token secret. If no other system uses the scope, remove the
   scope.
5. Remove `pr-quiz-backend.json`. If a local `.ci-sp-client-id` file exists,
   remove it too.
