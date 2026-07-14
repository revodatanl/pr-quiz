"""Job-side pure logic: question count scaling, batching, model output parsing."""
import json

import pytest

from quiz_logic import (
    MAX_QUESTIONS,
    MIN_DIFFICULTY_FACTOR,
    OPTIONS_PER_QUESTION,
    allocate_questions,
    apply_ambiguity_results,
    batch_sizes,
    chunk_files,
    clamp_difficulty,
    compute_question_count,
    dedupe_questions,
    extract_text,
    is_valid_repo,
    normalize_text,
    parse_ambiguity_verdicts,
    parse_difficulty,
    parse_distractors,
    parse_questions,
    parse_soft_dedup,
    SPACING_MAX,
    SPACING_MIN,
    narrow_spacing,
    rebuild_options,
    remove_soft_duplicates,
    reserve_slot,
    retry_wait,
    skip_difficulty_judge,
    widen_spacing,
)


def _question(text, options=None, correct_index=0, explanation=""):
    return {
        "question": text,
        "options": options or ["a", "b", "c", "d"],
        "correct_index": correct_index,
        "explanation": explanation,
    }


class TestExtractText:
    def test_plain_string_passes_through(self):
        assert extract_text('{"questions": []}') == '{"questions": []}'

    def test_content_block_list_joins_text_blocks_only(self):
        content = [
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "thinking..."}]},
            {"type": "text", "text": '{"questions":'},
            {"type": "text", "text": " []}"},
        ]
        assert extract_text(content) == '{"questions": []}'

    def test_empty_list_gives_empty_string(self):
        assert extract_text([]) == ""


class TestComputeQuestionCount:
    def test_one_line_change_gives_one_question(self):
        assert compute_question_count(1) == 1

    def test_zero_lines_still_gives_one_question(self):
        assert compute_question_count(0) == 1

    def test_forty_lines_still_one_question(self):
        assert compute_question_count(40) == 1

    def test_forty_one_lines_gives_two_questions(self):
        assert compute_question_count(41) == 2

    def test_very_big_pr_capped_at_twenty(self):
        assert compute_question_count(100_000) == 20

    def test_exactly_at_cap_boundary(self):
        assert compute_question_count(800) == 20
        assert compute_question_count(799) == 20  # ceil(799/40) = 20
        assert compute_question_count(760) == 19

    def test_default_factor_equals_explicit_one(self):
        for lines in (1, 41, 799, 100_000):
            assert compute_question_count(lines) == compute_question_count(lines, 1.0)

    def test_low_factor_reduces_count(self):
        assert compute_question_count(400, 0.2) == 2

    def test_high_factor_increases_count(self):
        assert compute_question_count(80, 5.0) == 10

    def test_high_factor_still_capped(self):
        assert compute_question_count(400, 5.0) == 20

    def test_ceil_applied_after_multiplying(self):
        # ceil(60/40*2.0)=ceil(3.0)=3; ceil-before-multiply would give 4
        assert compute_question_count(60, 2.0) == 3

    def test_min_clamp_survives_tiny_factor(self):
        assert compute_question_count(10, 0.2) == 1


class TestBatchSizes:
    def test_small_total_single_batch(self):
        assert batch_sizes(5) == [5]

    def test_exact_multiple_of_batch(self):
        assert batch_sizes(40) == [20, 20]

    def test_remainder_becomes_last_batch(self):
        assert batch_sizes(50) == [20, 20, 10]

    def test_max_pool_hundred_questions(self):
        assert batch_sizes(100) == [20, 20, 20, 20, 20]


class TestDedupeQuestions:
    def test_distinct_questions_all_kept_in_order(self):
        qs = [_question("What changed?"), _question("Why change?")]
        assert dedupe_questions(qs) == qs

    def test_exact_duplicate_dropped_keeps_first(self):
        first = _question("What changed?")
        dupe = _question("What changed?", correct_index=2)
        assert dedupe_questions([first, dupe]) == [first]

    def test_case_and_whitespace_variants_are_duplicates(self):
        first = _question("What  changed in   pricing.py?")
        variant = _question("what changed in pricing.py?")
        assert dedupe_questions([first, variant]) == [first]

    def test_empty_list_stays_empty(self):
        assert dedupe_questions([]) == []


