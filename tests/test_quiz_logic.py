"""Job-side pure logic: question count scaling, batching, model output parsing."""
import json

import pytest

import quiz_logic
from quiz_logic import (
    DELETED_BLOCK_HEADER,
    MAX_QUESTIONS,
    MIN_DIFFICULTY_FACTOR,
    OPTIONS_PER_QUESTION,
    allocate_questions,
    apply_ambiguity_results,
    batch_sizes,
    chunk_files,
    compute_question_count,
    dedupe_questions,
    deleted_file_text,
    deletion_block,
    deletion_references,
    deletions_size,
    extract_text,
    is_generated_path,
    is_valid_repo,
    normalize_text,
    parse_gitattributes_generated,
    parse_glob_list,
    prepare_files,
    render_patch,
    parse_ambiguity_verdicts,
    parse_difficulty,
    parse_distractors,
    parse_questions,
    parse_soft_dedup,
    SPACING_MAX,
    SPACING_MIN,
    narrow_spacing,
    rebuild_options,
    remove_soft_duplicates,
    reserve_slot,
    retry_wait,
    skip_difficulty_judge,
    widen_spacing,
)


def _question(text, options=None, correct_index=0, explanation=""):
    return {
        "question": text,
        "options": options or ["a", "b", "c", "d"],
        "correct_index": correct_index,
        "explanation": explanation,
    }


class TestExtractText:
    def test_plain_string_passes_through(self):
        assert extract_text('{"questions": []}') == '{"questions": []}'

    def test_content_block_list_joins_text_blocks_only(self):
        content = [
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "thinking..."}]},
            {"type": "text", "text": '{"questions":'},
            {"type": "text", "text": " []}"},
        ]
        assert extract_text(content) == '{"questions": []}'

    def test_empty_list_gives_empty_string(self):
        assert extract_text([]) == ""


class TestComputeQuestionCount:
    def test_one_line_change_gives_one_question(self):
        assert compute_question_count(1) == 1

    def test_zero_lines_still_gives_one_question(self):
        assert compute_question_count(0) == 1

    def test_forty_lines_still_one_question(self):
        assert compute_question_count(40) == 1

    def test_forty_one_lines_gives_two_questions(self):
        assert compute_question_count(41) == 2

    def test_very_big_pr_capped_at_twenty(self):
        assert compute_question_count(100_000) == 20

    def test_exactly_at_cap_boundary(self):
        assert compute_question_count(800) == 20
        assert compute_question_count(799) == 20  # ceil(799/40) = 20
        assert compute_question_count(760) == 19

    def test_default_factor_equals_explicit_one(self):
        for lines in (1, 41, 799, 100_000):
            assert compute_question_count(lines) == compute_question_count(lines, 1.0)

    def test_low_factor_reduces_count(self):
        assert compute_question_count(400, 0.2) == 2

    def test_high_factor_increases_count(self):
        assert compute_question_count(80, 5.0) == 10

    def test_high_factor_still_capped(self):
        assert compute_question_count(400, 5.0) == 20

    def test_ceil_applied_after_multiplying(self):
        # ceil(60/40*2.0)=ceil(3.0)=3; ceil-before-multiply would give 4
        assert compute_question_count(60, 2.0) == 3

    def test_min_clamp_survives_tiny_factor(self):
        assert compute_question_count(10, 0.2) == 1


class TestBatchSizes:
    def test_small_total_single_batch(self):
        assert batch_sizes(5) == [5]

    def test_exact_multiple_of_batch(self):
        assert batch_sizes(40) == [20, 20]

    def test_remainder_becomes_last_batch(self):
        assert batch_sizes(50) == [20, 20, 10]

    def test_max_pool_hundred_questions(self):
        assert batch_sizes(100) == [20, 20, 20, 20, 20]


class TestDedupeQuestions:
    def test_distinct_questions_all_kept_in_order(self):
        qs = [_question("What changed?"), _question("Why change?")]
        assert dedupe_questions(qs) == qs

    def test_exact_duplicate_dropped_keeps_first(self):
        first = _question("What changed?")
        dupe = _question("What changed?", correct_index=2)
        assert dedupe_questions([first, dupe]) == [first]

    def test_case_and_whitespace_variants_are_duplicates(self):
        first = _question("What  changed in   pricing.py?")
        variant = _question("what changed in pricing.py?")
        assert dedupe_questions([first, variant]) == [first]

    def test_empty_list_stays_empty(self):
        assert dedupe_questions([]) == []


class TestParseQuestions:
    def _valid_question(self, **overrides):
        q = {
            "question": "What does the change do?",
            "options": ["a", "b", "c", "d"],
            "correct_index": 2,
            "explanation": "because",
        }
        q.update(overrides)
        return q

    def test_parses_valid_payload(self):
        raw = json.dumps({"questions": [self._valid_question()]})
        parsed = parse_questions(raw)
        assert len(parsed) == 1
        assert parsed[0]["question"] == "What does the change do?"
        assert parsed[0]["correct_index"] == 2

    def test_drops_question_with_out_of_range_correct_index(self):
        raw = json.dumps(
            {"questions": [self._valid_question(), self._valid_question(correct_index=4)]}
        )
        assert len(parse_questions(raw)) == 1

    def test_drops_question_with_wrong_option_count(self):
        raw = json.dumps(
            {"questions": [self._valid_question(options=["a", "b"]), self._valid_question()]}
        )
        assert len(parse_questions(raw)) == 1

    def test_drops_question_with_empty_text(self):
        raw = json.dumps(
            {"questions": [self._valid_question(question="  "), self._valid_question()]}
        )
        assert len(parse_questions(raw)) == 1

    def test_missing_explanation_is_allowed(self):
        q = self._valid_question()
        del q["explanation"]
        parsed = parse_questions(json.dumps({"questions": [q]}))
        assert parsed[0]["explanation"] == ""

    def test_invalid_json_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_questions("not json {")

    def test_no_valid_questions_raises_value_error(self):
        raw = json.dumps({"questions": [self._valid_question(correct_index=99)]})
        with pytest.raises(ValueError):
            parse_questions(raw)


