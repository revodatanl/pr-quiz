#!/usr/bin/env python3
"""pr-quiz guided installer.

Single-file, stdlib-only (Python >= 3.10, Windows + POSIX). Wraps the
Databricks CLI and GitHub CLI; both must be on PATH (`doctor` checks).

Subcommands
    doctor    preflight: CLIs on PATH, workspace + GitHub auth working
    backend   org admin, one-time per workspace: render + deploy the bundle,
              create tables, store the GitHub token, create the CI service
              principal, apply grants, write pr-quiz-backend.json
    onboard   repo maintainer, per consumer repo: open a PR with the caller
              workflows, set repo secrets/variables, require the quiz-gate
              status on the default branch

`--dry-run` (backend/onboard) executes read-only lookups but only PRINTS
mutating commands. Nothing destructive happens without `--force`.

Design note: all decision logic lives in pure functions (testable without
subprocess); every external command goes through a single Runner callable.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

# =========================================================================
# SECTION: constants
# =========================================================================

DEFAULT_WORKFLOWS_REPO = "revodatanl/pr-quiz"
DEFAULT_WORKFLOWS_REF = "v1"
DEFAULT_STATUS_CONTEXT = "quiz-gate"
DEFAULT_HANDOFF = "pr-quiz-backend.json"
DEFAULT_BUILD_DIR = ".build"
ONBOARD_BRANCH = "pr-quiz/onboard"
# One year; must be the seconds-suffix form -- "8760h" fails to parse.
SP_SECRET_LIFETIME = "31536000s"

GITHUB_TOKEN_PROMPT = (
    "GitHub token for the generation job (classic PAT with `repo` scope --\n"
    "it fetches PR diffs, posts quiz comments, and sets commit statuses).\n"
    "Token (hidden): "
)

SP_UI_FALLBACK = """\
CLI creation failed - create manually: Settings > Identity and access > Service principals
then generate an OAuth secret on the SP page and add GitHub repo secrets:
  DATABRICKS_HOST / DATABRICKS_CLIENT_ID / DATABRICKS_CLIENT_SECRET"""

SP_SECRET_UI_FALLBACK = """\
Generate the OAuth secret manually:
  Settings > Identity and access > Service principals > {name} > Secrets > Generate secret
Store it somewhere safe; `onboard` prompts for it when setting GitHub repo secrets."""


# =========================================================================
# SECTION: embedded caller-workflow templates
# (exact copies of templates/callers/*.yml -- a parity test in
#  tests/test_installer.py keeps them from drifting)
# =========================================================================

CALLER_QUIZ_GENERATE = """\
# PR-quiz caller: generates a quiz for each PR and manages the merge-gate
# commit status. Copy this file to .github/workflows/quiz-generate.yml in
# your repo, then:
#
#   1. Replace OWNER/REPO in `uses:` below and PIN it to a released tag or
#      commit SHA (e.g. @v1) - never a moving branch.
#   2. Set repo secrets: DATABRICKS_HOST, DATABRICKS_CLIENT_ID,
#      DATABRICKS_CLIENT_SECRET (a service principal that can run the quiz
#      job and manage the quiz app).
#   3. Optionally set repo variables (defaults shown below): QUIZ_APP_NAME,
#      QUIZ_JOB_NAME, QUIZ_STATUS_CONTEXT, QUIZ_WAIVE_AUTHORS, QUIZ_TARGET_BRANCH.
#   4. Adjust `branches:` below AND QUIZ_TARGET_BRANCH (or its fallback) to
#      the branch your quiz gate protects: the `branches:` filter only
#      applies to pull_request events, not to /quiz comments - target_branch
#      is what stops /quiz from generating quizzes for PRs into other
#      branches.
#
# Note: issue_comment workflows only run the version on your DEFAULT branch -
# /quiz does nothing until this file is merged there.
name: quiz-generate

on:
  issue_comment:
    types: [created]
  pull_request:
    types: [opened, reopened, synchronize]
    branches: [main]  # branches whose PRs require the quiz gate

permissions:
  contents: read
  pull-requests: write
  statuses: write

jobs:
  quiz-generate:
    uses: OWNER/REPO/.github/workflows/quiz-generate.yml@v1  # <-- replace + pin
    with:
      # `|| 'x'` keeps an unset variable from overriding the reusable
      # workflow's default with an empty string.
      app_name: ${{ vars.QUIZ_APP_NAME || 'pr-quiz' }}
      job_name: ${{ vars.QUIZ_JOB_NAME || 'pr-quiz-generator' }}
      status_context: ${{ vars.QUIZ_STATUS_CONTEXT || 'quiz-gate' }}
      waive_authors: ${{ vars.QUIZ_WAIVE_AUTHORS || 'dependabot[bot]' }}
      target_branch: ${{ vars.QUIZ_TARGET_BRANCH || 'main' }}  # keep in sync with `branches:` above
    secrets:
      databricks_host: ${{ secrets.DATABRICKS_HOST }}
      databricks_client_id: ${{ secrets.DATABRICKS_CLIENT_ID }}
      databricks_client_secret: ${{ secrets.DATABRICKS_CLIENT_SECRET }}
"""

CALLER_QUIZ_GATE = """\
# PR-quiz caller: re-evaluates the merge gate on a /quiz-check comment or a
# manual dispatch. Copy this file to .github/workflows/quiz-gate.yml in your
# repo, then:
#
#   1. Replace OWNER/REPO in `uses:` below and PIN it to a released tag or
#      commit SHA (e.g. @v1) - never a moving branch.
#   2. Set repo secrets: DATABRICKS_HOST, DATABRICKS_CLIENT_ID,
#      DATABRICKS_CLIENT_SECRET (a service principal that can query the
#      results warehouse).
#   3. Set repo variable QUIZ_WAREHOUSE_ID (required). Optional variables
#      (defaults shown below): QUIZ_RESULTS_TABLE, QUIZ_STATUS_CONTEXT.
#
# Note: issue_comment workflows only run the version on your DEFAULT branch -
# /quiz-check does nothing until this file is merged there.
name: quiz-gate

on:
  issue_comment:
    types: [created]
  workflow_dispatch:
    inputs:
      pr_number:
        description: PR number to re-evaluate
        required: true

permissions:
  pull-requests: read
  statuses: write

jobs:
  quiz-gate:
    uses: OWNER/REPO/.github/workflows/quiz-gate.yml@v1  # <-- replace + pin
    with:
      warehouse_id: ${{ vars.QUIZ_WAREHOUSE_ID }}
      # `|| 'x'` keeps an unset variable from overriding the reusable
      # workflow's default with an empty string.
      results_table: ${{ vars.QUIZ_RESULTS_TABLE || 'workspace.pr_quiz.quiz_results' }}
      status_context: ${{ vars.QUIZ_STATUS_CONTEXT || 'quiz-gate' }}
      pr_number: ${{ inputs.pr_number }}
    secrets:
      databricks_host: ${{ secrets.DATABRICKS_HOST }}
      databricks_client_id: ${{ secrets.DATABRICKS_CLIENT_ID }}
      databricks_client_secret: ${{ secrets.DATABRICKS_CLIENT_SECRET }}
