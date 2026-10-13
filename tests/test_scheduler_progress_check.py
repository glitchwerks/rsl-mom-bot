"""Portable local health probe remains quiet and never sends external alerts."""

from unittest.mock import MagicMock, patch

import pytest

from mom_bot.health import check


@pytest.mark.parametrize(
    "gateway,progress,expected",
    [
        ({"bot_connected": True}, {"schema": 1, "healthy": True}, 0),
        ({"bot_connected": False}, {"schema": 1, "healthy": True}, 1),
        ({"bot_connected": "true"}, {"schema": 1, "healthy": True}, 1),
        ({"bot_connected": True}, {"schema": 1, "healthy": False}, 1),
        ({"bot_connected": True}, {"schema": True, "healthy": True}, 1),
        ({"bot_connected": True}, {"schema": 1, "healthy": "true"}, 1),
        ({"bot_connected": True}, None, 1),
    ],
)
def test_both_signals_required(gateway, progress, expected, capsys):
    """Connectivity cannot conceal stalled/absent progress or malformed flags."""
    with patch.object(check, "read_endpoint", side_effect=[gateway, progress]) as read:
        assert check.main() == expected
    assert read.call_args_list[0].args == (8001, "/api/health")
    if gateway.get("bot_connected") is True:
        assert read.call_args_list[1].args == (8080, "/healthz/progress")
    assert capsys.readouterr() == ("", "")


def test_protocol_failure_is_quiet(capsys):
    """Raw response or connection errors cannot enter Docker health logs."""
    with patch.object(
        check, "read_endpoint", side_effect=check.http.client.IncompleteRead(b"private-body", 99)
    ):
        assert check.main() == 1
    assert capsys.readouterr() == ("", "")


def test_local_endpoints_ignore_proxy_environment_and_bound_body():
    """No ambient proxy or redirect can move the probe to another endpoint."""
    response = MagicMock()
    response.__enter__.return_value = response
    response.status = 200
    response.read.return_value = b"x" * 4097
    with patch.object(check.urllib.request, "build_opener") as opener:
        opener.return_value.open.return_value = response
        assert check.read_endpoint(8080, "/healthz/progress") is None
        assert opener.return_value.open.call_args.args == (
            "http://127.0.0.1:8080/healthz/progress",
        )
        assert opener.call_args.args[0].proxies == {}
    with pytest.raises(ValueError):
        check.NoRedirect().redirect_request(None)
