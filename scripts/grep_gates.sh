#!/usr/bin/env bash
# Repo-hygiene grep gates.
#
# Two tiers of checks, all run over tracked files only (git ls-files, so
# .databricks/, .venv, build output and other local state are never scanned):
#
#   default    always-on gates that must pass on every commit. They guard the
#              SHIPPED / consumer-facing surface (the bundle template, the
#              composite action, the installer, the reusable workflows and the
#              caller templates) against hardcoded dogfood infrastructure
#              values leaking out of this repo.
#
#   --release  additionally run the tag-time gates. They enforce the release
#              invariant described in RELEASING.md (actions/gate-check must be
#              pinned to a version tag, never a moving branch). CI wires them to
#              tag pushes only.
#
# A gate fails (exit 1) if its pattern is found; offending lines are printed.
#
# The always-on gates scan the entire tracked tree except docs (*.md), mermaid
# diagrams (*.mermaid) and the gate machinery itself; the dogfood values have
# been scrubbed from .just/, the tracked .dev/init-config.example.json and
# .vscode/, so those paths are now scanned too. The canonical placeholder
# warehouse id 0123456789abcdef is allowlisted per line, so a schema example or
# installer test carrying only the placeholder passes while a real id fails.
set -euo pipefail

# The gate machinery must never scan itself: this script and ci.yml both embed
# the very patterns below as string literals.
SELF_EXCLUDES=(
  ':(exclude).github/workflows/ci.yml'
  ':(exclude)scripts/grep_gates.sh'
)

# Always-on gates additionally skip docs and mermaid diagrams.
ALWAYS_EXCLUDES=(
  "${SELF_EXCLUDES[@]}"
  ':(exclude)*.md'
  ':(exclude)*.mermaid'
)

# Release gates skip only docs and the gate machinery: an unpinned gate-check
# ref must be caught everywhere real, dev recipes included.
RELEASE_EXCLUDES=(
  "${SELF_EXCLUDES[@]}"
  ':(exclude)*.md'
)

status=0

# deny BASE_ARRAY_NAME NAME PATTERN [extra pathspec excludes...]
# Fails the gate if PATTERN (a grep -E regex) matches the CONTENTS of any
# non-excluded tracked file. Uses GNU grep for \b support (present on ubuntu
# runners and Git Bash); -I skips binary files. The canonical fake warehouse id
# 0123456789abcdef is allowlisted by stripping the token itself (not the whole
# line) before re-applying the pattern, so a line with only the placeholder
# passes while any real id survives the strip and still fails - even when a real
# id shares a line with the placeholder (a file with both fails).
deny() {
  local -n base=$1
  local name=$2 pattern=$3
  shift 3
  local hits
  hits=$(git ls-files -z -- "${base[@]}" "$@" \
    | xargs -0 -r grep -HnIE "$pattern" 2>/dev/null \
    | sed 's/0123456789abcdef//g' \
    | grep -E "$pattern" || true)
  if [ -n "$hits" ]; then
    echo "FAIL  $name"
    echo "$hits" | sed 's#^#  #'
    status=1
  else
    echo "ok    $name"
  fi
}

echo "== always-on gates =="

# Hardcoded Databricks App URLs must not ship in the template/action/installer/
# reusable workflows/caller templates - they would pin adopters to this repo's
# dogfood workspace.
deny ALWAYS_EXCLUDES "no hardcoded databricksapps.com URLs" 'databricksapps\.com'

# 16-hex SQL warehouse IDs are workspace-specific; anywhere in the shipped
# surface is a leak. The canonical placeholder 0123456789abcdef is allowlisted
# per line by deny(). Word boundaries keep 40-char commit SHAs (e.g. SHA-pinned
# action refs) from matching.
deny ALWAYS_EXCLUDES "no hardcoded 16-hex warehouse IDs" '\b[0-9a-f]{16}\b'

if [ "${1:-}" = "--release" ]; then
  echo "== release (tag-time) gates =="

  # RELEASE-BLOCKING: the gate-check action must stay pinned to a version tag,
  # never a moving branch. gate-check@master (or any branch) resolves at runtime
  # for every adopter independent of their @v1 workflow pin, so a moving ref
  # there hands whoever can push this repo's default branch control of every
  # gate step. (see the comment at quiz-gate.yml and RELEASING.md.)
  deny RELEASE_EXCLUDES "actions/gate-check must be pinned (not @master)" 'gate-check@master'
fi

if [ "$status" -ne 0 ]; then
  echo "grep gates FAILED"
  exit 1
fi
echo "grep gates passed"
