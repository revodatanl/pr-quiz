"""Pure quiz-attempt logic (no I/O) - unit-tested in tests/test_app_logic.py."""


def rotate_attempt(pool, consumed_ids, rng):
    """Pick this attempt's N questions, preferring ones not yet consumed this
    session so retakes surface fresh questions ("every attempt rotates").

    Returns (chosen, new_consumed_ids). new_consumed_ids is consumed_ids plus the
    chosen question ids. When fewer than N unconsumed questions remain the pool
    has cycled: consumed resets and a fresh cycle starts from the whole pool
    (so the consumed counter cycles cleanly rather than sticking near the top).
    The input consumed_ids set is not mutated.
    """
    n = min(pool[0]["n_per_attempt"], len(pool))
    consumed_ids = set(consumed_ids)
    unconsumed = [q for q in pool if q["question_id"] not in consumed_ids]
    if len(unconsumed) < n:
        consumed_ids = set()
        unconsumed = list(pool)
    chosen = rng.sample(unconsumed, n)
    return chosen, consumed_ids | {q["question_id"] for q in chosen}


def shuffle_options(question, rng):
    """Return a copy with options in random order (correct_index re-pointed).

    Models bias the correct answer toward fixed positions; shuffling stops
    takers from gaming answer placement.
    """
    order = list(range(len(question["options"])))
    rng.shuffle(order)
    shuffled = dict(question)
    shuffled["options"] = [question["options"][i] for i in order]
    shuffled["correct_index"] = order.index(question["correct_index"])
    return shuffled


def filter_active_quizzes(quizzes, pr_states):
    """Keep quizzes with no passed result whose PR is not affirmatively closed.

    pr_states is keyed by (repo, pr_number) — a bare pr_number is ambiguous
    once one app instance can serve multiple repos, since two repos can each
    have their own PR #12.

    Fail-open: "unknown" states and PRs missing from pr_states count as open —
    a GitHub hiccup must never hide a takeable quiz. Merged PRs report "closed".
    """
    return [
        q for q in quizzes
        if not q["has_passed_result"]
        and pr_states.get((q["repo"], q["pr_number"])) != "closed"
    ]


def pr_numbers_for_labels(quizzes):
    """All distinct (repo, pr_number) pairs among quizzes with a non-None PR
    number, as a sorted tuple (stable st.cache_data key). Includes passed
    quizzes: their titles are still shown in the picker when the "Active PRs
    only" toggle is off. The same map also feeds the active filter (extra
    entries are harmless — filter_active_quizzes only looks up pairs it is
    asked about). Keyed by (repo, pr_number), not pr_number alone, since two
    repos can each have their own PR of the same number."""
    return tuple(sorted({
        (q["repo"], q["pr_number"]) for q in quizzes if q["pr_number"] is not None
    }))


def distinct_repos(pool):
    """Sorted distinct (provider, repo) pairs appearing in a pool.

    A pool loaded by sha alone (no repo filter) can span more than one repo
    if two different repos happen to share a head_sha. Callers must check this
    before trusting pool[0]'s provider/repo as ground truth for the whole
    pool — more than one entry means disambiguation is required.
    """
    return tuple(sorted({(q["provider"], q["repo"]) for q in pool}))


def quiz_identity(head_sha, question):
    """Session identity of the quiz being answered: the commit plus the
    RESOLVED (provider, repo) of its pool — not just the requested repo filter.

    The distinction matters on sha-only deep links: when two repos share a
    head_sha, the disambiguation picker resolves which repo's pool the session
    is answering. Flipping that picker mid-attempt changes this identity even
    though the sha (and the absent repo filter) did not — and the in-progress
    attempt, built from the OTHER repo's pool, must be discarded. Keeping it
    would submit repo A's questions as a result recorded (and gate-published)
    on repo B: a cross-repo pass forgery.
    """
    return (head_sha, question["provider"], question["repo"])


def build_quiz_label(repo, pr_number, head_sha, title, *, sep=" — ", max_title=60):
    """Picker option label: '<repo> #<n> — <sha[:12]>' plus ' — <title>' when a
    title is present. Title is trimmed, and truncated to max_title chars
    (ellipsis included) so a long PR title cannot blow out the selectbox
    width. Separator defaults to an em dash to match the app's existing label
    style. The repo prefix (rather than a bare "PR #<n>") disambiguates quizzes
    from different repos that happen to share a PR number."""
    label = f"{repo} #{pr_number}{sep}{head_sha[:12]}"
    title = str(title).strip()
    if not title:
        return label
    if len(title) > max_title:
        title = title[:max_title - 1] + "…"
    return f"{label}{sep}{title}"


def warm_is_fresh(now, ts, ttl):
    """True when the background-warmed store is still fresh. ts == 0.0 means the
    warmer has not populated it yet, so it is never fresh."""
    return ts > 0 and (now - ts) < ttl


def attempt_ordinal(submitted, in_progress):
    """1-based number of the attempt the taker is on. `submitted` is how many
    attempts they have already recorded; the current attempt adds one only while
    it is in progress (not yet submitted) — once submitted it is already counted
    in `submitted`, so the verdict view keeps showing that same number."""
    return submitted + (1 if in_progress else 0)


def grade_attempt(pool, answers):
    """Score answered questions against the pool's answer key. 100% = passed."""
    correct = sum(
        1 for q in pool if answers.get(q["question_id"]) == q["correct_index"]
    )
    score_pct = round(100.0 * correct / len(pool), 2)
    return {
        "score_pct": score_pct,
        "n_questions": len(pool),
        "passed": score_pct == 100.0,
    }
