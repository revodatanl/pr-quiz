"""Generate a quiz question pool for a PR head commit.

Serverless job entrypoint: fetches the PR diff from GitHub, scales question
count with diff size and an LLM-judged difficulty factor, generates 5xN
multiple-choice questions via a foundation-model serving endpoint (one prompt
per diff chunk), validates the pool (LLM soft-dedup with one topic-steered
top-up round, then an LLM ambiguity check that drops flagged questions while
the pool stays at/above target and only repairs them below it), and
(re)writes the pool for that SHA.
"""
import argparse
import json
import math
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests
from databricks.sdk import WorkspaceClient

from prompts import (
    AMBIGUITY_PROMPT_TEMPLATE,
    AMBIGUITY_SCHEMA,
    DIFFICULTY_SCHEMA,
    DISTRACTOR_PROMPT_TEMPLATE,
    DISTRACTOR_SCHEMA,
    JUDGE_PROMPT_TEMPLATE,
    PROMPT_TEMPLATE,
    QUIZ_SCHEMA,
    SOFT_DEDUP_PROMPT_TEMPLATE,
    SOFT_DEDUP_SCHEMA,
)
from quiz_logic import (
    BATCH_SIZE,
    LINES_PER_QUESTION,
    MAX_QUESTIONS,
    MIN_QUESTIONS,
    allocate_questions,
    apply_ambiguity_results,
    batch_sizes,
    chunk_files,
    compute_question_count,
    dedupe_questions,
    extract_text,
    is_valid_repo,
    parse_ambiguity_verdicts,
    parse_difficulty,
    parse_distractors,
    parse_questions,
    SPACING_MIN,
    narrow_spacing,
    parse_soft_dedup,
    rebuild_options,
    remove_soft_duplicates,
    reserve_slot,
    retry_wait,
    skip_difficulty_judge,
    widen_spacing,
)
from diff_providers import SUPPORTED_PROVIDERS, get_provider

DIFF_CHAR_LIMIT = 150_000
MAX_TOKENS_PER_BATCH = 8000  # gpt-oss spends output tokens on reasoning; 4500 truncated 20-question JSON
MAX_TOKENS_SOFT_DEDUP = 16000  # one call reasons over the WHOLE pool; 8000 came back
                               # empty (all reasoning, no text) on a 184-question pool
SECONDS_BETWEEN_ROUNDS = 15
POOL_MULTIPLIER = 5
PARALLEL_CALLS = 4  # concurrent endpoint requests per round; 429s are absorbed by the retry backoff
OVERPROVISION = 2  # each round asks for 2x the shortfall; failed batches and duplicates eat the slack
MAX_ROUNDS = 3
MAX_ATTEMPTS = 3  # parse/network/HTTP failures: something is wrong, give up quickly
MAX_RATE_LIMIT_ATTEMPTS = 5  # 429s are transient by definition: outlasting the budget
                             # refill beats abandoning a batch or dropping a question


def log(msg):
    """Timestamped (UTC) job log line; flushed so parallel workers interleave cleanly."""
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


class _RateLimited(RuntimeError):
    pass


# Serialized admission gate for all worker threads. Every request start claims
# a distinct slot (reserve_slot), so a phase kicking off 4 parallel calls fires
# them staggered instead of as one burst — bursts, not sustained load, are what
# the endpoint 429s. After a 429 the gate is pushed forward by the backoff and
# waiting threads reopen one by one; a shared absolute window here caused
# synchronized retry volleys that re-collided and escalated in lockstep.
# Spacing is adaptive: a 429 doubles it (short validation calls fired every
# ~1s trip the endpoint's request-rate limit), successes decay it back.
_gate_lock = threading.Lock()
_gate_next_start = 0.0
_gate_spacing = SPACING_MIN


def _acquire_request_slot():
    """Sleep until this thread's admission slot; returns how long it waited."""
    global _gate_next_start
    with _gate_lock:
        start_at, _gate_next_start = reserve_slot(
            _gate_next_start, time.monotonic(), _gate_spacing
        )
    wait = start_at - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    return max(wait, 0.0)


