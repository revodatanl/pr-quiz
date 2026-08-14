"""Pure quiz-generation logic (no I/O) - unit-tested in tests/test_quiz_logic.py.

Sizing, request pacing and model-output parsing. diff_corpus.py turns the PR's
diff into the corpus these questions are asked about.
"""
import json
import math
import re

MIN_QUESTIONS = 1
MAX_QUESTIONS = 20
LINES_PER_QUESTION = 40
BATCH_SIZE = 20
OPTIONS_PER_QUESTION = 4
MIN_DIFFICULTY_FACTOR = 0.2
MAX_DIFFICULTY_FACTOR = 5.0
# At/above this many changed lines even MIN_DIFFICULTY_FACTOR yields MAX_QUESTIONS,
# so the difficulty judge call is skipped. Float (~3999.9999999999995): integer
# comparisons around the boundary are still correct; never compare it for equality.
JUDGE_SKIP_LINES = LINES_PER_QUESTION * MAX_QUESTIONS / MIN_DIFFICULTY_FACTOR
RETRY_AFTER_CAP = 120  # trust Retry-After, but never park a worker longer than this
SPACING_MIN = 1.0  # request-start spacing floor: long calls self-pace anyway
SPACING_MAX = 8.0  # widening cap: past this, waiting out the 429 ladder is cheaper
# owner/name (GitHub) or org/project/repo (Azure DevOps) - no leading/trailing
# slash, no whitespace or other separators inside a segment. The value is
# spliced into a provider API URL path, so segments must be ASCII (re.ASCII
# keeps \w from matching Unicode letters) and never dot-only ("." / ".."
# path traversal); dot-prefixed names like ".github" stay valid.
_REPO_SEGMENT = r"(?!\.+(?:/|\Z))[\w.-]+"
REPO_PATTERN = re.compile(rf"{_REPO_SEGMENT}(?:/{_REPO_SEGMENT}){{1,2}}", re.ASCII)


def is_valid_repo(repo):
    """True when repo is a plausible owner/name or org/project/repo identifier."""
    return bool(REPO_PATTERN.fullmatch(repo))


def compute_question_count(changed_lines, difficulty_factor=1.0):
    """N scales with PR size and judged difficulty: 1 up to 20 (very big or hard PR)."""
    scaled = math.ceil(changed_lines / LINES_PER_QUESTION * difficulty_factor)
    return max(MIN_QUESTIONS, min(MAX_QUESTIONS, scaled))


def skip_difficulty_judge(changed_lines):
    """True when even MIN_DIFFICULTY_FACTOR still yields MAX_QUESTIONS."""
    return changed_lines >= JUDGE_SKIP_LINES


