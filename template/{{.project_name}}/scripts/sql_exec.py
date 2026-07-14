"""Execute SQL against a Databricks warehouse via the Statement Execution API.

Auth goes through the Databricks CLI (profile flag locally, env vars in CI).
"""
import argparse
import json
import pathlib
import subprocess
import sys


def run_statement(statement: str, warehouse_id: str, profile: str | None) -> dict:
    cmd = ["databricks", "api", "post", "/api/2.0/sql/statements"]
    if profile:
        cmd += ["--profile", profile]
    body = {"statement": statement, "warehouse_id": warehouse_id, "wait_timeout": "30s"}
    cmd += ["--json", json.dumps(body)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"databricks api post failed: {proc.stderr.strip()}")
    result = json.loads(proc.stdout)
    state = result.get("status", {}).get("state")
    if state != "SUCCEEDED":
        raise RuntimeError(f"statement {state}: {json.dumps(result.get('status'))}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", help="SQL file; statements split on ';'")
    source.add_argument("--statement", help="single SQL statement")
    parser.add_argument("--warehouse-id", required=True)
    parser.add_argument("--profile", default=None)
    args = parser.parse_args()

    if args.file:
        text = pathlib.Path(args.file).read_text(encoding="utf-8")
        statements = [s.strip() for s in text.split(";") if s.strip()]
    else:
        statements = [args.statement]

    for stmt in statements:
        result = run_statement(stmt, args.warehouse_id, args.profile)
        print(f"OK: {stmt.splitlines()[0][:80]}")
        for row in result.get("result", {}).get("data_array") or []:
            print("  ", row)
    return 0


if __name__ == "__main__":
    sys.exit(main())
