---
linter: {cli: none, config: none}  # no Markdown linter is installed or configured in this repo yet — add one (e.g. markdownlint-cli2) and fill this in before enabling lint gating
status_values: [current, intended, facts]  # allowed values for every docs/*.md file's `status:` front-matter field
---

# Documentation Structure

This file says where documentation lives in `pr-quiz` and how each doc must mark what kind of information it holds. Read it before adding, moving, or renaming any `.md` file.

## TL;DR

- `README.md` at the repo root is the primary entry point: overview, developer flow, architecture, commands, limits. It is always **current** — it describes the system as it runs now.
- `quiz-merge-gate.mermaid` at the repo root is the diagram the README links to. Edit it with the `mermaid` skill.
- `docs/*.md` holds anything that outgrows a README section (design write-ups, decisions, research). Every file there must declare a `status` in YAML front matter: `current`, `intended`, or `facts`.
- No Markdown linter is installed or configured in this repo yet (see the `linter` field above). Add one before expecting automated lint checks.

## Layout

| Location | Holds | Status rule |
| --- | --- | --- |
| `README.md` | The single entry-point doc: what the project is, how to run and use it, current commands and gotchas | Always **current**. A README describing something else is a bug in the README, not a new doc kind |
| `*.mermaid` (repo root) | Diagrams linked directly from the README | Same status as the doc that links it; edit via the `mermaid` skill |
| `docs/*.md` | Anything too long for a README section: design notes, decision records, research | Must declare `status` in front matter (see below) |
| `docs/img/` | Screenshots and images the README and docs reference | None — binary assets, not prose, so no `status` front matter |
| `docs/documentation-structure.md` | This file: linter setting + layout rules | Governance — none of the three kinds below applies |

Start a new file in `docs/` once a topic outgrows a README section. Don't add `docs/` subfolders until there are enough files under `docs/` to need them.

## Classifying a document

Every file under `docs/` opens with a YAML front-matter block naming its kind:

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
- [quiz-merge-gate.mermaid](../quiz-merge-gate.mermaid)
