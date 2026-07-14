"""Minimal GitHub REST client for the app: quiz-gate status, PR result comments,
and PR state reads for the picker filter.

Deliberately parallel to the job's GitHub access (src/job/github_diff.py builds
the same Accept/Bearer headers): the app deploys src/app only, so the plumbing
cannot be imported across that boundary. Keep header changes in sync.
"""
import os
from concurrent.futures import ThreadPoolExecutor

import requests

API = "https://api.github.com"
STATUS_DESCRIPTION_LIMIT = 140  # GitHub rejects longer status descriptions


class GitHubError(RuntimeError):
    """Publishing failed; the message is safe to surface in the UI."""


def _headers():
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise GitHubError("GITHUB_TOKEN is not configured for the app")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }


def _post(url, payload):
    try:
        resp = requests.post(url, json=payload, headers=_headers(), timeout=30)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        raise GitHubError(f"GitHub API call failed: {e}") from e


def post_commit_status(repo, sha, state, description, context="quiz-gate"):
    _post(
        f"{API}/repos/{repo}/statuses/{sha}",
        {
            "state": state,
            "context": context,
            "description": description[:STATUS_DESCRIPTION_LIMIT],
        },
    )


def post_pr_comment(repo, pr_number, body):
    """Returns the comment's html_url."""
    data = _post(f"{API}/repos/{repo}/issues/{pr_number}/comments", {"body": body})
    return data.get("html_url", "")


# The read helpers below deliberately do NOT reuse _headers()/GitHubError:
# those hard-fail without a token, while reads must work anonymously and fail
# open (src/job/github_diff.py builds anonymous-when-no-token headers the same
# way, though its diff fetch still raises on HTTP errors).


def get_pr_meta(repo, pr_number):
    """Return {"state": "open"/"closed", "title": <str>} from one PR GET, or
    {"state": "unknown", "title": ""} if the lookup fails. Callers treat unknown
    as open (a GitHub hiccup must not hide a valid quiz) and an empty title as
    "no title to show". State drives the active filter; title labels the picker.

    Short timeout on purpose: failing open makes a timeout free in correctness
    terms, while 20 PRs / 8 workers x 30s could stall the picker ~90s and poison
    the 60s cache with all-"unknown"."""
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = requests.get(f"{API}/repos/{repo}/pulls/{pr_number}", headers=headers, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        return {"state": data.get("state", "unknown"), "title": data.get("title") or ""}
    except requests.RequestException:
        return {"state": "unknown", "title": ""}


def get_pr_metas(pairs):
    """Meta ({"state","title"}) per (repo, pr_number) pair, fetched concurrently:
    ~20 sequential GETs would stall the picker for seconds on a cold cache.

    Keyed on (repo, pr_number) rather than pr_number alone so one app instance
    can check PRs across multiple repos in a single batch — two repos can each
    have their own PR of the same number."""
    pairs = list(pairs)
    if not pairs:
        return {}
    with ThreadPoolExecutor(max_workers=min(8, len(pairs))) as ex:
        return dict(zip(pairs, ex.map(lambda rp: get_pr_meta(rp[0], rp[1]), pairs)))
