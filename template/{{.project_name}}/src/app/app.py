"""PR quiz WebApp: serve N rotated questions for a head commit, grade, publish result."""
import os
import random
from concurrent.futures import ThreadPoolExecutor

import streamlit as st
import streamlit.components.v1 as components

import comment_format
import llm_client
import quiz_store
import ui
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
)
from scm_providers import PublishError, get_provider

# Commit-status context the merge gate keys off. Baked into app.yaml at render
# time from the status_context template input (see app.yaml.tmpl); must match
# QUIZ_STATUS_CONTEXT in the caller workflows and the gate check, otherwise a
# passed quiz posts success on a context the branch protection does not require
# and the merge stays blocked. Fallback matches the template/workflow default.
STATUS_CONTEXT = os.environ.get("QUIZ_STATUS_CONTEXT", "quiz-gate")


def _html(markup):
    """Render trusted HTML from ui.py builders (single audit point for unsafe_allow_html)."""
    st.markdown(markup, unsafe_allow_html=True)


# Defined above do_refresh() so a refresh click can clear it; the decorator
# renders nothing, so running before set_page_config is fine.
@st.cache_data(ttl=60, show_spinner="Checking PRs on GitHub…")
def _pr_info(pairs):
    # The picker lists quizzes across every repo (pr_numbers_for_labels), not
    # one resolved pool row, so there is no single per-call provider to
    # thread through here — same v1-is-github-only limitation the label
    # comment below already flags for a second provider to revisit.
    return get_provider("github").get_pr_metas(pairs)


def do_refresh():
    """Drop warmed/cached reads and rerun — shared by the picker and deep-link 🔄."""
    quiz_store.refresh(st.session_state.get("sha"), st.session_state.get("repo_filter"))
    _pr_info.clear()
    st.rerun()


quiz_store.start_warmer()  # warm the quiz list + warehouse before the first visit
st.set_page_config(page_title="PR Merge-Gate Quiz", page_icon="🧠")
ui.inject_css()

_html(ui.hero())


def taker_identity():
    try:
        return st.context.headers.get("X-Forwarded-Email") or "unknown"
    except Exception:
        return "unknown"


def pick_sha():
    """Resolve which quiz to show: returns (head_sha, repo). repo is None when
    it is not yet known — a `?sha=` deep link with no `?repo=` — leaving the
    sha-only lookup and any disambiguation to the caller (see distinct_repos()
    below). The picker branch always knows repo: quizzes are already grouped
    per (repo, pr_number) below, so each selection carries its own repo."""
    sha = st.query_params.get("sha")
    if sha:
        return sha, st.query_params.get("repo")
    quizzes = quiz_store.recent_quizzes()
    if not quizzes:
        st.info("No quizzes published yet. Comment `/quiz` on a pull request first.")
        st.stop()
    with st.container(key="picker-card"):
        # Native flex row: toggle pushed left, 🔄 pushed hard right (flush with
        # the selectbox edge below) — columns + CSS could not guarantee that.
        with st.container(horizontal=True, horizontal_alignment="distribute",
                          vertical_alignment="center"):
            active_only = st.toggle(
                "Active PRs only", value=True, key="active_only",
                help="Hide quizzes whose PR is closed/merged, or that already have a passed result.",
            )
            if st.button("🔄", key="refresh", help="Refresh the quiz list and question pools"):
                do_refresh()
        # One GET per listed PR yields state + title (fails open to open/no-title).
        # State drives the active filter; title labels the option — so titles show
        # regardless of the toggle. Pairs are (repo, pr_number): one app instance
        # can list quizzes from several repos, each with its own PR numbering.
        label_pairs = pr_numbers_for_labels(quizzes)
        info = _pr_info(label_pairs) if label_pairs else {}
        if active_only:
            states = {pair: m["state"] for pair, m in info.items()}
            quizzes = filter_active_quizzes(quizzes, states)
            if not quizzes:
                st.info("No active PR quizzes. Turn off **Active PRs only** to see all quizzes.")
                st.stop()
        # Label text is the dict key, so two quizzes rendering the same label
        # would collapse into one entry. Labels carry repo + PR + sha, which is
        # unique in v1 (github-only); if a second provider lands with a repo
        # named identically (e.g. ADO org/project/repo colliding with a GitHub
        # owner/name), make the label — or this key — provider-aware.
        labels = {
            build_quiz_label(
                q["repo"], q["pr_number"], q["head_sha"],
                info.get((q["repo"], q["pr_number"]), {}).get("title", ""),
            ): (q["head_sha"], q["repo"])
            for q in quizzes
        }
        return labels[st.selectbox("Pick a quiz (PR head commit)", list(labels))]


