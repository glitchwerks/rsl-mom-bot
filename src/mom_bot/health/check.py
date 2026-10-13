"""Opt-in local portable probe: gateway connection AND completed scheduler work.

No credentials, remote callbacks, log output, database operations or Discord writes.
Azure's existing liveness probe is intentionally separate and unchanged.
"""

from __future__ import annotations

import http.client
import json
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Do not follow a local probe response to any other destination."""

    def redirect_request(self, *args: object, **kwargs: object) -> None:
        """Fail quietly rather than following redirects."""
        raise ValueError("redirect refused")


def read_endpoint(port: int, route: str) -> object:
    """Read only the fixed localhost endpoints, with small response limits."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(f"http://127.0.0.1:{port}{route}", timeout=2) as response:
        if response.status != 200:
            return None
        raw = response.read(4097)
    if len(raw) > 4096:
        return None
    return json.loads(raw)


def main() -> int:
    """Exit zero only when both independent local health signals qualify."""
    try:
        gateway = read_endpoint(8001, "/api/health")
        if not isinstance(gateway, dict) or gateway.get("bot_connected") is not True:
            return 1
        progress = read_endpoint(8080, "/healthz/progress")
        if not isinstance(progress, dict):
            return 1
        return (
            0
            if type(progress.get("schema")) is int
            and progress.get("schema") == 1
            and progress.get("healthy") is True
            else 1
        )
    except (OSError, ValueError, TypeError, http.client.HTTPException):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
