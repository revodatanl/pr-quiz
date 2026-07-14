"""Tests for installer/pr_quiz_install.py -- pure decision logic only.

Convention: no real subprocess calls; command-shaped tests use a FakeRunner
returning canned output. Parity tests pin the embedded assets (caller
templates, config schema) to their on-disk sources so they cannot drift.
"""
import base64
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "installer"))

import pr_quiz_install as inst  # noqa: E402


def _norm(text: str) -> str:
    return text.replace("\r\n", "\n")


# =========================================================================
# parity: embedded assets vs on-disk sources
# =========================================================================


@pytest.mark.parametrize("name", sorted(inst.CALLER_TEMPLATES))
def test_embedded_caller_matches_template_file(name):
    on_disk = (ROOT / "templates" / "callers" / name).read_text(encoding="utf-8")
    assert _norm(inst.CALLER_TEMPLATES[name]) == _norm(on_disk), (
        f"installer's embedded {name} drifted from templates/callers/{name}; "
        "update the string constant in installer/pr_quiz_install.py"
    )


def test_config_schema_matches_template_schema():
    schema = json.loads(
        (ROOT / "databricks_template_schema.json").read_text(encoding="utf-8")
    )
    props = schema["properties"]
    assert set(props) == set(inst.CONFIG_KEY_NAMES)
    for key in inst.CONFIG_KEYS:
        assert props[key.name].get("default") == key.default, key.name
        assert props[key.name].get("pattern") == key.pattern, key.name


# =========================================================================
# caller-template stamping
# =========================================================================


def test_stamp_replaces_uses_line_only():
    out = inst.stamp_caller(inst.CALLER_QUIZ_GENERATE, "acme/quiz", "v2", "develop")
    assert "    uses: acme/quiz/.github/workflows/quiz-generate.yml@v2\n" in out
    assert "OWNER/REPO/.github" not in out
    assert "# <-- replace + pin" not in out
    # instructional comments are left untouched
    assert "Replace OWNER/REPO" in out
    assert "(e.g. @v1)" in out


def test_stamp_substitutes_default_branch():
    out = inst.stamp_caller(inst.CALLER_QUIZ_GENERATE, "acme/quiz", "v1", "develop")
    assert "branches: [develop]" in out
    assert "|| 'develop' }}" in out
    assert "[main]" not in out
    assert "'main'" not in out


def test_stamp_gate_template_has_no_branch_tokens():
    out = inst.stamp_caller(inst.CALLER_QUIZ_GATE, "acme/quiz", "v1", "develop")
    assert "    uses: acme/quiz/.github/workflows/quiz-gate.yml@v1\n" in out
    assert "develop" not in out.replace("acme/quiz/.github", "")


def test_stamp_is_idempotent():
    once = inst.stamp_caller(inst.CALLER_QUIZ_GENERATE, "acme/quiz", "v1", "develop")
    twice = inst.stamp_caller(once, "acme/quiz", "v1", "develop")
    assert once == twice


def test_stamped_caller_files_paths():
    files = inst.stamped_caller_files("acme/quiz", "v1", "main")
    assert set(files) == {
        ".github/workflows/quiz-generate.yml",
        ".github/workflows/quiz-gate.yml",
    }


# =========================================================================
# branch-protection merge
# =========================================================================


def test_merge_protection_no_existing_creates_minimal():
    body = inst.merge_protection(None, "quiz-gate")
    assert body == {
        "required_status_checks": {
            "strict": False,
            "checks": [{"context": "quiz-gate", "app_id": -1}],
        },
        "enforce_admins": False,
        "required_pull_request_reviews": None,
        "restrictions": None,
    }


