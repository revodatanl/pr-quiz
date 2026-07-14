"""ui.py presentational builders: escaping, JS-targeted ids, exact user-facing strings."""
from pathlib import Path

import pytest

import ui

SHA = "4a66932507d9e185e792d9e97ee68541db5aabf7"  # sha[:12] == 4a66932507d9
URL = "https://github.com/org/repo/pull/428#issuecomment-99"


class TestHero:
    def test_gradient_headline(self):
        out = ui.hero()
        assert 'class="highlight"' in out
        assert "unlock the merge" in out

    def test_kicker(self):
        assert 'class="kicker"' in ui.hero()

    def test_copy_mentions_perfect_score_and_rotation(self):
        out = ui.hero()
        assert "100%" in out
        assert "rotate" in out


class TestMetaRow:
    REPO = "org/repo"

    def test_repo_pr_number_and_truncated_sha(self):
        out = ui.meta_row(self.REPO, 428, SHA, "dev@example.com", 4, 12, 3, 7)
        assert "org/repo · PR #428 · 4a66932507d9" in out
        assert SHA[:13] not in out  # sha truncated to 12 chars

    def test_attempt_and_consumption_line(self):
        out = ui.meta_row(self.REPO, 428, SHA, "dev@example.com", 4, 12, 3, 7)
        assert "Attempt 3" in out
        assert "4 questions per attempt" in out
        assert "remaining in pool 5" in out
        assert "(7/12 consumed)" in out
        assert "every attempt rotates" in out

    def test_remaining_is_pool_minus_consumed(self):
        out = ui.meta_row(self.REPO, 428, SHA, "dev@example.com", 5, 20, 1, 5)
        assert "remaining in pool 15" in out
        assert "(5/20 consumed)" in out

    def test_taker_shown(self):
        out = ui.meta_row(self.REPO, 428, SHA, "dev@example.com", 4, 12, 1, 4)
        assert "Signed in as" in out
        assert "dev@example.com" in out

    def test_taker_escaped(self):
        out = ui.meta_row(self.REPO, 428, SHA, 'eve<img src=x>&"z@example.com', 4, 12, 1, 4)
        assert "eve&lt;img src=x&gt;&amp;&quot;z@example.com" in out
        assert "<img" not in out

    def test_sha_escaped(self):
        out = ui.meta_row(self.REPO, 428, '<bad>&sha"99xxxx', "dev@example.com", 4, 12, 1, 4)
        assert "&lt;bad&gt;&amp;sha&quot;99" in out
        assert "<bad>" not in out

    def test_repo_escaped(self):
        out = ui.meta_row('o<r>g&"x/repo', 428, SHA, "dev@example.com", 4, 12, 1, 4)
        assert "o&lt;r&gt;g&amp;&quot;x/repo" in out
        assert "<r>" not in out


class TestProgressPanel:
    def test_single_panel_wraps_head_and_bar(self):
        # One block so progress.js pins title, caption, counter, and bar together.
        out = ui.progress_panel(5)
        assert out.startswith('<section class="progress-panel">')
        assert 'class="quiz-head"' in out
        assert 'class="progress-block"' in out

    def test_title_and_caption(self):
        out = ui.progress_panel(5)
        assert "Quiz progress" in out
        assert "Answer all questions before submitting." in out

    def test_caption_and_counter_share_one_row(self):
        # Title sits on its own line; the quiz-head row holds ONLY the caption
        # (left) and the live counter (right), so they render side by side.
        out = ui.progress_panel(5)
        assert '<div class="quiz-head"><div class="quiz-progress-text">Answer all questions' in out

    def test_progress_label_id_and_initial_count(self):
        out = ui.progress_panel(5)
        assert 'id="quiz-progress-label"' in out
        assert "0 / 5 answered" in out

    def test_count_follows_total(self):
        assert "0 / 3 answered" in ui.progress_panel(3)

    def test_fill_id_and_zero_width(self):
        out = ui.progress_panel(5)
        assert 'id="quiz-progress-fill"' in out
        assert "width: 0%" in out

    def test_bar_starts_hidden_but_head_does_not(self):
        # Only the bar depends on JS; the header text must show regardless.
        out = ui.progress_panel(5)
        assert '<div class="progress-block" hidden>' in out
        assert not out.startswith('<section class="progress-panel" hidden>')

    def test_stripe_geometry_follows_total(self):
        assert "calc(100%/4" in ui.progress_panel(4)
        assert "calc(100%/5" in ui.progress_panel(5)
        assert ui.progress_panel(4) != ui.progress_panel(5)

    def test_zero_total_clamps_to_one(self):
        # total=0 must not divide by zero in stripe geometry.
        out = ui.progress_panel(0)
        assert "calc(100%/1" in out
        assert "0 / 1 answered" in out