def _delay_requests(seconds, label):
    """Rate-limit cooldown: pause admissions and widen the spacing between them."""
    global _gate_next_start, _gate_spacing
    with _gate_lock:
        _gate_next_start = max(_gate_next_start, time.monotonic() + seconds)
        widened = widen_spacing(_gate_spacing)
        if widened != _gate_spacing:
            log(f"{label}: request spacing {_gate_spacing:.1f}s -> {widened:.1f}s")
        _gate_spacing = widened


def _relax_request_spacing():
    global _gate_spacing
    with _gate_lock:
        _gate_spacing = narrow_spacing(_gate_spacing)


def _call_endpoint(w, endpoint, body, parse, label="model"):
    """POST one serving-endpoint request with retries; parse validates each attempt.

    Separate failure budgets: real errors (bad response, network) stop after
    MAX_ATTEMPTS, while 429s — transient by definition — get
    MAX_RATE_LIMIT_ATTEMPTS to outlast the endpoint's opaque budget refill.
    """
    url = f"{w.config.host}/serving-endpoints/{endpoint}/invocations"
    errors = 0
    throttles = 0
    last_error = None
    while errors < MAX_ATTEMPTS and throttles < MAX_RATE_LIMIT_ATTEMPTS:
        waited = _acquire_request_slot()
        if waited > 2:
            log(f"{label}: waited {waited:.0f}s for a request slot")
        started = time.monotonic()
        try:
            r = requests.post(url, headers=w.config.authenticate(), json=body, timeout=300)
            if r.status_code == 429:
                wait = retry_wait(throttles, r.headers.get("Retry-After"))
                _delay_requests(wait, label)
                raise _RateLimited(f"rate limited (429), backing off {wait:.0f}s")
            r.raise_for_status()
            content = extract_text(r.json()["choices"][0]["message"]["content"])
            result = parse(content)
            _relax_request_spacing()
            log(f"{label}: ok in {time.monotonic() - started:.0f}s")
            return result
        except (RuntimeError, ValueError, requests.RequestException) as e:
            last_error = e
            if isinstance(e, _RateLimited):
                throttles += 1
                if throttles < MAX_RATE_LIMIT_ATTEMPTS:
                    # the wait happens at the gate: the next attempt gets a
                    # distinct slot after the cooldown, never a synchronized volley
                    log(f"{label}: 429 #{throttles}/{MAX_RATE_LIMIT_ATTEMPTS}; "
                        f"retrying after cooldown ({e})")
            elif isinstance(e, ValueError):
                # bad model OUTPUT (unparseable/empty response): the endpoint is
                # healthy, waiting cannot help — retry immediately, only the
                # admission gate spaces the next attempt
                errors += 1
                if errors < MAX_ATTEMPTS:
                    log(f"{label}: attempt {errors}/{MAX_ATTEMPTS} failed ({e}); "
                        f"retrying immediately")
            else:
                errors += 1
                if errors < MAX_ATTEMPTS:
                    wait = 20 * errors
                    log(f"{label}: attempt {errors}/{MAX_ATTEMPTS} failed ({e}); "
                        f"retrying in {wait}s")
                    time.sleep(wait)
    raise RuntimeError(f"{label} call failed after retries: {last_error}")


def call_model(w, endpoint, prompt, prior_questions, covered_topics=(), label="quiz batch"):
    """One batch request against the serving endpoint; returns validated questions."""
    messages = [{"role": "user", "content": prompt}]
    if prior_questions:
        avoid = "; ".join(q["question"][:80] for q in prior_questions[-40:])
        messages.append(
            {"role": "user", "content": f"Already asked, do NOT repeat: {avoid}"}
        )
    if covered_topics:
        topics = "\n".join(f"- {t}" for t in covered_topics)
        messages.append(
            {"role": "user", "content": "The quiz already covers these topics; do NOT "
                                        f"write questions about them:\n{topics}"}
        )
    body = {
        "messages": messages,
        "max_tokens": MAX_TOKENS_PER_BATCH,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "quiz", "schema": QUIZ_SCHEMA, "strict": True},
        },
    }
    return _call_endpoint(w, endpoint, body, parse_questions, label)