EXISTING_PROTECTION = {
    "required_status_checks": {
        "strict": True,
        "contexts": ["ci", "lint"],
        "checks": [
            {"context": "ci", "app_id": 15368},
            {"context": "lint", "app_id": None},
        ],
    },
    "enforce_admins": {"enabled": True},
    "required_pull_request_reviews": {
        "dismiss_stale_reviews": True,
        "require_code_owner_reviews": False,
        "required_approving_review_count": 2,
        "require_last_push_approval": True,
        "dismissal_restrictions": {
            "users": [{"login": "alice"}],
            "teams": [{"slug": "platform"}],
            "apps": [],
        },
    },
    "restrictions": {
        "users": [{"login": "bob"}],
        "teams": [{"slug": "release"}],
        "apps": [{"slug": "deploy-bot"}],
    },
    "required_linear_history": {"enabled": True},
    "allow_force_pushes": {"enabled": False},
    "allow_deletions": {"enabled": False},
    "required_conversation_resolution": {"enabled": True},
}


def test_merge_protection_preserves_existing_and_adds_context():
    body = inst.merge_protection(EXISTING_PROTECTION, "quiz-gate")
    checks = body["required_status_checks"]["checks"]
    assert {"context": "ci", "app_id": 15368} in checks  # pinned app preserved
    assert {"context": "lint", "app_id": -1} in checks  # null -> any source
    assert {"context": "quiz-gate", "app_id": -1} in checks  # added, unpinned
    assert len(checks) == 3
    assert body["required_status_checks"]["strict"] is True
    assert body["enforce_admins"] is True


def test_merge_protection_maps_reviews_and_restrictions():
    body = inst.merge_protection(EXISTING_PROTECTION, "quiz-gate")
    reviews = body["required_pull_request_reviews"]
    assert reviews["dismiss_stale_reviews"] is True
    assert reviews["require_code_owner_reviews"] is False
    assert reviews["required_approving_review_count"] == 2
    assert reviews["require_last_push_approval"] is True
    assert reviews["dismissal_restrictions"] == {
        "users": ["alice"],
        "teams": ["platform"],
        "apps": [],
    }
    assert body["restrictions"] == {
        "users": ["bob"],
        "teams": ["release"],
        "apps": ["deploy-bot"],
    }
    assert body["required_linear_history"] is True
    assert body["allow_force_pushes"] is False
    assert body["required_conversation_resolution"] is True
    # toggles absent from the GET response are not invented
    assert "lock_branch" not in body


def test_merge_protection_does_not_duplicate_existing_context():
    existing = {
        "required_status_checks": {
            "strict": False,
            "checks": [{"context": "quiz-gate", "app_id": None}],
        },
        "enforce_admins": {"enabled": False},
    }
    body = inst.merge_protection(existing, "quiz-gate")
    checks = body["required_status_checks"]["checks"]
    assert checks == [{"context": "quiz-gate", "app_id": -1}]


def test_merge_protection_contexts_only_shape():
    # older API responses may carry only `contexts`, no `checks`
    existing = {"required_status_checks": {"strict": False, "contexts": ["ci"]}}
    body = inst.merge_protection(existing, "quiz-gate")
    assert body["required_status_checks"]["checks"] == [
        {"context": "ci", "app_id": -1},
        {"context": "quiz-gate", "app_id": -1},
    ]
    assert body["required_pull_request_reviews"] is None
    assert body["restrictions"] is None


def test_protection_has_context():
    assert not inst.protection_has_context(None, "quiz-gate")
    assert not inst.protection_has_context({}, "quiz-gate")
    assert inst.protection_has_context(
        {"required_status_checks": {"contexts": ["quiz-gate"]}}, "quiz-gate"
    )
    assert inst.protection_has_context(
        {"required_status_checks": {"checks": [{"context": "quiz-gate"}]}}, "quiz-gate"
    )
    assert not inst.protection_has_context(
        {"required_status_checks": {"contexts": ["ci"]}}, "quiz-gate"
    )


def test_ruleset_requires_checks():
    assert not inst.ruleset_requires_checks([])
    assert not inst.ruleset_requires_checks(None)
    assert not inst.ruleset_requires_checks([{"type": "pull_request"}])
    assert inst.ruleset_requires_checks(
        [{"type": "pull_request"}, {"type": "required_status_checks", "parameters": {}}]
    )


# =========================================================================
# config merging + validation
# =========================================================================


