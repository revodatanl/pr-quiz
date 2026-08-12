"""Merge-gate verdict: does the LAST published quiz result for this exact
(provider, repo, head_sha) score 100%?

Exit 0 = gate passes, exit 1 = gate blocks, exit 2 = infrastructure error
(warehouse/CLI failure or bad configuration - not a verdict on the quiz).
Prints a one-line reason (used as the commit status description by the
quiz-gate workflow / gate-check action).
"""
import argparse
import json
import re
import subprocess
import sys

DEFAULT_TABLE = "workspace.pr_quiz.quiz_results"

# GitHub caps commit-status descriptions at 140 characters; longer ones 422.
MAX_VERDICT_LEN = 140

# Table names are interpolated into the query (the SQL Statement Execution API
# has no bind-parameter form for identifiers), so validate the shape before
# it ever reaches an f-string.
TABLE_NAME_RE = re.compile(r"^[A-Za-z0-9_.]+$")


def clip_verdict(message: str) -> str:
    """Collapse a message to one line and cap it at MAX_VERDICT_LEN chars so it
    always fits a GitHub commit-status description."""
    line = " ".join(message.split())
    if len(line) <= MAX_VERDICT_LEN:
        return line
    return line[: MAX_VERDICT_LEN - 3] + "..."


def build_gate_query(table: str) -> str:
    """Gate query for `table`, filtered by the full (provider, repo, head_sha)
    tenant key so an identical commit landing in two repos cannot inherit the
    other repo's pass."""
    if not TABLE_NAME_RE.fullmatch(table):
        raise ValueError(f"invalid results table name: {table!r}")
    return (
        f"SELECT score_pct, passed, submitted_at, n_questions FROM {table} "
        "WHERE head_sha = :sha AND repo = :repo AND provider = :provider "
        "ORDER BY submitted_at DESC LIMIT 1"
    )


def is_waiver(row: list) -> bool:
    """True when the row records a waive rather than a quiz someone sat.

    The job writes a passing, zero-question row when a PR has nothing quizzable.
    A real attempt always asks at least one question. Anything that is not a
    readable question count - a short row, a NULL, a non-numeric value - reads as
    an attempt, so a waive is only ever claimed on an explicit zero.
    """
    try:
        return int(row[3]) == 0
    except (IndexError, TypeError, ValueError):
        return False


def format_verdict(row: list | None, sha: str, repo: str) -> tuple[bool, str]:
    """(passed, one-line message) for the latest result row (or None)."""
    short = sha[:8]
    if row is None:
        message = f"BLOCKED: no quiz result for {repo}@{short} - comment /quiz and take the quiz"
        return False, clip_verdict(message)
    score, passed = float(row[0]), row[1] in ("true", "True", True)
    if passed and score == 100.0:
        if is_waiver(row):
            return True, clip_verdict(
                f"PASSED: quiz waived on {repo}@{short} - no reviewable changes"
            )
        return True, clip_verdict(f"PASSED: quiz scored 100% on {repo}@{short}")
    return False, clip_verdict(
        f"BLOCKED: last quiz on {repo}@{short} scored {score:.0f}% "
        "(needs 100%) - retake via the quiz app"
    )


def format_error(exc: Exception) -> str:
    """One-line ERROR verdict for infrastructure failures (exit 2), so a broken
    warehouse/CLI is distinguishable from a blocked gate in the commit status."""
    return clip_verdict(f"ERROR: {exc}")


def latest_result(
    sha: str, repo: str, provider: str, table: str, warehouse_id: str, profile: str | None
) -> list | None:
    query = build_gate_query(table)
    cmd = ["databricks", "api", "post", "/api/2.0/sql/statements"]
    if profile:
        cmd += ["--profile", profile]
    body = {
        "statement": query,
        "warehouse_id": warehouse_id,
        "wait_timeout": "30s",
        "parameters": [
            {"name": "sha", "value": sha},
            {"name": "repo", "value": repo},
            {"name": "provider", "value": provider},
        ],
    }
    cmd += ["--json", json.dumps(body)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"statement execution failed: {proc.stderr.strip()}")
    result = json.loads(proc.stdout)
    state = result.get("status", {}).get("state")
    if state != "SUCCEEDED":
        raise RuntimeError(f"statement {state}: {json.dumps(result.get('status'))}")
    rows = result.get("result", {}).get("data_array") or []
    return rows[0] if rows else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True, help="head commit SHA to evaluate")
    parser.add_argument("--repo", required=True,
                        help='repository the commit belongs to, e.g. "owner/name"')
    parser.add_argument("--provider", default="github",
                        help="SCM provider that produced the commit (default: %(default)s)")
    parser.add_argument("--warehouse-id", required=True,
                        help="Databricks SQL warehouse ID to run the gate query against")
    parser.add_argument("--table", default=DEFAULT_TABLE,
                        help="fully qualified quiz_results table (default: %(default)s)")
    parser.add_argument("--profile", default=None,
                        help="Databricks CLI profile (optional; CI authenticates via env)")
    args = parser.parse_args()

    try:
        row = latest_result(
            args.sha, args.repo, args.provider, args.table, args.warehouse_id, args.profile
        )
    except (RuntimeError, ValueError, OSError) as exc:
        # Infra/config failure, not a quiz verdict: print a one-line ERROR
        # verdict (usable as a commit-status description) and exit 2 so callers
        # can tell it apart from a blocked gate. OSError covers a missing
        # databricks CLI (FileNotFoundError from subprocess), which must still
        # produce an ERROR verdict instead of an unhandled traceback.
        print(format_error(exc))
        return 2

    passed, message = format_verdict(row, args.sha, args.repo)
    print(message)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