class TestSkipDifficultyJudge:
    def test_below_threshold_does_not_skip(self):
        assert skip_difficulty_judge(3999) is False

    def test_at_threshold_skips(self):
        assert skip_difficulty_judge(4000) is True

    def test_skip_threshold_still_yields_max_questions(self):
        assert compute_question_count(4000, MIN_DIFFICULTY_FACTOR) == MAX_QUESTIONS


class TestParseDifficulty:
    def test_parses_valid_factor(self):
        assert parse_difficulty('{"difficulty_factor": 1.5}') == 1.5

    def test_clamps_above_maximum(self):
        assert parse_difficulty('{"difficulty_factor": 7.3}') == 5.0

    def test_clamps_below_minimum(self):
        assert parse_difficulty('{"difficulty_factor": 0.05}') == 0.2

    def test_integer_factor_allowed(self):
        assert parse_difficulty('{"difficulty_factor": 2}') == 2.0

    def test_reasoning_key_ignored(self):
        assert parse_difficulty('{"difficulty_factor": 1.0, "reasoning": "routine"}') == 1.0

    def test_missing_key_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_difficulty('{"reasoning": "no factor"}')

    def test_bool_factor_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_difficulty('{"difficulty_factor": true}')

    def test_string_factor_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_difficulty('{"difficulty_factor": "2.0"}')

    def test_nan_factor_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_difficulty('{"difficulty_factor": NaN}')

    def test_infinity_factor_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_difficulty('{"difficulty_factor": Infinity}')

    def test_invalid_json_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_difficulty("not json {")


class TestConfiguredGlobs:
    """QUIZ_GENERATED_GLOBS and .gitattributes share one normalizer."""

    def test_splits_and_strips_a_glob_list(self):
        assert parse_glob_list("*.lock, dist/*") == ("*.lock", "dist/*")
        assert parse_glob_list("") == ()

    def test_both_sources_normalize_identically(self):
        for pattern in ("/build/out.js", "gen/", "proto/**", "docs/**/x.html"):
            assert parse_glob_list(pattern) == parse_gitattributes_generated(
                f"{pattern} linguist-generated"
            ), pattern

    def test_rooted_and_double_star_patterns_match_at_every_depth(self):
        assert is_generated_path("sub/build/out.js", parse_glob_list("/build/out.js"))
        for path in ("docs/gen.html", "docs/a/gen.html", "docs/a/b/gen.html"):
            assert is_generated_path(path, parse_glob_list("docs/**/gen.html")), path

    @pytest.mark.parametrize("pattern", ["*", "**", "***", "/**/", "**/*", "*/*", "**/**"])
    def test_match_everything_patterns_are_dropped(self, pattern):
        # fnmatch's "*" crosses "/", so any of these would mark every file in
        # every PR generated and waive the gate repo-wide. The normalization
        # itself produces some of them, so the guard has to run after it.
        assert parse_glob_list(pattern) == (), pattern
        assert parse_gitattributes_generated(f"{pattern} linguist-generated") == ()

    def test_one_bad_entry_does_not_poison_the_rest(self):
        assert parse_glob_list("*, dist/*") == ("dist/*",)

    @pytest.mark.parametrize("pattern", ["dist/", "/dist/", "dist/**"])
    def test_a_directory_pattern_covers_its_contents(self, pattern):
        # "dist/" alone normalized to the glob "dist", which only ever matched a
        # FILE called dist - i.e. nothing, silently. Git reads the trailing slash
        # as the directory's contents.
        for path in ("dist/app.js", "dist/a/b.js", "web/dist/app.js"):
            assert is_generated_path(path, parse_glob_list(pattern)), (pattern, path)
        assert is_generated_path(path, parse_gitattributes_generated(
            f"{pattern} linguist-generated"
        ))

    def test_a_directory_pattern_does_not_match_a_file_of_that_name(self):
        # git's own rule: "dist/" is the directory, never a file called dist.
        assert not is_generated_path("dist", parse_glob_list("dist/"))

    def test_a_bare_name_stays_a_filename_match(self):
        # Also git's rule: a slashless pattern matches a FILE of that name at any
        # depth. Inventing the directory sense here would make the same line mean
        # different things to git and to us - write "dist/" for the directory.
        assert is_generated_path("a/b/dist", parse_glob_list("dist"))
        assert not is_generated_path("dist/app.js", parse_glob_list("dist"))

    def test_gitattributes_honours_set_and_ignores_unset(self):
        text = (
            "# comment\n[attr]binary -diff\n"
            "*.lock linguist-generated=true\n"
            "schema.json linguist-generated\n"
            "keep.lock -linguist-generated\n"
            "x.json linguist-generated=false\n"
            "* text=auto\n"
        )
        assert parse_gitattributes_generated(text) == ("*.lock", "schema.json")


