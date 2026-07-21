---
linter: {cli: none, config: none}  # cli: the linter's command name; config: path to its config file. Both are "none" because no Markdown linter is installed or configured in this repo yet — add one (e.g. markdownlint-cli2) and fill this in before enabling lint gating (failing CI on lint errors)
status_values: [current, intended, facts]  # allowed values for every docs/*.md file's `status:` front-matter field
---

# Documentation Structure

This file says where documentation lives in `pr-quiz` and how each doc must mark what kind of information it holds. Read it before adding, moving, or renaming any `.md` file.

## Layout

| Location | Holds | Status rule |
| --- | --- | --- |
| `README.md` | Quickstart landing page: what the project is, the install commands, how it works, must-read gotchas before wiring a repo, topology trade-offs (shared vs. per-team backend deployment), and a documentation index. Detail that isn't needed on first read (full prerequisites, configuration reference, repo layout, dev setup) lives in `docs/adopting.md` or `CONTRIBUTING.md` instead | Always **current**. A README describing something else is a bug in the README, not a new doc kind |
| `*.mermaid` (repo root) | Diagrams linked directly from the README | Same status as the doc that links it; edit with the project's `mermaid` skill (an AI-assistant helper for Mermaid diagrams), or directly in any text editor — it's plain text |
| `CONTRIBUTING.md` (repo root) | Contributor-facing material: repo layout, local dev setup, test conventions, fixture PRs (deterministic test PRs that exercise the pipeline end to end), PR expectations, and how to report a vulnerability | Always **current** |
| `docs/*.md` | Anything too long for a README section: adopter/operator guides (prerequisites, configuration reference, backend topologies, troubleshooting), design notes, decision records (a written record of a decision and why it was made), research | Must declare `status` in front matter (see below) |
| `docs/img/` | Screenshots and images the README and docs reference | None — binary assets, not prose, so no `status` front matter |
| `SECURITY.md`, `RELEASING.md`, `CHANGELOG.md`, `LICENSE` (repo root) | Standard open-source root files at their conventional GitHub filenames: security policy, release process, version history, license | No `status` front matter — fixed-purpose community files, not part of the `docs/` classification scheme |
| `docs/documentation-structure.md` | This file: linter setting + layout rules | This file has no `status` field — it's governance about the other kinds, not one of them |

Start a new file in `docs/` once a topic outgrows a README section. Don't add `docs/` subfolders until there are enough files under `docs/` to need them.

## Classifying a document

Every file under `docs/` opens with a YAML front-matter block — a `---`-delimited metadata header — naming its kind:

```yaml
---
status: current   # or: intended, facts
---
```

- **current** — describes the system as it works today. Verified against the code; every claim must be true right now.
- **intended** — a plan or proposal, possibly not built yet. Mark it clearly as intended; never quietly edit it later to match what the code became. If it's superseded, retire or replace it instead.
- **facts** — reference material, findings, or decisions that don't depend on code state (for example a decision record or a research summary).

When asking someone to review an `intended` or `facts` doc, say so up front — it must not be judged against current code.

`README.md` never needs this front matter: by the rule above it is always current. If a README section must describe something not yet built, label that section "planned" inline instead of letting the whole file drift from current status.

## References

- [README.md](../README.md)
- [CONTRIBUTING.md](../CONTRIBUTING.md)
- [quiz-merge-gate.mermaid](../quiz-merge-gate.mermaid)
- [adopting.md](adopting.md)
- [operating.md](operating.md)
- [threat-model.md](threat-model.md)