def judge_difficulty(w, endpoint, diff_text):
    """Rate whole-diff difficulty; returns a clamped factor, or 1.0 after failed retries."""
    body = {
        "messages": [{"role": "user", "content": JUDGE_PROMPT_TEMPLATE.format(diff=diff_text)}],
        "max_tokens": MAX_TOKENS_PER_BATCH,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "difficulty", "schema": DIFFICULTY_SCHEMA, "strict": True},
        },
    }
    try:
        return _call_endpoint(w, endpoint, body, parse_difficulty, "difficulty judge")
    except RuntimeError as e:
        log(f"difficulty judge failed ({e}); falling back to factor 1.0")
        return 1.0


def decide_difficulty(w, endpoint, files, changed_lines):
    """Whole-diff difficulty factor; skips the judge when it cannot change the outcome."""
    if skip_difficulty_judge(changed_lines):
        log(f"{changed_lines} changed lines guarantee MAX questions; difficulty judge skipped")
        return 1.0
    judge_diff = "\n\n".join(f["text"] for f in files)[:DIFF_CHAR_LIMIT]
    log(f"difficulty judge: rating {len(judge_diff)} chars of diff")
    return judge_difficulty(w, endpoint, judge_diff)


def _tolerant_call(w, endpoint, chunk, size, prior_questions, covered_topics=(),
                   label="quiz batch"):
    """One generation batch; a batch that exhausts its retries is abandoned
    instead of killing the run — the next round makes up the shortfall."""
    prompt = PROMPT_TEMPLATE.format(count=size, diff=chunk["text"])
    try:
        questions = call_model(w, endpoint, prompt, prior_questions, covered_topics, label)
        log(f"{label}: {len(questions)} questions")
        return questions
    except RuntimeError as e:
        log(f"{label}: abandoned ({e})")
        return []


def generate_pool(w, endpoint, files, total, avoid=(), covered_topics=()):
    """Generate at least `total` questions, splitting the diff chunk-wise across prompts.

    Each round over-provisions the remaining shortfall by OVERPROVISION and runs
    its batches in parallel; duplicates are dropped afterwards and the next
    round tops up whatever is still missing. `total` is a minimum: the surplus
    is kept — a bigger pool just gives attempts more rotation variety.

    `avoid` and `covered_topics` steer top-up runs away from an existing pool:
    avoided questions join the do-not-repeat list and covered topics are named
    as off-limits territory in every prompt.
    """
    chunks = chunk_files(files)
    weights = [c["changed_lines"] for c in chunks]
    log(f"generation: pool of {total}+ questions across {len(chunks)} chunks")
    questions = []
    for round_no in range(1, MAX_ROUNDS + 1):
        need = total - len(questions)
        if need <= 0:
            break
        if round_no > 1:
            log(f"generation: cooling {SECONDS_BETWEEN_ROUNDS}s before round {round_no}")
            time.sleep(SECONDS_BETWEEN_ROUNDS)
        alloc = allocate_questions(need * OVERPROVISION, weights)
        batches = [
            (c, size, f"gen r{round_no} batch {i}")
            for i, (c, size) in enumerate(
                ((c, size) for c, q in zip(chunks, alloc) for size in batch_sizes(q)), 1
            )
        ]
        log(f"generation round {round_no}/{MAX_ROUNDS}: {len(batches)} parallel batches "
            f"for {need * OVERPROVISION} questions "
            f"({', '.join(f'{b[2]}={b[1]}q' for b in batches)})")
        prior = list(avoid) + questions
        with ThreadPoolExecutor(max_workers=PARALLEL_CALLS) as pool:
            results = pool.map(
                lambda b: _tolerant_call(w, endpoint, b[0], b[1], prior, covered_topics, b[2]),
                batches,
            )
            for result in results:
                questions.extend(result)
        questions = dedupe_questions(questions)
        log(f"generation round {round_no}/{MAX_ROUNDS}: pool now {len(questions)} unique "
            f"questions (minimum {total})")
    return questions


