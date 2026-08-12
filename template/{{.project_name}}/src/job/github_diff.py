"""GitHub-side I/O for quiz generation: token lookup + PR diff fetch."""
import base64

import requests

from quiz_logic import (
    is_unreviewable,
    parse_gitattributes_generated,
    prepare_files,
    render_patch,
)

API = "https://api.github.com"
# Guards the difflib pass, not the download: SequenceMatcher on a pair of
# multi-megabyte files costs minutes to build a patch that
# RECONSTRUCTED_PATCH_LINES truncates anyway.
MAX_BLOB_BYTES = 4 * 1024 * 1024


def get_github_token(w, scope, key):
    try:
        return w.dbutils.secrets.get(scope=scope, key=key)
    except Exception:
        return None  # public repo: anonymous API access still works, just rate-limited


def _headers(token, accept="application/vnd.github+json"):
    headers = {"Accept": accept}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _pr_refs(repo, pr_number, token):
    """(base_sha, head_sha) for the PR, or (None, None) when they cannot be read."""
    try:
        r = requests.get(
            f"{API}/repos/{repo}/pulls/{pr_number}", headers=_headers(token), timeout=30
        )
        r.raise_for_status()
        payload = r.json()
        return payload["base"]["sha"], payload["head"]["sha"]
    except (requests.RequestException, ValueError, TypeError, KeyError):
        return None, None


def fetch_generated_globs(repo, pr_number, token):
    """Globs the repo's .gitattributes marks linguist-generated, read at the PR's BASE.

    Never at the head: there, a PR could mark its own source generated and
    shrink its own quiz. A missing or unreadable file is a reason to quiz
    normally, never to fail the run.
    """
    base_sha, _ = _pr_refs(repo, pr_number, token)
    if not base_sha:
        return ()
    try:
        r = requests.get(
            f"{API}/repos/{repo}/contents/.gitattributes",
            params={"ref": base_sha},
            headers=_headers(token),
            timeout=30,
        )
        if r.status_code == 404:
            return ()
        r.raise_for_status()
        text = base64.b64decode(r.json().get("content", "")).decode("utf-8", "replace")
    except (requests.RequestException, ValueError, TypeError):
        return ()
    return parse_gitattributes_generated(text)


def _blob_text(repo, path, ref, token):
    """A file's contents at `ref` as text, or None when no patch can be built from it.

    None means the read failed, the blob is too big, or it is binary. A 404
    returns "" instead: the path does not exist at that ref, which for an added
    file is an empty previous version.
    """
    try:
        r = requests.get(
            f"{API}/repos/{repo}/contents/{path}",
            params={"ref": ref},
            headers=_headers(token, accept="application/vnd.github.raw"),
            timeout=30,
        )
        if r.status_code == 404:
            return ""
        r.raise_for_status()
        blob = r.content
    except requests.RequestException:
        return None
    if len(blob) > MAX_BLOB_BYTES or b"\0" in blob:
        return None
    try:
        return blob.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _restore_missing_patches(repo, pr_number, raw, token):
    """Rebuild, in place, the patch GitHub declined to return for a text change.

    Only records is_unreviewable flags are touched, so an ordinary PR costs no
    extra requests. What cannot be rebuilt keeps its missing patch and is
    reported by prepare_files.

    ponytail: diffs against the PR's base commit, not the merge base - one API
    call rather than three. Resolve compare/{base}...{head} ->
    merge_base_commit.sha if base-branch drift starts to matter.
    """
    targets = [f for f in raw if is_unreviewable(f)]
    if not targets:
        return
    base_sha, head_sha = _pr_refs(repo, pr_number, token)
    if not (base_sha and head_sha):
        return
    for f in targets:
        # A rename reads its own path at base, which 404s to "", so it rebuilds
        # as all-additions. Only the moved-from context is lost.
        old = _blob_text(repo, f["filename"], base_sha, token)
        new = _blob_text(repo, f["filename"], head_sha, token)
        if old is None or new is None:
            continue
        patch = render_patch(old, new)
        if patch:
            f["patch"] = patch
        else:
            # Same text on both sides: a line-ending or mode-only edit. Nothing
            # to quiz, so drop the count that flagged it.
            f["changed_lines"] = 0


def fetch_pr_diff(repo, pr_number, token, generated_globs=()):
    """Return the PreparedDiff for a PR, from the files API.

    Pages the API into raw per-file records and hands the whole list to
    quiz_logic, which owns every judgement about them. Missing patches are
    rebuilt first, so an oversized diff still counts toward the question count.
    """
    raw = []
    page = 1
    while True:
        r = requests.get(
            f"{API}/repos/{repo}/pulls/{pr_number}/files",
            params={"per_page": 100, "page": page},
            headers=_headers(token),
            timeout=30,
        )
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        for f in batch:
            raw.append(
                {
                    "filename": f["filename"],
                    # every real payload carries status: the default only keeps a
                    # partial one from raising mid-paging
                    "status": f.get("status", "modified"),
                    "changed_lines": f.get("additions", 0) + f.get("deletions", 0),
                    "patch": f.get("patch"),
                }
            )
        page += 1
    _restore_missing_patches(repo, pr_number, raw, token)
    return prepare_files(raw, generated_globs)
