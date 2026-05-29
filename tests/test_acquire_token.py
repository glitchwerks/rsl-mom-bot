"""Unit tests for mom_bot.migrations.acquire_token.

These tests exercise the token-acquisition helper using mocked credentials
so that no real IMDS / Azure network call is made.  The real IMDS endpoint
is only reachable inside an Azure Container Apps environment; running the
helper with a real managed identity locally is intentionally out of scope.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest  # noqa: F401 (used in pytest.raises)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"
_CLIENT_ID = "test-client-id-1234"
_FAKE_TOKEN = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.fake"


def _make_token_response(token: str = _FAKE_TOKEN) -> SimpleNamespace:
    """Return a minimal token response object matching AccessToken protocol.

    Args:
        token: Raw token string to embed.

    Returns:
        A SimpleNamespace with a ``.token`` attribute.
    """
    return SimpleNamespace(token=token)


# ---------------------------------------------------------------------------
# Tests for get_postgres_access_token
# ---------------------------------------------------------------------------


class TestGetPostgresAccessToken:
    """Tests for ``get_postgres_access_token``."""

    def test_returns_token_string_on_success(self) -> None:
        """Returns the raw token string when credential succeeds.

        Monkeypatches ManagedIdentityCredential so no IMDS call is made.
        Verifies the helper extracts and returns ``.token`` from the
        credential response.
        """
        from mom_bot.migrations.acquire_token import get_postgres_access_token

        mock_cred = MagicMock()
        mock_cred.get_token.return_value = _make_token_response(_FAKE_TOKEN)

        with patch(
            "mom_bot.migrations.acquire_token.ManagedIdentityCredential",
            return_value=mock_cred,
        ):
            result = get_postgres_access_token(_CLIENT_ID)

        assert result == _FAKE_TOKEN

    def test_passes_correct_scope(self) -> None:
        """Calls ``get_token`` with the Postgres AAD scope.

        Verifies the helper passes the exact scope string required by
        Azure Database for PostgreSQL Flexible Server AAD authentication.
        """
        from mom_bot.migrations.acquire_token import get_postgres_access_token

        mock_cred = MagicMock()
        mock_cred.get_token.return_value = _make_token_response()

        with patch(
            "mom_bot.migrations.acquire_token.ManagedIdentityCredential",
            return_value=mock_cred,
        ):
            get_postgres_access_token(_CLIENT_ID)

        mock_cred.get_token.assert_called_once_with(_SCOPE)

    def test_passes_correct_client_id_to_credential(self) -> None:
        """Constructs ManagedIdentityCredential with the supplied client_id.

        Verifies the helper forwards ``client_id`` as a keyword argument to
        ``ManagedIdentityCredential`` so the correct user-assigned managed
        identity is selected.
        """
        from mom_bot.migrations.acquire_token import get_postgres_access_token

        mock_cred = MagicMock()
        mock_cred.get_token.return_value = _make_token_response()

        with patch(
            "mom_bot.migrations.acquire_token.ManagedIdentityCredential",
        ) as mock_cls:
            mock_cls.return_value = mock_cred
            get_postgres_access_token(_CLIENT_ID)

        mock_cls.assert_called_once_with(client_id=_CLIENT_ID)

    def test_raises_on_credential_error(self) -> None:
        """Propagates exceptions raised by the credential.

        When ``ManagedIdentityCredential.get_token`` raises (e.g., IMDS not
        reachable, identity misconfigured), the helper must not swallow the
        exception — it re-raises so the caller can handle it.
        """
        from mom_bot.migrations.acquire_token import get_postgres_access_token

        mock_cred = MagicMock()
        mock_cred.get_token.side_effect = Exception("IMDS not reachable: connection refused")

        with patch(
            "mom_bot.migrations.acquire_token.ManagedIdentityCredential",
            return_value=mock_cred,
        ):
            with pytest.raises(Exception, match="IMDS not reachable"):
                get_postgres_access_token(_CLIENT_ID)