class TestIsGeneratedPath:
    @pytest.mark.parametrize("path", [
        "uv.lock", "services/api/uv.lock", "web/package-lock.json", "api/go.sum",
        "static/app.min.js", "app.js.map", "tests/__snapshots__/ui.snap",
        "api/schema_pb2.py", "rpc/service.pb.go", "src/models.generated.ts",
        "vendor/lib/x.go", "third_party/vendor/y.go", "web/node_modules/pkg/index.js",
    ])
    def test_generated_paths_match(self, path):
        assert is_generated_path(path)

    @pytest.mark.parametrize("path", [
        "src/job/quiz_logic.py", "README.md", "pyproject.toml", "src/locking.py",
        "docs/lockfiles.md", "notebooks/etl.ipynb",
    ])
    def test_authored_paths_do_not_match(self, path):
        assert not is_generated_path(path)

    def test_matching_is_case_insensitive_on_every_platform(self):
        # Regression guard for fnmatch (host-normcased) vs fnmatchcase: with
        # fnmatch these pass on Windows and fail on the Linux job runtime.
        assert is_generated_path("Sub/Dir/UV.Lock")
        assert is_generated_path("Generated/Out.txt", ("generated/*",))


class TestEditsGitattributes:
    """The one flag that stops an empty corpus from being waived."""

    def _raw(self, name, changed=10, patch="@@\n+x", status="modified"):
        return {"filename": name, "status": status, "changed_lines": changed,
                "patch": patch}

    def test_an_ordinary_pr_is_not_flagged(self):
        assert prepare_files([self._raw("app.py")]).edits_gitattributes is False

    @pytest.mark.parametrize("name", [".gitattributes", "web/.gitattributes"])
    def test_editing_gitattributes_is_flagged_at_any_depth(self, name):
        # Otherwise a PR could declare its own files generated and waive itself.
        assert prepare_files([self._raw(name)]).edits_gitattributes is True

    def test_a_generated_gitattributes_is_still_flagged(self):
        # Flagged from the raw records, before the generated filter runs: a glob
        # covering .gitattributes must not be able to hide the edit to it.
        diff = prepare_files([self._raw(".gitattributes")], (".gitattributes",))
        assert (diff.files, diff.edits_gitattributes) == ([], True)


class TestDeletionReferences:
    def test_stem_match_finds_the_dropped_import(self):
        others = [("src/job/main.py", "-from retry import retry_wait")]
        assert deletion_references("src/job/retry.py", others) == ("src/job/main.py",)

    def test_short_stem_does_not_match(self):
        # "api" would otherwise cite half the PR
        assert deletion_references("src/api.py", [("c.py", "call the api")]) == ()

    def test_unrelated_files_are_not_cited(self):
        assert deletion_references("src/retry.py", [("o.py", "+nothing")]) == ()


class TestDeletedFileText:
    def _patch(self, n):
        return "\n".join(f"-line {i}" for i in range(n))

    def test_excerpt_is_capped(self):
        text = deleted_file_text("retry.py", 100, self._patch(100), (), preview_lines=5)
        assert "-line 4" in text
        assert "-line 5" not in text

    def test_short_patch_is_shown_in_full(self):
        assert "-line 2" in deleted_file_text("retry.py", 3, self._patch(3), ())


class TestDeletionBlock:
    def _deleted(self, name, size=50):
        return {"filename": name, "status": "removed", "text": "d" * size,
                "changed_lines": 0, "references": ()}

    def test_empty_input_gives_empty_string(self):
        assert deletion_block([], 1000) == ""

    def test_overflowing_entries_lose_the_excerpt_but_keep_the_name(self):
        deleted = [self._deleted("a.py", 400), self._deleted("b.py", 400)]
        # room for the header, one excerpt, and the line naming the other
        budget = len(DELETED_BLOCK_HEADER) + 500
        block = deletion_block(deleted, budget)
        assert block.count("d" * 400) == 1
        assert "b.py" in block
        assert len(block) <= budget

    def test_the_naming_line_is_budgeted_too(self):
        # It used to be appended after the fit loop, so a long list of names
        # pushed the block past the budget chunk_files had handed out (measured
        # overflow: +2,358 chars on a 10,000 budget).
        deleted = [self._deleted(f"{'d' * 70}{i}.py", 900) for i in range(60)]
        assert len(deletion_block(deleted, 10_000)) <= 10_000

    def test_everything_fitting_costs_no_reserve(self):
        deleted = [self._deleted("a.py", 400), self._deleted("b.py", 400)]
        block = deletion_block(deleted, deletions_size(deleted))
        assert block.count("d" * 400) == 2
        assert "also deleted" not in block


class TestRenderPatch:
    """Rebuilding the diff GitHub declines to return for an oversized text file."""

    def test_shape_matches_a_github_patch(self):
        patch = render_patch("a\nb\nc\n", "a\nB\nc\n")
        assert patch.startswith("@@")
        assert "-b" in patch and "+B" in patch
        # GitHub's patch text carries no ---/+++ file headers
        assert not patch.startswith("---")
        assert "+++ " not in patch

    def test_identical_versions_give_no_patch(self):
        assert render_patch("same\n", "same\n") == ""

    def test_added_file_renders_as_all_additions(self):
        patch = render_patch("", "new line\n")
        assert "+new line" in patch

    def test_a_removed_line_of_dashes_survives_header_stripping(self):
        # Stripping the file headers by prefix would eat this: "---" renders as
        # "----", and a removed "+++x" as "++++x".
        patch = render_patch("---\n+++x\nkeep\n", "keep\n")
        assert "----" in patch
        assert "-+++x" in patch

    def test_long_diff_is_capped_and_says_so(self, monkeypatch):
        monkeypatch.setattr(quiz_logic, "RECONSTRUCTED_PATCH_LINES", 10)
        patch = render_patch("\n".join(str(i) for i in range(500)), "")
        assert len(patch.splitlines()) == 11  # 10 diff lines + the truncation marker
        assert "truncated at 10 lines" in patch


