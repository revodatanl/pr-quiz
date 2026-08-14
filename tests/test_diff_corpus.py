"""Turning a PR diff into the quizzable corpus: generated paths, deletions, chunking."""
import re
from pathlib import Path

import pytest

import diff_corpus
from diff_corpus import (
    DELETED_BLOCK_HEADER,
    chunk_files,
    deleted_file_text,
    deletion_block,
    deletion_references,
    deletions_size,
    is_generated_path,
    parse_gitattributes_generated,
    parse_glob_list,
    prepare_files,
    render_patch,
)


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

    def test_the_gitattributes_example_in_the_docs_actually_works(self):
        # Drift guard, like tests/test_ddl_sync.py: this snippet is what adopters
        # copy, and a rule git silently ignores costs them their whole quiz. It
        # shipped once as a bare "dist/" with a trailing "#" comment, which
        # declares nothing at all.
        docs = (Path(__file__).resolve().parents[1] / "docs" / "adopting.md").read_text(
            encoding="utf-8"
        )
        example = re.search(r"```gitattributes\n(.*?)```", docs, re.DOTALL)
        assert example, "no gitattributes example in docs/adopting.md"
        globs = parse_gitattributes_generated(example.group(1))
        assert is_generated_path("src/api/schema.json", globs)
        assert is_generated_path("web/dist/app.js", globs)


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
        monkeypatch.setattr(diff_corpus, "RECONSTRUCTED_PATCH_LINES", 10)
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
