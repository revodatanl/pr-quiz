"""github_client PR-meta reads: the fail-open contract (a GitHub hiccup must
never hide a takeable quiz) and per-PR alignment of the concurrent batch read.
Each read returns both the PR state (for the active filter) and its title (for
the picker label) from a single GET.

Pure/no-network: requests.get (or get_pr_meta) is monkeypatched so nothing
touches a real socket.
"""
import requests

import github_client


class _Resp:
    """Minimal stand-in for a requests.Response: just .json() and
    .raise_for_status()."""

    def __init__(self, payload=None, raises=None):
        self._payload = {} if payload is None else payload
        self._raises = raises

    def raise_for_status(self):
        if self._raises is not None:
            raise self._raises

    def json(self):
        return self._payload


class TestGetPrMeta:
    def test_open_state_and_title_returned(self, monkeypatch):
        monkeypatch.setattr(
            github_client.requests, "get",
            lambda *a, **k: _Resp({"state": "open", "title": "Fix the widget"}),
        )
        assert github_client.get_pr_meta("org/repo", 1) == {
            "state": "open", "title": "Fix the widget"
        }

    def test_closed_state_returned(self, monkeypatch):
        monkeypatch.setattr(
            github_client.requests, "get",
            lambda *a, **k: _Resp({"state": "closed", "title": "t"}),
        )
        assert github_client.get_pr_meta("org/repo", 1)["state"] == "closed"

    def test_missing_state_key_is_unknown(self, monkeypatch):
        # 200 with a body that omits "state" must fail open, not KeyError.
        monkeypatch.setattr(
            github_client.requests, "get", lambda *a, **k: _Resp({"title": "no state"})
        )
        meta = github_client.get_pr_meta("org/repo", 1)
        assert meta["state"] == "unknown"
        assert meta["title"] == "no state"

    def test_missing_title_key_is_empty_string(self, monkeypatch):
        monkeypatch.setattr(
            github_client.requests, "get", lambda *a, **k: _Resp({"state": "open"})
        )
        assert github_client.get_pr_meta("org/repo", 1)["title"] == ""

    def test_null_title_coerced_to_empty_string(self, monkeypatch):
        monkeypatch.setattr(
            github_client.requests, "get",
            lambda *a, **k: _Resp({"state": "open", "title": None}),
        )
        assert github_client.get_pr_meta("org/repo", 1)["title"] == ""

    def test_request_exception_is_unknown(self, monkeypatch):
        def boom(*a, **k):
            raise requests.RequestException("network down")

        monkeypatch.setattr(github_client.requests, "get", boom)
        assert github_client.get_pr_meta("org/repo", 1) == {"state": "unknown", "title": ""}

    def test_http_error_from_raise_for_status_is_unknown(self, monkeypatch):
        # A non-2xx response: raise_for_status() raises HTTPError (a
        # RequestException subclass), which must be swallowed to "unknown".
        monkeypatch.setattr(
            github_client.requests,
            "get",
            lambda *a, **k: _Resp(raises=requests.HTTPError("404 Not Found")),
        )
        assert github_client.get_pr_meta("org/repo", 1) == {"state": "unknown", "title": ""}

    def test_timeout_is_unknown(self, monkeypatch):
        # Timeout is a RequestException subclass; the short-timeout design
        # relies on it failing open.
        def boom(*a, **k):
            raise requests.Timeout("timed out")

        monkeypatch.setattr(github_client.requests, "get", boom)
        assert github_client.get_pr_meta("org/repo", 1) == {"state": "unknown", "title": ""}


class TestGetPrMetaHeaders:
    def test_bearer_header_present_when_token_set(self, monkeypatch):
        captured = {}

        def fake_get(url, headers=None, timeout=None):
            captured["headers"] = headers
            return _Resp({"state": "open", "title": "t"})

        monkeypatch.setenv("GITHUB_TOKEN", "secret-token")
        monkeypatch.setattr(github_client.requests, "get", fake_get)
        github_client.get_pr_meta("org/repo", 1)
        assert captured["headers"]["Authorization"] == "Bearer secret-token"
        assert captured["headers"]["Accept"] == "application/vnd.github+json"

    def test_no_bearer_header_when_token_absent(self, monkeypatch):
        # Reads must work anonymously; no token => no Authorization header.
        captured = {}

        def fake_get(url, headers=None, timeout=None):
            captured["headers"] = headers
            return _Resp({"state": "open", "title": "t"})

        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setattr(github_client.requests, "get", fake_get)
        github_client.get_pr_meta("org/repo", 1)
        assert "Authorization" not in captured["headers"]
        assert captured["headers"]["Accept"] == "application/vnd.github+json"


