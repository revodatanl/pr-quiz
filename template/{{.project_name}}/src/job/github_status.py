"""GitHub commit-status writes for the job: the quiz-gate waive.

A deliberate twin of post_commit_status in src/app/github_client.py. The app
deploys src/app only and the job src/job only, so the two cannot share code.
Keep header and payload changes in sync on both sides.
"""
import requests

API = "https://api.github.com"
STATUS_DESCRIPTION_LIMIT = 140  # GitHub rejects longer status descriptions


def post_commit_status(repo, sha, state, description, context, token):
    """Publish `state` for `sha` under `context`.

    Raises instead of swallowing. The only caller is the waive path, where a
    silent failure leaves the PR pending forever with nothing to explain it.
    """
    r = requests.post(
        f"{API}/repos/{repo}/statuses/{sha}",
        json={
            "state": state,
            "context": context,
            "description": description[:STATUS_DESCRIPTION_LIMIT],
        },
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=30,
    )
    r.raise_for_status()
