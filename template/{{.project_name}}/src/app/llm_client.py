"""Wrong-answer explanations from the model serving endpoint.

Deliberately duplicates the job's endpoint plumbing (src/job/generate_quiz.py
`_call_endpoint`, src/job/quiz_logic.py `extract_text`): the app deploys
src/app only, and a UI spinner needs short retries, not the job's 20/40/60s
backoff.
"""
import json
import os
import time

import requests

ENDPOINT = os.environ.get("SERVING_ENDPOINT", "databricks-gpt-oss-120b")
MAX_TOKENS = 8000  # gpt-oss spends output tokens on reasoning; lower truncates JSON
RETRIES = 1  # a taker is watching a spinner; worst case must stay in low minutes
BACKOFF_S = 5
TIMEOUT_S = 120

EXPLAIN_SCHEMA = {
    "type": "object",
    "properties": {
        "explanations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question_id": {"type": "string"},
                    "explanation": {"type": "string"},
                },
                "required": ["question_id", "explanation"],
            },
        }
    },
    "required": ["explanations"],
}

EXPLAIN_PROMPT_TEMPLATE = """A developer failed a code-review quiz about a pull request.
For every item below, explain in 2-3 sentences why the selected answer is wrong and why
the correct answer is right. Be concrete about what each answer claims; do not mention
option letters or indexes. Return exactly one explanation per question_id.

Missed questions (JSON):
{items}"""


def extract_text(content):
    """Chat content may be a plain string or a list of content blocks
    (reasoning models return [{'type': 'reasoning', ...}, {'type': 'text', 'text': ...}])."""
    if isinstance(content, str):
        return content
    return "".join(
        block.get("text", "") for block in content if block.get("type") == "text"
    )


def build_explanation_body(missed):
    """Request body for one batched explanation call over all missed questions."""
    items = [
        {
            "question_id": m["question_id"],
            "question": m["question"],
            "options": m["options"],
            "selected_answer": m["options"][m["selected_index"]],
            "correct_answer": m["options"][m["correct_index"]],
        }
        for m in missed
    ]
    prompt = EXPLAIN_PROMPT_TEMPLATE.format(items=json.dumps(items, indent=2))
    return {
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": MAX_TOKENS,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "explanations", "schema": EXPLAIN_SCHEMA, "strict": True},
        },
    }


def parse_explanations(raw):
    """Return {question_id: explanation}; raises ValueError so the caller can retry."""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"model response is not valid JSON: {e}") from e
    out = {}
    for item in payload.get("explanations", []):
        qid = str(item.get("question_id", "")).strip()
        text = str(item.get("explanation", "")).strip()
        if qid and text:
            out[qid] = text
    if not out:
        raise ValueError("model response contained no explanations")
    return out


def generate_explanations(missed):
    """ONE batched endpoint call for all missed questions; {question_id: text}."""
    from databricks.sdk.core import Config  # lazy: keeps the module importable in tests

    cfg = Config()
    url = f"{cfg.host}/serving-endpoints/{ENDPOINT}/invocations"
    body = build_explanation_body(missed)
    last_error = None
    for attempt in range(RETRIES + 1):
        if attempt:
            time.sleep(BACKOFF_S * attempt)
        try:
            r = requests.post(url, headers=cfg.authenticate(), json=body, timeout=TIMEOUT_S)
            if r.status_code == 429:
                raise RuntimeError("rate limited (429)")
            r.raise_for_status()
            content = extract_text(r.json()["choices"][0]["message"]["content"])
            return parse_explanations(content)
        except (
            RuntimeError,
            ValueError,
            KeyError,
            IndexError,  # 200 with {"choices": []}
            TypeError,  # content: null
            AttributeError,  # content blocks that are not dicts
            requests.RequestException,
        ) as e:
            last_error = e
    raise RuntimeError(f"explanation generation failed after retries: {last_error}")
