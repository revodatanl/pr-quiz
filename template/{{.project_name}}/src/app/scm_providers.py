"""Provider registry for the app: which SCM (GitHub, later Azure DevOps) a
quiz's pool row belongs to. v1 registers github_client only — a registered
provider just needs to match SCMProvider's shape (duck typing), so a v2
provider drops in here with zero changes to app.py or quiz_store.py.

Deliberately parallel to src/job/diff_providers.py: the app deploys src/app
only (see resources/quiz_app.yml's source_code_path), so the two registries
cannot share a module.
"""
from typing import Callable, Iterable, Protocol, Tuple

import github_client


class SCMProvider(Protocol):
    """The app-side operations app.py needs from an SCM. Attributes, not
    methods with `self` — a registered provider is a module (see
    github_client.py), not a class instance.

    Behavioral contract a v2 implementation must honor:

    - post_commit_status(repo, sha, state, description, context): publish the
      quiz-gate status for the commit under `context` (app.py passes the
      status_context the backend was configured with). `state` vocabulary is
      exactly 'success' (passed) or 'failure' (blocked) — the merge gate
      (actions/gate-check) keys off these, so a provider whose status model
      differs must map onto them. Raises PublishError-compatible errors on
      failure.
    - post_pr_comment(repo, pr_number, body) -> str: post `body` (markdown —
      renders on GitHub and ADO alike) as a PR comment; returns the
      comment's html URL, or '' when the provider cannot supply one. Raises
      PublishError-compatible errors on failure.
    - get_pr_metas(pairs) -> dict: for each (repo, pr_number) pair, a
      normalized {'state': 'open'|'closed'|'unknown', 'title': <str>} entry
      ('' when there is no title; merged PRs report 'closed'). Must fail
      OPEN, never raise: any per-pair lookup error yields
      {'state': 'unknown', 'title': ''} for that pair only — a provider
      hiccup must never hide a takeable quiz.
    """

    post_commit_status: Callable[..., None]
    post_pr_comment: Callable[..., str]
    get_pr_metas: Callable[[Iterable[Tuple[str, int]]], dict]


# Alias so app.py can catch a provider-neutral error regardless of which SCM
# raised it. v1 has exactly one provider, so this alias is exact today. v2:
# do NOT replace it with a base class defined in this module — github_client
# would have to import it from here while this module imports github_client
# (circular import). Put the shared base in a leaf errors module, or wrap
# each provider's errors at the adapter level, instead.
PublishError = github_client.GitHubError

_PROVIDERS = {"github": github_client}


def get_provider(name: str) -> SCMProvider:
    """Look up the SCM implementation registered for `name`.

    Raises PublishError for an unregistered name so it reads as a publish
    failure wherever it escapes; app.py additionally resolves the provider
    up front, right after pool resolution, so an unknown provider (deploy
    skew: the job learning a provider before the app does) stops the page
    with a friendly message instead of surfacing at submit time.
    """
    try:
        return _PROVIDERS[name]
    except KeyError:
        raise PublishError(
            f"Unknown SCM provider: {name!r} (supported: {sorted(_PROVIDERS)})"
        ) from None
