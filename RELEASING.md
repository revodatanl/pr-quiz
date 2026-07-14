# Releasing

pr-quiz is consumed by pinning its reusable workflows (GitHub workflows other
repos call) and its `gate-check` composite action (a small action bundled in
this repo) to a released reference — a tag or a commit SHA.
[Commitizen](https://commitizen-tools.github.io/commitizen/) (the `cz` command,
pinned in [`requirements-dev.txt`](requirements-dev.txt)) drives releases through
two GitHub Actions workflows. You never tag by hand.

`cz` reads the commit history to pick the next version, so it relies on
[Conventional Commits](https://www.conventionalcommits.org/) — a commit-message
convention where the subject starts with a type like `feat:` or `fix:`.

## TL;DR

- Releases use [Semantic Versioning](https://semver.org/): an immutable
  `vX.Y.Z` tag per release, plus a moving `v1` tag consumers pin to.
- A release runs in two human-triggered stages:
  1. [`release-prep.yml`](.github/workflows/release-prep.yml) opens a
     `chore(release): vX.Y.Z` PR (version bump + changelog). No tag yet.
  2. [`release-publish.yml`](.github/workflows/release-publish.yml) tags and
     publishes after that PR is merged.
- Before any release, one invariant must hold: the `gate-check` action stays
  pinned to a version tag, never a moving branch — enforced by
  [`scripts/grep_gates.sh`](scripts/grep_gates.sh) and kept in sync by `cz bump`.
- Some protections live only in the repo admin UI. See
  [Repository settings to enable](#repository-settings-to-enable).

## Versioning scheme

- Cut an immutable version tag per release: `v1.0.0`, `v1.0.1`, `v1.1.0`, ...
- Maintain a **moving major tag** `v1` that always points at the newest
  `v1.x.y`. Consumers pin `@v1` to get patches automatically, or pin a full
  commit SHA for maximum strictness.
- A breaking change to how consumers call pr-quiz — the reusable-workflow
  inputs or the `gate-check` action's inputs — requires a new major tag (`v2`)
  and a matching moving tag.

## Preconditions

One invariant must hold before any tag is cut, and the release gate enforces it:
the `gate-check` composite action in
[`.github/workflows/quiz-gate.yml`](.github/workflows/quiz-gate.yml) must stay
pinned to a **version tag, never a moving branch**. That ref resolves at runtime
for every adopter, independent of their `@v1` workflow pin, so a moving branch
there would hand whoever can push this repo's default branch control of every
adopter's gate step — with the `DATABRICKS_*` secrets in scope. `cz bump` keeps
the pin in sync with each release automatically (see below); the `--release`
tier of [`scripts/grep_gates.sh`](scripts/grep_gates.sh) is the backstop. CI
runs the script on every `v*` tag push (the tag-only `release-gate` job in
[`.github/workflows/ci.yml`](.github/workflows/ci.yml)), and both release
workflows re-run it before doing anything. Run it locally first:

```bash
bash scripts/grep_gates.sh --release
```

## How a release happens

Two workflows, run in order. Both are `workflow_dispatch` (triggered by hand
from the Actions tab). Neither pushes commits to the default branch; they
create only a PR, tags, and a release.

### Stage 1 — prep the release PR

Run [`release-prep.yml`](.github/workflows/release-prep.yml). It takes an
optional `version` input (an exact version like `1.2.3`; leave it empty to let
`cz` auto-increment from the Conventional-Commit history). The workflow:

1. Runs the release gates and `pytest`, failing fast before it changes anything.
2. Runs `cz bump --files-only --changelog`. This updates the version in
   [`.cz.toml`](.cz.toml), rewrites the `gate-check@vX.Y.Z` pin in
   [`quiz-gate.yml`](.github/workflows/quiz-gate.yml) (see
   [How the gate-check pin stays current](#how-the-gate-check-pin-stays-current)),
   and regenerates [`CHANGELOG.md`](CHANGELOG.md) — editing files only, without
   committing or tagging.
3. Commits those changes to a new `release/vX.Y.Z` branch and opens a
   `chore(release): vX.Y.Z` PR against the default branch.

A human then reviews the PR — **especially the changelog entry and the
auto-updated `gate-check` pin** — and squash-merges it. No tag exists yet.

### Stage 2 — tag and publish

After the stage-1 PR is merged, run
[`release-publish.yml`](.github/workflows/release-publish.yml) on the
default-branch HEAD. It:

1. Re-runs the release gates and `pytest` on the exact commit being tagged.
2. Reads the version from [`.cz.toml`](.cz.toml) (`cz version --project`).
3. Asserts the `vX.Y.Z` tag does not already exist, locally or on the remote,
   and refuses to run if it does — a published tag is never moved.
4. Creates the annotated `vX.Y.Z` tag (a tag that carries its own message) and
   force-moves the bare `v1` tag to the same commit, then pushes both.
5. Publishes a GitHub Release, with notes taken from that version's section of
   [`CHANGELOG.md`](CHANGELOG.md).

### How the gate-check pin stays current

[`.cz.toml`](.cz.toml) lists `quiz-gate.yml` in `version_files`. That makes
`cz bump` rewrite the `gate-check@vX.Y.Z` pin in the same bump that sets the new
version — so the "pin the action to the release" invariant can never be
forgotten. The rule only matches a pin that is already a version tag, so it
takes effect from v1.0.1 onward. The first pin is set differently; see
[First release (v1.0.0) bootstrap](#first-release-v100-bootstrap).

### About the changelog format

Commitizen writes changelog headers as `## vX.Y.Z (YYYY-MM-DD)`, not the
Keep-a-Changelog `## [X.Y.Z]` form. Stage 2 extracts release notes by matching
that `## vX.Y.Z` header, so keep every entry in that shape — including the
hand-written v1.0.0 entry.

## First release (v1.0.0) bootstrap

v1.0.0 is special because there is no prior tag for `cz` to bump from, so the
two-stage flow does not fully apply:

- Write its [`CHANGELOG.md`](CHANGELOG.md) entry **by hand**, in the
  `## v1.0.0 (date)` form that stage 2 expects — there is no commit history for
  `cz` to generate it from.
- The `gate-check` pin is set to `@v1.0.0` **by hand** for the first release,
  not by `cz` — `version_files` only rewrites an existing version-tag pin, so
  the very first tag's pin must be written manually.
- Produce the first tag and release by running
  [`release-publish.yml`](.github/workflows/release-publish.yml) directly on the
  default-branch HEAD. There is no stage-1 PR to prepare.

From v1.0.1 onward, use the normal two-stage flow above.

## Repository settings to enable

These GitHub settings cannot be enforced from a workflow. Apply them in the repo
admin UI:

- **Squash-merge commit title = PR title.** The squashed commit subject then
  comes from the PR title, which [`pr-title.yml`](.github/workflows/pr-title.yml)
  lints as a Conventional Commit. That keeps `cz` able to derive versions and
  changelog entries from the default-branch history.
- **Tag ruleset on `v*.*.*`** blocking tag delete and update, so published
  version tags are immutable. **Exclude the bare `v1` tag** — it must stay
  force-movable so stage 2 can re-point it each release.
- **Immutable releases**, so a published release's assets and target cannot be
  silently changed after the fact.
- **Require review from Code Owners** in branch protection, so
  [`.github/CODEOWNERS`](.github/CODEOWNERS) is enforced rather than advisory.

## After renaming

Once the public name is live, tell adopters to update their caller workflows'
`uses:` references to the new `owner/repo` and keep the `@v1` (or SHA) pin.
