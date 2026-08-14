"""Pins load-bearing quiz-prompt prose so future edits cannot silently drop rules."""
from prompts import (
    AMBIGUITY_PROMPT_TEMPLATE,
    DISTRACTOR_PROMPT_TEMPLATE,
    PROMPT_TEMPLATE,
    SOFT_DEDUP_PROMPT_TEMPLATE,
)


def _prompt(count=5, diff="+ added_line"):
    return PROMPT_TEMPLATE.format(count=count, diff=diff)


def _flat(text):
    return " ".join(text.split())


class TestQuizPromptTemplate:
    def test_formats_count_and_diff_placeholders(self):
        prompt = _prompt(count=7, diff="+ sentinel_diff_line")
        assert "Generate exactly 7 multiple-choice questions" in prompt
        assert "+ sentinel_diff_line" in prompt

    def test_bans_text_search_answerable_questions(self):
        flat = _flat(_prompt())
        assert (
            "never ask a question whose correct answer is a verbatim token "
            "findable by text-searching the diff"
        ) in flat
        assert "What is the new value of X?" in flat

    def test_keeps_what_changed_questions_that_need_understanding(self):
        flat = _flat(_prompt())
        assert "what changed, why it likely changed, and its impact" in flat
        assert (
            'A "what changed" question is allowed only when its answer states '
            "what the change does or means"
        ) in flat

    def test_semantic_naming_style_is_conditional_and_context_specific(self):
        flat = _flat(_prompt())
        assert "whose meaning is not obvious from the name alone" in flat
        assert "In <function or file>, what does <name> refer to?" in flat
        assert "specific to this diff (it would not hold in another codebase)" in flat
        assert "Skip this style when every name is self-explanatory" in flat

    def test_never_generic_definition_questions(self):
        flat = _flat(_prompt())
        assert "Never ask for a definition that is true everywhere" in flat
        assert "What does CI stand for?" in flat

class TestSoftDedupPromptTemplate:
    def test_formats_questions_placeholder(self):
        prompt = SOFT_DEDUP_PROMPT_TEMPLATE.format(questions="0. sentinel_question")
        assert "0. sentinel_question" in prompt

    def test_pins_duplicate_definition(self):
        flat = _flat(SOFT_DEDUP_PROMPT_TEMPLATE)
        assert "anyone who can answer one can necessarily answer the other" in flat
        assert "test DIFFERENT facts are NOT duplicates" in flat

    def test_asks_for_covered_topics(self):
        flat = _flat(SOFT_DEDUP_PROMPT_TEMPLATE)
        assert "covered_topics" in flat


class TestAmbiguityPromptTemplate:
    def test_formats_items_placeholder(self):
        prompt = AMBIGUITY_PROMPT_TEMPLATE.format(items='[{"index": 0}]')
        assert '[{"index": 0}]' in prompt

    def test_pins_option_set_only_judgement(self):
        flat = _flat(AMBIGUITY_PROMPT_TEMPLATE)
        assert "Do NOT judge whether the correct answer is factually true" in flat
        assert "exactly one defensible choice" in flat


class TestDistractorPromptTemplate:
    def test_formats_question_and_correct_placeholders(self):
        prompt = DISTRACTOR_PROMPT_TEMPLATE.format(
            question="sentinel_question?", correct="sentinel_answer"
        )
        assert "sentinel_question?" in prompt
        assert "sentinel_answer" in prompt

    def test_pins_three_new_incorrect_options(self):
        flat = _flat(DISTRACTOR_PROMPT_TEMPLATE)
        assert "exactly 3 NEW incorrect options" in flat
        assert "Do not restate or reword the correct answer" in flat