class TestPrepareFiles:
    def _raw(self, name, status="modified", changed=10, patch="@@\n+x"):
        return {"filename": name, "status": status, "changed_lines": changed,
                "patch": patch}

    def test_corpus_shape_and_weight(self):
        diff = prepare_files([self._raw("b.py"), self._raw("a.py")])
        assert [f["filename"] for f in diff.files] == ["b.py", "a.py"]  # provider order
        assert set(diff.files[0]) == {"filename", "status", "text", "changed_lines",
                                      "references"}
        assert diff.files[0]["text"].endswith("@@\n+x")
        assert diff.changed_lines == 20

    def test_generated_files_are_dropped_uncounted_and_named(self):
        raw = [self._raw("uv.lock", changed=4000), self._raw("app.py", changed=6)]
        diff = prepare_files(raw)
        assert [f["filename"] for f in diff.files] == ["app.py"]
        assert diff.changed_lines == 6
        assert diff.skipped_generated == ("uv.lock",)

    def test_configured_globs_reach_the_predicate(self):
        raw = [self._raw("dist/bundle.js", changed=900), self._raw("app.py", changed=4)]
        diff = prepare_files(raw, ("dist/*",))
        assert [f["filename"] for f in diff.files] == ["app.py"]
        assert diff.changed_lines == 4

    def test_deleted_file_weighs_nothing_and_cites_its_caller(self):
        raw = [
            self._raw("src/retry.py", status="removed", changed=900,
                      patch="\n".join(f"-line {i}" for i in range(900))),
            self._raw("src/main.py", patch="-from retry import retry_wait"),
        ]
        diff = prepare_files(raw)
        assert diff.changed_lines == 10  # only the surviving file's lines
        assert diff.files[0]["changed_lines"] == 0
        assert diff.files[0]["references"] == ("src/main.py",)
        assert "-line 899" not in diff.files[0]["text"]  # excerpt capped

    def test_deleted_files_do_not_cite_each_other(self):
        raw = [self._raw(f"{d}/retry.py", status="removed", patch="-import retry")
               for d in ("a", "b")]
        assert all(f["references"] == () for f in prepare_files(raw).files)

    def test_binary_is_neither_shown_nor_counted(self):
        diff = prepare_files([self._raw("logo.png", changed=0, patch=None)])
        assert (diff.files, diff.changed_lines, diff.unreviewable) == ([], 0, ())

    def test_an_undiffable_change_is_reported_even_though_it_cannot_be_quizzed(self):
        raw = [self._raw("src/huge.py", changed=9000, patch=None), self._raw("app.py")]
        diff = prepare_files(raw)
        assert [f["filename"] for f in diff.files] == ["app.py"]
        assert diff.changed_lines == 10
        assert diff.unreviewable == ("src/huge.py",)

    def test_a_patchless_generated_file_is_not_flagged_undiffable(self):
        # A big lock file is exactly where GitHub drops the patch, and exactly
        # the case a waive exists for: flagging it would fail every such PR.
        assert prepare_files([self._raw("uv.lock", changed=3502, patch=None)]) \
            .unreviewable == ()
        assert prepare_files([self._raw("dist/app.js", changed=900, patch=None)],
                             ("dist/*",)).unreviewable == ()

    def test_a_patchless_deletion_is_not_flagged_undiffable(self):
        # GitHub returns no patch for a large removal; the file is still kept as
        # deletion context, so there is nothing unreviewable about it.
        diff = prepare_files([self._raw("gone.py", status="removed", changed=80,
                                        patch=None)])
        assert diff.unreviewable == ()
        assert [f["filename"] for f in diff.files] == ["gone.py"]

    def test_an_all_generated_pr_yields_an_empty_corpus(self):
        diff = prepare_files([self._raw("uv.lock", changed=4000)])
        assert (diff.files, diff.changed_lines) == ([], 0)


