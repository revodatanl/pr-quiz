"""src/job/diff_providers.py (template payload): the job-side SCM registry generate_quiz.py
resolves --provider against. v1 registers GitHub only (a thin adapter over
github_diff); SUPPORTED_PROVIDERS here is generate_quiz.py's single source of
truth for its CLI validation, so a v2 provider (e.g. Azure DevOps) plugs in
here with zero changes to generate_quiz.py.
"""
import pytest

import diff_providers
import github_diff


class TestGetProvider:
    def test_known_name_maps_onto_the_real_github_diff_functions(self):
        # The registered impl is a thin adapter with provider-neutral member
        # names; identity per function proves calls reach the real
        # github_diff implementation, not copies or wrappers.
        impl = diff_providers.get_provider("github")
        assert impl.fetch_pr_diff is github_diff.fetch_pr_diff
        assert impl.get_token is github_diff.get_github_token

    def test_returned_provider_exposes_the_ops_generate_quiz_uses(self):
        impl = diff_providers.get_provider("github")
        assert callable(impl.fetch_pr_diff)
        assert callable(impl.get_token)

    def test_unknown_name_raises_with_a_clear_message(self):
        with pytest.raises(diff_providers.UnknownProviderError, match="azuredevops"):
            diff_providers.get_provider("azuredevops")


class TestSupportedProviders:
    def test_matches_registry_keys(self):
        assert diff_providers.SUPPORTED_PROVIDERS == {"github"}

    def test_generate_quiz_derives_its_constant_from_this_registry(self):
        # Same object, not a copy: the registry's keys are the single source
        # of truth for generate_quiz.py's --provider validation.
        import generate_quiz

        assert generate_quiz.SUPPORTED_PROVIDERS is diff_providers.SUPPORTED_PROVIDERS