class TestParseQuestions:
    def _valid_question(self, **overrides):
        q = {
            "question": "What does the change do?",
            "options": ["a", "b", "c", "d"],
            "correct_index": 2,
            "explanation": "because",
        }
        q.update(overrides)
        return q

    def test_parses_valid_payload(self):
        raw = json.dumps({"questions": [self._valid_question()]})
        parsed = parse_questions(raw)
        assert len(parsed) == 1
        assert parsed[0]["question"] == "What does the change do?"
        assert parsed[0]["correct_index"] == 2

    def test_drops_question_with_out_of_range_correct_index(self):
        raw = json.dumps(
            {"questions": [self._valid_question(), self._valid_question(correct_index=4)]}
        )
        assert len(parse_questions(raw)) == 1

    def test_drops_question_with_wrong_option_count(self):
        raw = json.dumps(
            {"questions": [self._valid_question(options=["a", "b"]), self._valid_question()]}
        )
        assert len(parse_questions(raw)) == 1

    def test_drops_question_with_empty_text(self):
        raw = json.dumps(
            {"questions": [self._valid_question(question="  "), self._valid_question()]}
        )
        assert len(parse_questions(raw)) == 1

    def test_missing_explanation_is_allowed(self):
        q = self._valid_question()
        del q["explanation"]
        parsed = parse_questions(json.dumps({"questions": [q]}))
        assert parsed[0]["explanation"] == ""

    def test_invalid_json_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_questions("not json {")

    def test_no_valid_questions_raises_value_error(self):
        raw = json.dumps({"questions": [self._valid_question(correct_index=99)]})
        with pytest.raises(ValueError):
            parse_questions(raw)


class TestClampDifficulty:
    def test_below_minimum_clamps_to_floor(self):
        assert clamp_difficulty(0.05) == 0.2

    def test_above_maximum_clamps_to_ceiling(self):
        assert clamp_difficulty(7.3) == 5.0

    def test_in_range_value_passes_through(self):
        assert clamp_difficulty(1.5) == 1.5
        assert clamp_difficulty(2) == 2.0


class TestSkipDifficultyJudge:
    def test_below_threshold_does_not_skip(self):
        assert skip_difficulty_judge(3999) is False

    def test_at_threshold_skips(self):
        assert skip_difficulty_judge(4000) is True

    def test_skip_threshold_still_yields_max_questions(self):
        assert compute_question_count(4000, MIN_DIFFICULTY_FACTOR) == MAX_QUESTIONS


class TestParseDifficulty:
    def test_parses_valid_factor(self):
        assert parse_difficulty('{"difficulty_factor": 1.5}') == 1.5

    def test_clamps_above_maximum(self):
        assert parse_difficulty('{"difficulty_factor": 7.3}') == 5.0

    def test_clamps_below_minimum(self):
        assert parse_difficulty('{"difficulty_factor": 0.05}') == 0.2

    def test_integer_factor_allowed(self):
        assert parse_difficulty('{"difficulty_factor": 2}') == 2.0

    def test_reasoning_key_ignored(self):
        assert parse_difficulty('{"difficulty_factor": 1.0, "reasoning": "routine"}') == 1.0

    def test_missing_key_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_difficulty('{"reasoning": "no factor"}')

    def test_bool_factor_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_difficulty('{"difficulty_factor": true}')

    def test_string_factor_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_difficulty('{"difficulty_factor": "2.0"}')

    def test_nan_factor_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_difficulty('{"difficulty_factor": NaN}')

    def test_infinity_factor_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_difficulty('{"difficulty_factor": Infinity}')

    def test_invalid_json_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_difficulty("not json {")


