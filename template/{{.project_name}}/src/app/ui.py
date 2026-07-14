"""Presentational layer for the quiz app: pure HTML/JS string builders + CSS injection.

Rules of this module:
- No I/O and no widget state — builders are pure functions returning HTML strings.
  Only inject_css() touches Streamlit, and only to inject styles.css.
- Every interpolated value is passed through html.escape() (quote=True). That
  prevents attribute breakout (an interpolated value cannot escape its quotes or
  inject markup) but does NOT neutralize a `javascript:` URL scheme in an href.
  The only interpolated URL is published_url, which is server-sourced (GitHub's
  html_url), so this is safe in practice — no scheme allowlist is applied.
- progress_script() is the single JS producer; it must be rendered through
  st.components.v1.html(..., height=0) because DOMPurify strips <script> from
  st.html/st.markdown. The script is cosmetic only — server-side validation in
  app.py remains the enforcement.

Class names and ids match src/app/styles.css.
"""
import html
from pathlib import Path

import streamlit as st

CSS_PATH = Path(__file__).parent / "styles.css"
JS_PATH = Path(__file__).parent / "progress.js"


def _esc(value) -> str:
    """html.escape any value (also quotes, so it is attribute-safe)."""
    return html.escape(str(value), quote=True)


def inject_css() -> None:
    """Inject styles.css into the page.

    st.html(path) auto-wraps .css file content in <style> and occupies zero
    layout space (verified on streamlit 1.56).
    """
    st.html(CSS_PATH)


def hero() -> str:
    """Hero card: kicker, gradient-clipped headline, and intro copy.

    Copy is kept generic: question count and retake limits vary per quiz,
    so neither is hardcoded here.
    """
    return (
        '<section class="hero"><div class="hero-content">'
        '<div class="kicker">PR quiz · understanding check</div>'
        '<h1 class="hero-title"><span class="highlight">'
        "Prove human understanding to unlock the merge</span></h1>"
        '<p class="copy">Answer a set of questions about this pull request. '
        "Merging requires 100% understanding of your changes, so nothing less "
        "than a perfect score opens the gate. Questions rotate on every "
        "attempt — a retake is never the same quiz.</p>"
        "</div></section>"
    )


def meta_row(repo, pr_number, sha: str, taker: str, n_per_attempt, pool_size,
             attempt_number, consumed) -> str:
    """PR meta card + taker chip.

    - repo: repository identifier (owner/name) — escaped; shown ahead of the PR
      number, since one app instance serves quizzes for multiple repos and a
      deep-link view has no other repo cue.
    - pr_number: PR number (int).
    - sha: full head commit sha; truncated to 12 chars for display.
    - taker: identity string (e.g. email) — escaped, shown in the chip.
    - n_per_attempt / pool_size: attempt size and pool size for the sub-line.
    - attempt_number: 1-based ordinal of the current attempt this session.
    - consumed: distinct pool questions used so far this session (including the
      current attempt); remaining = pool_size - consumed.
    """
    sha12 = _esc(sha[:12])
    remaining = pool_size - consumed
    return (
        '<section class="meta-row">'
        '<div class="pr-card">'
        '<div class="meta-label">Pull request · commit</div>'
        f'<div class="meta-value">{_esc(repo)} · PR #{_esc(pr_number)} · {sha12}</div>'
        f'<div class="meta-sub">Attempt {_esc(attempt_number)} · '
        f'{_esc(n_per_attempt)} questions per attempt · '
        f'remaining in pool {_esc(remaining)} '
        f'({_esc(consumed)}/{_esc(pool_size)} consumed) · every attempt rotates</div>'
        "</div>"
        '<div class="taker-chip">'
        '<div class="meta-label">Signed in as</div>'
        f'<div class="meta-value">{_esc(taker)}</div>'
        "</div></section>"
    )


