"""Tests for the I/O of github_diff.

These tests cover two things. First, the commit that supplies the
generated-path declarations of the repo. Second, the rebuild of a patch that
GitHub does not return for a large file.

No test uses the network. Each test replaces github_diff.requests.get with a
fake, so nothing opens a socket.
"""
import base64
from unittest.mock import Mock

import requests

import github_diff

BASE_SHA = "1" * 40
HEAD_SHA = "2" * 40


def _resp(payload=None, content=b"", status_code=200, raises=None):
    """A fake requests.Response with a status, a body, and an optional error."""
    return Mock(
        content=content,
        status_code=status_code,
        json=Mock(return_value={} if payload is None else payload),
        raise_for_status=Mock(side_effect=raises),
    )


class _Api:
    """Answers each GET of github_diff by URL, and records every call."""

    def __init__(self, gitattributes=None, blobs=None, files=None, pr_fails=False):
        self.gitattributes = gitattributes  # text, or None to give a 404
        self.blobs = blobs or {}  # (path, ref) -> bytes, or an Exception to raise
        self.files = files or []  # one page of PR file records
        self.pr_fails = pr_fails  # if True, the base/head SHA read fails
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        if url.endswith("/files"):
            return _resp(payload=self.files if (params or {}).get("page", 1) == 1 else [])
        if "/pulls/" in url:
            if self.pr_fails:
                return _resp(raises=requests.RequestException("boom"))
            return _resp(payload={"base": {"sha": BASE_SHA}, "head": {"sha": HEAD_SHA}})
        if url.endswith("/contents/.gitattributes"):
            if self.gitattributes is None:
                return _resp(status_code=404)
            encoded = base64.b64encode(self.gitattributes.encode()).decode()
            return _resp(payload={"content": encoded})
        blob = self.blobs.get((url.split("/contents/", 1)[1], (params or {}).get("ref")))
        if isinstance(blob, Exception):
            return _resp(raises=blob)
        if blob is None:
            return _resp(status_code=404)
        return _resp(content=blob)


def _record(name, patch=None, changed=9000, status="modified"):
    """One record from the PR files API. By default it has no patch, but real
    line counts. Any positive `changed` works: is_unreviewable only reads > 0."""
    return {
        "filename": name, "status": status, "patch": patch,
        "additions": changed, "deletions": 0,
    }


class TestFetchGeneratedGlobs:
    def test_declarations_are_read_from_the_base_commit_never_the_head(self, monkeypatch):
        # This is the security property. If the declarations come from the head,
        # the pull request under review supplies its own review rules. A PR can
        # then mark its own source generated and cut its quiz to one question.
        api = _Api(gitattributes="dist/* linguist-generated\n")
        monkeypatch.setattr(github_diff.requests, "get", api.get)
        assert github_diff.fetch_generated_globs("org/repo", 7, "tok") == ("dist/*",)
        assert [p.get("ref") for u, p in api.calls
                if u.endswith("/.gitattributes")] == [BASE_SHA]

    def test_missing_gitattributes_is_no_declarations(self, monkeypatch):
        monkeypatch.setattr(github_diff.requests, "get", _Api().get)
        assert github_diff.fetch_generated_globs("org/repo", 7, "tok") == ()

    def test_an_unreadable_pr_is_no_declarations(self, monkeypatch):
        # Fail-soft at every level. If GitHub fails, the job makes a normal quiz.
        # It must never fail the run and block a merge.
        api = _Api(gitattributes="*.lock linguist-generated", pr_fails=True)
        monkeypatch.setattr(github_diff.requests, "get", api.get)
        assert github_diff.fetch_generated_globs("org/repo", 7, "tok") == ()
        # never even attempted
        assert not [u for u, _ in api.calls if u.endswith("/.gitattributes")]

    def test_a_malformed_contents_body_is_no_declarations(self, monkeypatch):
        # A 200 reply with no readable "content" must not raise out of the job.
        def get(url, **kwargs):
            if "/pulls/" in url:
                return _resp(payload={"base": {"sha": BASE_SHA}, "head": {"sha": HEAD_SHA}})
            return _resp(payload={"content": None})

        monkeypatch.setattr(github_diff.requests, "get", get)
        assert github_diff.fetch_generated_globs("org/repo", 7, "tok") == ()


