"""App-side pure logic: attempt sampling (rotation), option shuffling, grading."""
import random

from app_logic import (
    attempt_ordinal,
    build_quiz_label,
    distinct_repos,
    filter_active_quizzes,
    grade_attempt,
    pr_numbers_for_labels,
    quiz_identity,
    rotate_attempt,
    shuffle_options,
    warm_is_fresh,
)


def _pool(size, n_per_attempt=3, provider="github", repo="org/repo"):
    return [
        {
            "question_id": f"q{i}",
            "question": f"Question {i}?",
            "options": ["a", "b", "c", "d"],
            "correct_index": i % 4,
            "explanation": "",
            "n_per_attempt": n_per_attempt,
            "provider": provider,
            "repo": repo,
        }
        for i in range(size)
    ]


def _quiz(pr_number, has_passed_result=False, sha=None, repo="org/repo"):
    return {
        "head_sha": sha or f"sha-{pr_number}",
        "pr_number": pr_number,
        "has_passed_result": has_passed_result,
        "repo": repo,
    }


class TestRotateAttempt:
    def test_picks_n_per_attempt_questions(self):
        pool = _pool(15, n_per_attempt=3)
        chosen, _ = rotate_attempt(pool, consumed_ids=set(), rng=random.Random(1))
        assert len(chosen) == 3

    def test_chosen_questions_come_from_pool(self):
        pool = _pool(15, n_per_attempt=3)
        pool_ids = {q["question_id"] for q in pool}
        chosen, _ = rotate_attempt(pool, consumed_ids=set(), rng=random.Random(1))
        assert all(q["question_id"] in pool_ids for q in chosen)

    def test_no_duplicate_questions_in_attempt(self):
        pool = _pool(15, n_per_attempt=3)
        chosen, _ = rotate_attempt(pool, consumed_ids=set(), rng=random.Random(2))
        ids = [q["question_id"] for q in chosen]
        assert len(ids) == len(set(ids))

    def test_returned_consumed_includes_chosen(self):
        pool = _pool(15, n_per_attempt=3)
        chosen, consumed = rotate_attempt(pool, consumed_ids=set(), rng=random.Random(1))
        assert {q["question_id"] for q in chosen} <= consumed
        assert len(consumed) == 3

    def test_prefers_unconsumed_questions(self):
        # Enough fresh questions remain, so a retake must not repeat consumed ones.
        pool = _pool(15, n_per_attempt=3)
        already = {"q0", "q1", "q2", "q3", "q4"}
        chosen, _ = rotate_attempt(pool, consumed_ids=already, rng=random.Random(7))
        assert all(q["question_id"] not in already for q in chosen)

    def test_accumulates_consumed_across_attempts(self):
        pool = _pool(15, n_per_attempt=3)
        rng = random.Random(3)
        _, consumed1 = rotate_attempt(pool, consumed_ids=set(), rng=rng)
        _, consumed2 = rotate_attempt(pool, consumed_ids=consumed1, rng=rng)
        assert consumed1 < consumed2  # strict superset
        assert len(consumed2) == 6

    def test_cycle_resets_when_not_enough_unconsumed(self):
        # 5-question pool, 3 per attempt: after one attempt only 2 remain (< 3),
        # so the next attempt starts a fresh cycle and consumed drops back to n.
        pool = _pool(5, n_per_attempt=3)
        rng = random.Random(4)
        _, consumed1 = rotate_attempt(pool, consumed_ids=set(), rng=rng)
        assert len(consumed1) == 3
        chosen2, consumed2 = rotate_attempt(pool, consumed_ids=consumed1, rng=rng)
        assert len(chosen2) == 3
        assert len(consumed2) == 3  # reset, not 6

    def test_pool_smaller_than_n_returns_whole_pool(self):
        pool = _pool(2, n_per_attempt=5)
        chosen, consumed = rotate_attempt(pool, consumed_ids=set(), rng=random.Random(1))
        assert len(chosen) == 2
        assert len(consumed) == 2

    def test_does_not_mutate_input_consumed(self):
        pool = _pool(15, n_per_attempt=3)
        original = {"q0"}
        rotate_attempt(pool, consumed_ids=original, rng=random.Random(1))
        assert original == {"q0"}


