"""PR comment markdown builders: pass/fail templates and missed-item extraction."""
from comment_format import build_fail_comment, build_pass_comment, missed_items

SHA = "4a66932507d9e185e792d9e97ee68541db5aabf7"


def _review():
    """Two questions: q1 answered correctly, q2 missed (selected 0, correct 2)."""
    return [
        {
            "q": {
                "question_id": "q1",
                "question": "What does refresh() clear?",
                "options": ["caches", "tables", "tokens", "logs"],
                "correct_index": 0,
                "explanation": "stored explanation one",
                "n_per_attempt": 2,
            },
            "given": 0,
        },
        {
            "q": {
                "question_id": "q2",
                "question": "Which permission does the endpoint need?",
                "options": ["CAN_MANAGE", "CAN_VIEW", "CAN_QUERY", "CAN_USE"],
                "correct_index": 2,
                "explanation": "stored explanation two",
                "n_per_attempt": 2,
            },
            "given": 0,
        },
    ]


class TestMissedItems:
    def test_only_wrong_answers_returned(self):
        missed = missed_items(_review())
        assert [m["question_id"] for m in missed] == ["q2"]

    def test_fields_extracted(self):
        missed = missed_items(_review())[0]
        assert missed["question"] == "Which permission does the endpoint need?"
        assert missed["options"] == ["CAN_MANAGE", "CAN_VIEW", "CAN_QUERY", "CAN_USE"]
        assert missed["selected_index"] == 0
        assert missed["correct_index"] == 2

    def test_all_correct_gives_empty_list(self):
        review = _review()
        review[1]["given"] = review[1]["q"]["correct_index"]
        assert missed_items(review) == []


class TestBuildPassComment:
    def test_contains_every_question_and_correct_answer(self):
        body = build_pass_comment(SHA, "dev@example.com", _review())
        assert "What does refresh() clear?" in body
        assert "✅ caches" in body
        assert "Which permission does the endpoint need?" in body
        assert "✅ CAN_QUERY" in body

    def test_header_has_short_sha_and_taker(self):
        body = build_pass_comment(SHA, "dev@example.com", _review())
        assert f"`{SHA[:12]}`" in body
        assert "dev@example.com" in body
        assert "100%" in body

    def test_wraps_questions_in_details_block(self):
        body = build_pass_comment(SHA, "dev@example.com", _review())
        assert "<details>" in body and "</details>" in body


class TestBuildFailComment:
    def test_correct_question_gets_check_line_with_answer(self):
        body = build_fail_comment(SHA, "dev@example.com", 50.0, _review(), {})
        assert "✅ **What does refresh() clear?** — correct: caches" in body

    def test_missed_question_shows_selected_and_correct(self):
        body = build_fail_comment(SHA, "dev@example.com", 50.0, _review(), {})
        assert "❌ **Which permission does the endpoint need?**" in body
        assert "Your answer: CAN_MANAGE" in body
        assert "Correct answer: CAN_QUERY" in body

    def test_ai_explanation_preferred(self):
        body = build_fail_comment(
            SHA, "dev@example.com", 50.0, _review(), {"q2": "AI explanation here"}
        )
        assert "💡 AI explanation here" in body
        assert "stored explanation two" not in body

    def test_falls_back_to_stored_explanation(self):
        body = build_fail_comment(SHA, "dev@example.com", 50.0, _review(), {})
        assert "💡 stored explanation two" in body

    def test_falls_back_to_stock_line_when_nothing_available(self):
        review = _review()
        review[1]["q"]["explanation"] = ""
        body = build_fail_comment(SHA, "dev@example.com", 50.0, review, {})
        assert "💡 No explanation available." in body

    def test_header_score_and_tally(self):
        body = build_fail_comment(SHA, "dev@example.com", 50.0, _review(), {})
        assert "50% on" in body
        assert "gate needs 100%" in body
        assert "1/2 correctly" in body
