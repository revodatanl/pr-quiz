# Security Policy

## Reporting a vulnerability

Report suspected vulnerabilities privately through GitHub's **Report a
vulnerability** button under this repository's **Security** tab (GitHub private
security advisories). Do not open a public issue for a security report.

Please include: what you found, how to reproduce it, and the impact you expect.
We aim to acknowledge a report within a few working days and will coordinate a
fix and disclosure timeline with you.

## Supported versions

Security fixes target the latest `v1` release. Pin the reusable workflows to a
released tag or commit SHA (see below) so you receive fixes deliberately rather
than from a moving branch.

## Trust model

This is the summary; [docs/threat-model.md](docs/threat-model.md) has the full
assets/actors/boundaries breakdown and the accepted-risks table.

### The gate is a commit status, and any writer can forge it (v1)

In v1 the merge gate is a GitHub **commit status** named `quiz-gate`. Branch
protection requires that status to be green. Anyone with write access to the
repository can set a commit status directly via the API, so a collaborator with
write access can forge a passing `quiz-gate` status without taking the quiz.

This is an **accepted risk in v1**: the gate raises the bar for reviewers acting
in good faith; it is not a control against a malicious collaborator who already
has write access. **v2 closes this** by moving to the GitHub Checks API, where a
check run is bound to the GitHub App that created it and cannot be forged by a
repository collaborator.

### Pin the reusable workflows

Consumers call the reusable workflows and the composite action by reference.
**Pin them to a released tag (e.g. `@v1`) or a full commit SHA — never a moving
branch like `@master`.** A moving branch means whoever can push to this repo's
default branch controls what runs on your runners, with your `DATABRICKS_*`
secrets in scope. The caller templates ship with a `@v1` pin and a reminder.

After this repo is renamed to its public name, the old name becomes claimable;
a stale reference to a moving branch on the old name would follow GitHub's
rename redirect into a repository an attacker could claim (repo-jacking). A tag
or SHA pin avoids this.

### Quiz answers live in a Delta table

Generated questions and their correct answers are stored in the
`question_pool` Delta table in your Databricks workspace. Anyone with backend
admin access (workspace admins, and principals granted `SELECT` on the schema)
can read the answers. Treat the quiz as a good-faith review aid, not as a secret
exam. Restrict schema grants to the app and CI service principals plus the
operators who need them.

### Fork pull requests never receive secrets

Fork PRs cannot exfiltrate your Databricks credentials:

- The `quiz-generate` reusable workflow's `pull_request` path runs only when
  `github.event.pull_request.head.repo.full_name == github.repository` — a
  same-repo guard, so fork PRs are skipped.
- GitHub already force-downgrades the token to read-only and withholds repo
  secrets on fork-PR events, so even the skipped run has nothing to leak.
- The only path that generates a quiz for a fork PR is a **maintainer** posting
  `/quiz`. That comment runs in base-repo context (with secrets) and is gated
  on the commenter's author association, so an outside contributor cannot
  trigger it.

### Waived authors skip the quiz by design

Authors listed in the `QUIZ_WAIVE_AUTHORS` repo variable (default
`dependabot[bot]`) bypass the quiz gate: their PRs get an automatic passing
`quiz-gate` status instead of a generated quiz, because a human cannot take a
quiz on an automated dependency bot's PR. Anyone you add to this list can merge
without passing the quiz.

Keep the list tight — only trusted automation. Enter logins comma-separated
with no spaces (e.g. `dependabot[bot],renovate[bot]`); a stray space makes an
entry silently fail to match. The match is on
`github.event.pull_request.user.login`, and GitHub App logins carry a reserved
`[bot]` suffix that a normal user cannot spoof. The waiver covers the automatic
gate only: it does not bypass your other required status checks or review
rules, and a maintainer can still post `/quiz` to generate a quiz on a waived
author's PR.

## Hardening built in

- Comment triggers (`/quiz`, `/quiz-check`) are gated on author association
  with a padded-token match, so associations like `NONE` cannot substring-match
  into the allowed list.
- Event-derived values are passed to shell steps through environment variables,
  never interpolated inline into `run:` scripts (shell-injection hardening).
- Third-party actions are SHA-pinned.
- `concurrency` groups cap comment-triggered runs so comment spam cannot fan out
  parallel jobs (denial-of-wallet protection on public repos).