def test_merge_config_precedence_flags_over_file_over_defaults():
    flags = {"project_name": "flagged", "warehouse_id": None}
    file_values = {"project_name": "filed", "warehouse_id": "0123456789abcdef"}
    cfg = inst.merge_config(flags, file_values)
    assert cfg["project_name"] == "flagged"  # flag wins
    assert cfg["warehouse_id"] == "0123456789abcdef"  # file fills flag gap
    assert cfg["catalog"] == "workspace"  # schema default
    assert cfg["workspace_host"] is None  # no default anywhere


def test_merge_config_rejects_unknown_file_keys():
    with pytest.raises(ValueError, match="warehouse_idd"):
        inst.merge_config({}, {"warehouse_idd": "typo"})


def test_validate_config_flags_missing_and_pattern_violations():
    cfg = inst.merge_config({}, {})
    cfg.update(workspace_host="http://insecure", warehouse_id="not-hex")
    problems = "\n".join(inst.validate_config(cfg))
    assert "workspace_host" in problems
    assert "warehouse_id" in problems


def test_validate_config_accepts_good_config():
    cfg = inst.merge_config(
        {},
        {
            "workspace_host": "https://dbc-1234.cloud.databricks.com",
            "warehouse_id": "0123456789abcdef",
        },
    )
    assert inst.validate_config(cfg) == []


def test_validate_config_rejects_bad_project_name():
    cfg = inst.merge_config(
        {"project_name": "Bad_Name"},
        {"workspace_host": "https://x", "warehouse_id": "0123456789abcdef"},
    )
    assert any(p.startswith("project_name") for p in inst.validate_config(cfg))


def test_format_warehouse_choices():
    choices = inst.format_warehouse_choices(
        [{"id": "0123456789abcdef", "name": "Starter", "state": "RUNNING", "cluster_size": "2X-Small"}]
    )
    assert choices == [("0123456789abcdef", "Starter  [0123456789abcdef]  2X-Small, RUNNING")]


# =========================================================================
# handoff artifact
# =========================================================================


def _good_cfg() -> dict:
    return inst.merge_config(
        {},
        {
            "workspace_host": "https://dbc-1234.cloud.databricks.com",
            "warehouse_id": "0123456789abcdef",
        },
    )


def test_build_and_parse_handoff_round_trip():
    handoff = inst.build_handoff(
        _good_cfg(),
        profile="free",
        ci_client_id="abc-123",
        app_url="https://app.example",
    )
    parsed = inst.parse_handoff(json.dumps(handoff))
    assert parsed["host"] == "https://dbc-1234.cloud.databricks.com"
    assert parsed["app_name"] == "pr-quiz"
    assert parsed["job_name"] == "pr-quiz-generator"
    assert parsed["results_table"] == "workspace.pr_quiz.quiz_results"
    assert parsed["ci_client_id"] == "abc-123"
    assert parsed["status_context"] == "quiz-gate"


def test_parse_handoff_rejects_missing_keys():
    with pytest.raises(ValueError, match="ci_client_id"):
        inst.parse_handoff(json.dumps({"host": "https://x"}))


def test_parse_handoff_rejects_non_object():
    with pytest.raises(ValueError, match="JSON object"):
        inst.parse_handoff("[1, 2]")


# =========================================================================
# misc lookups
# =========================================================================


def test_find_service_principal_in_array_and_wrapper():
    sps = [
        {"displayName": "other", "applicationId": "x", "id": 1},
        {"displayName": "pr-quiz-ci", "applicationId": "abc", "id": 2},
    ]
    assert inst.find_service_principal(sps, "pr-quiz-ci")["applicationId"] == "abc"
    assert inst.find_service_principal({"Resources": sps}, "pr-quiz-ci")["id"] == 2
    assert inst.find_service_principal(sps, "nope") is None
    assert inst.find_service_principal({}, "nope") is None


def test_as_list_tolerates_wrappers():
    assert inst.as_list([1, 2]) == [1, 2]
    assert inst.as_list({"scopes": [1]}, "scopes") == [1]
    assert inst.as_list({"other": [1]}, "scopes") == []
    assert inst.as_list(None, "scopes") == []