def new_attempt(pool, consumed_ids):
    rng = random.Random()
    chosen, _ = rotate_attempt(pool, consumed_ids, rng)
    st.session_state.attempt = [shuffle_options(q, rng) for q in chosen]
    st.session_state.result = None


def publish_result(provider_impl, repo, sha, pr_number, result):
    """Post the quiz-gate status for this attempt — latest attempt wins, the same
    rule actions/gate-check/gate_check.py applies — and, on a pass, the results
    comment. provider_impl is resolved on the main thread right after pool
    resolution (see the get_provider call below pick_sha's pool handling), so
    by the time this runs on a background thread an unknown provider has
    already stopped the page — never a traceback inside a future."""
    short = sha[:8]
    if result["passed"]:
        state, description = "success", f"PASSED: quiz scored 100% on {short}"
    else:
        state = "failure"
        description = (
            f"BLOCKED: last quiz on {short} scored {result['score_pct']:.0f}% "
            "(needs 100%) - retake via the quiz app"
        )
    try:
        provider_impl.post_commit_status(repo, sha, state, description, context=STATUS_CONTEXT)
        outcome = {"ok": True}
        if result["passed"]:
            outcome["url"] = provider_impl.post_pr_comment(
                repo,
                pr_number,
                comment_format.build_pass_comment(sha, result["taker"], result["review"]),
            )
        return outcome
    except PublishError as e:
        return {"ok": False, "error": str(e)}


sha, repo_filter = pick_sha()
# sha + repo_filter identify which CACHED POOL this session loads (do_refresh
# clears by this key). Attempt identity is decided further down, on the
# RESOLVED (sha, provider, repo) — the disambiguation selectbox below can
# change the resolved repo without either of these values moving.
st.session_state.sha = sha
st.session_state.repo_filter = repo_filter

pool = quiz_store.load_pool(sha, repo=repo_filter)
if not pool:
    # A regeneration in progress can briefly read as empty; do not let that
    # empty result occupy the cache for the whole TTL.
    quiz_store.load_pool.clear(sha, repo=repo_filter)
    where = f" in `{repo_filter}`" if repo_filter else ""
    st.warning(
        f"No quiz found for commit `{sha[:12]}`{where}. "
        "Comment `/quiz` on the PR to generate one."
    )
    st.stop()

# A sha-only deep link (no ?repo=) can match more than one repo if two repos
# happen to share a head_sha; pool[0] is only trustworthy once the pool is
# known to hold a single repo's questions.
repos_in_pool = distinct_repos(pool)
if len(repos_in_pool) > 1:
    chosen = st.selectbox(
        "This commit exists in more than one repository — pick one",
        repos_in_pool,
        format_func=lambda pair: f"{pair[0]}:{pair[1]}",
        key="repo-disambiguation",
    )
    pool = [q for q in pool if (q["provider"], q["repo"]) == chosen]

provider = pool[0]["provider"]
repo = pool[0]["repo"]
pr_number = pool[0]["pr_number"]

# Resolve the provider implementation NOW, on the resolved pool row — deploy
# skew makes an unknown provider reachable (a newer job version can write
# pool rows for a provider this app version does not know yet), and failing
# here turns that into a friendly page instead of a traceback at submit time.
try:
    provider_impl = get_provider(provider)
except PublishError as e:
    st.error(
        f"This quiz cannot be taken on this app version: {e}. "
        "Update the app to one that supports this provider, then retry."
    )
    st.stop()

