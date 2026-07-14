"""Markdown builders for PR result comments. Pure: no I/O, no streamlit.

`review` is the attempt review list built in app.py:
[{"q": <shuffled question dict>, "given": <selected option index>}].
"""


def missed_items(review):
    """Wrong answers as flat dicts for explanation generation and templating."""
    return [
        {
            "question_id": item["q"]["question_id"],
            "question": item["q"]["question"],
            "options": item["q"]["options"],
            "selected_index": item["given"],
            "correct_index": item["q"]["correct_index"],
        }
        for item in review
        if item["given"] != item["q"]["correct_index"]
    ]


def build_pass_comment(sha, taker, review):
    n = len(review)
    lines = [
        f"## 🧠 Quiz passed — 100% on `{sha[:12]}`",
        "",
        f"**{taker}** answered all {n} attempt questions correctly. "
        "The `quiz-gate` status is set to **success**.",
        "",
        "<details>",
        f"<summary>Attempt questions and correct answers ({n})</summary>",
        "",
    ]
    for i, item in enumerate(review, start=1):
        q = item["q"]
        lines.append(f"{i}. **{q['question']}**")
        lines.append(f"   ✅ {q['options'][q['correct_index']]}")
    lines += [
        "",
        "</details>",
        "",
        "_Each attempt samples from a larger pool; retakes see different questions._",
    ]
    return "\n".join(lines)


def build_fail_comment(sha, taker, score_pct, review, explanations):
    """`explanations` maps question_id -> AI text; falls back to the stored
    pool explanation, then to a stock line."""
    missed_ids = {m["question_id"] for m in missed_items(review)}
    n = len(review)
    n_correct = n - len(missed_ids)
    lines = [
        f"## 🧠 Quiz attempt — {score_pct:.0f}% on `{sha[:12]}` (gate needs 100%)",
        "",
        f"**{taker}** answered {n_correct}/{n} correctly.",
        "",
    ]
    for i, item in enumerate(review, start=1):
        q, given = item["q"], item["given"]
        correct = q["options"][q["correct_index"]]
        if q["question_id"] not in missed_ids:
            lines.append(f"{i}. ✅ **{q['question']}** — correct: {correct}")
        else:
            explanation = (
                explanations.get(q["question_id"])
                or q.get("explanation")
                or "No explanation available."
            )
            lines.append(f"{i}. ❌ **{q['question']}**")
            lines.append(f"   - Your answer: {q['options'][given]}")
            lines.append(f"   - Correct answer: {correct}")
            lines.append(f"   - 💡 {explanation}")
    return "\n".join(lines)
