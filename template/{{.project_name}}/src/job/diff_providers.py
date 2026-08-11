"""Provider registry for the job: which SCM (GitHub, later Azure DevOps)
supplies a PR's diff plus the token that authenticates the fetch. v1
registers GitHub only; generate_quiz.py derives its SUPPORTED_PROVIDERS CLI
validation from this registry's keys, so a v2 provider drops in here with
zero changes to generate_quiz.py.

Deliberately parallel to src/app/scm_providers.py: the job deploys src/job
only (see resources/quiz_job.yml's python_file), so the two registries
cannot share a module.
"""
from types import SimpleNamespace
from typing import Callable, Protocol

import github_diff
import github_status


class DiffProvider(Protocol):
    """The job-side operations generate_quiz.py needs from an SCM.
    Attributes, not methods with `self` — a registered provider is a plain
    namespace over functions, not a class instance.

    Behavioral contract a v2 implementation must honor:

    - get_token(w, scope, key): read the provider API token from the
      workspace secret scope; returns None when unavailable (anonymous
      access, where the provider supports it), never raises.
    - fetch_generated_globs(repo, ref, token) -> tuple of globs: the paths the
      repo itself declares as machine-generated at `ref` (for GitHub, the
      .gitattributes linguist-generated entries). MUST fail soft — a missing or
      unreadable declaration returns () and the run continues.
    - fetch_pr_diff(repo, pr_number, token, generated_globs=()) ->
      (quiz_logic.PreparedDiff, waive_blockers): a provider pages its API into
      raw {'filename', 'status', 'changed_lines', 'patch'} records and returns
      quiz_logic.prepare_files(raw, generated_globs) alongside
      quiz_logic.waive_blockers(raw), rather than building either itself — what
      is quizzable, what it weighs, and whether an empty result may be waived
      stay pure, tested and identical across providers. 'status' uses GitHub's
      vocabulary — added / removed / modified / renamed / copied / changed — and
      a v2 provider maps its own onto it; only 'removed' is branched on
      (quiz_logic.DELETED_STATUSES). A provider MUST report a real changed-line
      count on a file whose diff it declines to return, and zero on a binary:
      that difference is the only thing separating an unreviewable change from an
      absent one, and treating them alike opens a way past the gate.
    - post_commit_status(repo, sha, state, description, context, token): publish
      the merge-gate status. The job uses this only to waive the gate, so
      'success' is the only state it passes. Raises on failure.
    """

    fetch_generated_globs: Callable[..., tuple]
    fetch_pr_diff: Callable[..., tuple]
    get_token: Callable[..., object]
    post_commit_status: Callable[..., None]


# github_diff.py and github_status.py keep their GitHub-flavored function names
# (they are GitHub modules); this thin adapter maps them onto the Protocol's
# provider-neutral member names.
_PROVIDERS = {
    "github": SimpleNamespace(
        fetch_generated_globs=github_diff.fetch_generated_globs,
        fetch_pr_diff=github_diff.fetch_pr_diff,
        get_token=github_diff.get_github_token,
        post_commit_status=github_status.post_commit_status,
    )
}

# generate_quiz.py's --provider CLI validation imports this: the registry's
# keys are the single source of truth for which providers are supported.
SUPPORTED_PROVIDERS = frozenset(_PROVIDERS)


class UnknownProviderError(ValueError):
    """No registered diff-fetch implementation for this provider name."""


def get_provider(name: str) -> DiffProvider:
    """Look up the SCM implementation registered for `name`."""
    try:
        return _PROVIDERS[name]
    except KeyError:
        raise UnknownProviderError(
            f"Unknown SCM provider: {name!r} (supported: {sorted(_PROVIDERS)})"
        ) from None