# Attempt identity = the RESOLVED quiz, not the requested one: switching quiz
# in the picker, following a different deep link, or flipping the multi-repo
# disambiguation selectbox above must all discard the in-progress attempt and
# its recorded answers. Without the resolved repo in this check, an attempt
# built from repo A's pool would survive a disambiguation flip to repo B and
# submit A's questions as a result on B (cross-repo pass forgery).
identity = quiz_identity(sha, pool[0])
if st.session_state.get("quiz_identity") != identity:
    for q in st.session_state.get("attempt") or []:
        # Drop the previous attempt's radio selections: a later attempt on the
        # other quiz must never find pre-filled answers under a reused key.
        st.session_state.pop(f"q-{q['question_id']}", None)
    st.session_state.quiz_identity = identity
    st.session_state.attempt = None
    st.session_state.result = None

taker = taker_identity()
# Lifetime progress for this taker on this commit (cached; cleared on submit):
# submitted-attempt count + the distinct questions already recorded as shown.
progress = quiz_store.taker_progress(sha, taker, provider, repo)
# Keep only recorded questions that still exist in the pool: re-generating a
# pool for the same commit mints fresh question_ids, so stale ones must not
# count toward consumed (which would exceed the pool) or steer rotation.
pool_ids = {q["question_id"] for q in pool}
consumed_ids = progress["consumed_ids"] & pool_ids

# Build the current attempt — avoiding questions used in past sessions — before
# the meta row, so the row can report the attempt number and pool consumption.
if st.session_state.get("attempt") is None:
    new_attempt(pool, consumed_ids)
attempt = st.session_state.attempt
result = st.session_state.get("result")

# Attempt number is lifetime: submitted attempts plus the current in-progress
# one (result is None until it is submitted). Consumed counts the distinct
# recorded questions plus the current attempt's (shown now, recorded on submit).
attempt_number = attempt_ordinal(progress["attempts"], in_progress=result is None)
consumed = len(consumed_ids | {q["question_id"] for q in attempt})

_html(ui.meta_row(
    repo=repo,
    pr_number=pr_number,
    sha=sha,
    taker=taker,
    n_per_attempt=pool[0]["n_per_attempt"],
    pool_size=len(pool),
    attempt_number=attempt_number,
    consumed=consumed,
))

# The picker's 🔄 lives in its filter block; a deep link (?sha=) skips the picker,
# so give that view its own refresh here.
if st.query_params.get("sha"):
    with st.container(horizontal=True, horizontal_alignment="right"):
        if st.button("🔄", key="refresh-deep", help="Reload this quiz's question pool"):
            do_refresh()

if result is None:
    # One panel (title + caption + counter + bar) in a keyed anchor so
    # progress.js can pin the whole thing while the questions scroll.
    with st.container(key="progress-sticky"):
        _html(ui.progress_panel(len(attempt)))
    with st.form("quiz"):
        answers = {}
        for i, q in enumerate(attempt, start=1):
            with st.container(key=f"qcard-{i}"):
                # The topline carries the question; the radio keeps it as its
                # (collapsed) label for screen readers. The Q number comes from
                # the card's CSS counter, so card order must stay 1..N here.
                _html(ui.question_topline(q["question"]))
                choice = st.radio(
                    q["question"],
                    options=list(range(len(q["options"]))),
                    format_func=lambda idx, opts=q["options"]: opts[idx],
                    index=None,
                    key=f"q-{q['question_id']}",
                    label_visibility="collapsed",
                )
                answers[q["question_id"]] = choice
        submitted = st.form_submit_button("Submit answers")

    # JS must go through components.html — DOMPurify strips <script> from st.markdown
    components.html(ui.progress_script(len(attempt)), height=0)

    if submitted:
        if any(v is None for v in answers.values()):
            st.error("Answer every question before submitting.")
            st.stop()
        # The verdict must not wait on I/O: grade in memory, start the two
        # independent writes (warehouse INSERT, GitHub publish) in background
        # threads, and rerun immediately — the verdict view below renders the
        # score first and only then waits on these futures. taker was read on
        # the main thread above (st.context is unavailable off-thread).
        result = grade_attempt(attempt, answers)
        result["taker"] = taker
        result["review"] = [
            {"q": q, "given": answers[q["question_id"]]} for q in attempt
        ]
        # provider_impl was resolved on the main thread, right after pool
        # resolution — bound at submit time below, same property as every
        # other value passed to the futures.
        ex = ThreadPoolExecutor(max_workers=2)
        st.session_state.pending_submit = {
            "save": ex.submit(
                quiz_store.save_result,
                provider=provider,
                repo=repo,
                head_sha=sha,
                pr_number=pr_number,
                taker=taker,
                score_pct=result["score_pct"],
                n_questions=result["n_questions"],
                passed=result["passed"],
                question_ids=[q["question_id"] for q in attempt],
            ),
            "publish": ex.submit(publish_result, provider_impl, repo, sha, pr_number, result),
        }
        ex.shutdown(wait=False)  # lets the submitted writes finish
        st.session_state.result = result
        st.rerun()
