"""GitHub-side I/O for quiz generation: token lookup + PR diff fetch."""
import requests


def get_github_token(w, scope, key):
    try:
        return w.dbutils.secrets.get(scope=scope, key=key)
    except Exception:
        return None  # public repo: anonymous API access still works, just rate-limited


def fetch_pr_diff(repo, pr_number, token):
    """Return (files, total_changed_lines) from the GitHub PR files API.

    files holds one {"filename", "text", "changed_lines"} entry per file that
    has a patch; total_changed_lines also counts patchless (binary/oversized)
    files so question-count sizing sees the whole PR.
    """
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    total_lines = 0
    files = []
    page = 1
    while True:
        r = requests.get(
            f"https://api.github.com/repos/{repo}/pulls/{pr_number}/files",
            params={"per_page": 100, "page": page},
            headers=headers,
            timeout=30,
        )
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        for f in batch:
            changed = f.get("additions", 0) + f.get("deletions", 0)
            total_lines += changed
            patch = f.get("patch")
            if patch:
                files.append(
                    {
                        "filename": f["filename"],
                        "text": f"--- {f['filename']} ({f['status']})\n{patch}",
                        "changed_lines": changed,
                    }
                )
        page += 1
    return files, total_lines
