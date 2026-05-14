"""Unit tests for ``lcp.core.config`` — env overrides + secure defaults."""

from __future__ import annotations

import warnings

import pytest

from lcp.core.config import Settings, get_settings

pytestmark = pytest.mark.unit


class TestSecureDefaults:

    def test_rest_host_defaults_to_loopback(self) -> None:
        """C-3 regression: must never default to 0.0.0.0."""

        settings = Settings()
        assert settings.rest_host == "127.0.0.1"

    def test_grpc_host_defaults_to_loopback(self) -> None:
        settings = Settings()
        assert settings.grpc_host == "127.0.0.1"

    def test_mtls_required_by_default(self) -> None:
        settings = Settings()
        assert settings.mtls_require_client_cert is True

    def test_rls_enforced_by_default(self) -> None:
        settings = Settings()
        assert settings.enforce_tenant_rls is True


class TestAlgorithmAllowList:

    def test_default_allow_list_excludes_symmetric(self) -> None:
        """C-2 regression: HS256 must NOT be in the default allow-list."""

        settings = Settings()
        assert "HS256" not in settings.oidc_allowed_algorithms
        assert "HS384" not in settings.oidc_allowed_algorithms
        assert "none" not in settings.oidc_allowed_algorithms
        assert "RS256" in settings.oidc_allowed_algorithms

    def test_leeway_non_zero(self) -> None:
        settings = Settings()
        assert settings.oidc_clock_skew_leeway_seconds > 0


class TestEnvOverride:

    def test_env_overrides_are_picked_up(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LCP_OIDC_AUDIENCE", "custom-aud")
        monkeypatch.setenv("LCP_DB_POOL_SIZE", "42")
        get_settings.cache_clear()
        try:
            settings = get_settings()
            assert settings.oidc_audience == "custom-aud"
            assert settings.db_pool_size == 42
        finally:
            get_settings.cache_clear()


class TestGetSettingsCached:

    def test_same_instance_on_repeated_calls(self) -> None:
        get_settings.cache_clear()
        try:
            a = get_settings()
            b = get_settings()
            assert a is b
        finally:
            get_settings.cache_clear()


class TestUnknownEnvVarWarning:
    """M-7: extra="ignore" is kept for compatibility but typos must be visible."""

    def test_warns_on_typo_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A misspelled LCP_ variable must produce a UserWarning."""

        monkeypatch.setenv("LCP_OIDC_AUDIANCE", "typo-value")  # <- missing D
        with pytest.warns(UserWarning, match="LCP_OIDC_AUDIANCE"):
            Settings()

    def test_no_warning_for_known_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Correctly spelled LCP_ variables must not trigger a warning."""

        monkeypatch.setenv("LCP_OIDC_AUDIENCE", "valid-aud")
        with warnings.catch_warnings():
            warnings.simplefilter("error", UserWarning)
            Settings()  # must not raise

    def test_no_warning_with_no_lcp_extras(self) -> None:
        """Baseline: the default environment must not warn."""

        with warnings.catch_warnings():
            warnings.simplefilter("error", UserWarning)
            Settings()  # must not raise