def soft_dedupe_pool(w, endpoint, questions):
    """LLM pass dropping semantic duplicates; returns (questions, covered_topics).

    Tolerant: validation is an enhancement, not a gate — after failed retries
    the pool is kept as-is (same fallback stance as judge_difficulty).
    """
    if len(questions) < 2:
        return questions, []
    numbered = "\n".join(f"{i}. {q['question']}" for i, q in enumerate(questions))
    log(f"soft-dedup: checking {len(questions)} questions ({len(numbered)} chars, one call)")
    body = {
        "messages": [
            {"role": "user", "content": SOFT_DEDUP_PROMPT_TEMPLATE.format(questions=numbered)}
        ],
        "max_tokens": MAX_TOKENS_SOFT_DEDUP,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "soft_dedup", "schema": SOFT_DEDUP_SCHEMA, "strict": True},
        },
    }
    try:
        verdict = _call_endpoint(
            w, endpoint, body, lambda raw: parse_soft_dedup(raw, len(questions)), "soft-dedup"
        )
    except RuntimeError as e:
        log(f"soft-dedup check failed ({e}); keeping pool as-is")
        return questions, []
    unique = remove_soft_duplicates(questions, verdict["groups"])
    log(f"soft-dedup: removed {len(questions) - len(unique)} of {len(questions)} questions "
        f"across {len(verdict['groups'])} duplicate groups; "
        f"{len(verdict['topics'])} covered topics")
    return unique, verdict["topics"]


def _check_ambiguity_batch(w, endpoint, questions, indices, label="ambiguity batch"):
    """One ambiguity-verdict call for a batch of pool indices; returns {index: bool}.

    Tolerant per batch: after failed retries the batch's questions simply
    default to not-ambiguous.
    """
    items = json.dumps(
        [
            {
                "index": i,
                "question": questions[i]["question"],
                "options": questions[i]["options"],
                "correct_answer": questions[i]["options"][questions[i]["correct_index"]],
            }
            for i in indices
        ],
        indent=2,
    )
    body = {
        "messages": [{"role": "user", "content": AMBIGUITY_PROMPT_TEMPLATE.format(items=items)}],
        "max_tokens": MAX_TOKENS_PER_BATCH,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "ambiguity", "schema": AMBIGUITY_SCHEMA, "strict": True},
        },
    }
    try:
        verdicts = _call_endpoint(
            w, endpoint, body, lambda raw: parse_ambiguity_verdicts(raw, set(indices)), label
        )
        log(f"{label}: {sum(verdicts.values())} of {len(indices)} questions flagged")
        return verdicts
    except RuntimeError as e:
        log(f"{label}: abandoned ({e}); treating its {len(indices)} questions as unambiguous")
        return {}


def check_pool_ambiguity(w, endpoint, questions, indices=None, stage="check"):
    """Batched parallel ambiguity verdicts; returns the set of flagged indices."""
    indices = list(range(len(questions)) if indices is None else indices)
    batches = [indices[i:i + BATCH_SIZE] for i in range(0, len(indices), BATCH_SIZE)]
    log(f"ambiguity {stage}: {len(indices)} questions in {len(batches)} parallel batches")
    labeled = [(b, f"ambiguity {stage} batch {i}/{len(batches)}")
               for i, b in enumerate(batches, 1)]
    flagged = set()
    with ThreadPoolExecutor(max_workers=PARALLEL_CALLS) as pool:
        for verdicts in pool.map(
            lambda b: _check_ambiguity_batch(w, endpoint, questions, b[0], b[1]), labeled
        ):
            flagged.update(i for i, ambiguous in verdicts.items() if ambiguous)
    return flagged


def _regenerate_distractors(w, endpoint, question, label="distractors"):
    """Regenerate a flagged question's wrong options; returns a candidate or None."""
    correct = question["options"][question["correct_index"]]
    prompt = DISTRACTOR_PROMPT_TEMPLATE.format(question=question["question"], correct=correct)
    body = {
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": MAX_TOKENS_PER_BATCH,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "distractors", "schema": DISTRACTOR_SCHEMA, "strict": True},
        },
    }
    try:
        distractors = _call_endpoint(
            w, endpoint, body, lambda raw: parse_distractors(raw, correct), label
        )
    except RuntimeError as e:
        log(f"{label}: abandoned ({e})")
        return None
    return rebuild_options(question, distractors)