class TestChunkFiles:
    def _file(self, name, size, changed=10):
        return {"filename": name, "text": "x" * size, "changed_lines": changed}

    def test_all_files_fit_single_chunk(self):
        files = [self._file("a.py", 100, changed=3), self._file("b.py", 200, changed=7)]
        chunks = chunk_files(files, budget=1000)
        assert len(chunks) == 1
        assert chunks[0]["text"] == "x" * 100 + "\n\n" + "x" * 200
        assert chunks[0]["changed_lines"] == 10
        assert chunks[0]["filenames"] == ["a.py", "b.py"]

    def test_overflow_splits_in_input_order(self):
        files = [self._file("a.py", 60), self._file("b.py", 60), self._file("c.py", 60)]
        chunks = chunk_files(files, budget=130)
        assert [c["filenames"] for c in chunks] == [["a.py", "b.py"], ["c.py"]]
        assert all(len(c["text"]) <= 130 for c in chunks)

    def test_oversize_file_truncated_to_budget(self):
        chunks = chunk_files([self._file("big.py", 500)], budget=100)
        assert len(chunks) == 1
        assert chunks[0]["text"] == "x" * 100

    def test_max_chunks_cap_drops_tail_files(self):
        files = [self._file(f"f{i}.py", 60) for i in range(4)]
        chunks = chunk_files(files, budget=60, max_chunks=2)
        assert len(chunks) == 2
        assert [c["filenames"] for c in chunks] == [["f0.py"], ["f1.py"]]

    def test_empty_input_returns_empty_list(self):
        assert chunk_files([]) == []

    def test_file_exactly_at_budget_fits(self):
        chunks = chunk_files([self._file("a.py", 100)], budget=100)
        assert len(chunks) == 1
        assert chunks[0]["text"] == "x" * 100


class TestAllocateQuestions:
    def test_single_weight_takes_all(self):
        assert allocate_questions(10, [7]) == [10]

    def test_proportional_split_with_remainder(self):
        assert allocate_questions(4, [30, 10]) == [3, 1]

    def test_equal_weights_largest_remainder_favors_lower_index(self):
        assert allocate_questions(10, [1, 1, 1]) == [4, 3, 3]

    def test_min_one_guaranteed_for_each_chunk(self):
        assert allocate_questions(5, [1000, 1, 1]) == [3, 1, 1]

    def test_total_below_chunk_count_gives_heaviest_one_each(self):
        assert allocate_questions(2, [5, 10, 1]) == [1, 1, 0]

    def test_tie_break_favors_lower_index(self):
        assert allocate_questions(4, [5, 5, 5]) == [2, 1, 1]

    def test_all_zero_weights_sum_preserved(self):
        assert sum(allocate_questions(7, [0, 0, 0])) == 7

    def test_allocation_always_sums_to_total(self):
        cases = [(1, [3]), (9, [2, 5]), (17, [40, 1, 9]), (100, [550] * 5), (3, [0, 4, 0, 4])]
        for total, weights in cases:
            assert sum(allocate_questions(total, weights)) == total


class TestNormalizeText:
    def test_casefolds_and_collapses_whitespace(self):
        assert normalize_text("The  Quick\tFox ") == "the quick fox"

    def test_already_normal_text_unchanged(self):
        assert normalize_text("plain text") == "plain text"


