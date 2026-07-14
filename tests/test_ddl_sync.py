"""Drift guard: the inline question_pool DDL in write_pool (generate_quiz.py)
must define exactly the same columns as the template's init_tables.sql.tmpl.

The two CREATE TABLE statements are intentionally duplicated (the job must be
able to bootstrap its own table); this test is the sync mechanism.
"""
import re
from pathlib import Path

from generate_quiz import POOL_TABLE_DDL

_RAW_TMPL = (
    Path(__file__).resolve().parents[1]
    / "template" / "{{.project_name}}" / "sql" / "init_tables.sql.tmpl"
).read_text()

# Normalize the Go-template actions ({{.catalog}}, {{.schema}}) the same way
# `databricks bundle init` renders them, so the parser sees plain SQL. The
# column bodies being compared must never contain template actions.
INIT_TABLES_SQL = re.sub(r"\{\{\.\w+\}\}", "x", _RAW_TMPL)
assert "{{" not in INIT_TABLES_SQL, "unexpected Go-template action in init_tables.sql.tmpl"


def _split_top_level(body):
    """Split a DDL column body on commas outside <...> type parameters."""
    parts, depth, current = [], 0, ""
    for ch in body:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    parts.append(current)
    return [p.strip() for p in parts if p.strip()]


def _ddl_columns(create_stmt):
    """Parse a CREATE TABLE statement into (name, type, not_null) tuples."""
    body = create_stmt[create_stmt.index("(") + 1 : create_stmt.rindex(")")]
    columns = []
    for part in _split_top_level(body):
        tokens = part.split()
        name = tokens[0].lower()
        rest = " ".join(tokens[1:]).upper()
        not_null = rest.endswith("NOT NULL")
        col_type = rest.removesuffix("NOT NULL").strip()
        columns.append((name, col_type, not_null))
    return columns


def _question_pool_stmt_from_init_sql():
    match = re.search(
        r"CREATE TABLE IF NOT EXISTS \S*question_pool\s*\(.*?\);",
        INIT_TABLES_SQL,
        re.DOTALL,
    )
    assert match, "question_pool CREATE TABLE not found in init_tables.sql.tmpl"
    return match.group(0)


class TestQuestionPoolDdlSync:
    def test_parser_reads_real_columns_not_empty(self):
        # Guards the guard: an empty-vs-empty comparison must not pass trivially.
        columns = _ddl_columns(_question_pool_stmt_from_init_sql())
        names = [name for name, _, _ in columns]
        assert len(columns) >= 10
        assert {"provider", "repo", "head_sha", "question_id"} <= set(names)

    def test_inline_write_pool_ddl_matches_init_tables_sql(self):
        sql_file_columns = _ddl_columns(_question_pool_stmt_from_init_sql())
        inline_columns = _ddl_columns(POOL_TABLE_DDL)
        assert inline_columns == sql_file_columns