else:
    if result.get("github") is None:
        # Writes still in flight (first rerun after submit): show the verdict
        # NOW — st renders top-to-bottom, so the card is visible while the
        # spinner below it waits for the background writes to land.
        _html(ui.verdict_card(
            passed=result["passed"],
            score_pct=result["score_pct"],
            sha=sha,
            pr_number=pr_number,
        ))
        if result["passed"]:
            st.balloons()
        pending = st.session_state.pop("pending_submit", None)
        with st.spinner("Updating the merge gate on GitHub…"):
            if pending is None:  # session lost mid-submit (e.g. app restart)
                result["github"] = {
                    "ok": False,
                    "error": "submission state was lost — comment /quiz-check to sync the gate",
                }
            else:
                try:
                    result["github"] = pending["publish"].result(timeout=30)
                except Exception as e:
                    result["github"] = {"ok": False, "error": str(e)}
                try:
                    pending["save"].result(timeout=30)
                except Exception as e:
                    result["save_error"] = str(e)
        quiz_store.taker_progress.clear()  # attempt count + consumed changed
        st.rerun()
    github = result["github"]
    if result.get("save_error"):
        st.warning(
            f"Recording this attempt in the database failed ({result['save_error']}) — "
            "a `/quiz-check` recount will not see it."
        )
    if result["passed"]:
        published_url = github["url"] if github["ok"] else None
        _html(ui.verdict_card(
            passed=True,
            score_pct=result["score_pct"],
            sha=sha,
            pr_number=pr_number,
            published_url=published_url,
        ))
        if not github["ok"]:
            st.warning(
                f"Publishing to GitHub failed ({github['error']}). "
                f"Comment `/quiz-check` on PR #{pr_number} to refresh the merge gate."
            )
    else:
        _html(ui.verdict_card(
            passed=False,
            score_pct=result["score_pct"],
            sha=sha,
            pr_number=pr_number,
        ))
        if not github["ok"]:
            st.warning(
                f"Gate status update failed ({github['error']}); "
                f"comment `/quiz-check` on PR #{pr_number} to sync it."
            )
        for item in result["review"]:
            q, given = item["q"], item["given"]
            if given != q["correct_index"]:
                with st.expander(f"❌ {q['question']}"):
                    st.write(f"Your answer: {q['options'][given]}")
                    if q["explanation"]:
                        st.caption(q["explanation"])
        if result.get("published_url"):
            st.success(f"Results posted to [PR #{pr_number}]({result['published_url']}).")
        elif st.button("Publish results to PR", key="publish-results"):
            missed = comment_format.missed_items(result["review"])
            with st.spinner(f"Generating explanations for {len(missed)} missed question(s)…"):
                try:
                    explanations = llm_client.generate_explanations(missed)
                except Exception as e:
                    st.warning(f"AI explanations unavailable ({e}); using stored ones.")
                    explanations = {}
            body = comment_format.build_fail_comment(
                sha,
                result["taker"],
                result["score_pct"],
                result["review"],
                explanations,
            )
            try:
                with st.spinner("Posting to GitHub…"):
                    result["published_url"] = provider_impl.post_pr_comment(
                        repo, pr_number, body
                    )
                st.rerun()
            except PublishError as e:
                st.warning(f"Could not post to GitHub ({e}). Retry, or retake the quiz.")
        if st.button("Retake with different questions", key="retake"):
            for q in attempt:
                st.session_state.pop(f"q-{q['question_id']}", None)
            new_attempt(pool, consumed_ids)
            st.rerun()
