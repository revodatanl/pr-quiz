"""Syntax guard for the shipped caller-workflow templates.

templates/callers/*.yml live outside .github/workflows/, so actionlint never
parses them in CI - a broken template would ship silently and only fail on the
adopter's default branch. This parses each template as YAML and pins the
load-bearing structure: triggers, an explicit permissions block, and a pinned
reusable-workflow reference wired to the secrets.

(test_installer.py already guards byte-level drift between these files and the
installer's embedded copies, so parsing the on-disk files covers both.)
"""
from pathlib import Path

import pytest
import yaml  # hard dependency: a skipped guard is a silently unshipped guard

ROOT = Path(__file__).resolve().parents[1]
CALLERS = ROOT / "templates" / "callers"
TEMPLATE_NAMES = sorted(p.name for p in CALLERS.glob("*.yml"))


def _load(name: str) -> dict:
    return yaml.safe_load((CALLERS / name).read_text(encoding="utf-8"))


def test_both_templates_present():
    assert TEMPLATE_NAMES == ["quiz-gate.yml", "quiz-generate.yml"]


@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_template_parses_to_workflow(name):
    doc = _load(name)
    assert isinstance(doc, dict)
    # YAML 1.1 reads the unquoted `on:` key as boolean True.
    triggers = doc.get("on", doc.get(True))
    assert isinstance(triggers, dict) and triggers, f"{name} has no triggers"
    assert isinstance(doc.get("permissions"), dict), (
        f"{name} must carry the explicit permissions block the reusable "
        "workflow documents as the caller's responsibility"
    )
    assert doc.get("jobs"), f"{name} declares no jobs"


def test_generate_template_passes_waive_authors():
    job = next(iter(_load("quiz-generate.yml")["jobs"].values()))
    waive = job["with"]["waive_authors"]
    assert "QUIZ_WAIVE_AUTHORS" in waive and "dependabot[bot]" in waive


@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_template_calls_reusable_workflow_with_secrets(name):
    jobs = _load(name)["jobs"]
    assert len(jobs) == 1, f"{name} should hold exactly one thin caller job"
    job = next(iter(jobs.values()))
    uses = job.get("uses", "")
    # The OWNER/REPO placeholder must stay pinned to a ref, never a bare path.
    assert "/.github/workflows/" in uses and "@" in uses, (
        f"{name} caller job must `uses:` the reusable workflow at a pinned ref"
    )
    assert set(job.get("secrets", {})) == {
        "databricks_host",
        "databricks_client_id",
        "databricks_client_secret",
    }, f"{name} must pass exactly the three Databricks secrets through"