class TestShuffleOptions:
    def _question(self):
        return {
            "question_id": "q1",
            "question": "Q?",
            "options": ["alpha", "beta", "gamma", "delta"],
            "correct_index": 1,
            "explanation": "",
            "n_per_attempt": 3,
        }

    def test_correct_answer_tracks_its_new_position(self):
        shuffled = shuffle_options(self._question(), rng=random.Random(3))
        assert shuffled["options"][shuffled["correct_index"]] == "beta"

    def test_option_set_preserved(self):
        shuffled = shuffle_options(self._question(), rng=random.Random(3))
        assert sorted(shuffled["options"]) == ["alpha", "beta", "delta", "gamma"]

    def test_original_question_not_mutated(self):
        q = self._question()
        shuffle_options(q, rng=random.Random(3))
        assert q["options"] == ["alpha", "beta", "gamma", "delta"]
        assert q["correct_index"] == 1

    def test_positions_actually_vary_across_rngs(self):
        q = self._question()
        positions = {
            shuffle_options(q, rng=random.Random(seed))["correct_index"]
            for seed in range(10)
        }
        assert len(positions) > 1


class TestGradeAttempt:
    def test_all_correct_scores_hundred_and_passes(self):
        pool = _pool(4)
        answers = {q["question_id"]: q["correct_index"] for q in pool}
        result = grade_attempt(pool, answers)
        assert result["score_pct"] == 100.0
        assert result["passed"] is True
        assert result["n_questions"] == 4

    def test_one_wrong_fails(self):
        pool = _pool(4)
        answers = {q["question_id"]: q["correct_index"] for q in pool}
        answers["q0"] = (pool[0]["correct_index"] + 1) % 4
        result = grade_attempt(pool, answers)
        assert result["score_pct"] == 75.0
        assert result["passed"] is False

    def test_missing_answer_counts_as_wrong(self):
        pool = _pool(2)
        answers = {"q0": pool[0]["correct_index"]}
        result = grade_attempt(pool, answers)
        assert result["score_pct"] == 50.0
        assert result["passed"] is False


class TestFilterActiveQuizzes:
    def test_open_unpassed_quiz_kept(self):
        quizzes = [_quiz(1)]
        assert filter_active_quizzes(quizzes, {("org/repo", 1): "open"}) == quizzes

    def test_passed_quiz_dropped_even_when_pr_open(self):
        quizzes = [_quiz(1, has_passed_result=True)]
        assert filter_active_quizzes(quizzes, {("org/repo", 1): "open"}) == []

    def test_closed_pr_dropped(self):
        # Merged PRs also report "closed", so this covers merged too.
        assert filter_active_quizzes([_quiz(1)], {("org/repo", 1): "closed"}) == []

    def test_unknown_state_kept(self):
        # Fail-open: a fetched-but-failed lookup returns "unknown", which must
        # not hide the quiz (a `== "open"` predicate would drop it).
        quizzes = [_quiz(1)]
        assert filter_active_quizzes(quizzes, {("org/repo", 1): "unknown"}) == quizzes

    def test_unexpected_state_string_kept(self):
        # Anything except "closed" is kept — locks in the fail-open predicate
        # against a future refactor to an allowlist of known states.
        quizzes = [_quiz(1)]
        assert filter_active_quizzes(quizzes, {("org/repo", 1): "future-state"}) == quizzes

    def test_pr_missing_from_states_kept(self):
        quizzes = [_quiz(1)]
        assert filter_active_quizzes(quizzes, {}) == quizzes

    def test_none_pr_number_kept(self):
        quizzes = [_quiz(None, sha="sha-none")]
        assert filter_active_quizzes(quizzes, {("org/repo", 1): "closed"}) == quizzes

    def test_empty_input_returns_empty(self):
        assert filter_active_quizzes([], {}) == []

    def test_same_pr_number_different_repo_not_confused(self):
        # Two repos can each have their own PR #1; repo A's closed PR #1 must
        # not hide repo B's open PR #1.
        quizzes = [_quiz(1, repo="org/repo-a"), _quiz(1, repo="org/repo-b", sha="sha-b")]
        states = {("org/repo-a", 1): "closed", ("org/repo-b", 1): "open"}
        kept = filter_active_quizzes(quizzes, states)
        assert [q["repo"] for q in kept] == ["org/repo-b"]

    def test_order_preserved_for_mixed_list(self):
        quizzes = [
            _quiz(1),
            _quiz(2, has_passed_result=True),
            _quiz(3),
            _quiz(4),
        ]
        states = {
            ("org/repo", 1): "open",
            ("org/repo", 2): "open",
            ("org/repo", 3): "closed",
            ("org/repo", 4): "unknown",
        }
        kept = filter_active_quizzes(quizzes, states)
        assert [q["pr_number"] for q in kept] == [1, 4]

    def test_inputs_not_mutated(self):
        quizzes = [_quiz(1), _quiz(2, has_passed_result=True)]
        states = {("org/repo", 1): "closed"}
        filter_active_quizzes(quizzes, states)
        assert quizzes == [_quiz(1), _quiz(2, has_passed_result=True)]
        assert states == {("org/repo", 1): "closed"}