class TestParseSoftDedup:
    def test_parses_groups_and_topics(self):
        raw = json.dumps(
            {"duplicate_groups": [[0, 2], [1, 3]], "covered_topics": ["retry backoff"]}
        )
        assert parse_soft_dedup(raw, 4) == {"groups": [[0, 2], [1, 3]], "topics": ["retry backoff"]}

    def test_empty_groups_is_valid_no_duplicates(self):
        raw = json.dumps({"duplicate_groups": [], "covered_topics": []})
        assert parse_soft_dedup(raw, 4) == {"groups": [], "topics": []}

    def test_invalid_json_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_soft_dedup("not json {", 4)

    def test_missing_duplicate_groups_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_soft_dedup(json.dumps({"covered_topics": []}), 4)

    def test_non_list_duplicate_groups_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_soft_dedup(json.dumps({"duplicate_groups": "none"}), 4)

    def test_out_of_range_and_negative_indices_dropped(self):
        raw = json.dumps({"duplicate_groups": [[0, 9], [-1, 1, 2]]})
        assert parse_soft_dedup(raw, 4)["groups"] == [[1, 2]]

    def test_bool_indices_dropped(self):
        # JSON true/false are bool, an int subclass: must not become indices 1/0
        raw = json.dumps({"duplicate_groups": [[True, False], [2, 3]]})
        assert parse_soft_dedup(raw, 4)["groups"] == [[2, 3]]

    def test_non_int_indices_dropped(self):
        raw = json.dumps({"duplicate_groups": [["0", 1.5, 2, 3]]})
        assert parse_soft_dedup(raw, 4)["groups"] == [[2, 3]]

    def test_group_shrinking_below_two_discarded(self):
        raw = json.dumps({"duplicate_groups": [[0, 99], [1]]})
        assert parse_soft_dedup(raw, 4)["groups"] == []

    def test_non_list_group_discarded(self):
        raw = json.dumps({"duplicate_groups": ["0,1", [2, 3]]})
        assert parse_soft_dedup(raw, 4)["groups"] == [[2, 3]]

    def test_repeated_index_within_group_deduped(self):
        raw = json.dumps({"duplicate_groups": [[2, 0, 2, 0]]})
        assert parse_soft_dedup(raw, 4)["groups"] == [[2, 0]]

    def test_missing_topics_tolerated_as_empty(self):
        raw = json.dumps({"duplicate_groups": []})
        assert parse_soft_dedup(raw, 4)["topics"] == []

    def test_non_list_topics_tolerated_as_empty(self):
        raw = json.dumps({"duplicate_groups": [], "covered_topics": "retry"})
        assert parse_soft_dedup(raw, 4)["topics"] == []

    def test_topics_coerced_stripped_and_empties_dropped(self):
        raw = json.dumps({"duplicate_groups": [], "covered_topics": [" retry backoff ", "", 42]})
        assert parse_soft_dedup(raw, 4)["topics"] == ["retry backoff", "42"]


class TestRemoveSoftDuplicates:
    def test_keeps_lowest_index_of_group(self):
        qs = [_question("a"), _question("b"), _question("c")]
        assert remove_soft_duplicates(qs, [[2, 0]]) == [qs[0], qs[1]]

    def test_multiple_disjoint_groups(self):
        qs = [_question(t) for t in "abcdef"]
        result = remove_soft_duplicates(qs, [[0, 1], [3, 5]])
        assert result == [qs[0], qs[2], qs[3], qs[4]]

    def test_overlapping_groups_resolve_deterministically(self):
        qs = [_question("a"), _question("b"), _question("c")]
        forward = remove_soft_duplicates(qs, [[0, 1], [1, 2]])
        backward = remove_soft_duplicates(qs, [[1, 2], [0, 1]])
        assert forward == backward == [qs[0]]

    def test_empty_groups_returns_same_questions(self):
        qs = [_question("a"), _question("b")]
        assert remove_soft_duplicates(qs, []) == qs

    def test_all_indices_one_group_keeps_first(self):
        qs = [_question("a"), _question("b"), _question("c")]
        assert remove_soft_duplicates(qs, [[0, 1, 2]]) == [qs[0]]

    def test_order_preserved(self):
        qs = [_question(t) for t in "abcde"]
        assert remove_soft_duplicates(qs, [[1, 3]]) == [qs[0], qs[1], qs[2], qs[4]]


