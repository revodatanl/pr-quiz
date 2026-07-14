"""src/app/scm_providers.py (template payload): the app-side SCM registry app.py resolves a pool
row's `provider` string against. v1 registers github_client only;
get_provider() is the seam a v2 provider (e.g. Azure DevOps) plugs into
without touching app.py.
"""
import pytest

import github_client
import scm_providers


class TestGetProvider:
    def test_known_name_returns_the_registered_module(self):
        # Identity, not just equivalence: app.py's provider_impl must BE
        # github_client, so its calls actually reach the real implementation.
        assert scm_providers.get_provider("github") is github_client

    def test_returned_provider_exposes_the_ops_app_uses(self):
        impl = scm_providers.get_provider("github")
        assert callable(impl.post_commit_status)
        assert callable(impl.post_pr_comment)
        assert callable(impl.get_pr_metas)

    def test_unknown_name_raises_publish_error(self):
        with pytest.raises(scm_providers.PublishError):
            scm_providers.get_provider("azuredevops")

    def test_unknown_name_error_message_names_the_provider(self):
        with pytest.raises(scm_providers.PublishError, match="azuredevops"):
            scm_providers.get_provider("azuredevops")


class TestPublishError:
    def test_is_the_github_error_alias(self):
        # app.py catches PublishError instead of GitHubError; if this ever
        # drifts from an alias to a copy, that except clause stops catching
        # github_client's real errors.
        assert scm_providers.PublishError is github_client.GitHubError
