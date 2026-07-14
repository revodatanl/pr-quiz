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


class DiffProvider(Protocol):
    """The job-side operations generate_quiz.py needs from an SCM.
    Attributes, not methods with `self` — a registered provider is a plain
    namespace over functions, not a class instance.

    Behavioral contract a v2 implementation must honor:

    - get_token(w, scope, key): read the provider API token from the
      workspace secret scope; returns None when unavailable (anonymous
      access, where the provider supports it), never raises.
    - fetch_pr_diff(repo, pr_number, token) -> (files, total_changed_lines):
      `files` holds one {'filename', 'text', 'changed_lines'} dict per file
      that HAS patch text — 'text' carries a filename/status header line
      followed by the unified diff. `total_changed_lines` counts changed
      lines across ALL files, including patchless ones (binary/oversized),
      so question-count sizing sees the whole PR.
    """

    fetch_pr_diff: Callable[..., tuple]
    get_token: Callable[..., object]


# github_diff.py keeps its GitHub-flavored function names (it is a GitHub
# module and stays untouched); this thin adapter maps them onto the
# Protocol's provider-neutral member names.
_PROVIDERS = {
    "github": SimpleNamespace(
        fetch_pr_diff=github_diff.fetch_pr_diff,
        get_token=github_diff.get_github_token,
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
