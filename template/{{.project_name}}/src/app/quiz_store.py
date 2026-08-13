"""Warehouse access for the quiz app (runs as the app's service principal).

Reads are pre-warmed: a daemon thread (start_warmer) refreshes the recent-quizzes
list into a module-global store on an interval, which also keeps the serverless
warehouse hot so the first visitor — and every submit INSERT — skips cold start.
"""
import json
import os
import threading
import time

import streamlit as st
from databricks import sql
from databricks.sdk.core import Config

from app_logic import warm_is_fresh

_cfg = Config()
SCHEMA = os.environ.get("QUIZ_SCHEMA", "workspace.pr_quiz")
POOL_TABLE = f"{SCHEMA}.question_pool"
RESULTS_TABLE = f"{SCHEMA}.quiz_results"

# Background warmer: interval between refreshes; freshness window runs a bit
# longer than the interval so a warmed value stays servable across one refresh's
# latency. Interval is env-tunable (min 30s). See start_warmer().
_WARM_INTERVAL = max(30, int(os.environ.get("QUIZ_WARM_INTERVAL_SEC", "180")))
_WARM_TTL = _WARM_INTERVAL + 60
_DEFAULT_LIMIT = 20
_warm = {"quizzes": None, "ts": 0.0}
_warm_lock = threading.Lock()
_warmer_started = False


def _connect():
    return sql.connect(
        server_hostname=_cfg.host.replace("https://", ""),
        http_path=f"/sql/1.0/warehouses/{os.environ['DATABRICKS_WAREHOUSE_ID']}",
        credentials_provider=lambda: _cfg.authenticate,
    )


# One process-wide connection, serialized by the lock: the connect handshake
# costs ~1-2s, which dominated submit INSERTs and cold reads. The warmer's
# interval query doubles as a keepalive for the session.
_conn = None
_conn_lock = threading.Lock()


def _get_conn():
    global _conn
    if _conn is None:
        _conn = _connect()
    return _conn


def _drop_conn():
    global _conn
    try:
        if _conn is not None:
            _conn.close()
    except Exception:
        pass
    _conn = None


def _run(query, params=None, fetch=True):
    with _conn_lock:
        try:
            with _get_conn().cursor() as cur:
                cur.execute(query, params or {})
                return cur.fetchall() if fetch else None
        except Exception:
            # Stale/dropped session: reconnect and retry — reads only. A failed
            # write may still have landed server-side; retrying could apply it
            # twice, so writes surface the error instead.
            _drop_conn()
            if not fetch:
                raise
            with _get_conn().cursor() as cur:
                cur.execute(query, params or {})
                return cur.fetchall()


def _as_options(value):
    if value is None:
        return []

    if isinstance(value, list):
        return value

    if isinstance(value, tuple):
        return list(value)

    # Databricks SQL Connector can return ARRAY columns as numpy.ndarray
    if hasattr(value, "tolist"):
        converted = value.tolist()
        return converted if isinstance(converted, list) else [converted]

    if isinstance(value, (str, bytes, bytearray)):
        return json.loads(value)

    raise TypeError(f"Unsupported options type: {type(value).__name__}")


@st.cache_data(ttl=300, show_spinner="Loading question pool…")
def load_pool(head_sha, repo=None):
    """Pool rows for a head commit, each carrying its provider+repo.

    When `repo` is known (a deep-link `?repo=` or a picker selection, both of
    which already resolved a specific repo) it filters by repo+sha, the
    precise/cheap query. Left None, it looks up by sha alone: if the sha
    exists in exactly one repo the caller can proceed as normal, but if two
    repos share the sha the returned pool spans both and the caller must
    disambiguate before trusting pool[0]'s repo/provider.
    """
    if repo:
        rows = _run(
            f"""SELECT provider, repo, question_id, question, options, correct_index,
                       coalesce(explanation, '') AS explanation, n_per_attempt, pr_number
                FROM {POOL_TABLE} WHERE head_sha = %(sha)s AND repo = %(repo)s""",
            {"sha": head_sha, "repo": repo},
        )
    else:
        rows = _run(
            f"""SELECT provider, repo, question_id, question, options, correct_index,
                       coalesce(explanation, '') AS explanation, n_per_attempt, pr_number
                FROM {POOL_TABLE} WHERE head_sha = %(sha)s""",
            {"sha": head_sha},
        )
    # Read by column name: reordering the SELECT list cannot shift values into the
    # wrong keys, and a renamed column raises AttributeError here.
    return [
        {
            "provider": r.provider,
            "repo": r.repo,
            "question_id": r.question_id,
            "question": r.question,
            "options": _as_options(r.options),
            "correct_index": int(r.correct_index),
            "explanation": r.explanation,
            "n_per_attempt": int(r.n_per_attempt),
            "pr_number": int(r.pr_number) if r.pr_number is not None else None,
        }
        for r in rows
    ]