class TestChunkFiles:
    def _file(self, name, size, changed=10):
        return {"filename": name, "status": "modified", "text": "x" * size,
                "changed_lines": changed, "references": ()}

    def _deleted(self, name, size=40, references=()):
        return {"filename": name, "status": "removed", "text": "d" * size,
                "changed_lines": 0, "references": references}

    def test_all_files_fit_single_chunk(self):
        files = [self._file("a.py", 100, changed=3), self._file("b.py", 200, changed=7)]
        chunks = chunk_files(files, budget=1000)
        assert len(chunks) == 1
        assert chunks[0]["text"] == "x" * 100 + "\n\n" + "x" * 200
        assert chunks[0]["changed_lines"] == 10
        assert chunks[0]["filenames"] == ["a.py", "b.py"]

    def test_overflow_splits_in_input_order(self):
        files = [self._file("a.py", 60), self._file("b.py", 60), self._file("c.py", 60)]
        chunks = chunk_files(files, budget=130)
        assert [c["filenames"] for c in chunks] == [["a.py", "b.py"], ["c.py"]]
        assert all(len(c["text"]) <= 130 for c in chunks)

    def test_oversize_file_truncated_to_budget(self):
        chunks = chunk_files([self._file("big.py", 500)], budget=100)
        assert len(chunks) == 1
        assert chunks[0]["text"] == "x" * 100

    def test_max_chunks_cap_drops_tail_files(self):
        files = [self._file(f"f{i}.py", 60) for i in range(4)]
        chunks = chunk_files(files, budget=60, max_chunks=2)
        assert len(chunks) == 2
        assert [c["filenames"] for c in chunks] == [["f0.py"], ["f1.py"]]

    def test_empty_input_returns_empty_list(self):
        assert chunk_files([]) == []

    def test_file_exactly_at_budget_fits(self):
        chunks = chunk_files([self._file("a.py", 100)], budget=100)
        assert len(chunks) == 1
        assert chunks[0]["text"] == "x" * 100

    def test_a_deletion_lands_only_in_the_chunk_that_referenced_it(self):
        # The whole point: the deletion travels with the code that shows its
        # impact, and is not bought twice by asking about it in every chunk.
        files = [
            self._deleted("gone.py", references=("b.py",)),
            self._file("a.py", 400),
            self._file("b.py", 400),
        ]
        chunks = chunk_files(files, budget=700)
        assert len(chunks) == 2
        assert [c["filenames"] for c in chunks] == [["a.py"], ["gone.py", "b.py"]]
        assert sum(c["text"].count("d" * 40) for c in chunks) == 1

    def test_an_unreferenced_deletion_falls_back_to_the_closest_path(self):
        files = [
            self._deleted("src/api/gone.py"),
            self._file("docs/x.md", 400),
            self._file("src/api/keep.py", 400),
        ]
        chunks = chunk_files(files, budget=700)
        assert chunks[1]["filenames"] == ["src/api/gone.py", "src/api/keep.py"]

    def test_deletions_do_not_change_chunk_weights(self):
        files = [self._deleted("gone.py"), self._file("a.py", 100, changed=7)]
        assert chunk_files(files, budget=1000)[0]["changed_lines"] == 7

    def test_delete_only_corpus_gives_one_chunk_of_just_the_block(self):
        chunks = chunk_files([self._deleted("a.py"), self._deleted("b.py")], budget=1000)
        assert len(chunks) == 1
        assert chunks[0]["changed_lines"] == 0
        assert chunks[0]["filenames"] == ["a.py", "b.py"]

    def test_survivors_are_packed_against_the_reduced_budget(self):
        survivors = [self._file("a.py", 300), self._file("b.py", 300)]
        # 300 + 2 + 300 = 602 fits a 660 budget on its own...
        assert len(chunk_files(survivors, budget=660)) == 1
        # ...but not once the deletion reserve is taken out of the same budget
        assert len(chunk_files([self._deleted("gone.py", 100)] + survivors, budget=660)) == 2

    def test_a_small_deletion_does_not_cost_a_share_it_never_uses(self):
        # The reserve used to be a flat budget // DELETED_BLOCK_SHARE the moment
        # any deletion existed, so one tiny deleted file cost a big PR a third of
        # the diff the model ever sees (measured: 145,000 -> 100,079 chars).
        survivors = [self._file(f"f{i}.py", 29_000) for i in range(8)]
        alone = sum(len(c["text"]) for c in chunk_files(survivors))
        with_deletion = sum(
            len(c["text"]) for c in chunk_files([self._deleted("gone.py", 50)] + survivors)
        )
        assert with_deletion >= alone


class TestAllocateQuestions:
    def test_single_weight_takes_all(self):
        assert allocate_questions(10, [7]) == [10]

    def test_proportional_split_with_remainder(self):
        assert allocate_questions(4, [30, 10]) == [3, 1]

    def test_equal_weights_largest_remainder_favors_lower_index(self):
        assert allocate_questions(10, [1, 1, 1]) == [4, 3, 3]

    def test_min_one_guaranteed_for_each_chunk(self):
        assert allocate_questions(5, [1000, 1, 1]) == [3, 1, 1]

    def test_total_below_chunk_count_gives_heaviest_one_each(self):
        assert allocate_questions(2, [5, 10, 1]) == [1, 1, 0]

    def test_tie_break_favors_lower_index(self):
        assert allocate_questions(4, [5, 5, 5]) == [2, 1, 1]

    def test_all_zero_weights_sum_preserved(self):
        assert sum(allocate_questions(7, [0, 0, 0])) == 7

    def test_allocation_always_sums_to_total(self):
        cases = [(1, [3]), (9, [2, 5]), (17, [40, 1, 9]), (100, [550] * 5), (3, [0, 4, 0, 4])]
        for total, weights in cases:
            assert sum(allocate_questions(total, weights)) == total


class TestNormalizeText:
    def test_casefolds_and_collapses_whitespace(self):
        assert normalize_text("The  Quick\tFox ") == "the quick fox"

    def test_already_normal_text_unchanged(self):
        assert normalize_text("plain text") == "plain text"


