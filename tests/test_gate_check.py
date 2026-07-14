"""Pure-logic tests for actions/gate-check/gate_check.py: table-name validation
and verdict formatting. latest_result() shells out to the databricks CLI and is
not covered here, matching this repo's convention of not mocking subprocess I/O
- except the missing-CLI path, which is exercised for real by stripping PATH
(no subprocess ever starts).
"""
import sys

import pytest

from gate_check import (
    DEFAULT_TABLE,
    MAX_VERDICT_LEN,
    build_gate_query,
    clip_verdict,
    format_error,
    format_verdict,
    main,
)


class TestBuildGateQuery:
    def test_default_table_is_qualified(self):
        query = build_gate_query(DEFAULT_TABLE)
        assert f"FROM {DEFAULT_TABLE} " in query

    def test_filters_on_full_tenant_key(self):
        # head_sha alone would let an identical commit in a second repo inherit
        # the first repo's pass - the bug this filter closes.
        query = build_gate_query(DEFAULT_TABLE)
        assert "head_sha = :sha" in query
        assert "repo = :repo" in query
        assert "provider = :provider" in query

    def test_custom_table_is_interpolated(self):
        query = build_gate_query("workspace.other_schema.results")
        assert "FROM workspace.other_schema.results " in query

    @pytest.mark.parametrize(
        "bad_table",
        [
            "workspace.pr_quiz.quiz_results; DROP TABLE x",
            "workspace.pr_quiz.quiz_results--",
            "workspace pr_quiz",
            "table (1)",
            "",
        ],
    )
    def test_rejects_names_outside_the_allowed_shape(self, bad_table):
        with pytest.raises(ValueError):
            build_gate_query(bad_table)


class TestFormatVerdict:
    def test_no_row_blocks(self):
        passed, message = format_verdict(None, "abc123def456", "org/repo")
        assert passed is False
        assert "BLOCKED" in message
        assert "org/repo@abc123de" in message

    def test_passing_row_passes(self):
        row = [100.0, True, "2026-07-13T00:00:00Z"]
        passed, message = format_verdict(row, "abc123def456", "org/repo")
        assert passed is True
        assert message == "PASSED: quiz scored 100% on org/repo@abc123de"

    def test_passing_row_with_string_booleans_passes(self):
        # The SQL Statement Execution API returns booleans as strings.
        row = ["100.0", "true", "2026-07-13T00:00:00Z"]
        passed, message = format_verdict(row, "abc123def456", "org/repo")
        assert passed is True

    def test_low_score_blocks_with_score_in_message(self):
        row = [42.0, False, "2026-07-13T00:00:00Z"]
        passed, message = format_verdict(row, "abc123def456", "org/repo")
        assert passed is False
        assert "42%" in message
        assert "org/repo@abc123de" in message

    def test_full_score_but_not_passed_blocks(self):
        row = [100.0, False, "2026-07-13T00:00:00Z"]
        passed, message = format_verdict(row, "abc123def456", "org/repo")
        assert passed is False

    def test_long_repo_name_verdict_fits_commit_status_limit(self):
        # GitHub rejects commit-status descriptions over 140 chars with a 422;
        # a long owner/name must not push the verdict past the limit.
        long_repo = "some-very-long-github-organization-name/" + "a" * 120
        passed, message = format_verdict(None, "abc123def456", long_repo)
        assert passed is False
        assert len(message) == MAX_VERDICT_LEN
        assert message.startswith("BLOCKED: no quiz result for")
        assert message.endswith("...")


class TestClipVerdict:
    def test_short_message_unchanged(self):
        assert clip_verdict("PASSED: ok") == "PASSED: ok"

    def test_multiline_message_collapses_to_one_line(self):
        assert clip_verdict("ERROR: line one\nline two\n  line three") == (
            "ERROR: line one line two line three"
        )

    def test_long_message_truncated_to_limit_with_marker(self):
        clipped = clip_verdict("x" * 500)
        assert len(clipped) == MAX_VERDICT_LEN
        assert clipped.endswith("...")

    def test_exactly_at_limit_is_untouched(self):
        message = "y" * MAX_VERDICT_LEN
        assert clip_verdict(message) == message


class TestMainInfraErrors:
    def test_missing_databricks_cli_is_error_verdict_exit_2(
        self, monkeypatch, capsys, tmp_path
    ):
        # With the databricks CLI absent from PATH, subprocess.run raises
        # FileNotFoundError (an OSError). That is an infrastructure failure:
        # it must print the one-line ERROR verdict and exit 2, not escape as
        # an unhandled traceback (= empty verdict + exit 1 in the action).
        monkeypatch.setenv("PATH", str(tmp_path))  # empty dir: no databricks
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "gate_check.py",
                "--sha", "abc123def456",
                "--repo", "org/repo",
                "--warehouse-id", "w123",
            ],
        )
        exit_code = main()
        out = capsys.readouterr().out.strip()
        assert exit_code == 2
        assert out.startswith("ERROR:")
        assert "\n" not in out


class TestFormatError:
    def test_starts_with_error_prefix(self):
        message = format_error(RuntimeError("statement FAILED: boom"))
        assert message == "ERROR: statement FAILED: boom"

    def test_multiline_cli_stderr_becomes_one_clipped_line(self):
        # databricks CLI stderr can be a multiline dump; the verdict must stay a
        # single commit-status-sized line.
        exc = RuntimeError("statement execution failed: " + "trace line\n" * 40)
        message = format_error(exc)
        assert "\n" not in message
        assert len(message) <= MAX_VERDICT_LEN
        assert message.startswith("ERROR: statement execution failed:")