def _recent_quizzes_rows(limit=_DEFAULT_LIMIT):
    """Uncached recent-quizzes fetch (SQL + parse). Shared by the st-facing
    recent_quizzes() and the background warmer, so the warmer needs no Streamlit
    script context."""
    # Join fan-out is safe: both aggregates are duplicate-insensitive.
    # Grouped/joined on (provider, repo, head_sha), not head_sha alone: two
    # different repos can otherwise share a head_sha, and a head_sha-only join
    # would leak a passed result (or PR number) from one repo onto the other's
    # identical commit.
    # Note: LIMIT applies before the app-side active filter, so an active quiz
    # older than the newest `limit` can be absent from the picker (deep links
    # by sha are unaffected).
    rows = _run(
        f"""SELECT qp.provider, qp.repo, qp.head_sha, any_value(qp.pr_number) AS pr,
                   max(qp.generated_at) AS ts,
                   max(CASE WHEN qr.passed THEN 1 ELSE 0 END) AS has_passed_result
            FROM {POOL_TABLE} qp
            LEFT JOIN {RESULTS_TABLE} qr
                ON qr.provider = qp.provider AND qr.repo = qp.repo AND qr.head_sha = qp.head_sha
            GROUP BY qp.provider, qp.repo, qp.head_sha ORDER BY ts DESC LIMIT {int(limit)}"""
    )
    return [
        {
            "provider": r.provider,
            "repo": r.repo,
            "head_sha": r.head_sha,
            "pr_number": int(r.pr) if r.pr is not None else None,
            "has_passed_result": bool(r.has_passed_result),
        }
        for r in rows
    ]


def recent_quizzes(limit=_DEFAULT_LIMIT):
    """Recent quizzes for the picker, served from the background-warmed store
    when it is fresh (instant, no warehouse round-trip). Falls back to a live
    fetch on a cold/stale store or a non-default limit."""
    if limit == _DEFAULT_LIMIT:
        with _warm_lock:
            if _warm["quizzes"] is not None and warm_is_fresh(
                time.monotonic(), _warm["ts"], _WARM_TTL
            ):
                return list(_warm["quizzes"])
    with st.spinner("Loading recent quizzes…"):
        quizzes = _recent_quizzes_rows(limit)
    if limit == _DEFAULT_LIMIT:
        _store_warm(quizzes)
    return quizzes


def _store_warm(quizzes):
    with _warm_lock:
        _warm["quizzes"] = list(quizzes)
        _warm["ts"] = time.monotonic()


def _warm_once():
    """One warm cycle: refresh the recent-quizzes store (the query also keeps the
    serverless warehouse hot)."""
    _store_warm(_recent_quizzes_rows(_DEFAULT_LIMIT))


def _warm_loop():
    while True:
        try:
            _warm_once()
        except Exception:
            # Best-effort: a warehouse blip must never crash the warmer thread.
            pass
        time.sleep(_WARM_INTERVAL)


def start_warmer():
    """Start the background warmer once per process (idempotent across Streamlit
    reruns). Daemon thread, so it never blocks app shutdown."""
    global _warmer_started
    with _warm_lock:
        if _warmer_started:
            return
        _warmer_started = True
    threading.Thread(target=_warm_loop, name="quiz-warmer", daemon=True).start()


def refresh(sha=None, repo=None):
    """Drop the warmed quiz list (forcing a fresh fetch) and taker-progress
    reads, plus only the given sha's pool (matched on the same repo filter the
    caller loaded it with) so other sessions' cached pools survive; without a
    sha, drop all pools."""
    with _warm_lock:
        _warm["ts"] = 0.0
    taker_progress.clear()
    if sha:
        load_pool.clear(sha, repo=repo)
    else:
        load_pool.clear()


@st.cache_data(ttl=300, show_spinner=False)
def taker_progress(head_sha, taker, provider, repo):
    """Lifetime progress for this taker on this commit: how many attempts they
    have submitted, and the distinct pool questions already recorded as shown
    (the question_ids JSON stored per attempt). Drives the attempt counter, the
    consumed/remaining line, and cross-session question rotation.

    Filtered by (provider, repo) as well as head_sha: two different repos can
    share a head_sha, and without this filter their attempt counts and
    consumed-question sets would merge into one taker's progress.

    Cached per (sha, taker, provider, repo) so it does not add a warehouse
    round-trip to every rerun; cleared on submit and by refresh() so it
    reflects the newest attempt.
    """
    rows = _run(
        f"""SELECT question_ids FROM {RESULTS_TABLE}
            WHERE head_sha = %(sha)s AND taker = %(taker)s
                  AND provider = %(provider)s AND repo = %(repo)s""",
        {"sha": head_sha, "taker": taker, "provider": provider, "repo": repo},
    )
    consumed = set()
    for r in rows:
        consumed.update(_as_options(r.question_ids))  # JSON (or NULL on old rows)
    return {"attempts": len(rows), "consumed_ids": consumed}


def save_result(provider, repo, head_sha, pr_number, taker, score_pct, n_questions,
                 passed, question_ids):
    # question_ids: the pool question_ids shown this attempt, stored as a JSON
    # string (the SQL connector binds scalars cleanly, not ARRAY literals).
    # taker_progress() reads them back to count consumption and rotate questions.
    _run(
        f"""INSERT INTO {RESULTS_TABLE}
            (provider, repo, head_sha, pr_number, taker, score_pct, n_questions,
             passed, question_ids, submitted_at)
            VALUES (%(provider)s, %(repo)s, %(sha)s, %(pr)s, %(taker)s, %(score)s, %(n)s,
                    %(passed)s, %(qids)s, current_timestamp())""",
        {
            "provider": provider,
            "repo": repo,
            "sha": head_sha,
            "pr": pr_number,
            "taker": taker,
            "score": score_pct,
            "n": n_questions,
            "passed": passed,
            "qids": json.dumps(list(question_ids)),
        },
        fetch=False,
    )