"""

CALLER_TEMPLATES: dict[str, str] = {
    "quiz-generate.yml": CALLER_QUIZ_GENERATE,
    "quiz-gate.yml": CALLER_QUIZ_GATE,
}


# =========================================================================
# SECTION: config schema
# (mirrors databricks_template_schema.json -- a parity test keeps keys,
#  defaults, and patterns in sync)
# =========================================================================


@dataclass(frozen=True)
class ConfigKey:
    name: str
    description: str
    default: str | None = None
    pattern: str | None = None


CONFIG_KEYS: tuple[ConfigKey, ...] = (
    ConfigKey(
        "project_name",
        "Project name (bundle name and app/job name prefix)",
        default="pr-quiz",
        pattern=r"^[a-z][a-z0-9-]{0,24}[a-z0-9]$",
    ),
    ConfigKey(
        "workspace_host",
        "Databricks workspace URL, e.g. https://dbc-xxxxxxxx-xxxx.cloud.databricks.com",
        pattern=r"^https://.+$",
    ),
    ConfigKey(
        "catalog",
        "Unity Catalog catalog for the quiz schema ('workspace' on Free Edition)",
        default="workspace",
        pattern=r"^[A-Za-z0-9_]+$",
    ),
    ConfigKey(
        "schema",
        "Schema for the quiz tables",
        default="pr_quiz",
        pattern=r"^[A-Za-z0-9_]+$",
    ),
    ConfigKey(
        "warehouse_id",
        "16-hex SQL warehouse ID (Compute > SQL warehouses > your warehouse > ID)",
        pattern=r"^[0-9a-f]{16}$",
    ),
    ConfigKey(
        "serving_endpoint",
        "Foundation-model serving endpoint for question generation",
        default="databricks-gpt-oss-120b",
        pattern=r"^[A-Za-z0-9_.-]+$",
    ),
    ConfigKey(
        "secret_scope",
        "Secret scope holding the GitHub token",
        default="pr-quiz",
        pattern=r"^[A-Za-z0-9_.-]+$",
    ),
    ConfigKey(
        "secret_key_github",
        "Secret key (inside the scope) holding the GitHub token",
        default="github_token",
        pattern=r"^[A-Za-z0-9_.-]+$",
    ),
    ConfigKey(
        "status_context",
        "Commit-status context the merge gate posts under (must match the caller "
        "workflows' QUIZ_STATUS_CONTEXT and the required branch-protection check)",
        default=DEFAULT_STATUS_CONTEXT,
        pattern=r"^[A-Za-z0-9._/-]+$",
    ),
)

CONFIG_KEY_NAMES = tuple(k.name for k in CONFIG_KEYS)

HANDOFF_REQUIRED_KEYS = (
    "host",
    "warehouse_id",
    "app_name",
    "job_name",
    "results_table",
    "ci_client_id",
    "status_context",
)


# =========================================================================
# SECTION: output helpers
# =========================================================================


def _p(tag: str, msg: str) -> None:
    print(f"[{tag}] {msg}")


def ok(msg: str) -> None:
    _p("ok", msg)


def skip(msg: str) -> None:
    _p("skip", msg)


def do(msg: str) -> None:
    _p("do", msg)


def fail(msg: str) -> None:
    _p("FAIL", msg)


def banner(msg: str) -> None:
    print(f"\n--- {msg} ---")


def report(dry_run: bool, msg: str) -> None:
    """[ok] normally; in dry-run, phrase it as a would-be outcome."""
    if dry_run:
        _p("dry-run", f"would report: {msg}")
    else:
        ok(msg)


class StepError(Exception):
    """A step failed hard enough that continuing makes no sense."""


# =========================================================================
# SECTION: Runner -- the single subprocess boundary
# =========================================================================


@dataclass
class RunResult:
    code: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.code == 0

    def json(self) -> Any:
        return json.loads(self.out)


def json_of(res: "RunResult", default: Any = None) -> Any:
    """Parse a RunResult's stdout as JSON; `default` on failure/empty/error."""
    if not res.ok or not res.out.strip():
        return default
    try:
        return json.loads(res.out)
    except ValueError:
        return default


# A Runner is any callable with this signature; tests use a fake.
RunnerFn = Callable[..., RunResult]


def format_argv(argv: Sequence[str], mask: Sequence[int] = ()) -> str:
    shown = ["***" if i in mask else a for i, a in enumerate(argv)]
    return " ".join(shown)