class TestParseAmbiguityVerdicts:
    def test_parses_valid_verdicts(self):
        raw = json.dumps({"verdicts": [
            {"index": 0, "ambiguous": True},
            {"index": 2, "ambiguous": False, "reason": "clear"},
        ]})
        assert parse_ambiguity_verdicts(raw, {0, 1, 2}) == {0: True, 2: False}

    def test_index_outside_valid_set_dropped(self):
        raw = json.dumps({"verdicts": [
            {"index": 7, "ambiguous": True},
            {"index": 1, "ambiguous": True},
        ]})
        assert parse_ambiguity_verdicts(raw, {0, 1}) == {1: True}

    def test_string_ambiguous_dropped(self):
        raw = json.dumps({"verdicts": [
            {"index": 0, "ambiguous": "true"},
            {"index": 1, "ambiguous": False},
        ]})
        assert parse_ambiguity_verdicts(raw, {0, 1}) == {1: False}

    def test_bool_index_dropped(self):
        # JSON true is bool, an int subclass: must not be verdict for index 1
        raw = json.dumps({"verdicts": [
            {"index": True, "ambiguous": True},
            {"index": 0, "ambiguous": False},
        ]})
        assert parse_ambiguity_verdicts(raw, {0, 1}) == {0: False}

    def test_duplicate_index_keeps_first_verdict(self):
        raw = json.dumps({"verdicts": [
            {"index": 0, "ambiguous": True},
            {"index": 0, "ambiguous": False},
        ]})
        assert parse_ambiguity_verdicts(raw, {0}) == {0: True}

    def test_all_false_is_valid(self):
        raw = json.dumps({"verdicts": [{"index": i, "ambiguous": False} for i in range(3)]})
        assert parse_ambiguity_verdicts(raw, {0, 1, 2}) == {0: False, 1: False, 2: False}

    def test_zero_valid_verdicts_raises_value_error(self):
        raw = json.dumps({"verdicts": [{"index": 9, "ambiguous": True}]})
        with pytest.raises(ValueError):
            parse_ambiguity_verdicts(raw, {0, 1})

    def test_empty_verdicts_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_ambiguity_verdicts(json.dumps({"verdicts": []}), {0})

    def test_missing_verdicts_key_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_ambiguity_verdicts(json.dumps({}), {0})

    def test_invalid_json_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_ambiguity_verdicts("not json {", {0})


class TestParseDistractors:
    def test_parses_three_valid_distractors(self):
        raw = json.dumps({"distractors": [" one ", "two", "three"]})
        assert parse_distractors(raw, "the answer") == ["one", "two", "three"]

    def test_two_distractors_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_distractors(json.dumps({"distractors": ["one", "two"]}), "the answer")

    def test_four_distractors_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_distractors(json.dumps({"distractors": ["1", "2", "3", "4"]}), "the answer")

    def test_distractor_equal_to_correct_raises_value_error(self):
        raw = json.dumps({"distractors": ["one", "The  Answer", "three"]})
        with pytest.raises(ValueError):
            parse_distractors(raw, "the answer")

    def test_duplicate_distractors_raise_value_error(self):
        with pytest.raises(ValueError):
            parse_distractors(json.dumps({"distractors": ["one", "ONE", "three"]}), "the answer")

    def test_empty_distractor_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_distractors(json.dumps({"distractors": ["one", "  ", "three"]}), "the answer")

    def test_missing_key_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_distractors(json.dumps({}), "the answer")

    def test_invalid_json_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_distractors("not json {", "the answer")


