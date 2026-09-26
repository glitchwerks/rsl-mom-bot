"""Regression tests for Dependabot CI eligibility (#357)."""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOW = _ROOT / ".github" / "workflows" / "ci.yml"
_JOB_GUARD_RE = re.compile(r"^    if: \|\n(?P<body>(?:      .+\n)+)", re.MULTILINE)


def _job_guards() -> list[str]:
    text = _WORKFLOW.read_text(encoding="utf-8")
    return [match.group("body") for match in _JOB_GUARD_RE.finditer(text)]


def test_every_ci_job_allows_dependabot() -> None:
    guards = _job_guards()

    assert guards, "ci.yml contains no guarded jobs"
    assert all("github.actor == 'dependabot[bot]'" in guard for guard in guards)


def test_every_ci_job_keeps_external_author_restriction() -> None:
    guards = _job_guards()

    assert all("github.event.pull_request.author_association" in guard for guard in guards)


def test_ci_jobs_do_not_receive_write_permissions_or_secrets() -> None:
    text = _WORKFLOW.read_text(encoding="utf-8")

    assert "contents: write" not in text
    assert "secrets:" not in text