class TestPrNumbersForLabels:
    def test_includes_passed_quizzes(self):
        # Unlike the old to-check set, labels need titles for every shown quiz,
        # including passed ones (visible when the "Active PRs only" toggle is off).
        quizzes = [_quiz(1), _quiz(2, has_passed_result=True)]
        assert pr_numbers_for_labels(quizzes) == (("org/repo", 1), ("org/repo", 2))

    def test_dedupes_shared_pr_across_shas(self):
        quizzes = [_quiz(7, sha="sha-a"), _quiz(7, sha="sha-b")]
        assert pr_numbers_for_labels(quizzes) == (("org/repo", 7),)

    def test_excludes_none_pr_number(self):
        quizzes = [_quiz(None, sha="sha-none"), _quiz(3)]
        assert pr_numbers_for_labels(quizzes) == (("org/repo", 3),)

    def test_returns_sorted_tuple(self):
        # Sorted tuple: used as a st.cache_data key, so it must be hashable and
        # deterministic regardless of input order.
        quizzes = [_quiz(5), _quiz(2), _quiz(9)]
        assert pr_numbers_for_labels(quizzes) == (("org/repo", 2), ("org/repo", 5), ("org/repo", 9))

    def test_same_pr_number_different_repo_both_kept(self):
        # Two repos with their own PR #1 must both surface — bare pr_number
        # would collapse them into a single (wrong) entry.
        quizzes = [_quiz(1, repo="org/repo-a"), _quiz(1, repo="org/repo-b", sha="sha-b")]
        assert pr_numbers_for_labels(quizzes) == (("org/repo-a", 1), ("org/repo-b", 1))

    def test_empty_input_returns_empty_tuple(self):
        assert pr_numbers_for_labels([]) == ()


class TestDistinctRepos:
    def _row(self, provider="github", repo="org/repo"):
        return {"provider": provider, "repo": repo}

    def test_single_repo_pool_returns_one_pair(self):
        pool = [self._row() for _ in range(3)]
        assert distinct_repos(pool) == (("github", "org/repo"),)

    def test_two_repos_sharing_a_sha_both_returned(self):
        # The scenario a sha-only deep link must guard against: two different
        # repos' rows sharing the queried head_sha.
        pool = [self._row(repo="org/repo-a"), self._row(repo="org/repo-b")]
        assert distinct_repos(pool) == (("github", "org/repo-a"), ("github", "org/repo-b"))

    def test_returns_sorted_tuple(self):
        pool = [self._row(repo="org/z"), self._row(repo="org/a")]
        assert distinct_repos(pool) == (("github", "org/a"), ("github", "org/z"))

    def test_empty_pool_returns_empty_tuple(self):
        assert distinct_repos([]) == ()

    def test_different_providers_same_repo_name_kept_distinct(self):
        pool = [self._row(provider="github"), self._row(provider="azuredevops")]
        assert distinct_repos(pool) == (("azuredevops", "org/repo"), ("github", "org/repo"))