class TestFetchPrDiffRebuildsMissingPatches:
    def test_an_oversized_text_diff_is_rebuilt_shown_and_counted(self, monkeypatch):
        # Before this rebuild, a dropped file also lost its changed lines from the
        # question count. That made one attack possible: pad a file until GitHub
        # stops its diff, then pass the gate.
        api = _Api(
            files=[_record("big.py")],
            blobs={("big.py", BASE_SHA): b"a\n", ("big.py", HEAD_SHA): b"a\nb\n"},
        )
        monkeypatch.setattr(github_diff.requests, "get", api.get)
        diff = github_diff.fetch_pr_diff("org/repo", 7, "tok")
        assert [f["filename"] for f in diff.files] == ["big.py"]
        assert diff.changed_lines == 9000
        assert diff.unreviewable == ()
        assert "+b" in diff.files[0]["text"]

    def test_a_renamed_file_rebuilds_as_all_additions(self, monkeypatch):
        # The new path gives a 404 at the base commit, which reads as an empty
        # previous version. The old location is lost. The file is still shown and
        # still counted, and the gate depends on that.
        api = _Api(
            files=[_record("new.py", status="renamed")],
            blobs={("new.py", HEAD_SHA): b"a\nb\n"},
        )
        monkeypatch.setattr(github_diff.requests, "get", api.get)
        diff = github_diff.fetch_pr_diff("org/repo", 7, "tok")
        assert diff.unreviewable == ()
        assert diff.changed_lines == 9000
        assert "+a" in diff.files[0]["text"] and "+b" in diff.files[0]["text"]

    def test_a_binary_blob_is_not_rebuilt(self, monkeypatch):
        api = _Api(
            files=[_record("blob.bin")],
            blobs={("blob.bin", BASE_SHA): b"a\n", ("blob.bin", HEAD_SHA): b"a\x00b"},
        )
        monkeypatch.setattr(github_diff.requests, "get", api.get)
        diff = github_diff.fetch_pr_diff("org/repo", 7, "tok")
        assert diff.files == []
        assert diff.unreviewable == ("blob.bin",)

    def test_a_blob_past_the_size_cap_is_not_rebuilt(self, monkeypatch):
        monkeypatch.setattr(github_diff, "MAX_BLOB_BYTES", 8)
        api = _Api(
            files=[_record("huge.py")],
            blobs={("huge.py", BASE_SHA): b"a\n", ("huge.py", HEAD_SHA): b"x" * 99},
        )
        monkeypatch.setattr(github_diff.requests, "get", api.get)
        diff = github_diff.fetch_pr_diff("org/repo", 7, "tok")
        assert diff.unreviewable == ("huge.py",)

    def test_a_failed_blob_read_leaves_it_unreviewable(self, monkeypatch):
        api = _Api(
            files=[_record("big.py")],
            blobs={("big.py", BASE_SHA): requests.RequestException("boom"),
                   ("big.py", HEAD_SHA): b"a\nb\n"},
        )
        monkeypatch.setattr(github_diff.requests, "get", api.get)
        diff = github_diff.fetch_pr_diff("org/repo", 7, "tok")
        assert diff.unreviewable == ("big.py",)

    def test_a_line_ending_only_change_is_dropped_not_failed(self, monkeypatch):
        # splitlines() gives the same result for \r\n and \n. A CRLF-to-LF change
        # in a file too large for GitHub to diff rebuilds to an empty patch. There
        # is nothing to quiz, but the run must not fail.
        api = _Api(
            files=[_record("crlf.py")],
            blobs={("crlf.py", BASE_SHA): b"a\r\nb\r\n", ("crlf.py", HEAD_SHA): b"a\nb\n"},
        )
        monkeypatch.setattr(github_diff.requests, "get", api.get)
        diff = github_diff.fetch_pr_diff("org/repo", 7, "tok")
        assert (diff.files, diff.changed_lines, diff.unreviewable) == ([], 0, ())

    def test_an_ordinary_pr_costs_no_extra_requests(self, monkeypatch):
        api = _Api(files=[_record("app.py", patch="@@\n+x", changed=10)])
        monkeypatch.setattr(github_diff.requests, "get", api.get)
        diff = github_diff.fetch_pr_diff("org/repo", 7, "tok")
        assert diff.changed_lines == 10
        assert [url for url, _ in api.calls if not url.endswith("/files")] == []