class TestParseSoftDedup:
    def test_parses_groups_and_topics(self):
        raw = json.dumps(
            {"duplicate_groups": [[0, 2], [1, 3]], "covered_topics": ["retry backoff"]}
        )
        assert parse_soft_dedup(raw, 4) == {"groups": [[0, 2], [1, 3]], "topics": ["retry backoff"]}

    def test_empty_groups_is_valid_no_duplicates(self):
        raw = json.dumps({"duplicate_groups": [], "covered_topics": []})
        assert parse_soft_dedup(raw, 4) == {"groups": [], "topics": []}

    def test_invalid_json_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_soft_dedup("not json {", 4)

    def test_missing_duplicate_groups_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_soft_dedup(json.dumps({"covered_topics": []}), 4)

    def test_non_list_duplicate_groups_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_soft_dedup(json.dumps({"duplicate_groups": "none"}), 4)

    def test_out_of_range_and_negative_indices_dropped(self):
        raw = json.dumps({"duplicate_groups": [[0, 9], [-1, 1, 2]]})
        assert parse_soft_dedup(raw, 4)["groups"] == [[1, 2]]

    def test_bool_indices_dropped(self):
        # JSON true/false are bool, an int subclass: must not become indices 1/0
        raw = json.dumps({"duplicate_groups": [[True, False], [2, 3]]})
        assert parse_soft_dedup(raw, 4)["groups"] == [[2, 3]]

    def test_non_int_indices_dropped(self):
        raw = json.dumps({"duplicate_groups": [["0", 1.5, 2, 3]]})
        assert parse_soft_dedup(raw, 4)["groups"] == [[2, 3]]

    def test_group_shrinking_below_two_discarded(self):
        raw = json.dumps({"duplicate_groups": [[0, 99], [1]]})
        assert parse_soft_dedup(raw, 4)["groups"] == []

    def test_non_list_group_discarded(self):
        raw = json.dumps({"duplicate_groups": ["0,1", [2, 3]]})
        assert parse_soft_dedup(raw, 4)["groups"] == [[2, 3]]

    def test_repeated_index_within_group_deduped(self):
        raw = json.dumps({"duplicate_groups": [[2, 0, 2, 0]]})
        assert parse_soft_dedup(raw, 4)["groups"] == [[2, 0]]

    def test_missing_topics_tolerated_as_empty(self):
        raw = json.dumps({"duplicate_groups": []})
        assert parse_soft_dedup(raw, 4)["topics"] == []

    def test_non_list_topics_tolerated_as_empty(self):
        raw = json.dumps({"duplicate_groups": [], "covered_topics": "retry"})
        assert parse_soft_dedup(raw, 4)["topics"] == []

    def test_topics_coerced_stripped_and_empties_dropped(self):
        raw = json.dumps({"duplicate_groups": [], "covered_topics": [" retry backoff ", "", 42]})
        assert parse_soft_dedup(raw, 4)["topics"] == ["retry backoff", "42"]


class TestRemoveSoftDuplicates:
    def test_keeps_lowest_index_of_group(self):
        qs = [_question("a"), _question("b"), _question("c")]
        assert remove_soft_duplicates(qs, [[2, 0]]) == [qs[0], qs[1]]

    def test_multiple_disjoint_groups(self):
        qs = [_question(t) for t in "abcdef"]
        result = remove_soft_duplicates(qs, [[0, 1], [3, 5]])
        assert result == [qs[0], qs[2], qs[3], qs[4]]

    def test_overlapping_groups_resolve_deterministically(self):
        qs = [_question("a"), _question("b"), _question("c")]
        forward = remove_soft_duplicates(qs, [[0, 1], [1, 2]])
        backward = remove_soft_duplicates(qs, [[1, 2], [0, 1]])
        assert forward == backward == [qs[0]]

    def test_empty_groups_returns_same_questions(self):
        qs = [_question("a"), _question("b")]
        assert remove_soft_duplicates(qs, []) == qs

    def test_all_indices_one_group_keeps_first(self):
        qs = [_question("a"), _question("b"), _question("c")]
        assert remove_soft_duplicates(qs, [[0, 1, 2]]) == [qs[0]]

    def test_order_preserved(self):
        qs = [_question(t) for t in "abcde"]
        assert remove_soft_duplicates(qs, [[1, 3]]) == [qs[0], qs[1], qs[2], qs[4]]


class TestParseAmbiguityVerdicts:
    def test_parses_valid_verdicts(self):
        raw = json.dumps({"verdicts": [
            {"index": 0, "ambiguous": True},
            {"index": 2, "ambiguous": False, "reason": "clear"},
        ]})
        assert parse_ambiguity_verdicts(raw, {0, 1, 2}) == {0: True, 2: False}

    def test_index_outside_valid_set_dropped(self):
        raw = json.dumps({"verdicts": [
            {"index": 7, "ambiguous": True},
            {"index": 1, "ambiguous": True},
        ]})
        assert parse_ambiguity_verdicts(raw, {0, 1}) == {1: True}

    def test_string_ambiguous_dropped(self):
        raw = json.dumps({"verdicts": [
            {"index": 0, "ambiguous": "true"},
            {"index": 1, "ambiguous": False},
        ]})
        assert parse_ambiguity_verdicts(raw, {0, 1}) == {1: False}

    def test_bool_index_dropped(self):
        # JSON true is bool, an int subclass: must not be verdict for index 1
        raw = json.dumps({"verdicts": [
            {"index": True, "ambiguous": True},
            {"index": 0, "ambiguous": False},
        ]})
        assert parse_ambiguity_verdicts(raw, {0, 1}) == {0: False}

    def test_duplicate_index_keeps_first_verdict(self):
        raw = json.dumps({"verdicts": [
            {"index": 0, "ambiguous": True},
            {"index": 0, "ambiguous": False},
        ]})
        assert parse_ambiguity_verdicts(raw, {0}) == {0: True}

    def test_all_false_is_valid(self):
        raw = json.dumps({"verdicts": [{"index": i, "ambiguous": False} for i in range(3)]})
        assert parse_ambiguity_verdicts(raw, {0, 1, 2}) == {0: False, 1: False, 2: False}

    def test_zero_valid_verdicts_raises_value_error(self):
        raw = json.dumps({"verdicts": [{"index": 9, "ambiguous": True}]})
        with pytest.raises(ValueError):
            parse_ambiguity_verdicts(raw, {0, 1})

    def test_empty_verdicts_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_ambiguity_verdicts(json.dumps({"verdicts": []}), {0})

    def test_missing_verdicts_key_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_ambiguity_verdicts(json.dumps({}), {0})

    def test_invalid_json_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_ambiguity_verdicts("not json {", {0})


