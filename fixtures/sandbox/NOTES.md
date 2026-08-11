# Sandbox notes

This folder only exists on `fixture/*` branches. It carries generated
content used to exercise the PR quiz merge gate end to end.

## Why a docs-only fixture?

A pure-markdown change is the easiest possible diff to review. The
difficulty judge should rate it near the 0.2 minimum, and with only a
dozen changed lines the quiz stays at a single question.

Regenerate with `just fixture-prs small`; clean up with `just fixture-clean`.