class TestRebuildOptions:
    def test_correct_answer_lands_at_original_index_zero(self):
        q = _question("q", options=["right", "w1", "w2", "w3"], correct_index=0)
        rebuilt = rebuild_options(q, ["d1", "d2", "d3"])
        assert rebuilt["options"] == ["right", "d1", "d2", "d3"]
        assert rebuilt["correct_index"] == 0

    def test_correct_answer_lands_at_middle_index(self):
        q = _question("q", options=["w1", "w2", "right", "w3"], correct_index=2)
        rebuilt = rebuild_options(q, ["d1", "d2", "d3"])
        assert rebuilt["options"] == ["d1", "d2", "right", "d3"]
        assert rebuilt["correct_index"] == 2

    def test_correct_answer_lands_at_last_index(self):
        q = _question("q", options=["w1", "w2", "w3", "right"], correct_index=3)
        rebuilt = rebuild_options(q, ["d1", "d2", "d3"])
        assert rebuilt["options"] == ["d1", "d2", "d3", "right"]
        assert rebuilt["correct_index"] == 3

    def test_result_has_full_option_count(self):
        q = _question("q", correct_index=1)
        assert len(rebuild_options(q, ["d1", "d2", "d3"])["options"]) == OPTIONS_PER_QUESTION

    def test_question_and_explanation_preserved(self):
        q = _question("what changed?", explanation="because")
        rebuilt = rebuild_options(q, ["d1", "d2", "d3"])
        assert rebuilt["question"] == "what changed?"
        assert rebuilt["explanation"] == "because"

    def test_input_dict_not_mutated(self):
        q = _question("q", options=["right", "w1", "w2", "w3"], correct_index=0)
        rebuild_options(q, ["d1", "d2", "d3"])
        assert q["options"] == ["right", "w1", "w2", "w3"]

    def test_rebuilt_question_round_trips_through_parse_questions(self):
        q = _question("what changed?", options=["w1", "right", "w2", "w3"], correct_index=1)
        rebuilt = rebuild_options(q, ["d1", "d2", "d3"])
        parsed = parse_questions(json.dumps({"questions": [rebuilt]}))
        assert parsed == [rebuilt]


class TestApplyAmbiguityResults:
    def test_empty_resolutions_keeps_everything(self):
        qs = [_question("a"), _question("b")]
        assert apply_ambiguity_results(qs, {}) == qs

    def test_none_resolution_drops_question(self):
        qs = [_question("a"), _question("b"), _question("c")]
        assert apply_ambiguity_results(qs, {1: None}) == [qs[0], qs[2]]

    def test_dict_resolution_replaces_in_place(self):
        qs = [_question("a"), _question("b")]
        replacement = _question("b", options=["w", "x", "y", "z"])
        assert apply_ambiguity_results(qs, {1: replacement}) == [qs[0], replacement]

    def test_mixed_keep_drop_replace_preserves_order(self):
        qs = [_question(t) for t in "abcd"]
        replacement = _question("c2")
        result = apply_ambiguity_results(qs, {1: None, 2: replacement})
        assert result == [qs[0], replacement, qs[3]]

    def test_unknown_index_ignored(self):
        qs = [_question("a")]
        assert apply_ambiguity_results(qs, {9: None}) == qs

    def test_all_dropped_gives_empty_list(self):
        qs = [_question("a"), _question("b")]
        assert apply_ambiguity_results(qs, {0: None, 1: None}) == []


class TestRetryWait:
    def test_missing_header_uses_backoff_ladder(self):
        assert retry_wait(0) == 10.0
        assert retry_wait(1) == 20.0
        assert retry_wait(2) == 40.0
        assert retry_wait(3) == 80.0

    def test_numeric_header_wins_over_ladder(self):
        assert retry_wait(0, "30") == 30.0
        assert retry_wait(2, "5") == 5.0

    def test_float_header_accepted(self):
        assert retry_wait(0, "2.5") == 2.5

    def test_header_clamped_to_floor_and_cap(self):
        assert retry_wait(0, "0") == 1.0
        assert retry_wait(0, "-7") == 1.0
        assert retry_wait(0, "9999") == 120.0

    def test_malformed_header_falls_back_to_ladder(self):
        assert retry_wait(1, "soon") == 20.0
        assert retry_wait(1, "") == 20.0

    def test_non_finite_header_falls_back_to_ladder(self):
        assert retry_wait(0, "inf") == 10.0
        assert retry_wait(0, "nan") == 10.0