def test_format_argv_masks_secrets():
    argv = ["databricks", "secrets", "put-secret", "s", "k", "--string-value", "tok"]
    shown = inst.format_argv(argv, mask=[6])
    assert "tok" not in shown
    assert "***" in shown


# =========================================================================
# step decisions via a fake runner (no real subprocess)
# =========================================================================


class FakeRunner:
    """Runner double: matches a substring of the joined argv to canned results."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, argv, *, mutating=False, input_text=None, cwd=None, mask=()):
        self.calls.append({"argv": list(argv), "mutating": mutating, "input": input_text})
        joined = " ".join(argv)
        for substring, result in self.responses:
            if substring in joined:
                return result
        return inst.RunResult(1, "", f"no fake response for: {joined}")


def test_resolve_sp_secrets_cli_falls_back_to_proxy_name():
    runner = FakeRunner(
        [
            ("service-principal-secrets-proxy --help", inst.RunResult(0, "usage", "")),
            ("service-principal-secrets --help", inst.RunResult(1, "", "unknown command")),
        ]
    )
    assert inst.resolve_sp_secrets_cli(runner) == "service-principal-secrets-proxy"


def test_resolve_sp_secrets_cli_prefers_unproxied_name():
    runner = FakeRunner([("--help", inst.RunResult(0, "usage", ""))])
    assert inst.resolve_sp_secrets_cli(runner) == "service-principal-secrets"


def test_resolve_sp_secrets_cli_none_when_unavailable():
    runner = FakeRunner([])
    assert inst.resolve_sp_secrets_cli(runner) is None


def test_json_of_handles_bad_output():
    assert inst.json_of(inst.RunResult(0, "", ""), []) == []
    assert inst.json_of(inst.RunResult(1, "[1]", ""), []) == []
    assert inst.json_of(inst.RunResult(0, "not json", ""), {}) == {}
    assert inst.json_of(inst.RunResult(0, '{"a": 1}', "")) == {"a": 1}


def test_runner_dry_run_skips_mutations(capsys):
    runner = inst.Runner(dry_run=True)
    res = runner(["definitely-not-a-real-binary", "--flag"], mutating=True)
    assert res.ok and res.out == ""
    assert "definitely-not-a-real-binary" in capsys.readouterr().out


def test_merge_protection_preserves_empty_dismissal_restrictions():
    # {"users": [], "teams": [], "apps": []} means NOBODY may dismiss;
    # dropping the field would degrade it to "anyone".
    existing = {
        "required_status_checks": {"strict": False, "checks": []},
        "required_pull_request_reviews": {
            "required_approving_review_count": 1,
            "dismissal_restrictions": {"users": [], "teams": [], "apps": []},
        },
    }
    body = inst.merge_protection(existing, "quiz-gate")
    assert body["required_pull_request_reviews"]["dismissal_restrictions"] == {
        "users": [],
        "teams": [],
        "apps": [],
    }


# =========================================================================
# step outcome matrices via FakeRunner (no network, no subprocess)
# =========================================================================


def _args(**overrides):
    base = dict(
        repo="o/r",
        dry_run=False,
        yes=True,
        workflows_repo="acme/quiz",
        workflows_ref="v1",
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


def _mutations(runner):
    return [c for c in runner.calls if c["mutating"]]


RULES_EMPTY = ("rules/branches/main", inst.RunResult(0, "[]", ""))
PROTECTION_PUT_OK = (
    "-X PUT repos/o/r/branches/main/protection",
    inst.RunResult(0, "{}", ""),
)


def test_protection_step_deferred_when_callers_not_on_default_branch():
    runner = FakeRunner([])
    status = inst.step_branch_protection(_args(), runner, "quiz-gate", "main", callers_live=False)
    assert status == "deferred"
    assert runner.calls == []  # nothing touched, not even reads


def test_protection_step_creates_minimal_on_demonstrable_404():
    runner = FakeRunner(
        [
            RULES_EMPTY,
            PROTECTION_PUT_OK,
            (
                "api repos/o/r/branches/main/protection",
                inst.RunResult(1, "", "gh: Branch not protected (HTTP 404)"),
            ),
        ]
    )
    status = inst.step_branch_protection(_args(), runner, "quiz-gate", "main", callers_live=True)
    assert status == "done"
    puts = _mutations(runner)
    assert len(puts) == 1
    assert json.loads(puts[0]["input"]) == inst.merge_protection(None, "quiz-gate")


def test_protection_step_manual_on_403_without_writing():
    runner = FakeRunner(
        [
            RULES_EMPTY,
            (
                "api repos/o/r/branches/main/protection",
                inst.RunResult(1, "", "gh: Forbidden (HTTP 403)"),
            ),
        ]
    )
    status = inst.step_branch_protection(_args(), runner, "quiz-gate", "main", callers_live=True)
    assert status == "manual"
    assert _mutations(runner) == []


def test_protection_step_aborts_on_unclassified_get_failure():
    # 5xx/rate-limit/network is NOT proof of "unprotected"; writing the
    # minimal body would wipe the adopter's settings.
    runner = FakeRunner(
        [
            RULES_EMPTY,
            (
                "api repos/o/r/branches/main/protection",
                inst.RunResult(1, "", "gh: Internal Server Error (HTTP 500)"),
            ),
        ]
    )
    with pytest.raises(inst.StepError, match="avoid overwriting"):
        inst.step_branch_protection(_args(), runner, "quiz-gate", "main", callers_live=True)
    assert _mutations(runner) == []


def test_protection_step_merges_existing_protection():
    runner = FakeRunner(
        [
            RULES_EMPTY,
            PROTECTION_PUT_OK,
            (
                "api repos/o/r/branches/main/protection",
                inst.RunResult(0, json.dumps(EXISTING_PROTECTION), ""),
            ),
        ]
    )
    status = inst.step_branch_protection(_args(), runner, "quiz-gate", "main", callers_live=True)
    assert status == "done"
    body = json.loads(_mutations(runner)[0]["input"])
    contexts = [c["context"] for c in body["required_status_checks"]["checks"]]
    assert contexts == ["ci", "lint", "quiz-gate"]
    assert body["enforce_admins"] is True  # preserved, not clobbered


def test_protection_step_skips_when_context_already_required():
    existing = {"required_status_checks": {"strict": False, "contexts": ["quiz-gate"]}}
    runner = FakeRunner(
        [
            RULES_EMPTY,
            ("api repos/o/r/branches/main/protection", inst.RunResult(0, json.dumps(existing), "")),
        ]
    )
    status = inst.step_branch_protection(_args(), runner, "quiz-gate", "main", callers_live=True)
    assert status == "done"
    assert _mutations(runner) == []


def test_protection_step_manual_when_ruleset_requires_checks():
    runner = FakeRunner(
        [
            (
                "rules/branches/main",
                inst.RunResult(0, json.dumps([{"type": "required_status_checks"}]), ""),
            ),
        ]
    )
    status = inst.step_branch_protection(_args(), runner, "quiz-gate", "main", callers_live=True)
    assert status == "manual"
    assert _mutations(runner) == []


WF_PATH = ".github/workflows/quiz-generate.yml"


def test_workflows_pr_skips_entirely_when_live_content_matches():
    runner = FakeRunner([])
    inst.step_workflows_pr(
        _args(), runner, {WF_PATH: "content"}, "main", live={WF_PATH: "content"}
    )
    assert runner.calls == []


def test_workflows_pr_all_skip_paths_make_no_mutations():
    files = {WF_PATH: "content"}
    b64 = base64.b64encode(b"content").decode("ascii")
    runner = FakeRunner(
        [
            ("git/ref/heads/pr-quiz/onboard", inst.RunResult(0, '{"object": {"sha": "bbb"}}', "")),
            ("git/ref/heads/main", inst.RunResult(0, '{"object": {"sha": "abc123"}}', "")),
            (
                "?ref=pr-quiz/onboard",
                inst.RunResult(0, json.dumps({"sha": "s1", "content": b64}), ""),
            ),
            ("pr list", inst.RunResult(0, '[{"number": 1, "url": "http://pr/1"}]', "")),
        ]
    )
    inst.step_workflows_pr(_args(), runner, files, "main", live={WF_PATH: None})
    assert _mutations(runner) == []  # branch exists, file identical, PR open


def test_workflows_pr_aborts_cleanly_on_shapeless_ref_response():
    runner = FakeRunner([("git/ref/heads/main", inst.RunResult(0, "{}", ""))])
    with pytest.raises(inst.StepError, match="cannot read branch"):
        inst.step_workflows_pr(
            _args(), runner, {WF_PATH: "content"}, "main", live={WF_PATH: None}
        )


def test_file_content_on_branch_decodes_and_handles_absence():
    b64 = base64.b64encode("hello".encode()).decode("ascii")
    runner = FakeRunner(
        [("contents/present", inst.RunResult(0, json.dumps({"content": b64, "sha": "x"}), ""))]
    )
    assert inst.file_content_on_branch(runner, "o/r", "present", "main") == "hello"
    assert inst.file_content_on_branch(runner, "o/r", "absent", "main") is None


def test_branch_protection_targets_target_branch_not_default():
    # --target-branch only moves the PROTECTED branch; endpoints must hit it.
    runner = FakeRunner(
        [
            ("rules/branches/develop", inst.RunResult(0, "[]", "")),
            ("-X PUT repos/o/r/branches/develop/protection", inst.RunResult(0, "{}", "")),
            (
                "api repos/o/r/branches/develop/protection",
                inst.RunResult(1, "", "gh: Branch not protected (HTTP 404)"),
            ),
        ]
    )
    status = inst.step_branch_protection(
        _args(), runner, "quiz-gate", "develop", callers_live=True, default_branch="main"
    )
    assert status == "done"
    puts = _mutations(runner)
    assert len(puts) == 1
    assert "repos/o/r/branches/develop/protection" in " ".join(puts[0]["argv"])
    # never touches the default branch's protection endpoint
    assert not any("branches/main/protection" in " ".join(c["argv"]) for c in runner.calls)


def test_onboard_default_and_target_branch_split(tmp_path, monkeypatch):
    # default=main, target=develop: the caller-workflow PR must target the
    # actual default branch (issue_comment workflows only run there), while
    # protection and QUIZ_TARGET_BRANCH follow --target-branch.
    handoff = {
        "host": "https://dbc-1234.cloud.databricks.com",
        "warehouse_id": "0123456789abcdef",
        "app_name": "pr-quiz",
        "job_name": "pr-quiz-generator",
        "results_table": "workspace.pr_quiz.quiz_results",
        "ci_client_id": "abc-123",
        "status_context": "quiz-gate",
        "app_url": "https://app.example",
    }
    handoff_path = tmp_path / "pr-quiz-backend.json"
    handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
    monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "sp-secret")

    stale_b64 = base64.b64encode(b"# stale caller\n").decode("ascii")
    runner = FakeRunner(
        [
            # existing-file check on the onboard branch -> absent (create)
            ("?ref=pr-quiz/onboard", inst.RunResult(1, "", "")),
            # live content on the DEFAULT branch: present but stale (forces a PR)
            ("?ref=main", inst.RunResult(0, json.dumps({"content": stale_b64, "sha": "x"}), "")),
            ("git/ref/heads/pr-quiz/onboard", inst.RunResult(1, "", "")),
            ("git/ref/heads/main", inst.RunResult(0, '{"object": {"sha": "abc123"}}', "")),
            ("git/refs", inst.RunResult(0, "{}", "")),
            ("-X PUT repos/o/r/contents", inst.RunResult(0, "{}", "")),
            ("pr list", inst.RunResult(0, "[]", "")),
            ("pr create", inst.RunResult(0, "http://pr/1", "")),
            ("secret set", inst.RunResult(0, "", "")),
            ("variable set", inst.RunResult(0, "", "")),
            ("rules/branches/develop", inst.RunResult(0, "[]", "")),
            ("-X PUT repos/o/r/branches/develop/protection", inst.RunResult(0, "{}", "")),
            (
                "api repos/o/r/branches/develop/protection",
                inst.RunResult(1, "", "gh: Branch not protected (HTTP 404)"),
            ),
            ("repos/o/r", inst.RunResult(0, '{"default_branch": "main"}', "")),
        ]
    )
    args = _args(
        target_branch="develop",
        no_protect=False,
        protect_only=False,
        handoff=str(handoff_path),
        profile="free",
    )
    rc = inst.cmd_onboard(args, runner=runner)
    assert rc == 0

    joined = [" ".join(c["argv"]) for c in runner.calls]

    # 1. the workflow PR bases on the DEFAULT branch, main
    pr_create = next(c for c in runner.calls if "pr create" in " ".join(c["argv"]))
    base_idx = pr_create["argv"].index("--base")
    assert pr_create["argv"][base_idx + 1] == "main"

    # 2. protection is applied on the TARGET branch, develop (never main)
    assert any("-X PUT repos/o/r/branches/develop/protection" in j for j in joined)
    assert not any("branches/main/protection" in j for j in joined)

    # 3. QUIZ_TARGET_BRANCH is set to the target branch, develop
    qtb = next(
        c for c in runner.calls
        if "variable set" in " ".join(c["argv"]) and "QUIZ_TARGET_BRANCH" in c["argv"]
    )
    body_idx = qtb["argv"].index("--body")
    assert qtb["argv"][body_idx + 1] == "develop"


# =========================================================================
# backend step ordering + deploy preflight
# =========================================================================


def test_backend_step_order_pins_hidden_dependencies():
    names = [fn.__name__ for _, fn in inst.BACKEND_STEPS]
    # The app resource declares the GitHub-token secret: scope + key must
    # exist BEFORE the first bundle deploy or deploy 404s (seen live on a
    # greenfield workspace; dogfood never hit it because the secret existed).
    assert names.index("step_github_secret") < names.index("step_deploy")
    assert names.index("step_render") == 0  # later steps need the rendered dir
    # Schema/job/app only exist after deploy.
    assert names.index("step_deploy") < names.index("step_init_tables")
    assert names.index("step_deploy") < names.index("step_grants")
    assert names.index("step_ci_sp") < names.index("step_grants")
    assert names[-1] == "step_handoff"


def test_deploy_step_fails_fast_on_missing_serving_endpoint():
    # Same-shaped hidden dependency as the secret: the app resource declares
    # the serving endpoint, so a missing endpoint 404s the whole deploy.
    runner = FakeRunner(
        [
            (
                "serving-endpoints get",
                inst.RunResult(1, "", "Error: Serving endpoint nope: 404 NOT_FOUND"),
            ),
        ]
    )
    state = inst.BackendState(bundle_dir=Path(".build/x"))
    with pytest.raises(inst.StepError, match="serving endpoint"):
        inst.step_deploy(_args(profile=None), _good_cfg(), runner, state)
    assert _mutations(runner) == []  # deploy never attempted


def test_deploy_step_runs_when_endpoint_exists():
    runner = FakeRunner(
        [
            ("serving-endpoints get", inst.RunResult(0, "{}", "")),
            ("bundle deploy", inst.RunResult(0, "", "")),
        ]
    )
    state = inst.BackendState(bundle_dir=Path(".build/x"))
    inst.step_deploy(_args(profile=None), _good_cfg(), runner, state)
    deploys = _mutations(runner)
    assert len(deploys) == 1 and "deploy" in " ".join(deploys[0]["argv"])


def test_deploy_step_proceeds_with_warning_when_endpoint_unverifiable():
    # A permissions/transport hiccup on the GET must not block deploy;
    # only a demonstrable not-found fails fast.
    runner = FakeRunner(
        [
            ("serving-endpoints get", inst.RunResult(1, "", "Error: connection reset")),
            ("bundle deploy", inst.RunResult(0, "", "")),
        ]
    )
    state = inst.BackendState(bundle_dir=Path(".build/x"))
    inst.step_deploy(_args(profile=None), _good_cfg(), runner, state)
    assert len(_mutations(runner)) == 1
