"""Model I/O contracts for quiz generation: prompt templates + response schemas."""

QUIZ_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                    "correct_index": {"type": "integer"},
                    "explanation": {"type": "string"},
                },
                "required": ["question", "options", "correct_index"],
            },
        }
    },
    "required": ["questions"],
}

DIFFICULTY_SCHEMA = {
    "type": "object",
    "properties": {
        "difficulty_factor": {"type": "number"},
        "reasoning": {"type": "string"},
    },
    "required": ["difficulty_factor"],
}

JUDGE_PROMPT_TEMPLATE = """You are calibrating a code-review quiz. Rate how hard the pull-request
diff below is to fully understand, as a single difficulty_factor between 0.2 and 5.0:
- 0.2 = trivial: docs, comments, typos, config text, mechanical renames.
- 1.0 = routine application code a reviewer reads at normal speed.
- 5.0 = extremely difficult: dense math or algorithms, concurrency, driver- or
  protocol-level code, subtle state machines.
Judge the whole diff as one unit and weigh its hardest parts more than its size;
diff size is already accounted for elsewhere. Also return a one-sentence reasoning.

PR diff:
```
{diff}
```"""

PROMPT_TEMPLATE = """You are generating a code-review comprehension quiz for a pull request.
A team member must answer every question correctly before the PR may merge, so the
questions must be answerable ONLY by someone who actually read and understood the diff.

Generate exactly {count} multiple-choice questions about the diff below. Rules:
- Each question has exactly 4 options and exactly one correct answer (correct_index, 0-based).
- Mix question styles across the set: what changed, why it likely changed, and its impact.
  When {count} allows, include at least one question phrased like "What is the main impact
  of <specific change>?" and at least one like "What are possible consequences of
  <specific change>?".
- Name concrete identifiers from the diff (functions, files, constants, config keys) in each
  question so it is unambiguous which change is meant.
- Test understanding, not reading: never ask a question whose correct answer is a verbatim
  token findable by text-searching the diff (like "Which file was changed?", "What is the
  new value of X?", "Which function was added?"). A "what changed" question is allowed
  only when its answer states what the change does or means, not which text it touched.
  Nothing about line numbers, whitespace, or counting edits.
- When {count} allows and the diff uses a name whose meaning is not obvious from the name alone
  (a variable, function, parameter, config key, or a term in docs or comments), include at
  least one question phrased like "In <function or file>, what does <name> refer to?". The
  correct answer must be specific to this diff (it would not hold in another codebase), and
  one wrong option should be the name's generic meaning. Never ask for a definition that is
  true everywhere ("What does CI stand for?"). Skip this style when every name is
  self-explanatory.
- Make wrong options plausible to someone who only skimmed the diff; keep all 4 options
  similar in length and tone so the correct one does not stand out.
- Vary the questions; do not repeat earlier questions in this conversation.
- The diff below may be only part of the pull request; ask only about changes visible in it.

PR diff:
```
{diff}
```"""

SOFT_DEDUP_SCHEMA = {
    "type": "object",
    "properties": {
        "duplicate_groups": {
            "type": "array",
            "items": {"type": "array", "items": {"type": "integer"}},
        },
        "covered_topics": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["duplicate_groups", "covered_topics"],
}

# Single call for the whole pool (<= ~100 questions, texts only): chunking
# would blind the model to cross-chunk duplicates, and the integer-group
# output stays tiny regardless of pool size.
SOFT_DEDUP_PROMPT_TEMPLATE = """You are reviewing the question pool of a code-review quiz about one
pull request. Find semantic duplicates: two questions are duplicates when anyone who can answer
one can necessarily answer the other — they test the same fact about the same change, even if
worded differently. Questions about the same file or function that test DIFFERENT facts are
NOT duplicates.
Return:
- duplicate_groups: each group lists the question numbers (from the numbered list below) that
  duplicate each other. Only groups of two or more; use each number in at most one group;
  return an empty list when there are no duplicates.
- covered_topics: short phrases (at most 10 words each) naming each distinct change or fact
  the pool as a whole covers.

Questions:
{questions}"""

AMBIGUITY_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "ambiguous": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["index", "ambiguous"],
            },
        }
    },
    "required": ["verdicts"],
}

AMBIGUITY_PROMPT_TEMPLATE = """You are auditing multiple-choice quiz questions. For each item below,
judge from the question and its options ALONE whether the marked correct answer is the only
defensibly correct option.
Mark ambiguous=true when any other option could reasonably be defended as correct, when options
overlap or are near-synonyms of the correct answer, or when the options do not clearly exclude
each other. Do NOT judge whether the correct answer is factually true of the codebase — you only
see the option set; judge only whether it has exactly one defensible choice. Return one verdict
per item, echoing its index.

Items (JSON):
{items}"""

DISTRACTOR_SCHEMA = {
    "type": "object",
    "properties": {"distractors": {"type": "array", "items": {"type": "string"}}},
    "required": ["distractors"],
}

DISTRACTOR_PROMPT_TEMPLATE = """A multiple-choice quiz question was flagged because more than one
option could be defended as correct. Write exactly 3 NEW incorrect options (distractors) for it.
Rules:
- Each distractor must be clearly wrong to someone who understood the change, yet plausible to
  someone who only skimmed it.
- No distractor may mean the same as, overlap with, or be defensible instead of the correct
  answer. Do not restate or reword the correct answer.
- Match the correct answer's length and tone so it does not stand out.

Question: {question}
Correct answer: {correct}"""