class TestReserveSlot:
    def test_idle_gate_starts_immediately(self):
        start, nxt = reserve_slot(next_start=0.0, now=100.0, spacing=1.0)
        assert start == 100.0
        assert nxt == 101.0

    def test_busy_gate_queues_after_previous_slot(self):
        start, nxt = reserve_slot(next_start=105.0, now=100.0, spacing=1.0)
        assert start == 105.0
        assert nxt == 106.0

    def test_consecutive_reservations_never_share_a_slot(self):
        nxt = 0.0
        starts = []
        for _ in range(4):  # four workers hitting the gate at the same instant
            start, nxt = reserve_slot(nxt, now=100.0, spacing=1.0)
            starts.append(start)
        assert starts == [100.0, 101.0, 102.0, 103.0]

    def test_spacing_zero_degenerates_to_no_pacing(self):
        start, nxt = reserve_slot(next_start=0.0, now=50.0, spacing=0.0)
        assert start == nxt == 50.0


class TestAdaptiveSpacing:
    def test_widen_doubles_spacing(self):
        assert widen_spacing(1.0) == 2.0
        assert widen_spacing(2.0) == 4.0

    def test_widen_caps_at_maximum(self):
        assert widen_spacing(SPACING_MAX) == SPACING_MAX
        assert widen_spacing(SPACING_MAX - 1.0) == SPACING_MAX

    def test_narrow_decays_gently(self):
        assert narrow_spacing(4.0) == 3.6

    def test_narrow_floors_at_minimum(self):
        assert narrow_spacing(SPACING_MIN) == SPACING_MIN
        assert narrow_spacing(SPACING_MIN * 1.05) == SPACING_MIN

    def test_widen_then_narrow_round_trip_stays_in_bounds(self):
        spacing = SPACING_MIN
        for _ in range(6):
            spacing = widen_spacing(spacing)
        assert spacing == SPACING_MAX
        for _ in range(100):
            spacing = narrow_spacing(spacing)
        assert spacing == SPACING_MIN


class TestIsValidRepo:
    def test_github_owner_slash_name_is_valid(self):
        assert is_valid_repo("octocat/hello-world") is True

    def test_azure_devops_org_project_repo_is_valid(self):
        assert is_valid_repo("my-org/my.project/my_repo") is True

    def test_single_segment_without_slash_is_invalid(self):
        assert is_valid_repo("hello-world") is False

    def test_four_segments_is_invalid(self):
        assert is_valid_repo("a/b/c/d") is False

    def test_empty_string_is_invalid(self):
        assert is_valid_repo("") is False

    def test_trailing_slash_is_invalid(self):
        assert is_valid_repo("owner/name/") is False

    def test_leading_slash_is_invalid(self):
        assert is_valid_repo("/owner/name") is False

    def test_space_in_segment_is_invalid(self):
        assert is_valid_repo("owner/name with space") is False

    def test_disallowed_character_is_invalid(self):
        assert is_valid_repo("owner/name@bad") is False

    def test_dot_only_name_segment_is_invalid(self):
        assert is_valid_repo("owner/..") is False
        assert is_valid_repo("owner/.") is False

    def test_dot_only_owner_segment_is_invalid(self):
        assert is_valid_repo("../name") is False

    def test_dot_only_middle_segment_is_invalid(self):
        assert is_valid_repo("owner/../repo") is False

    def test_dot_prefixed_segment_is_valid(self):
        assert is_valid_repo("owner/.github") is True

    def test_non_ascii_word_characters_are_invalid(self):
        # \w is Unicode by default; the repo value lands in a GitHub API URL path
        assert is_valid_repo("öwner/name") is False
        assert is_valid_repo("owner/naïve") is False


class TestSoftDedupContract:
    def test_parse_then_remove_end_to_end(self):
        qs = [
            _question("What does retry backoff do?"),
            _question("Explain the retry backoff behavior."),
            _question("Why was CHUNK_CHAR_BUDGET added?"),
            _question("What is the purpose of the retry backoff?"),
        ]
        raw = json.dumps({
            "duplicate_groups": [[0, 1, 3]],
            "covered_topics": ["retry backoff", "chunking budget"],
        })
        parsed = parse_soft_dedup(raw, len(qs))
        assert remove_soft_duplicates(qs, parsed["groups"]) == [qs[0], qs[2]]