class TestParseDistractors:
    def test_parses_three_valid_distractors(self):
        raw = json.dumps({"distractors": [" one ", "two", "three"]})
        assert parse_distractors(raw, "the answer") == ["one", "two", "three"]

    def test_two_distractors_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_distractors(json.dumps({"distractors": ["one", "two"]}), "the answer")

    def test_four_distractors_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_distractors(json.dumps({"distractors": ["1", "2", "3", "4"]}), "the answer")

    def test_distractor_equal_to_correct_raises_value_error(self):
        raw = json.dumps({"distractors": ["one", "The  Answer", "three"]})
        with pytest.raises(ValueError):
            parse_distractors(raw, "the answer")

    def test_duplicate_distractors_raise_value_error(self):
        with pytest.raises(ValueError):
            parse_distractors(json.dumps({"distractors": ["one", "ONE", "three"]}), "the answer")

    def test_empty_distractor_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_distractors(json.dumps({"distractors": ["one", "  ", "three"]}), "the answer")

    def test_missing_key_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_distractors(json.dumps({}), "the answer")

    def test_invalid_json_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_distractors("not json {", "the answer")


class TestRebuildOptions:
    def test_correct_answer_lands_at_original_index_zero(self):
        q = _question("q", options=["right", "w1", "w2", "w3"], correct_index=0)
        rebuilt = rebuild_options(q, ["d1", "d2", "d3"])
        assert rebuilt["options"] == ["right", "d1", "d2", "d3"]
        assert rebuilt["correct_index"] == 0

    def test_correct_answer_lands_at_middle_index(self):
        q = _question("q", options=["w1", "w2", "right", "w3"], correct_index=2)
        rebuilt = rebuild_options(q, ["d1", "d2", "d3"])
        assert rebuilt["options"] == ["d1", "d2", "right", "d3"]
        assert rebuilt["correct_index"] == 2

    def test_correct_answer_lands_at_last_index(self):
        q = _question("q", options=["w1", "w2", "w3", "right"], correct_index=3)
        rebuilt = rebuild_options(q, ["d1", "d2", "d3"])
        assert rebuilt["options"] == ["d1", "d2", "d3", "right"]
        assert rebuilt["correct_index"] == 3

    def test_result_has_full_option_count(self):
        q = _question("q", correct_index=1)
        assert len(rebuild_options(q, ["d1", "d2", "d3"])["options"]) == OPTIONS_PER_QUESTION

    def test_question_and_explanation_preserved(self):
        q = _question("what changed?", explanation="because")
        rebuilt = rebuild_options(q, ["d1", "d2", "d3"])
        assert rebuilt["question"] == "what changed?"
        assert rebuilt["explanation"] == "because"

    def test_input_dict_not_mutated(self):
        q = _question("q", options=["right", "w1", "w2", "w3"], correct_index=0)
        rebuild_options(q, ["d1", "d2", "d3"])
        assert q["options"] == ["right", "w1", "w2", "w3"]

    def test_rebuilt_question_round_trips_through_parse_questions(self):
        q = _question("what changed?", options=["w1", "right", "w2", "w3"], correct_index=1)
        rebuilt = rebuild_options(q, ["d1", "d2", "d3"])
        parsed = parse_questions(json.dumps({"questions": [rebuilt]}))
        assert parsed == [rebuilt]


class TestApplyAmbiguityResults:
    def test_empty_resolutions_keeps_everything(self):
        qs = [_question("a"), _question("b")]
        assert apply_ambiguity_results(qs, {}) == qs

    def test_none_resolution_drops_question(self):
        qs = [_question("a"), _question("b"), _question("c")]
        assert apply_ambiguity_results(qs, {1: None}) == [qs[0], qs[2]]

    def test_dict_resolution_replaces_in_place(self):
        qs = [_question("a"), _question("b")]
        replacement = _question("b", options=["w", "x", "y", "z"])
        assert apply_ambiguity_results(qs, {1: replacement}) == [qs[0], replacement]

    def test_mixed_keep_drop_replace_preserves_order(self):
        qs = [_question(t) for t in "abcd"]
        replacement = _question("c2")
        result = apply_ambiguity_results(qs, {1: None, 2: replacement})
        assert result == [qs[0], replacement, qs[3]]

    def test_unknown_index_ignored(self):
        qs = [_question("a")]
        assert apply_ambiguity_results(qs, {9: None}) == qs

    def test_all_dropped_gives_empty_list(self):
        qs = [_question("a"), _question("b")]
        assert apply_ambiguity_results(qs, {0: None, 1: None}) == []