def resolve_ambiguity(w, endpoint, questions, target):
    """Repair or drop questions whose option set has more than one defensible answer.

    The pool is over-provisioned, so flagged questions are simply DROPPED as
    long as the pool stays at or above `target` — repairing surplus questions
    just burns calls. Only when dropping would sink the pool below target do
    flagged questions get ONE distractor regeneration and ONE re-check. A
    question that stays ambiguous — or whose regeneration fails, since its
    ambiguity is already established — is dropped; the min-N guard in main()
    is the safety net. A failed re-check keeps the regenerated candidate:
    that is a validation call failing, so the tolerant-skip stance applies.
    """
    started = time.monotonic()
    flagged = sorted(check_pool_ambiguity(w, endpoint, questions))
    if not flagged:
        log(f"ambiguity check: no questions flagged ({time.monotonic() - started:.0f}s)")
        return questions
    for i in flagged:
        log(f"ambiguity: q{i} flagged: {questions[i]['question'][:70]}")
    remaining = len(questions) - len(flagged)
    if remaining >= target:
        log(f"ambiguity check done in {time.monotonic() - started:.0f}s: dropped all "
            f"{len(flagged)} flagged questions; pool {remaining} still >= target {target}, "
            f"regeneration skipped")
        return apply_ambiguity_results(questions, dict.fromkeys(flagged))
    log(f"ambiguity: pool would fall to {remaining} < target {target}; "
        f"regenerating distractors for {len(flagged)} questions in parallel")
    with ThreadPoolExecutor(max_workers=PARALLEL_CALLS) as pool:
        regenerated = pool.map(
            lambda i: _regenerate_distractors(w, endpoint, questions[i], f"distractors q{i}"),
            flagged,
        )
        candidates = {
            i: candidate for i, candidate in zip(flagged, regenerated) if candidate is not None
        }
    resolutions = dict.fromkeys(flagged)
    if candidates:
        repaired = list(questions)
        for i, candidate in candidates.items():
            repaired[i] = candidate
        still_ambiguous = check_pool_ambiguity(w, endpoint, repaired, candidates, "re-check")
        for i, candidate in candidates.items():
            resolutions[i] = None if i in still_ambiguous else candidate
            outcome = "still ambiguous -> dropped" if i in still_ambiguous else "repaired -> kept"
            log(f"ambiguity: q{i} {outcome}")
    for i in flagged:
        if i not in candidates:
            log(f"ambiguity: q{i} regeneration failed -> dropped")
    dropped = sum(1 for r in resolutions.values() if r is None)
    log(f"ambiguity check done in {time.monotonic() - started:.0f}s: {len(flagged)} flagged, "
        f"{len(candidates)} regenerated, {dropped} dropped")
    return apply_ambiguity_results(questions, resolutions)


# Keep in sync with the question_pool DDL in sql/init_tables.sql -
# tests/test_ddl_sync.py fails on any column/type/nullability drift.
POOL_TABLE_DDL = """CREATE TABLE IF NOT EXISTS {table} (
  provider STRING NOT NULL, repo STRING NOT NULL, head_sha STRING NOT NULL,
  pr_number INT, question_id STRING NOT NULL,
  question STRING NOT NULL, options ARRAY<STRING> NOT NULL,
  correct_index INT NOT NULL, explanation STRING,
  n_per_attempt INT NOT NULL, generated_at TIMESTAMP NOT NULL)"""