class Runner:
    """Executes argv via subprocess. In dry-run, mutating calls are printed
    instead of executed; read-only lookups still run."""

    def __init__(self, dry_run: bool = False) -> None:
        self.dry_run = dry_run

    def __call__(
        self,
        argv: Sequence[str],
        *,
        mutating: bool = False,
        input_text: str | None = None,
        cwd: str | Path | None = None,
        mask: Sequence[int] = (),
    ) -> RunResult:
        if self.dry_run and mutating:
            prefix = f"(cwd {cwd}) " if cwd else ""
            _p("dry-run", prefix + format_argv(argv, mask))
            if input_text is not None:
                for line in input_text.splitlines():
                    print(f"          | {line}")
            return RunResult(0, "", "")
        env = {**os.environ, "MSYS_NO_PATHCONV": "1"}  # Git Bash path-mangling guard
        try:
            proc = subprocess.run(
                list(argv),
                capture_output=True,
                input=input_text,
                cwd=str(cwd) if cwd else None,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError as exc:
            return RunResult(127, "", str(exc))
        return RunResult(proc.returncode, proc.stdout, proc.stderr)


def databricks_cmd(args: Sequence[str], profile: str | None) -> list[str]:
    cmd = ["databricks", *args]
    if profile:
        cmd += ["--profile", profile]
    return cmd


# =========================================================================
# SECTION: pure functions -- config
# =========================================================================


def merge_config(
    flag_values: dict[str, str | None], file_values: dict[str, Any]
) -> dict[str, str | None]:
    """Merge config sources: flags > file > schema defaults.

    Unknown keys in the file are an error (typo protection). Returns a dict
    with an entry for every schema key; unresolved keys are None.
    """
    unknown = sorted(set(file_values) - set(CONFIG_KEY_NAMES))
    if unknown:
        raise ValueError(f"unknown keys in config file: {', '.join(unknown)}")
    merged: dict[str, str | None] = {}
    for key in CONFIG_KEYS:
        value = flag_values.get(key.name)
        if value is None:
            value = file_values.get(key.name)
        if value is None:
            value = key.default
        merged[key.name] = str(value) if value is not None else None
    return merged


def validate_config(cfg: dict[str, str | None]) -> list[str]:
    """Return a list of human-readable problems; empty means valid."""
    problems: list[str] = []
    for key in CONFIG_KEYS:
        value = cfg.get(key.name)
        if value is None:
            problems.append(f"{key.name}: missing ({key.description})")
        elif key.pattern and not re.fullmatch(key.pattern, value):
            problems.append(f"{key.name}: {value!r} does not match {key.pattern}")
    return problems


def format_warehouse_choices(warehouses: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """(id, label) pairs for the numbered warehouse picker."""
    choices: list[tuple[str, str]] = []
    for wh in warehouses:
        wid = str(wh.get("id", ""))
        name = str(wh.get("name", "?"))
        state = str(wh.get("state", "?"))
        size = str(wh.get("cluster_size", "?"))
        choices.append((wid, f"{name}  [{wid}]  {size}, {state}"))
    return choices


# =========================================================================
# SECTION: pure functions -- caller-workflow stamping
# =========================================================================

_USES_LINE = re.compile(
    r"^(?P<indent>\s*)uses:\s*OWNER/REPO/(?P<path>\S+)@v1\s*(?:#.*)?$", re.MULTILINE
)


def stamp_caller(
    text: str, workflows_repo: str, ref: str, default_branch: str
) -> str:
    """Fill a caller template: `uses:` slug + ref, and default-branch names.

    Only `uses:` lines are rewritten (instructional comments mentioning
    OWNER/REPO or @v1 stay put). Idempotent: stamped text has no
    OWNER/REPO uses-line left to match, and branch tokens are exact-match.
    """

    def _sub(m: re.Match[str]) -> str:
        return f"{m.group('indent')}uses: {workflows_repo}/{m.group('path')}@{ref}"

    text = _USES_LINE.sub(_sub, text)
    text = text.replace("branches: [main]", f"branches: [{default_branch}]")
    text = text.replace("|| 'main' }}", "|| '" + default_branch + "' }}")
    return text


def stamped_caller_files(
    workflows_repo: str, ref: str, default_branch: str
) -> dict[str, str]:
    """Map of `.github/workflows/<name>` -> stamped content for a consumer repo."""
    return {
        f".github/workflows/{name}": stamp_caller(tpl, workflows_repo, ref, default_branch)
        for name, tpl in CALLER_TEMPLATES.items()
    }


# =========================================================================
# SECTION: pure functions -- branch protection
# =========================================================================


def _enabled(node: Any) -> bool:
    if isinstance(node, dict):
        return bool(node.get("enabled"))
    return bool(node)


def _names(items: Any, key: str) -> list[str]:
    out: list[str] = []
    for item in items or []:
        out.append(item[key] if isinstance(item, dict) else str(item))
    return out


def protection_has_context(existing: dict[str, Any] | None, context: str) -> bool:
    if not existing:
        return False
    rsc = existing.get("required_status_checks") or {}
    contexts = set(rsc.get("contexts") or [])
    contexts.update(c.get("context") for c in rsc.get("checks") or [] if isinstance(c, dict))
    return context in contexts


def merge_protection(existing: dict[str, Any] | None, context: str) -> dict[str, Any]:
    """Build the PUT /branches/{branch}/protection body.

    - No protection yet -> minimal body requiring only `context`, strict=false.
    - Existing protection -> preserve every setting the GET response exposes
      and add `context` once (no duplicates on re-run).
    - New/unpinned checks get app_id -1 ("any source"): the quiz app posts
      the status with a PAT, so pinning to a GitHub App would reject it.
    """
    if not existing:
        return {
            "required_status_checks": {
                "strict": False,
                "checks": [{"context": context, "app_id": -1}],
            },
            "enforce_admins": False,
            "required_pull_request_reviews": None,
            "restrictions": None,
        }

    body: dict[str, Any] = {}

    rsc = existing.get("required_status_checks") or {}
    raw_checks = rsc.get("checks")
    if raw_checks is None:
        raw_checks = [{"context": c, "app_id": None} for c in rsc.get("contexts") or []]
    checks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for chk in raw_checks:
        ctx = chk["context"]
        if ctx in seen:
            continue
        seen.add(ctx)
        app_id = chk.get("app_id")
        checks.append({"context": ctx, "app_id": -1 if app_id is None else app_id})
    if context not in seen:
        checks.append({"context": context, "app_id": -1})
    body["required_status_checks"] = {"strict": bool(rsc.get("strict")), "checks": checks}

    body["enforce_admins"] = _enabled(existing.get("enforce_admins"))

    rpr = existing.get("required_pull_request_reviews")
    if rpr:
        reviews: dict[str, Any] = {
            "dismiss_stale_reviews": bool(rpr.get("dismiss_stale_reviews")),
            "require_code_owner_reviews": bool(rpr.get("require_code_owner_reviews")),
            "required_approving_review_count": int(
                rpr.get("required_approving_review_count") or 0
            ),
            "require_last_push_approval": bool(rpr.get("require_last_push_approval")),
        }
        for field_name in ("dismissal_restrictions", "bypass_pull_request_allowances"):
            node = rpr.get(field_name)
            # Preserve the field whenever GET returned it -- even with all-empty
            # lists, which means "nobody", not "anyone".
            if isinstance(node, dict):
                reviews[field_name] = {
                    "users": _names(node.get("users"), "login"),
                    "teams": _names(node.get("teams"), "slug"),
                    "apps": _names(node.get("apps"), "slug"),
                }
        body["required_pull_request_reviews"] = reviews
    else:
        body["required_pull_request_reviews"] = None

    restrictions = existing.get("restrictions")
    if restrictions:
        body["restrictions"] = {
            "users": _names(restrictions.get("users"), "login"),
            "teams": _names(restrictions.get("teams"), "slug"),
            "apps": _names(restrictions.get("apps"), "slug"),
        }
    else:
        body["restrictions"] = None

    for toggle in (
        "required_linear_history",
        "allow_force_pushes",
        "allow_deletions",
        "block_creations",
        "required_conversation_resolution",
        "lock_branch",
        "allow_fork_syncing",
    ):
        if toggle in existing:
            body[toggle] = _enabled(existing[toggle])

    return body


def ruleset_requires_checks(rules: Any) -> bool:
    """True if a repo ruleset governing the branch already requires status
    checks (then we must not fight it with classic branch protection)."""
    if not isinstance(rules, list):
        return False
    return any(
        isinstance(rule, dict) and rule.get("type") == "required_status_checks"
        for rule in rules
    )


# =========================================================================
# SECTION: pure functions -- handoff artifact
# =========================================================================


def build_handoff(
    cfg: dict[str, str | None],
    *,
    profile: str | None,
    ci_client_id: str | None,
    app_url: str | None,
) -> dict[str, Any]:
    project = cfg["project_name"]
    return {
        "host": cfg["workspace_host"],
        "profile": profile,
        "warehouse_id": cfg["warehouse_id"],
        "app_name": project,
        "job_name": f"{project}-generator",
        "app_url": app_url,
        "results_table": f"{cfg['catalog']}.{cfg['schema']}.quiz_results",
        "ci_client_id": ci_client_id,
        # status_context is a template input (CONFIG_KEYS); the same value the
        # app was rendered with is carried to onboard, which sets it as the
        # QUIZ_STATUS_CONTEXT repo variable so every side agrees on the context.
        "status_context": cfg.get("status_context") or DEFAULT_STATUS_CONTEXT,
    }


def parse_handoff(text: str) -> dict[str, Any]:
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("handoff file must contain a JSON object")
    missing = [k for k in HANDOFF_REQUIRED_KEYS if not data.get(k)]
    if missing:
        raise ValueError(f"handoff file missing keys: {', '.join(missing)}")
    return data


# =========================================================================
# SECTION: pure functions -- misc lookups
# =========================================================================


def find_service_principal(sps: Any, display_name: str) -> dict[str, Any] | None:
    if isinstance(sps, dict):  # tolerate SCIM-style {"Resources": [...]} wrappers
        sps = sps.get("Resources") or sps.get("service_principals") or []
    for sp in sps:
        if sp.get("displayName") == display_name:
            return sp
    return None


def as_list(payload: Any, *keys: str) -> list[Any]:
    """CLI list output is usually a bare array; tolerate {key: [...]} wrappers."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in keys:
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


# =========================================================================
# SECTION: prompting helpers (thin I/O -- untested by convention)
# =========================================================================


def ask(prompt_text: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{prompt_text}{suffix}: ").strip()
    return answer or (default or "")

def ask_secret(prompt_text: str) -> str:
    return getpass.getpass(prompt_text)


def pick_warehouse(runner: RunnerFn, profile: str | None) -> str | None:
    """Numbered warehouse picker; None if listing failed or nothing chosen."""
    res = runner(databricks_cmd(["warehouses", "list", "-o", "json"], profile))
    if not res.ok:
        fail(f"could not list warehouses: {res.err.strip() or res.out.strip()}")
        return None
    choices = format_warehouse_choices(as_list(json_of(res, []), "warehouses"))
    if not choices:
        fail("no SQL warehouses visible to this identity")
        return None
    print("SQL warehouses:")
    for i, (_, label) in enumerate(choices, 1):
        print(f"  {i}. {label}")
    answer = ask("Pick a warehouse (number)", "1")
    try:
        return choices[int(answer) - 1][0]
    except (ValueError, IndexError):
        fail(f"invalid choice: {answer}")
        return None


# =========================================================================
# SECTION: doctor
# =========================================================================


def cmd_doctor(args: argparse.Namespace) -> int:
    runner = Runner()
    failures = 0

    if sys.version_info >= (3, 10):
        ok(f"python {sys.version.split()[0]}")
    else:
        fail(f"python {sys.version.split()[0]} -- need >= 3.10")
        failures += 1

    for tool, version_argv in (
        ("databricks", ["databricks", "--version"]),
        ("gh", ["gh", "--version"]),
    ):
        if shutil.which(tool) is None:
            fail(f"{tool} not found on PATH")
            failures += 1
            continue
        res = runner(version_argv)
        version = (res.out or res.err).strip().splitlines()[0] if (res.out or res.err) else "?"
        ok(f"{tool} on PATH ({version})")

    if shutil.which("databricks"):
        res = runner(databricks_cmd(["current-user", "me", "-o", "json"], args.profile))
        if res.ok:
            ok(f"databricks auth: {json_of(res, {}).get('userName', '?')}")
        else:
            hint = f" --profile {args.profile}" if args.profile else ""
            fail(
                "databricks current-user me failed -- run "
                f"`databricks auth login --host <workspace-url>{hint}` first\n"
                f"       {res.err.strip().splitlines()[0] if res.err.strip() else ''}"
            )
            failures += 1

    if shutil.which("gh"):
        res = runner(["gh", "auth", "status"])
        if res.ok:
            ok("gh auth status")
        else:
            fail("gh auth status failed -- run `gh auth login` (or export GH_TOKEN)")
            failures += 1

    if failures:
        fail(f"doctor: {failures} check(s) failed")
        return 1
    ok("doctor: all checks passed")
    return 0


# =========================================================================
# SECTION: backend -- config gathering
# =========================================================================


def gather_backend_config(args: argparse.Namespace, runner: RunnerFn) -> dict[str, str]:
    flag_values = {key.name: getattr(args, key.name) for key in CONFIG_KEYS}
    file_values: dict[str, Any] = {}
    if args.config_file:
        file_values = json.loads(Path(args.config_file).read_text(encoding="utf-8"))
    cfg = merge_config(flag_values, file_values)

    missing = [k for k in CONFIG_KEY_NAMES if cfg[k] is None]
    if missing and args.yes:
        raise StepError(
            f"--yes given but values missing (pass flags or --config-file): {', '.join(missing)}"
        )
    for key in CONFIG_KEYS:
        if cfg[key.name] is not None:
            continue
        if key.name == "warehouse_id":
            cfg[key.name] = pick_warehouse(runner, args.profile) or ask(key.description)
        else:
            cfg[key.name] = ask(key.description, key.default)

    problems = validate_config(cfg)
    if problems:
        raise StepError("invalid configuration:\n  " + "\n  ".join(problems))
    return {k: str(v) for k, v in cfg.items()}


def default_template_source() -> Path | None:
    """Prefer the repo this installer lives in; fall back to the cwd."""
    for candidate in (Path(__file__).resolve().parent.parent, Path.cwd()):
        if (candidate / "databricks_template_schema.json").is_file():
            return candidate
    return None


# =========================================================================
# SECTION: backend -- steps
# =========================================================================


@dataclass
class BackendState:
    """Mutable facts collected as backend steps run."""

    bundle_dir: Path | None = None
    ci_client_id: str | None = None
    ci_internal_id: str | None = None
    warnings: int = 0


def step_render(
    args: argparse.Namespace, cfg: dict[str, str], runner: RunnerFn, state: BackendState
) -> None:
    source: str | Path | None = args.template_source or default_template_source()
    if source is None:
        raise StepError(
            "no template source found: run from a checkout of the pr-quiz repo "
            "or pass --template-source <path-or-git-url>"
        )
    build_dir = Path(args.build_dir)
    bundle_dir = build_dir / cfg["project_name"]
    state.bundle_dir = bundle_dir

    if bundle_dir.joinpath("databricks.yml").is_file() and not args.force:
        skip(f"already rendered: {bundle_dir} (--force re-renders)")
        return

    if args.dry_run:
        _p("dry-run", f"write {build_dir / 'init-config.json'}")
        _p(
            "dry-run",
            format_argv(
                databricks_cmd(
                    [
                        "bundle", "init", str(source),
                        "--config-file", str(build_dir / "init-config.json"),
                        "--output-dir", str(build_dir),
                    ],
                    args.profile,
                )
            ),
        )
        return

    if args.force and bundle_dir.exists():
        do(f"removing previous render {bundle_dir}")
        shutil.rmtree(bundle_dir)
    build_dir.mkdir(parents=True, exist_ok=True)

    src_path = Path(source)
    if src_path.is_dir():
        # Stray __pycache__ dirs (from running the test suite) would be
        # copied into the rendered bundle; purge them from the source first.
        for pycache in (src_path / "template").rglob("__pycache__"):
            shutil.rmtree(pycache, ignore_errors=True)

    config_path = build_dir / "init-config.json"
    config_path.write_text(
        json.dumps({k: cfg[k] for k in CONFIG_KEY_NAMES}, indent=2) + "\n",
        encoding="utf-8",
    )
    res = runner(
        databricks_cmd(
            [
                "bundle", "init", str(source),
                "--config-file", str(config_path),
                "--output-dir", str(build_dir),
            ],
            args.profile,
        ),
        mutating=True,
    )
    if not res.ok:
        raise StepError(f"bundle init failed: {res.err.strip() or res.out.strip()}")

    # Own git repo: a parent .gitignore covering the build dir would
    # otherwise make bundle sync see zero files and deploy an empty bundle.
    if not (build_dir / ".git").exists():
        if shutil.which("git"):
            runner(["git", "init", "-q"], cwd=build_dir, mutating=True)
        else:
            _p("warn", "git not found: if the build dir is gitignored, bundle sync may see zero files")
    ok(f"rendered {bundle_dir}")


_NOT_FOUND_RE = re.compile(r"404|NOT_FOUND|RESOURCE_DOES_NOT_EXIST|does not exist", re.I)


def step_deploy(
    args: argparse.Namespace, cfg: dict[str, str], runner: RunnerFn, state: BackendState
) -> None:
    # The app resource also declares the serving endpoint (like the secret);
    # a missing endpoint 404s the whole deploy. Fail fast with a clear hint.
    endpoint = cfg["serving_endpoint"]
    res = runner(
        databricks_cmd(["serving-endpoints", "get", endpoint, "-o", "json"], args.profile)
    )
    if not res.ok:
        err = res.err.strip()
        if _NOT_FOUND_RE.search(err):
            raise StepError(
                f"serving endpoint '{endpoint}' not found -- the app resource requires "
                "it, so deploy would fail. Pass --serving-endpoint <existing endpoint> "
                "(Free Edition ships e.g. databricks-gpt-oss-120b)"
            )
        _p("warn", f"could not verify serving endpoint '{endpoint}'; proceeding "
                   f"({err.splitlines()[0] if err else 'unknown error'})")
    else:
        ok(f"serving endpoint '{endpoint}' exists")

    res = runner(
        databricks_cmd(["bundle", "deploy"], args.profile),
        mutating=True,
        cwd=state.bundle_dir,
    )
    if not res.ok:
        raise StepError(f"bundle deploy failed: {res.err.strip() or res.out.strip()}")
    report(args.dry_run, "bundle deployed (idempotent; re-runs update in place)")


def step_init_tables(
    args: argparse.Namespace, cfg: dict[str, str], runner: RunnerFn, state: BackendState
) -> None:
    argv = [
        sys.executable,
        "scripts/sql_exec.py",
        "--file", "sql/init_tables.sql",
        "--warehouse-id", cfg["warehouse_id"],
    ]
    if args.profile:
        argv += ["--profile", args.profile]
    res = runner(argv, mutating=True, cwd=state.bundle_dir)
    if not res.ok:
        raise StepError(f"init tables failed: {res.err.strip() or res.out.strip()}")
    report(args.dry_run, f"tables ready in {cfg['catalog']}.{cfg['schema']}")


def step_github_secret(
    args: argparse.Namespace, cfg: dict[str, str], runner: RunnerFn, state: BackendState
) -> None:
    scope, key = cfg["secret_scope"], cfg["secret_key_github"]

    res = runner(databricks_cmd(["secrets", "list-scopes", "-o", "json"], args.profile))
    scopes = {s.get("name") for s in as_list(json_of(res, []), "scopes")}
    if scope in scopes:
        skip(f"secret scope '{scope}' exists")
    else:
        res = runner(
            databricks_cmd(["secrets", "create-scope", scope], args.profile), mutating=True
        )
        if not res.ok:
            raise StepError(f"create-scope failed: {res.err.strip()}")
        report(args.dry_run, f"created secret scope '{scope}'")

    key_exists = False
    if scope in scopes:
        res = runner(databricks_cmd(["secrets", "list-secrets", scope, "-o", "json"], args.profile))
        key_exists = any(
            s.get("key") == key for s in as_list(json_of(res, []), "secrets")
        )
    if key_exists and not args.force:
        skip(f"secret {scope}/{key} already set (--force overwrites)")
        return

    if args.dry_run:
        token = "<github-token>"
    else:
        token = os.environ.get("PR_QUIZ_GITHUB_TOKEN", "")
        if not token and not args.yes:
            token = ask_secret(GITHUB_TOKEN_PROMPT)
    if not token:
        raise StepError("no GitHub token provided (set PR_QUIZ_GITHUB_TOKEN for non-interactive runs)")
    argv = databricks_cmd(
        ["secrets", "put-secret", scope, key, "--string-value", token], args.profile
    )
    res = runner(argv, mutating=True, mask=[argv.index(token)])
    if not res.ok:
        raise StepError(f"put-secret failed: {res.err.strip()}")
    report(args.dry_run, f"secret {scope}/{key} set")


def resolve_sp_secrets_cli(runner: RunnerFn) -> str | None:
    """The workspace-level SP OAuth-secret command name varies by CLI version."""
    for candidate in ("service-principal-secrets", "service-principal-secrets-proxy"):
        if runner(["databricks", candidate, "--help"]).ok:
            return candidate
    return None


def step_ci_sp(
    args: argparse.Namespace, cfg: dict[str, str], runner: RunnerFn, state: BackendState
) -> None:
    sp_name = f"{cfg['project_name']}-ci"

    res = runner(databricks_cmd(["service-principals", "list", "-o", "json"], args.profile))
    sp = find_service_principal(json_of(res, []), sp_name)
    if sp:
        state.ci_client_id = sp.get("applicationId")
        state.ci_internal_id = str(sp.get("id"))
        skip(f"service principal '{sp_name}' exists (client_id {state.ci_client_id})")
    else:
        res = runner(
            databricks_cmd(
                ["service-principals", "create", "--display-name", sp_name, "-o", "json"],
                args.profile,
            ),
            mutating=True,
        )
        if not res.ok:
            fail(f"service-principals create failed: {res.err.strip()}")
            print(SP_UI_FALLBACK)
            state.warnings += 1
            return
        if args.dry_run:
            state.ci_client_id = "<ci-client-id>"
            state.ci_internal_id = "<ci-sp-id>"
        else:
            created = json_of(res, {})
            state.ci_client_id = created.get("applicationId")
            state.ci_internal_id = str(created.get("id"))
            ok(f"created service principal '{sp_name}' (client_id {state.ci_client_id})")

    secrets_cli = resolve_sp_secrets_cli(runner)
    if secrets_cli is None:
        fail("this Databricks CLI has no service-principal-secrets command")
        print(SP_SECRET_UI_FALLBACK.format(name=sp_name))
        state.warnings += 1
        return

    if sp and not args.force:
        res = runner(
            databricks_cmd(
                [secrets_cli, "list", state.ci_internal_id or "", "-o", "json"], args.profile
            )
        )
        existing = as_list(json_of(res, []), "secrets")
        if existing:
            skip(
                "SP already has an OAuth secret; its value cannot be read back -- "
                "pass --force to mint an additional one"
            )
            return

    res = runner(
        databricks_cmd(
            [
                secrets_cli, "create", state.ci_internal_id or "",
                "--lifetime", SP_SECRET_LIFETIME, "-o", "json",
            ],
            args.profile,
        ),
        mutating=True,
    )
    if not res.ok:
        fail(f"{secrets_cli} create failed: {res.err.strip()}")
        print(SP_SECRET_UI_FALLBACK.format(name=sp_name))
        state.warnings += 1
        return
    if args.dry_run:
        return
    secret_value = json_of(res, {}).get("secret", "")
    print()
    print("  " + "=" * 66)
    print("  CI SP OAuth secret (shown ONCE -- store it; `onboard` asks for it):")
    print(f"    DATABRICKS_CLIENT_ID:     {state.ci_client_id}")
    print(f"    DATABRICKS_CLIENT_SECRET: {secret_value}")
    print(f"    expires: {SP_SECRET_LIFETIME} from now (rotate before then)")
    print("  " + "=" * 66)
    print()
    ok("CI SP OAuth secret created")


def _grant_sql(
    args: argparse.Namespace,
    cfg: dict[str, str],
    runner: RunnerFn,
    state: BackendState,
    statement: str,
) -> bool:
    argv = [
        sys.executable,
        "scripts/sql_exec.py",
        "--statement", statement,
        "--warehouse-id", cfg["warehouse_id"],
    ]
    if args.profile:
        argv += ["--profile", args.profile]
    res = runner(argv, mutating=True, cwd=state.bundle_dir)
    if not res.ok:
        fail(f"grant failed: {statement} -- {res.err.strip() or res.out.strip()}")
        state.warnings += 1
        return False
    return True


def step_grants(
    args: argparse.Namespace, cfg: dict[str, str], runner: RunnerFn, state: BackendState
) -> None:
    project, catalog, schema = cfg["project_name"], cfg["catalog"], cfg["schema"]

    res = runner(databricks_cmd(["apps", "get", project, "-o", "json"], args.profile))
    if not res.ok:
        fail(f"apps get {project} failed (is the deploy done?): {res.err.strip()}")
        state.warnings += 1
        return
    app_sp = json_of(res, {}).get("service_principal_client_id")
    if not app_sp:
        fail(f"app '{project}' has no service_principal_client_id yet; skipping grants")
        state.warnings += 1
        return
    ok(f"app SP: {app_sp}")

    _grant_sql(args, cfg, runner, state, f"GRANT USE CATALOG ON CATALOG {catalog} TO `{app_sp}`")
    _grant_sql(
        args, cfg, runner, state,
        f"GRANT USE SCHEMA, SELECT, MODIFY ON SCHEMA {catalog}.{schema} TO `{app_sp}`",
    )

    # App deployments staged by the app SP read bundle source from the
    # deploying user's home; without CAN_READ a CI-SP-initiated app start
    # fails with "no files found".
    res = runner(databricks_cmd(["current-user", "me", "-o", "json"], args.profile))
    me = json_of(res, {}).get("userName")
    if not me:
        fail("current-user me failed; skipping bundle-folder CAN_READ grant")
        state.warnings += 1
    else:
        ws_bundle_dir = f"/Users/{me}/.bundle/{project}"
        res = runner(
            databricks_cmd(["workspace", "get-status", ws_bundle_dir, "-o", "json"], args.profile)
        )
        if not res.ok:
            fail(f"workspace get-status {ws_bundle_dir} failed: {res.err.strip()}")
            state.warnings += 1
        else:
            dir_id = str(json_of(res, {}).get("object_id"))
            acl = [{"service_principal_name": app_sp, "permission_level": "CAN_READ"}]
            if state.ci_client_id:
                acl.append(
                    {"service_principal_name": state.ci_client_id, "permission_level": "CAN_READ"}
                )
            res = runner(
                databricks_cmd(
                    [
                        "workspace", "update-permissions", "directories", dir_id,
                        "--json", json.dumps({"access_control_list": acl}),
                    ],
                    args.profile,
                ),
                mutating=True,
            )
            if res.ok:
                report(args.dry_run, f"bundle folder CAN_READ granted on {ws_bundle_dir}")
            else:
                fail(f"bundle-folder grant failed: {res.err.strip()}")
                state.warnings += 1

    if not state.ci_client_id:
        skip("no CI SP known -- re-run backend after creating it to apply CI grants")
        return
    ci = state.ci_client_id
    ok(f"CI SP: {ci}")
    _grant_sql(args, cfg, runner, state, f"GRANT USE CATALOG ON CATALOG {catalog} TO `{ci}`")
    _grant_sql(
        args, cfg, runner, state,
        f"GRANT USE SCHEMA, SELECT ON SCHEMA {catalog}.{schema} TO `{ci}`",
    )
    res = runner(
        databricks_cmd(
            [
                "warehouses", "update-permissions", cfg["warehouse_id"],
                "--json",
                json.dumps(
                    {
                        "access_control_list": [
                            {"service_principal_name": ci, "permission_level": "CAN_USE"}
                        ]
                    }
                ),
            ],
            args.profile,
        ),
        mutating=True,
    )
    if not res.ok:
        fail(f"warehouse CAN_USE grant failed: {res.err.strip()}")
        state.warnings += 1

    job_name = f"{project}-generator"
    res = runner(
        databricks_cmd(["jobs", "list", "--name", job_name, "-o", "json"], args.profile)
    )
    jobs = as_list(json_of(res, []), "jobs")
    if not jobs:
        fail(f"job '{job_name}' not found; skipping job CAN_MANAGE_RUN grant")
        state.warnings += 1
    else:
        job_id = str(jobs[0].get("job_id"))
        res = runner(
            databricks_cmd(
                [
                    "jobs", "update-permissions", job_id,
                    "--json",
                    json.dumps(
                        {
                            "access_control_list": [
                                {"service_principal_name": ci, "permission_level": "CAN_MANAGE_RUN"}
                            ]
                        }
                    ),
                ],
                args.profile,
            ),
            mutating=True,
        )
        if res.ok:
            report(args.dry_run, f"job {job_name} CAN_MANAGE_RUN granted")
        else:
            fail(f"job grant failed: {res.err.strip()}")
            state.warnings += 1

    res = runner(
        databricks_cmd(
            [
                "apps", "update-permissions", project,
                "--json",
                json.dumps(
                    {
                        "access_control_list": [
                            {"service_principal_name": ci, "permission_level": "CAN_MANAGE"}
                        ]
                    }
                ),
            ],
            args.profile,
        ),
        mutating=True,
    )
    if res.ok:
        report(args.dry_run, f"app {project} CAN_MANAGE granted to CI SP")
    else:
        fail("app permission grant failed - grant CAN_MANAGE via UI (Compute > Apps > permissions)")
        state.warnings += 1
    report(args.dry_run, "grants done")


def step_handoff(
    args: argparse.Namespace, cfg: dict[str, str], runner: RunnerFn, state: BackendState
) -> None:
    res = runner(databricks_cmd(["apps", "get", cfg["project_name"], "-o", "json"], args.profile))
    app = json_of(res, {})
    app_url: str | None = app.get("url")
    # `bundle deploy` registers the app resource but does NOT push source or
    # start compute; until `bundle run quiz_app` runs once there is no active
    # deployment and the app serves nothing. Warn loudly so the next-step below
    # is not mistaken for optional (skip the check in dry-run: no real app yet).
    if not args.dry_run and not app.get("active_deployment"):
        _p("warn", "!! the app has NO active deployment yet -- it will NOT serve the")
        _p("warn", "   quiz until you run its source once (step 3 below):")
        _p("warn", f"     databricks bundle run quiz_app   (from {state.bundle_dir})")
    handoff = build_handoff(
        cfg,
        profile=args.profile,
        ci_client_id=state.ci_client_id,
        app_url=app_url,
    )
    out_path = Path(args.handoff_out)
    if args.dry_run:
        _p("dry-run", f"write {out_path}: {json.dumps(handoff)}")
    else:
        out_path.write_text(json.dumps(handoff, indent=2) + "\n", encoding="utf-8")
        ok(f"wrote {out_path}")
    print(
        "\nNext steps:\n"
        f"  1. Keep the CI SP OAuth secret from step 5 at hand (it is NOT in {out_path.name}).\n"
        f"  2. For each repo that should get the quiz gate:\n"
        f"       python {Path(sys.argv[0]).name} onboard --repo <owner>/<name> --handoff {out_path}\n"
        f"  3. Start the app once: databricks bundle run quiz_app (from {state.bundle_dir})"
    )


# Ordered backend pipeline. ORDER MATTERS: the app resource declares the
# GitHub-token secret (and the serving endpoint) as dependencies, so the
# secret scope + token must exist BEFORE the first `bundle deploy`; the
# schema/job/app must exist before init-tables and grants.
BACKEND_STEPS: tuple[tuple[str, Callable[..., None]], ...] = (
    ("render bundle template", step_render),
    ("GitHub token -> Databricks secret scope (the app resource needs it to deploy)", step_github_secret),
    ("bundle deploy (schema, job, app)", step_deploy),
    ("init tables (CREATE TABLE IF NOT EXISTS)", step_init_tables),
    ("CI service principal + OAuth secret", step_ci_sp),
    ("grants (app SP + CI SP)", step_grants),
    ("handoff artifact", step_handoff),
)


def cmd_backend(args: argparse.Namespace) -> int:
    runner = Runner(dry_run=args.dry_run)
    state = BackendState()
    try:
        cfg = gather_backend_config(args, runner)
    except (StepError, OSError, ValueError, EOFError, KeyboardInterrupt) as exc:
        fail(str(exc) or exc.__class__.__name__)
        return 1
    print("configuration:")
    for key in CONFIG_KEY_NAMES:
        print(f"  {key} = {cfg[key]}")
    try:
        for number, (label, step) in enumerate(BACKEND_STEPS, 1):
            banner(f"{number}/{len(BACKEND_STEPS)} {label}")
            step(args, cfg, runner, state)
    except (StepError, OSError, ValueError, EOFError, KeyError, TypeError, KeyboardInterrupt) as exc:
        fail(str(exc) or exc.__class__.__name__)
        return 1
    if state.warnings:
        fail(f"backend finished with {state.warnings} warning(s) -- see above")
        return 1
    ok("backend complete")
    return 0


# =========================================================================
# SECTION: onboard -- steps
# =========================================================================


def load_handoff(args: argparse.Namespace) -> dict[str, Any]:
    path = Path(args.handoff)
    if path.is_file():
        handoff = parse_handoff(path.read_text(encoding="utf-8"))
        ok(f"handoff loaded from {path}")
        return handoff
    if args.yes:
        raise StepError(f"handoff file not found: {path}")
    print(f"handoff file not found at {path}; enter the values from the backend install:")
    handoff = {
        "host": ask("Databricks workspace URL (https://...)"),
        "warehouse_id": ask("SQL warehouse ID (16-hex)"),
        "app_name": ask("Quiz app name", "pr-quiz"),
        "job_name": ask("Generation job name", "pr-quiz-generator"),
        "results_table": ask("Results table", "workspace.pr_quiz.quiz_results"),
        "ci_client_id": ask("CI service principal client_id (UUID)"),
        "status_context": ask("Commit-status context", DEFAULT_STATUS_CONTEXT),
    }
    return parse_handoff(json.dumps(handoff))


def gh_api_json(runner: RunnerFn, path: str) -> Any | None:
    return json_of(runner(["gh", "api", path]))


def file_content_on_branch(
    runner: RunnerFn, repo: str, path: str, branch: str
) -> str | None:
    """Decoded file content at repo@branch, or None if absent/unreadable."""
    data = gh_api_json(runner, f"repos/{repo}/contents/{path}?ref={branch}")
    if not isinstance(data, dict) or not data.get("content"):
        return None
    try:
        return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    except (ValueError, TypeError):  # malformed base64
        return None


def step_workflows_pr(
    args: argparse.Namespace,
    runner: RunnerFn,
    files: dict[str, str],
    default_branch: str,
    live: dict[str, str | None],
) -> None:
    """`live` maps each target path to its current content on the default
    branch (None if absent), pre-fetched by cmd_onboard."""
    banner("1/3 caller workflows via PR")
    repo = args.repo

    if all(live.get(path) == content for path, content in files.items()):
        skip(f"caller workflows already on {default_branch}; no PR needed")
        return

    base = gh_api_json(runner, f"repos/{repo}/git/ref/heads/{default_branch}")
    base_sha = (base.get("object") or {}).get("sha") if isinstance(base, dict) else None
    if not base_sha:
        raise StepError(f"cannot read branch {default_branch} of {repo} (auth? repo slug?)")

    if gh_api_json(runner, f"repos/{repo}/git/ref/heads/{ONBOARD_BRANCH}"):
        skip(f"branch {ONBOARD_BRANCH} already exists; updating files on it")
    else:
        res = runner(
            ["gh", "api", "-X", "POST", f"repos/{repo}/git/refs", "--input", "-"],
            input_text=json.dumps({"ref": f"refs/heads/{ONBOARD_BRANCH}", "sha": base_sha}),
            mutating=True,
        )
        if not res.ok:
            raise StepError(f"could not create branch {ONBOARD_BRANCH}: {res.err.strip()}")
        report(args.dry_run, f"created branch {ONBOARD_BRANCH} from {default_branch} ({base_sha[:8]})")

    for path, content in files.items():
        existing = gh_api_json(runner, f"repos/{repo}/contents/{path}?ref={ONBOARD_BRANCH}")
        body: dict[str, Any] = {
            "message": f"chore: add pr-quiz caller workflow {Path(path).name}",
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": ONBOARD_BRANCH,
        }
        if isinstance(existing, dict) and existing.get("sha"):
            try:
                existing_content = base64.b64decode(
                    existing.get("content", "") or ""
                ).decode("utf-8", errors="replace")
            except (ValueError, TypeError):
                existing_content = None
            if existing_content == content:
                skip(f"{path} already up to date on {ONBOARD_BRANCH}")
                continue
            body["sha"] = existing["sha"]
        res = runner(
            ["gh", "api", "-X", "PUT", f"repos/{repo}/contents/{path}", "--input", "-"],
            input_text=json.dumps(body),
            mutating=True,
        )
        if not res.ok:
            raise StepError(f"could not write {path}: {res.err.strip()}")
        report(args.dry_run, f"{'updated' if 'sha' in body else 'created'} {path} on {ONBOARD_BRANCH}")

    res = runner(
        ["gh", "pr", "list", "--repo", repo, "--head", ONBOARD_BRANCH, "--json", "number,url"]
    )
    open_prs = json_of(res, [])
    if open_prs:
        skip(f"PR already open: {open_prs[0].get('url', '#' + str(open_prs[0].get('number')))}")
        return
    res = runner(
        [
            "gh", "pr", "create",
            "--repo", repo,
            "--head", ONBOARD_BRANCH,
            "--base", default_branch,
            "--title", "Add pr-quiz merge-gate workflows",
            "--body",
            (
                "Adds the pr-quiz caller workflows (quiz-generate + quiz-gate).\n\n"
                "After merging, every PR gets an AI-generated quiz gate: comment `/quiz` "
                "on a PR to (re)generate its quiz, `/quiz-check` to re-evaluate the gate.\n\n"
                f"Reusable workflows are pinned to `{args.workflows_repo}@{args.workflows_ref}`.\n"
                "Repo secrets and variables were configured by the pr-quiz installer."
            ),
        ],
        mutating=True,
    )
    if not res.ok:
        raise StepError(f"gh pr create failed: {res.err.strip()}")
    report(args.dry_run, f"opened PR: {res.out.strip() or '(see repo)'}")


def step_secrets_and_variables(
    args: argparse.Namespace,
    runner: RunnerFn,
    handoff: dict[str, Any],
    target_branch: str,
) -> None:
    banner("2/3 repo secrets + variables")
    repo = args.repo

    if args.dry_run:
        sp_secret = "<ci-sp-oauth-secret>"
    else:
        sp_secret = os.environ.get("DATABRICKS_CLIENT_SECRET", "")
        if not sp_secret and not args.yes:
            sp_secret = ask_secret(
                f"OAuth secret for CI service principal {handoff['ci_client_id']} "
                "(printed once by `backend`; mint a new one there with --force if lost): "
            )
    if not sp_secret:
        raise StepError(
            "no CI SP secret provided (set DATABRICKS_CLIENT_SECRET for non-interactive runs)"
        )

    for name, value in (
        ("DATABRICKS_HOST", str(handoff["host"])),
        ("DATABRICKS_CLIENT_ID", str(handoff["ci_client_id"])),
        ("DATABRICKS_CLIENT_SECRET", sp_secret),
    ):
        argv = ["gh", "secret", "set", name, "--repo", repo, "--body", value]
        res = runner(argv, mutating=True, mask=[argv.index("--body") + 1])
        if not res.ok:
            raise StepError(f"gh secret set {name} failed: {res.err.strip()}")
        report(args.dry_run, f"secret {name} set")

    for name, value in (
        ("QUIZ_APP_NAME", str(handoff["app_name"])),
        ("QUIZ_JOB_NAME", str(handoff["job_name"])),
        ("QUIZ_WAREHOUSE_ID", str(handoff["warehouse_id"])),
        ("QUIZ_RESULTS_TABLE", str(handoff["results_table"])),
        ("QUIZ_STATUS_CONTEXT", str(handoff["status_context"])),
        ("QUIZ_TARGET_BRANCH", target_branch),
    ):
        res = runner(
            ["gh", "variable", "set", name, "--repo", repo, "--body", value], mutating=True
        )
        if not res.ok:
            raise StepError(f"gh variable set {name} failed: {res.err.strip()}")
        report(args.dry_run, f"variable {name} = {value}")


def step_branch_protection(
    args: argparse.Namespace,
    runner: RunnerFn,
    context: str,
    target_branch: str,
    callers_live: bool,
    default_branch: str | None = None,
) -> str:
    """Protect `target_branch` (the branch the gate guards). `default_branch`
    is only where the caller workflows must be merged for the gate to run at
    all -- it drives the DEFERRED message and defaults to target_branch when
    the two coincide.

    Returns "done" (success or nothing to do), "deferred" (caller workflows
    not merged yet), or "manual" (adopter must act in the UI)."""
    banner("3/3 branch protection")
    repo = args.repo
    default_branch = default_branch or target_branch

    if not callers_live:
        skip(
            f"protection DEFERRED: the caller workflows are not on {default_branch} yet.\n"
            f"       Requiring '{context}' now would block EVERY merge -- including the\n"
            "       onboarding PR itself -- because the gate can only post statuses once\n"
            "       the workflows are merged. After merging the onboarding PR, re-run:\n"
            f"         python {Path(sys.argv[0]).name} onboard --repo {repo} --protect-only"
        )
        return "deferred"

    rules = gh_api_json(runner, f"repos/{repo}/rules/branches/{target_branch}")
    if ruleset_requires_checks(rules):
        skip(
            f"a repo RULESET already requires status checks on {target_branch}.\n"
            f"       Add '{context}' to that ruleset instead:\n"
            f"       Settings > Rules > Rulesets > (the ruleset) > Require status checks"
        )
        return "manual"

    res = runner(["gh", "api", f"repos/{repo}/branches/{target_branch}/protection"])
    existing: dict[str, Any] | None = None
    if res.ok:
        existing = json_of(res)
        if not isinstance(existing, dict):
            raise StepError(
                "unexpected response reading branch protection -- aborting to avoid "
                "overwriting existing settings"
            )
    elif "Branch not protected" in res.err or "HTTP 404" in res.err:
        existing = None  # demonstrably unprotected -> create minimal protection
    elif "HTTP 403" in res.err or "Upgrade to GitHub Pro" in res.err:
        fail(
            "cannot read branch protection (HTTP 403). On GitHub Free, private repos\n"
            "       cannot use branch protection -- make the repo public or upgrade, then add\n"
            f"       required status check '{context}' under Settings > Branches."
        )
        return "manual"
    else:
        # 5xx / rate limit / network: NOT proof the branch is unprotected.
        # Writing the minimal body here could wipe the adopter's settings.
        raise StepError(
            f"could not read branch protection ({res.err.strip() or 'unknown error'}) -- "
            "aborting to avoid overwriting existing settings; re-run onboard later "
            "(or with --protect-only)"
        )

    if protection_has_context(existing, context):
        skip(f"'{context}' already required on {target_branch}")
        return "done"

    body = merge_protection(existing, context)
    res = runner(
        ["gh", "api", "-X", "PUT", f"repos/{repo}/branches/{target_branch}/protection", "--input", "-"],
        input_text=json.dumps(body),
        mutating=True,
    )
    if not res.ok:
        fail(
            f"could not update branch protection: {res.err.strip()}\n"
            f"       Add it manually: Settings > Branches > add rule for '{target_branch}'\n"
            f"       > Require status checks to pass > add '{context}'.\n"
            "       (GitHub Free: branch protection needs a PUBLIC repo.)"
        )
        return "manual"
    if existing:
        report(args.dry_run, f"'{context}' added to required status checks on {target_branch} (existing settings preserved)")
    else:
        report(args.dry_run, f"created branch protection on {target_branch} requiring '{context}'")
    return "done"


def print_onboard_checklist(
    args: argparse.Namespace,
    handoff: dict[str, Any],
    default_branch: str,
    protection_status: str,
    target_branch: str | None = None,
) -> None:
    target_branch = target_branch or default_branch
    app_url = str(handoff.get("app_url") or "<app-url>").rstrip("/")
    protect_line = ""
    if protection_status == "deferred":
        protect_line = (
            f"     THEN re-run: python {Path(sys.argv[0]).name} onboard --repo {args.repo} "
            "--protect-only\n"
            f"     to require the '{handoff['status_context']}' status on {target_branch} "
            "(deferred above).\n"
        )
    target_note = ""
    if target_branch != default_branch:
        target_note = (
            f"     (The gate protects '{target_branch}', but the workflows still merge to the\n"
            f"     default branch '{default_branch}' -- that is the only branch /quiz runs from.)\n"
        )
    print(
        "\nVerify the install:\n"
        f"  1. MERGE the onboarding PR first -- issue_comment workflows only run the\n"
        f"     version on the DEFAULT branch ({default_branch}); /quiz does nothing until merged.\n"
        + target_note
        + protect_line
        + "  2. Open a test PR; the quiz should generate automatically (or comment /quiz).\n"
        f"  3. Check the '{handoff['status_context']}' commit status appears on the PR head.\n"
        f"  4. Take the quiz at {app_url}/?sha=<head-sha> (the bot comment links it).\n"
        "  5. Score 100% -> status turns green -> merge unlocks. /quiz-check re-evaluates."
    )


ONBOARD_ERRORS = (
    StepError, ValueError, OSError, EOFError, KeyError, TypeError, KeyboardInterrupt,
)


def cmd_onboard(args: argparse.Namespace, runner: RunnerFn | None = None) -> int:
    # runner is injectable so the branch-semantics wiring can be driven end to
    # end with a FakeRunner in tests; production always uses the real Runner.
    if runner is None:
        runner = Runner(dry_run=args.dry_run)
    protection_status = "done"
    try:
        handoff = load_handoff(args)

        repo_info = gh_api_json(runner, f"repos/{args.repo}")
        if not isinstance(repo_info, dict):
            raise StepError(f"cannot read repo {args.repo} via gh api (auth? slug?)")
        # Two distinct branches: the caller workflows must land on the repo's
        # ACTUAL default branch (issue_comment workflows only ever run the
        # default-branch version -- /quiz dies silently anywhere else), while
        # --target-branch only picks which branch the gate protects and which
        # value QUIZ_TARGET_BRANCH carries.
        default_branch = repo_info.get("default_branch") or "main"
        target_branch = args.target_branch or default_branch
        ok(f"default branch (workflows PR): {default_branch}")
        if target_branch != default_branch:
            ok(f"gate target branch (protection + QUIZ_TARGET_BRANCH): {target_branch}")

        # Caller content is stamped for the TARGET branch (its `branches:` PR
        # filter and QUIZ_TARGET_BRANCH fallback name the protected branch),
        # but the PR that adds it targets the DEFAULT branch.
        files = stamped_caller_files(args.workflows_repo, args.workflows_ref, target_branch)
        # What is live on the default branch decides both the "nothing to PR"
        # skip and whether requiring the status is safe yet (see step 3/3).
        live = {
            path: file_content_on_branch(runner, args.repo, path, default_branch)
            for path in files
        }
        callers_live = all(content is not None for content in live.values())

        if args.protect_only:
            skip("workflow PR + secrets/variables skipped (--protect-only)")
        else:
            step_workflows_pr(args, runner, files, default_branch, live)
            step_secrets_and_variables(args, runner, handoff, target_branch)
        if args.no_protect:
            skip("branch protection skipped (--no-protect)")
        else:
            protection_status = step_branch_protection(
                args, runner, str(handoff["status_context"]), target_branch,
                callers_live, default_branch,
            )
    except ONBOARD_ERRORS as exc:
        fail(str(exc) or exc.__class__.__name__)
        return 1
    print_onboard_checklist(args, handoff, default_branch, protection_status, target_branch)
    if protection_status == "manual":
        fail("onboard finished, but branch protection needs the manual step above")
        return 1
    ok("onboard complete")
    return 0


# =========================================================================
# SECTION: CLI entry point
# =========================================================================


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pr_quiz_install.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_doctor = sub.add_parser("doctor", help="preflight checks (CLIs, auth, python)")
    p_doctor.add_argument("--profile", help="Databricks CLI profile to check")
    p_doctor.set_defaults(func=cmd_doctor)

    p_backend = sub.add_parser(
        "backend", help="deploy the Databricks side (org admin, once per workspace)"
    )
    p_backend.add_argument("--config-file", help="JSON with bundle-init keys (see databricks_template_schema.json)")
    p_backend.add_argument("--profile", help="Databricks CLI profile")
    for key in CONFIG_KEYS:
        p_backend.add_argument(
            "--" + key.name.replace("_", "-"),
            dest=key.name,
            help=key.description + (f" (default: {key.default})" if key.default else ""),
        )
    p_backend.add_argument("--template-source", help="bundle template path or git URL (default: this checkout)")
    p_backend.add_argument("--build-dir", default=DEFAULT_BUILD_DIR, help="where to render the bundle")
    p_backend.add_argument("--handoff-out", default=DEFAULT_HANDOFF, help="handoff artifact path")
    # --status-context is added by the CONFIG_KEYS loop above (it is a template
    # input, baked into app.yaml and carried into the handoff artifact).
    p_backend.add_argument("--yes", action="store_true", help="non-interactive; fail instead of prompting")
    p_backend.add_argument("--force", action="store_true", help="re-render, overwrite secrets, mint new SP secret")
    p_backend.add_argument("--dry-run", action="store_true", help="print mutating commands instead of executing")
    p_backend.set_defaults(func=cmd_backend)

    p_onboard = sub.add_parser(
        "onboard", help="wire a consumer repo to the quiz gate (repo maintainer)"
    )
    p_onboard.add_argument("--repo", required=True, metavar="OWNER/NAME", help="consumer repository")
    p_onboard.add_argument("--handoff", default=DEFAULT_HANDOFF, help="path to pr-quiz-backend.json")
    p_onboard.add_argument(
        "--workflows-repo",
        default=DEFAULT_WORKFLOWS_REPO,
        help="public repo hosting the reusable workflows",
    )
    p_onboard.add_argument(
        "--workflows-ref", default=DEFAULT_WORKFLOWS_REF, help="tag/SHA to pin the reusable workflows to"
    )
    p_onboard.add_argument(
        "--target-branch", help="branch the gate protects (default: repo's default branch)"
    )
    p_onboard.add_argument("--no-protect", action="store_true", help="skip branch protection changes")
    p_onboard.add_argument(
        "--protect-only",
        action="store_true",
        help="only apply branch protection (re-run after merging the onboarding PR)",
    )
    p_onboard.add_argument("--yes", action="store_true", help="non-interactive; fail instead of prompting")
    p_onboard.add_argument("--dry-run", action="store_true", help="print mutating commands instead of executing")
    p_onboard.set_defaults(func=cmd_onboard)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
