"""LLM explanation client: pure parts only (request building, parsing, extraction)."""
import json

import pytest

from comment_format import missed_items
from llm_client import (
    EXPLAIN_SCHEMA,
    MAX_TOKENS,
    build_explanation_body,
    extract_text,
    parse_explanations,
)


def _missed():
    return [
        {
            "question_id": "q2",
            "question": "Which permission does the endpoint need?",
            "options": ["CAN_MANAGE", "CAN_VIEW", "CAN_QUERY", "CAN_USE"],
            "selected_index": 0,
            "correct_index": 2,
        },
        {
            "question_id": "q7",
            "question": "What does refresh() clear?",
            "options": ["caches", "tables", "tokens", "logs"],
            "selected_index": 3,
            "correct_index": 0,
        },
    ]


class TestBuildExplanationBody:
    def test_strict_json_schema_and_max_tokens(self):
        body = build_explanation_body(_missed())
        assert body["max_tokens"] == MAX_TOKENS
        assert body["response_format"]["type"] == "json_schema"
        assert body["response_format"]["json_schema"]["schema"] == EXPLAIN_SCHEMA
        assert body["response_format"]["json_schema"]["strict"] is True

    def test_prompt_embeds_every_item_with_answer_texts(self):
        prompt = build_explanation_body(_missed())["messages"][0]["content"]
        assert "q2" in prompt and "q7" in prompt
        assert "CAN_MANAGE" in prompt  # selected answer text
        assert "CAN_QUERY" in prompt  # correct answer text
        assert "What does refresh() clear?" in prompt


class TestContractWithCommentFormat:
    def test_missed_items_output_feeds_build_explanation_body(self):
        """The real pipeline is missed_items -> build_explanation_body; exercise
        that contract so a field rename in either module fails a test."""
        review = [
            {
                "q": {
                    "question_id": "q9",
                    "question": "What changed in _run?",
                    "options": ["retries", "locking", "connections", "caching"],
                    "correct_index": 2,
                    "explanation": "",
                    "n_per_attempt": 1,
                },
                "given": 1,
            }
        ]
        body = build_explanation_body(missed_items(review))
        prompt = body["messages"][0]["content"]
        assert "q9" in prompt
        assert "locking" in prompt  # selected answer text
        assert "connections" in prompt  # correct answer text


class TestParseExplanations:
    def test_valid_response_maps_by_question_id(self):
        raw = json.dumps(
            {
                "explanations": [
                    {"question_id": "q2", "explanation": "Because CAN_QUERY invokes."},
                    {"question_id": "q7", "explanation": "refresh() clears caches."},
                ]
            }
        )
        assert parse_explanations(raw) == {
            "q2": "Because CAN_QUERY invokes.",
            "q7": "refresh() clears caches.",
        }

    def test_bad_json_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_explanations('{"explanations": [truncated')

    def test_malformed_entries_skipped(self):
        raw = json.dumps(
            {
                "explanations": [
                    {"question_id": "", "explanation": "no id"},
                    {"question_id": "q2", "explanation": ""},
                    {"question_id": "q7", "explanation": "kept"},
                ]
            }
        )
        assert parse_explanations(raw) == {"q7": "kept"}

    def test_no_valid_entries_raises(self):
        with pytest.raises(ValueError):
            parse_explanations(json.dumps({"explanations": []}))


class TestExtractText:
    def test_plain_string_passthrough(self):
        assert extract_text("hello") == "hello"

    def test_joins_text_blocks_and_skips_reasoning(self):
        content = [
            {"type": "reasoning", "summary": "thinking..."},
            {"type": "text", "text": '{"explanations":'},
            {"type": "text", "text": " []}"},
        ]
        assert extract_text(content) == '{"explanations": []}'