def write_pool(table, provider, repo, head_sha, pr_number, questions, n_per_attempt):
    """Idempotent per (provider, repo, head_sha): replace any previous pool for this commit."""
    from databricks.connect import DatabricksSession

    spark = DatabricksSession.builder.getOrCreate()
    spark.sql(POOL_TABLE_DDL.format(table=table))
    spark.sql(
        f"DELETE FROM {table} WHERE head_sha = :head_sha AND repo = :repo AND provider = :provider",
        args={"head_sha": head_sha, "repo": repo, "provider": provider},
    )
    rows = [
        (provider, repo, head_sha, pr_number, str(uuid.uuid4()), q["question"], q["options"],
         q["correct_index"], q["explanation"], n_per_attempt)
        for q in questions
    ]
    df = spark.createDataFrame(
        rows,
        "provider STRING, repo STRING, head_sha STRING, pr_number INT, question_id STRING, "
        "question STRING, options ARRAY<STRING>, correct_index INT, explanation STRING, "
        "n_per_attempt INT",
    )
    df.selectExpr("*", "current_timestamp() AS generated_at").write.mode("append").saveAsTable(table)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pr-number", type=int, required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--provider", default="github")
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--table", required=True)
    parser.add_argument("--secret-scope", required=True)
    parser.add_argument("--secret-key", required=True)
    args = parser.parse_args()

    if args.pr_number <= 0:
        raise SystemExit(f"pr_number job parameter must be a positive integer, got {args.pr_number}")
    if not re.fullmatch(r"[0-9a-f]{7,40}", args.head_sha):
        raise SystemExit("head_sha job parameter must be a 7-40 character lowercase hex sha")
    if not is_valid_repo(args.repo):
        raise SystemExit("repo job parameter must be owner/name (or org/project/repo), "
                         f"got {args.repo!r}")
    if args.provider not in SUPPORTED_PROVIDERS:
        raise SystemExit(f"provider job parameter must be one of {sorted(SUPPORTED_PROVIDERS)}, "
                         f"got {args.provider!r}")

    job_started = time.monotonic()
    log(f"quiz generation for PR #{args.pr_number} @ {args.head_sha[:8]} "
        f"(endpoint={args.endpoint}, table={args.table})")
    w = WorkspaceClient()
    provider_impl = get_provider(args.provider)
    token = provider_impl.get_token(w, args.secret_scope, args.secret_key)
    files, changed_lines = provider_impl.fetch_pr_diff(args.repo, args.pr_number, token)
    if not files:
        raise SystemExit(f"PR #{args.pr_number} has an empty diff; nothing to quiz")
    log(f"diff fetched: {len(files)} files, {changed_lines} changed lines, "
        f"{sum(len(f['text']) for f in files)} chars")

    raw_n = math.ceil(changed_lines / LINES_PER_QUESTION)
    log(f"size-based N: {raw_n} raw ({changed_lines} changed lines / "
        f"{LINES_PER_QUESTION} lines per question); difficulty factor applies "
        f"to this, then clamp to {MIN_QUESTIONS}..{MAX_QUESTIONS}")
    factor = decide_difficulty(w, args.endpoint, files, changed_lines)
    n = compute_question_count(changed_lines, factor)
    log(f"difficulty x{factor:.2f} -> N={n} per attempt, pool target {n * POOL_MULTIPLIER}")

    phase = time.monotonic()
    questions = generate_pool(w, args.endpoint, files, n * POOL_MULTIPLIER)
    log(f"generation done in {time.monotonic() - phase:.0f}s: {len(questions)} questions")

    phase = time.monotonic()
    questions, topics = soft_dedupe_pool(w, args.endpoint, questions)
    if len(questions) < n:  # one top-up round, steered away from already-covered topics
        shortfall = n - len(questions)
        log(f"pool below N={n} after soft-dedup; top-up round for {shortfall}+ questions")
        extra = generate_pool(w, args.endpoint, files, shortfall,
                              avoid=questions, covered_topics=topics)
        questions = dedupe_questions(questions + extra)
        questions, _ = soft_dedupe_pool(w, args.endpoint, questions)
    log(f"dedup phase done in {time.monotonic() - phase:.0f}s: {len(questions)} questions kept")
    if len(questions) < n:
        raise SystemExit(f"only {len(questions)} unique questions after dedup; need at least N={n}")

    questions = resolve_ambiguity(w, args.endpoint, questions, n * POOL_MULTIPLIER)
    if len(questions) < n:
        raise SystemExit(f"only {len(questions)} valid questions generated; need at least N={n}")

    phase = time.monotonic()
    log(f"writing {len(questions)} questions to {args.table}")
    write_pool(args.table, args.provider, args.repo, args.head_sha, args.pr_number, questions, n)
    log(f"pool written in {time.monotonic() - phase:.0f}s: {len(questions)} questions for "
        f"{args.head_sha[:8]}; job total {time.monotonic() - job_started:.0f}s")


if __name__ == "__main__":
    main()