class TestRetryWait:
    def test_missing_header_uses_backoff_ladder(self):
        assert retry_wait(0) == 10.0
        assert retry_wait(1) == 20.0
        assert retry_wait(2) == 40.0
        assert retry_wait(3) == 80.0

    def test_numeric_header_wins_over_ladder(self):
        assert retry_wait(0, "30") == 30.0
        assert retry_wait(2, "5") == 5.0

    def test_float_header_accepted(self):
        assert retry_wait(0, "2.5") == 2.5

    def test_header_clamped_to_floor_and_cap(self):
        assert retry_wait(0, "0") == 1.0
        assert retry_wait(0, "-7") == 1.0
        assert retry_wait(0, "9999") == 120.0

    def test_malformed_header_falls_back_to_ladder(self):
        assert retry_wait(1, "soon") == 20.0
        assert retry_wait(1, "") == 20.0

    def test_non_finite_header_falls_back_to_ladder(self):
        assert retry_wait(0, "inf") == 10.0
        assert retry_wait(0, "nan") == 10.0


class TestReserveSlot:
    def test_idle_gate_starts_immediately(self):
        start, nxt = reserve_slot(next_start=0.0, now=100.0, spacing=1.0)
        assert start == 100.0
        assert nxt == 101.0

    def test_busy_gate_queues_after_previous_slot(self):
        start, nxt = reserve_slot(next_start=105.0, now=100.0, spacing=1.0)
        assert start == 105.0
        assert nxt == 106.0

    def test_consecutive_reservations_never_share_a_slot(self):
        nxt = 0.0
        starts = []
        for _ in range(4):  # four workers hitting the gate at the same instant
            start, nxt = reserve_slot(nxt, now=100.0, spacing=1.0)
            starts.append(start)
        assert starts == [100.0, 101.0, 102.0, 103.0]

    def test_spacing_zero_degenerates_to_no_pacing(self):
        start, nxt = reserve_slot(next_start=0.0, now=50.0, spacing=0.0)
        assert start == nxt == 50.0


class TestAdaptiveSpacing:
    def test_widen_doubles_spacing(self):
        assert widen_spacing(1.0) == 2.0
        assert widen_spacing(2.0) == 4.0

    def test_widen_caps_at_maximum(self):
        assert widen_spacing(SPACING_MAX) == SPACING_MAX
        assert widen_spacing(SPACING_MAX - 1.0) == SPACING_MAX

    def test_narrow_decays_gently(self):
        assert narrow_spacing(4.0) == 3.6

    def test_narrow_floors_at_minimum(self):
        assert narrow_spacing(SPACING_MIN) == SPACING_MIN
        assert narrow_spacing(SPACING_MIN * 1.05) == SPACING_MIN

    def test_widen_then_narrow_round_trip_stays_in_bounds(self):
        spacing = SPACING_MIN
        for _ in range(6):
            spacing = widen_spacing(spacing)
        assert spacing == SPACING_MAX
        for _ in range(100):
            spacing = narrow_spacing(spacing)
        assert spacing == SPACING_MIN


class TestIsValidRepo:
    def test_github_owner_slash_name_is_valid(self):
        assert is_valid_repo("octocat/hello-world") is True

    def test_azure_devops_org_project_repo_is_valid(self):
        assert is_valid_repo("my-org/my.project/my_repo") is True

    def test_single_segment_without_slash_is_invalid(self):
        assert is_valid_repo("hello-world") is False

    def test_four_segments_is_invalid(self):
        assert is_valid_repo("a/b/c/d") is False

    def test_empty_string_is_invalid(self):
        assert is_valid_repo("") is False

    def test_trailing_slash_is_invalid(self):
        assert is_valid_repo("owner/name/") is False

    def test_leading_slash_is_invalid(self):
        assert is_valid_repo("/owner/name") is False

    def test_space_in_segment_is_invalid(self):
        assert is_valid_repo("owner/name with space") is False

    def test_disallowed_character_is_invalid(self):
        assert is_valid_repo("owner/name@bad") is False

    def test_dot_only_name_segment_is_invalid(self):
        assert is_valid_repo("owner/..") is False
        assert is_valid_repo("owner/.") is False

    def test_dot_only_owner_segment_is_invalid(self):
        assert is_valid_repo("../name") is False

    def test_dot_only_middle_segment_is_invalid(self):
        assert is_valid_repo("owner/../repo") is False

    def test_dot_prefixed_segment_is_valid(self):
        assert is_valid_repo("owner/.github") is True

    def test_non_ascii_word_characters_are_invalid(self):
        # \w is Unicode by default; the repo value lands in a GitHub API URL path
        assert is_valid_repo("öwner/name") is False
        assert is_valid_repo("owner/naïve") is False


class TestSoftDedupContract:
    def test_parse_then_remove_end_to_end(self):
        qs = [
            _question("What does retry backoff do?"),
            _question("Explain the retry backoff behavior."),
            _question("Why was CHUNK_CHAR_BUDGET added?"),
            _question("What is the purpose of the retry backoff?"),
        ]
        raw = json.dumps({
            "duplicate_groups": [[0, 1, 3]],
            "covered_topics": ["retry backoff", "chunking budget"],
        })
        parsed = parse_soft_dedup(raw, len(qs))
        assert remove_soft_duplicates(qs, parsed["groups"]) == [qs[0], qs[2]]