def _load(raw):
    """json.loads, but a parse failure raises the ValueError callers retry on."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"model response is not valid JSON: {e}") from e


def parse_difficulty(raw):
    """Validate a difficulty-judge response and return the clamped factor.

    Clamped into [MIN_DIFFICULTY_FACTOR, MAX_DIFFICULTY_FACTOR]. Raises ValueError
    on unparseable JSON or a missing, non-numeric or non-finite difficulty_factor,
    so the caller can retry.
    """
    payload = _load(raw)
    factor = payload.get("difficulty_factor") if isinstance(payload, dict) else None
    # bool is an int subclass: JSON true must not become factor 1.0
    if isinstance(factor, bool) or not isinstance(factor, (int, float)) or not math.isfinite(factor):
        raise ValueError("model response has no numeric difficulty_factor")
    return max(MIN_DIFFICULTY_FACTOR, min(MAX_DIFFICULTY_FACTOR, float(factor)))


def retry_wait(attempt, retry_after=None):
    """Seconds to wait before retrying a rate-limited call.

    Prefers the server's Retry-After header, clamped to [1, RETRY_AFTER_CAP];
    falls back to a 10/20/40/80 exponential ladder when the header is missing
    or malformed. The endpoint's request-rate budget refills within seconds,
    so the first retry is cheap and only repeat offenders wait long.
    """
    try:
        seconds = float(retry_after)
    except (TypeError, ValueError):
        seconds = None
    if seconds is None or not math.isfinite(seconds):
        return 10.0 * 2 ** attempt
    return min(max(seconds, 1.0), float(RETRY_AFTER_CAP))


def widen_spacing(current):
    """Double the request-start spacing after a 429, capped at SPACING_MAX.

    Short back-to-back calls are what trip the endpoint's request-rate limit;
    long calls never collide because their duration spaces them naturally.
    """
    return min(current * 2.0, SPACING_MAX)


def narrow_spacing(current):
    """Gently relax spacing after a success, never below SPACING_MIN."""
    return max(current * 0.9, SPACING_MIN)


def reserve_slot(next_start, now, spacing):
    """Claim the next request-start slot; returns (start_at, new_next_start).

    Serialized admission: each reservation starts no earlier than the previous
    one plus spacing, so callers arriving together get DISTINCT start times —
    a burst of parallel workers cannot fire simultaneous requests, and threads
    waiting out a rate-limit delay reopen one by one instead of stampeding.
    """
    start_at = max(next_start, now)
    return start_at, start_at + spacing


def normalize_text(text):
    """Case/whitespace-insensitive comparison key for question and option text."""
    return " ".join(text.casefold().split())


def dedupe_questions(questions):
    """Drop repeated questions (case/whitespace-insensitive text), keeping the first.

    Parallel generation calls cannot see each other's output, so duplicates are
    removed after each round instead of relying only on the prompt's
    do-not-repeat list.
    """
    seen = {}
    for q in questions:
        seen.setdefault(normalize_text(q["question"]), q)
    return list(seen.values())


def parse_soft_dedup(raw, pool_size):
    """Validate a soft-dedup response; return {"groups": [[int]], "topics": [str]}.

    Groups keep only real in-range int indices, order-preserved and deduped; a
    group needs 2+ survivors. An empty groups list is a VALID "no duplicates"
    outcome — deliberate contrast with parse_questions. Topics are best-effort:
    anything malformed collapses to []. Raises ValueError on unparseable JSON
    or a missing/non-list duplicate_groups, so the caller can retry.
    """
    payload = _load(raw)
    raw_groups = payload.get("duplicate_groups") if isinstance(payload, dict) else None
    if not isinstance(raw_groups, list):
        raise ValueError("model response has no duplicate_groups list")
    groups = []
    for group in raw_groups:
        if not isinstance(group, list):
            continue
        # Filter before dict.fromkeys, which dedupes order-preserved but would
        # raise on an unhashable junk entry. bool is an int subclass: JSON true
        # must not become index 1.
        indices = list(dict.fromkeys(
            i for i in group
            if isinstance(i, int) and not isinstance(i, bool) and 0 <= i < pool_size
        ))
        if len(indices) >= 2:
            groups.append(indices)
    raw_topics = payload.get("covered_topics")
    topics = []
    if isinstance(raw_topics, list):
        topics = [topic for topic in (str(t).strip() for t in raw_topics) if topic]
    return {"groups": groups, "topics": topics}


def remove_soft_duplicates(questions, groups):
    """Drop semantic duplicates flagged by the model, keeping each group's first.

    Dropping the union of every group's non-minimum indices keeps the result
    deterministic even when the model returns overlapping groups.
    """
    drop = set()
    for group in groups:
        drop.update(i for i in group if i != min(group))
    return [q for i, q in enumerate(questions) if i not in drop]


def allocate_questions(total, weights):
    """Split a question total across chunks proportional to their changed lines.

    Largest-remainder allocation in integer arithmetic; always sums to total.
    Every chunk gets at least one question unless total < len(weights), in which
    case the heaviest chunks win (ties to the lower index).
    """
    k = len(weights)
    if k == 0:
        return []
    if total <= 0:
        return [0] * k
    w = [max(0, x) for x in weights]
    if not any(w):
        w = [1] * k
    if total < k:
        alloc = [0] * k
        for i in sorted(range(k), key=lambda i: (-w[i], i))[:total]:
            alloc[i] = 1
        return alloc
    rest, s = total - k, sum(w)
    alloc = [1 + rest * x // s for x in w]
    remainders = [rest * x % s for x in w]
    for i in sorted(range(k), key=lambda i: (-remainders[i], i))[: total - sum(alloc)]:
        alloc[i] += 1
    return alloc


def batch_sizes(total):
    """Split a question total into model-request batches (output-token limits)."""
    full, rest = divmod(total, BATCH_SIZE)
    return [BATCH_SIZE] * full + ([rest] if rest else [])


def extract_text(content):
    """Chat content may be a plain string or a list of content blocks
    (reasoning models return [{'type': 'reasoning', ...}, {'type': 'text', 'text': ...}])."""
    if isinstance(content, str):
        return content
    return "".join(
        block.get("text", "") for block in content if block.get("type") == "text"
    )


def parse_questions(raw):
    """Validate a model response; return clean question dicts.

    Drops malformed entries; raises ValueError on unparseable JSON or when
    nothing valid remains, so the caller can retry the batch.
    """
    payload = _load(raw)
    valid = []
    for q in payload.get("questions", []):
        question = str(q.get("question", "")).strip()
        options = q.get("options")
        correct = q.get("correct_index")
        if not question:
            continue
        if not isinstance(options, list) or len(options) != OPTIONS_PER_QUESTION:
            continue
        if not isinstance(correct, int) or not 0 <= correct < len(options):
            continue
        valid.append(
            {
                "question": question,
                "options": [str(o) for o in options],
                "correct_index": correct,
                "explanation": str(q.get("explanation", "") or ""),
            }
        )
    if not valid:
        raise ValueError("model response contained no valid questions")
    return valid


def parse_ambiguity_verdicts(raw, valid_indices):
    """Validate an ambiguity-audit response; return {index: ambiguous_bool}.

    Verdicts with unknown indices or non-bool flags are dropped; the first
    verdict wins when an index repeats. Indices absent from the result are
    treated as not-ambiguous by the caller. Raises ValueError on unparseable
    JSON or when no verdict references a requested index (the model ignored
    the task), so the caller can retry.
    """
    payload = _load(raw)
    raw_verdicts = payload.get("verdicts") if isinstance(payload, dict) else None
    verdicts = {}
    for v in raw_verdicts if isinstance(raw_verdicts, list) else []:
        if not isinstance(v, dict):
            continue
        index, ambiguous = v.get("index"), v.get("ambiguous")
        # bool is an int subclass: JSON true must not become index 1
        if isinstance(index, bool) or not isinstance(index, int):
            continue
        if index in valid_indices and index not in verdicts and isinstance(ambiguous, bool):
            verdicts[index] = ambiguous
    if not verdicts:
        raise ValueError("model response contained no verdicts for the requested questions")
    return verdicts


def parse_distractors(raw, correct_answer):
    """Validate a distractor-regeneration response; return the clean distractors.

    Raises ValueError (so the caller can retry) unless exactly
    OPTIONS_PER_QUESTION - 1 non-empty distractors survive that are pairwise
    distinct and distinct from the correct answer under normalize_text.
    """
    payload = _load(raw)
    raw_distractors = payload.get("distractors") if isinstance(payload, dict) else None
    if not isinstance(raw_distractors, list):
        raise ValueError("model response has no distractors list")
    distractors = [str(d).strip() for d in raw_distractors]
    keys = {normalize_text(d) for d in distractors}
    if (
        len(distractors) != OPTIONS_PER_QUESTION - 1
        or not all(distractors)
        or len(keys) != len(distractors)
        or normalize_text(correct_answer) in keys
    ):
        raise ValueError("model response has no usable distractor set")
    return distractors


def rebuild_options(question, distractors):
    """Return a copy of a question with fresh distractors around the same answer.

    The original correct-answer text goes back in at the original
    correct_index, so the result still satisfies the parse_questions contract.
    """
    options = list(distractors)
    options.insert(question["correct_index"], question["options"][question["correct_index"]])
    return dict(question, options=options)


def apply_ambiguity_results(questions, resolutions):
    """Apply ambiguity fixes keyed by index: absent = keep, None = drop, dict = replace."""
    kept = []
    for i, q in enumerate(questions):
        if i not in resolutions:
            kept.append(q)
        elif resolutions[i] is not None:
            kept.append(resolutions[i])
    return kept