class TestQuestionTopline:
    def test_question_text(self):
        out = ui.question_topline("Why does the cache reset?")
        assert "Why does the cache reset?" in out
        assert 'class="q-text"' in out

    def test_question_escaped(self):
        out = ui.question_topline('Does x<y & "z" hold?')
        assert "Does x&lt;y &amp; &quot;z&quot; hold?" in out
        assert "<y" not in out

    def test_no_index_pill(self):
        # the Q number is painted by the card's ::before (CSS counter),
        # not by this markup
        out = ui.question_topline("Q?")
        assert "q-index" not in out
        assert "Q1" not in out


class TestVerdictCard:
    def test_pass_published_headline_and_copy(self):
        out = ui.verdict_card(
            passed=True, score_pct=100.0, sha=SHA, pr_number=428, published_url=URL
        )
        assert "💯 Passed!" in out
        assert "The <code>quiz-gate</code> status for <code>4a66932507d9</code> is green" in out
        assert "no <code>/quiz-check</code> needed." in out

    def test_pass_published_link(self):
        url = "https://github.com/org/repo/pull/428?a=1&b=2"
        out = ui.verdict_card(
            passed=True, score_pct=100.0, sha=SHA, pr_number=428, published_url=url
        )
        assert 'href="https://github.com/org/repo/pull/428?a=1&amp;b=2"' in out
        assert "Results posted to" in out
        assert "PR #428" in out

    def test_pass_unpublished(self):
        out = ui.verdict_card(
            passed=True, score_pct=100.0, sha=SHA, pr_number=428, published_url=None
        )
        assert "💯 Passed!" in out
        assert "Result saved for <code>4a66932507d9</code>." in out
        assert "Results posted" not in out
        assert "quiz-gate" not in out

    def test_fail_exact_text_with_rounded_score(self):
        out = ui.verdict_card(
            passed=False, score_pct=66.666, sha=SHA, pr_number=428, published_url=None
        )
        assert "Scored 67% — the gate needs 100%." in out
        assert "Passed" not in out

    def test_fail_variant_class(self):
        out = ui.verdict_card(
            passed=False, score_pct=0.0, sha=SHA, pr_number=428, published_url=None
        )
        assert "verdict-fail" in out


class TestProgressScript:
    def test_embeds_total(self):
        assert "const TOTAL = 4;" in ui.progress_script(4)
        assert "const TOTAL = 5;" in ui.progress_script(5)

    def test_targets_both_ids(self):
        out = ui.progress_script(5)
        assert "quiz-progress-label" in out
        assert "quiz-progress-fill" in out

    def test_reaches_into_parent_document(self):
        assert "window.parent.document" in ui.progress_script(5)

    def test_capturing_change_listener_and_initial_call(self):
        out = ui.progress_script(5)
        assert 'addEventListener("change", update, true)' in out
        assert "update();" in out

    def test_pins_progress_panel_on_scroll(self):
        # position:sticky loses to Streamlit's overflow wrappers, so the script
        # pins the whole panel (head + bar together): a capturing scroll
        # listener measures the anchor and flips it to position:fixed.
        out = ui.progress_script(5)
        assert "progress-pinned" in out
        assert ".progress-panel" in out
        assert 'addEventListener("scroll", sticky, true)' in out

    def test_is_a_script_tag(self):
        out = ui.progress_script(5).strip()
        assert out.startswith("<script>")
        assert out.endswith("</script>")


class TestInjectCss:
    def test_css_path_points_at_shipped_stylesheet(self):
        assert ui.CSS_PATH.name == "styles.css"
        assert ui.CSS_PATH.is_file()


class TestThemeConfig:
    @pytest.fixture()
    def cfg(self):
        tomllib = pytest.importorskip("tomllib")
        path = (
            Path(__file__).resolve().parents[1]
            / "template" / "{{.project_name}}" / "src" / "app" / ".streamlit" / "config.toml"
        )
        with open(path, "rb") as f:
            return tomllib.load(f)

    def test_minimal_toolbar(self, cfg):
        assert cfg["client"]["toolbarMode"] == "minimal"

    def test_light_theme_palette(self, cfg):
        theme = cfg["theme"]
        assert theme["base"] == "light"
        assert theme["primaryColor"] == "#D82AC8"
        assert theme["backgroundColor"] == "#F7F8FD"
        assert theme["secondaryBackgroundColor"] == "#FFFFFF"
        assert theme["textColor"] == "#0A0A56"
        assert theme["linkColor"] == "#3D21A3"
        assert theme["borderColor"] == "#DFE2F2"
        assert theme["baseRadius"] == "17px"
        assert theme["buttonRadius"] == "full"

    def test_inter_font_face(self, cfg):
        face = cfg["theme"]["fontFaces"][0]
        assert face["family"] == "Inter"
        assert face["weight"] == "100 900"
        assert face["url"].startswith("https://fonts.gstatic.com/")
        assert "Inter" in cfg["theme"]["font"]
        assert "Inter" in cfg["theme"]["headingFont"]

    def test_dark_mirrors_light(self, cfg):
        theme, dark = cfg["theme"], cfg["theme"]["dark"]
        for key in (
            "primaryColor",
            "backgroundColor",
            "secondaryBackgroundColor",
            "textColor",
            "borderColor",
        ):
            assert dark[key] == theme[key], key