def progress_panel(total) -> str:
    """Single pinnable panel: title line, then caption (left) + live counter
    (right) on one row, then the striped progress track — one block so
    progress.js pins them together while the questions scroll.

    The right-hand span carries id="quiz-progress-label" ("0 / {total}
    answered") and the fill carries id="quiz-progress-fill" (width 0%);
    progress_script() rewrites both on change. Stripe geometry is one segment
    per question — calc(100%/{total}). Only the inner progress-block ships
    `hidden` (unhidden by the script): a JS-less page still shows the header
    text, just no broken 0/N bar.
    """
    total = max(int(total), 1)
    stripes = (
        f"repeating-linear-gradient(90deg, transparent 0 calc(100%/{total} - 1px), "
        f"rgba(10,10,86,.16) calc(100%/{total} - 1px) calc(100%/{total})), "
        "linear-gradient(90deg, #DDE1F3, #ECEEF8)"
    )
    return (
        '<section class="progress-panel">'
        '<div class="quiz-title">Quiz progress</div>'
        '<div class="quiz-head">'
        '<div class="quiz-progress-text">Answer all questions before submitting.</div>'
        f'<div class="quiz-progress-text" id="quiz-progress-label">0 / {total} answered</div>'
        "</div>"
        '<div class="progress-block" hidden>'
        f'<div class="progress-track" style="background: {stripes};">'
        '<div class="progress-fill" id="quiz-progress-fill" style="width: 0%;"></div>'
        "</div></div></section>"
    )


def question_topline(question) -> str:
    """The question text, rendered above each answer set.

    This line IS the question: app.py collapses the radio widget's own label
    (which still carries the question for screen readers), saving a row per card.
    The "Q{n}" number is not in this markup — the card's ::before in styles.css
    paints it (CSS counter) as part of the gradient corner-tab + rail shape.
    """
    return (
        '<div class="q-topline">'
        f'<span class="q-text">{_esc(question)}</span></div>'
    )


def verdict_card(*, passed: bool, score_pct: float, sha: str, pr_number, published_url=None) -> str:
    """Result verdict card. Three variants, texts identical to today's app.py:

    - passed + published_url: "💯 Passed! The `quiz-gate` status for `{sha12}`
      is green — no `/quiz-check` needed." plus "Results posted to PR #{n}"
      where the PR reference is an <a> link to published_url.
    - passed, no published_url: "💯 Passed! Result saved for `{sha12}`."
    - not passed: "Scored {score_pct:.0f}% — the gate needs 100%."
      (published_url is ignored for the fail variant.)
    """
    sha12 = _esc(sha[:12])
    if passed:
        variant, headline = "verdict-pass", "💯 Passed!"
        if published_url:
            body = (
                f'<p class="verdict-copy">The <code>quiz-gate</code> status for '
                f"<code>{sha12}</code> is green — no <code>/quiz-check</code> needed.</p>"
                f'<p class="verdict-copy">Results posted to '
                f'<a href="{_esc(published_url)}" target="_blank" rel="noopener">'
                f"PR #{_esc(pr_number)}</a>.</p>"
            )
        else:
            body = f'<p class="verdict-copy">Result saved for <code>{sha12}</code>.</p>'
    else:
        variant = "verdict-fail"
        # Wording mirrors app.py verbatim (parity is required). ".0f" rounding to
        # "100%" is unreachable at current attempt sizes — the worst passing-adjacent
        # fail at N=4 is 75%, so a rounded "100%" fail can never be shown.
        headline = f"Scored {score_pct:.0f}% — the gate needs 100%."
        body = ""
    return (
        f'<section class="verdict-card {variant}">'
        '<div class="kicker">Merge gate</div>'
        f'<div class="verdict-headline"><span class="highlight">{headline}</span></div>'
        f"{body}</section>"
    )


def progress_script(total) -> str:
    """JS (as an HTML <script> string) keeping the progress UI live.

    Render via st.components.v1.html(progress_script(total), height=0): the
    component iframe is same-origin, so window.parent.document reaches the app
    page. On every change event (capturing delegation on the parent document —
    survives Streamlit node churn) it counts answered radiogroups inside the
    form, rewrites #quiz-progress-label, resizes #quiz-progress-fill, and
    unhides the progress block. Bails silently if any target is absent;
    cosmetic only — server-side validation stays the enforcement.

    The JS source lives in progress.js (module-relative, like styles.css) with a
    `__TOTAL__` placeholder, so the script is editable as plain JS.
    """
    total = max(int(total), 1)
    body = JS_PATH.read_text(encoding="utf-8").replace("__TOTAL__", str(total))
    return f"<script>\n{body}\n</script>"
