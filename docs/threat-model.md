---
status: facts
---

# Threat model

This expands the trust-model summary in [SECURITY.md](../SECURITY.md) with the
assets, actors, trust boundaries, accepted risks, and the mitigations planned
for v2. It describes design decisions and is not tied to a specific code
revision.

## Assets

| Asset | Where it lives | Why it matters |
| --- | --- | --- |
| Databricks credentials | `DATABRICKS_*` GitHub Actions secrets; the CI service principal's OAuth secret | Grant access to run jobs, manage the app, and query the workspace |
| GitHub token | Databricks secret scope (`github_token`) | Reads PR diffs and posts commit statuses |
| Quiz answers | `question_pool` Delta table | Correct answers; readable knowledge defeats the gate if leaked |
| Quiz results | `quiz_results` Delta table | The pass/fail record the gate reads |
| The merge gate itself | `quiz-gate` commit status + branch protection | The control that decides whether a PR can merge |
| Adopters' runners | GitHub-hosted runners executing the reusable workflows | Run our workflow code with adopters' secrets in scope |

## Actors

- **PR author (good faith)** — wants to pass the quiz to merge.
- **Reviewer / team member** — takes quizzes; the intended user.
- **Repository collaborator (write access)** — trusted with write; the boundary
  of what v1 defends against (see accepted risks).
- **Outside contributor / fork author** — untrusted; must not obtain secrets or
  trigger paid work.
- **Backend operator / workspace admin** — deploys and can read all tables.
- **Upstream maintainer (this repo)** — controls the reusable workflow code that
  runs on every adopter's runner.

## Trust boundaries

1. **Fork PR to base repo.** Fork-PR events get a read-only token and no
   secrets. The `quiz-generate` workflow additionally guards on
   `head.repo.full_name == github.repository`, so fork PRs are skipped rather
   than run without secrets. Only a maintainer `/quiz` comment (base-repo
   context, association-gated) generates for a fork PR.
2. **Comment author to workflow.** `/quiz` and `/quiz-check` are gated on
   `author_association` with a padded-token match, and `concurrency` groups cap
   fan-out from comment spam (denial-of-wallet on public repos).
3. **Untrusted event data to shell.** Event-derived values pass through env
   vars, never inline `${{ }}` in `run:` scripts, blocking shell injection on
   the runner.
4. **Upstream repo to adopter runner.** Adopters run our reusable workflows.
   Pinning to `@v1`/SHA bounds what code runs; a moving-branch reference would
   let anyone who can push to our default branch control adopters' runs. The
   `gate-check` action reference inside `quiz-gate.yml` is the sharpest case:
   it resolves at runtime independent of the adopter's own pin.
5. **Backend admin to answers.** Anyone with `SELECT` on the schema can read
   quiz answers. Grants are scoped to the app/CI service principals and
   operators.

## Accepted risks (v1) and v2 mitigations

| # | Risk | Why accepted in v1 | v2 mitigation |
| --- | --- | --- | --- |
| 1 | A write-access collaborator can forge a passing `quiz-gate` **commit status** via the API without taking the quiz | The gate raises the bar for good-faith reviewers; it is not a control against someone who already has write access | Move to the **GitHub Checks API**: a check run is bound to the creating GitHub App and cannot be forged by a repo collaborator |
| 2 | Quiz answers are readable by backend admins / anyone with schema `SELECT` | The quiz is a review aid, not a secret exam; scoping grants is sufficient for the intended use | Optionally store answers hashed / separate answer visibility from question visibility |
| 3 | The `gate-check` action reference is a runtime ref (`@master` pre-release) | Acceptable only until release; the tag-time CI gate blocks a `v1` tag while it is unpinned | Pinned to the release tag/SHA at tag time; enforced by `release-gate` |
| 4 | Repo-jacking of the pre-rename name | The rename happens before the public tag; the grep gate blocks the old slug at tag time | Old slug purged before tagging; consumers pin to tag/SHA on the public name |

## Non-goals

- Defending against a malicious workspace admin (they own the backend).
- Defending against a malicious write-access collaborator in v1 (see risk 1).
- Preventing a determined author from memorizing answers they can already read;
  retakes rotate questions to make this impractical, not impossible.