class TestGetPrMetas:
    def test_empty_input_returns_empty_and_makes_no_request(self, monkeypatch):
        def fail(*a, **k):
            raise AssertionError("requests.get must not be called for empty input")

        monkeypatch.setattr(github_client.requests, "get", fail)
        assert github_client.get_pr_metas([]) == {}

    def test_maps_each_pair_to_its_own_meta(self, monkeypatch):
        # Per-pair wrapper stub: an alignment bug (zip/order/concurrency)
        # would surface as a mismatched mapping.
        monkeypatch.setattr(
            github_client, "get_pr_meta",
            lambda repo, n: {"state": f"state-{n}", "title": f"title-{n}"},
        )
        result = github_client.get_pr_metas([("org/repo", 7), ("org/repo", 9), ("org/repo", 42)])
        assert result == {
            ("org/repo", 7): {"state": "state-7", "title": "title-7"},
            ("org/repo", 9): {"state": "state-9", "title": "title-9"},
            ("org/repo", 42): {"state": "state-42", "title": "title-42"},
        }

    def test_alignment_through_real_get_pr_meta(self, monkeypatch):
        # Drive the real get_pr_meta via a URL-keyed stub so the zip/order
        # contract is exercised end-to-end, not just the per-pair wrapper.
        def fake_get(url, headers=None, timeout=None):
            n = int(url.rsplit("/", 1)[-1])
            return _Resp({"state": f"state-{n}", "title": f"title-{n}"})

        monkeypatch.setattr(github_client.requests, "get", fake_get)
        result = github_client.get_pr_metas([("org/repo", 7), ("org/repo", 9), ("org/repo", 42)])
        assert result[("org/repo", 7)] == {"state": "state-7", "title": "title-7"}
        assert result[("org/repo", 42)] == {"state": "state-42", "title": "title-42"}

    def test_per_pr_failure_fails_open_without_poisoning_others(self, monkeypatch):
        # One PR's lookup blowing up must yield "unknown" for that PR only,
        # leaving its siblings' real state/title intact.
        def fake_get(url, headers=None, timeout=None):
            n = int(url.rsplit("/", 1)[-1])
            if n == 9:
                raise requests.Timeout("slow one")
            return _Resp({"state": "open", "title": "kept"})

        monkeypatch.setattr(github_client.requests, "get", fake_get)
        result = github_client.get_pr_metas([("org/repo", 7), ("org/repo", 9)])
        assert result[("org/repo", 7)] == {"state": "open", "title": "kept"}
        assert result[("org/repo", 9)] == {"state": "unknown", "title": ""}

    def test_duplicate_pair_collapses_to_one_entry(self, monkeypatch):
        # dict(zip(...)) folds duplicates; the surviving entry keeps the meta.
        monkeypatch.setattr(
            github_client, "get_pr_meta",
            lambda repo, n: {"state": f"state-{n}", "title": f"title-{n}"},
        )
        result = github_client.get_pr_metas([("org/repo", 7), ("org/repo", 9), ("org/repo", 7)])
        assert result == {
            ("org/repo", 7): {"state": "state-7", "title": "title-7"},
            ("org/repo", 9): {"state": "state-9", "title": "title-9"},
        }

    def test_different_repos_same_pr_number_kept_distinct(self, monkeypatch):
        # The reason for pair-keying: two repos each with a PR #7 must not
        # collapse into one entry, and each must query its own repo's API path.
        def fake_get(url, headers=None, timeout=None):
            repo = url.split("/repos/", 1)[1].rsplit("/pulls/", 1)[0]
            return _Resp({"state": "open", "title": f"title-{repo}"})

        monkeypatch.setattr(github_client.requests, "get", fake_get)
        result = github_client.get_pr_metas([("org/repo-a", 7), ("org/repo-b", 7)])
        assert result == {
            ("org/repo-a", 7): {"state": "open", "title": "title-org/repo-a"},
            ("org/repo-b", 7): {"state": "open", "title": "title-org/repo-b"},
        }


class TestPostCommitStatus:
    def _capture_post(self, monkeypatch):
        captured = {}

        def fake_post(url, json=None, headers=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            return _Resp({})

        monkeypatch.setenv("GITHUB_TOKEN", "secret-token")
        monkeypatch.setattr(github_client.requests, "post", fake_post)
        return captured

    def test_default_context_is_quiz_gate(self, monkeypatch):
        captured = self._capture_post(monkeypatch)
        github_client.post_commit_status("org/repo", "abc123", "success", "PASSED")
        assert captured["json"]["context"] == "quiz-gate"

    def test_custom_context_is_carried_through(self, monkeypatch):
        # app.py posts under the configured QUIZ_STATUS_CONTEXT; a backend with
        # a non-default context must publish on that same context, else branch
        # protection requires a check the gate never turns green.
        captured = self._capture_post(monkeypatch)
        github_client.post_commit_status(
            "org/repo", "abc123", "success", "PASSED", context="my-gate"
        )
        assert captured["json"]["context"] == "my-gate"
        assert captured["url"].endswith("/repos/org/repo/statuses/abc123")