class TestQuizIdentity:
    def _q(self, provider="github", repo="org/repo"):
        return {"provider": provider, "repo": repo, "question_id": "q0"}

    def test_same_sha_and_resolved_repo_is_stable(self):
        assert quiz_identity("sha-1", self._q()) == quiz_identity("sha-1", self._q())

    def test_different_sha_changes_identity(self):
        assert quiz_identity("sha-1", self._q()) != quiz_identity("sha-2", self._q())

    def test_resolved_repo_is_part_of_identity(self):
        # The critical case: same sha, disambiguation flipped to the other
        # repo — identity MUST change so the in-progress attempt is discarded,
        # or repo A's questions could record a pass on repo B.
        a = quiz_identity("sha-1", self._q(repo="org/repo-a"))
        b = quiz_identity("sha-1", self._q(repo="org/repo-b"))
        assert a != b

    def test_provider_is_part_of_identity(self):
        gh = quiz_identity("sha-1", self._q(provider="github"))
        ado = quiz_identity("sha-1", self._q(provider="azuredevops"))
        assert gh != ado

    def test_hashable_and_comparable_for_session_state(self):
        # Stored in st.session_state and compared with != on every rerun.
        identity = quiz_identity("sha-1", self._q())
        assert identity == ("sha-1", "github", "org/repo")
        assert hash(identity) == hash(("sha-1", "github", "org/repo"))


class TestBuildQuizLabel:
    SHA = "4a66932507d9e185e792d9e97ee68541db5aabf7"  # [:12] == 4a66932507d9
    REPO = "octocat/hello-world"

    def test_with_title(self):
        out = build_quiz_label(self.REPO, 428, self.SHA, "Fix the widget")
        assert out == "octocat/hello-world #428 — 4a66932507d9 — Fix the widget"

    def test_without_title_has_no_trailing_separator(self):
        out = build_quiz_label(self.REPO, 428, self.SHA, "")
        assert out == "octocat/hello-world #428 — 4a66932507d9"

    def test_whitespace_title_treated_as_absent(self):
        out = build_quiz_label(self.REPO, 428, self.SHA, "   ")
        assert out == "octocat/hello-world #428 — 4a66932507d9"

    def test_sha_truncated_to_twelve(self):
        out = build_quiz_label(self.REPO, 428, self.SHA, "t")
        assert "4a66932507d9" in out
        assert self.SHA[:13] not in out

    def test_long_title_truncated_with_ellipsis(self):
        title = "x" * 100
        out = build_quiz_label(self.REPO, 428, self.SHA, title, max_title=60)
        # the title segment is capped (ellipsis counts toward the cap)
        segment = out.rsplit(" — ", 1)[-1]
        assert len(segment) <= 60
        assert segment.endswith("…")

    def test_custom_separator(self):
        out = build_quiz_label(self.REPO, 428, self.SHA, "Fix it", sep=" - ")
        assert out == "octocat/hello-world #428 - 4a66932507d9 - Fix it"

    def test_different_repos_same_pr_number_produce_different_labels(self):
        # The reason for the repo prefix: without it, two repos' PR #1 would
        # render identically in the picker.
        a = build_quiz_label("org/repo-a", 1, self.SHA, "")
        b = build_quiz_label("org/repo-b", 1, self.SHA, "")
        assert a != b


class TestAttemptOrdinal:
    def test_first_attempt_in_progress(self):
        assert attempt_ordinal(submitted=0, in_progress=True) == 1

    def test_third_attempt_in_progress(self):
        # two attempts submitted, now taking the third
        assert attempt_ordinal(submitted=2, in_progress=True) == 3

    def test_viewing_just_submitted_result(self):
        # the attempt is submitted (already counted in `submitted`); its verdict
        # view keeps showing that same number, not the next one.
        assert attempt_ordinal(submitted=1, in_progress=False) == 1

    def test_viewing_later_submitted_result(self):
        assert attempt_ordinal(submitted=3, in_progress=False) == 3


class TestWarmIsFresh:
    def test_fresh_within_ttl(self):
        assert warm_is_fresh(now=100.0, ts=90.0, ttl=30.0) is True

    def test_stale_past_ttl(self):
        assert warm_is_fresh(now=200.0, ts=90.0, ttl=30.0) is False

    def test_never_warmed_zero_ts_is_stale(self):
        # ts == 0.0 means the warmer has not populated the store yet.
        assert warm_is_fresh(now=5.0, ts=0.0, ttl=30.0) is False

    def test_exact_ttl_boundary_is_stale(self):
        assert warm_is_fresh(now=120.0, ts=90.0, ttl=30.0) is False
