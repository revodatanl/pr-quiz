"""Drift guard: quiz_store.py queries must keep their multi-tenant key
fragments — the (provider, repo) join/filter/insert columns that stop results,
attempt counts, and passes from leaking across repos sharing a head_sha.

The queries are f-string SQL inside quiz_store.py, which cannot be imported
here (module import connects a databricks Config), so the guard parses the
source with ast and asserts on each function's text — the same read-the-file
stance as test_ddl_sync.py. Whitespace is normalized so a pure reformat cannot
break the guard, but dropping a tenant filter fails it.
"""
import ast
import re
from pathlib import Path

SOURCE = (
    Path(__file__).resolve().parents[1]
    / "template" / "{{.project_name}}" / "src" / "app" / "quiz_store.py"
).read_text(encoding="utf-8")
_TREE = ast.parse(SOURCE)


def _func_text(name):
    """Whitespace-normalized source of one top-level function in quiz_store.py."""
    for node in ast.walk(_TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return re.sub(r"\s+", " ", ast.get_source_segment(SOURCE, node))
    raise AssertionError(f"function {name!r} not found in quiz_store.py")


class TestTenantKeyFragments:
    def test_load_pool_selects_provider_and_repo(self):
        text = _func_text("load_pool")
        assert "SELECT provider, repo," in text

    def test_load_pool_repo_branch_filters_by_repo(self):
        text = _func_text("load_pool")
        assert "AND repo = %(repo)s" in text

    def test_recent_quizzes_joins_on_full_tenant_key(self):
        # head_sha-only join leaked a passed result in repo A onto repo B's
        # identical commit — the original security bug this guard protects.
        text = _func_text("_recent_quizzes_rows")
        assert "qr.provider = qp.provider" in text
        assert "qr.repo = qp.repo" in text
        assert "qr.head_sha = qp.head_sha" in text

    def test_recent_quizzes_groups_on_full_tenant_key(self):
        text = _func_text("_recent_quizzes_rows")
        assert "GROUP BY qp.provider, qp.repo, qp.head_sha" in text

    def test_taker_progress_filters_by_provider_and_repo(self):
        text = _func_text("taker_progress")
        assert "provider = %(provider)s" in text
        assert "repo = %(repo)s" in text

    def test_save_result_inserts_provider_and_repo_columns(self):
        text = _func_text("save_result")
        assert "(provider, repo, head_sha," in text
        assert "%(provider)s, %(repo)s," in text
