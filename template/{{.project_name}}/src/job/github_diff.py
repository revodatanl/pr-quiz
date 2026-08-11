"""GitHub-side I/O for quiz generation: token lookup + PR diff fetch."""
import base64

import requests

from quiz_logic import parse_gitattributes_generated, prepare_files, waive_blockers

API = "https://api.github.com"


def get_github_token(w, scope, key):
    try:
        return w.dbutils.secrets.get(scope=scope, key=key)
    except Exception:
        return None  # public repo: anonymous API access still works, just rate-limited


def _headers(token):
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_generated_globs(repo, ref, token):
    """Globs the repo's own .gitattributes marks linguist-generated at `ref`.

    Honoring the repo's declaration is what makes this match GitHub's own diff
    view, which collapses these files instead of showing them. Fail-soft by
    design: most repos have no .gitattributes, and a missing or unreadable one is
    a reason to quiz normally, never a reason to fail a run and block a merge.
    """
    try:
        r = requests.get(
            f"{API}/repos/{repo}/contents/.gitattributes",
            params={"ref": ref},
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


def fetch_pr_diff(repo, pr_number, token, generated_globs=()):
    """Return (PreparedDiff, waive_blockers) from the PR files API.

    Pages the API into raw per-file records and hands the whole list to
    quiz_logic, which owns every judgement about them - which files are quizzable,
    what each contributes to sizing, and whether an empty result may be waived at
    all - so those stay pure, tested, and identical across providers.
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
                    # every real payload carries status; defaulting keeps a
                    # partial one from raising halfway through the paging loop
                    "status": f.get("status", "modified"),
                    "changed_lines": f.get("additions", 0) + f.get("deletions", 0),
                    "patch": f.get("patch"),
                }
            )
        page += 1
    return prepare_files(raw, generated_globs), waive_blockers(raw, generated_globs)
