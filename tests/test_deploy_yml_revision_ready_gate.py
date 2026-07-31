"""Regression tests for the deploy.yml revision-ready gate (issue #332).

``.github/workflows/deploy.yml``'s ``az containerapp update`` step returns as
soon as the update API call is *accepted*, not once the new Container App
revision is actually ready and serving traffic. In single-revision mode this
leaves the old revision alive and answering Discord interactions for roughly
60-90 seconds after the workflow reports green (see the investigation in
``.claude/agent-memory/investigator/deploy_yml_no_ready_gate.md``).

These tests parse the workflow YAML as text (no PyYAML dependency exists in
this project — see ``pyproject.toml``) and assert that a readiness-polling
gate exists after the ``az containerapp update ... ca-mom-bot`` invocation,
that it actually polls (sleeps and retries) with a bounded, loud failure path
rather than hanging the job forever, and that it isn't neutered with
``continue-on-error: true`` so a failed poll can't be silently reported as a
successful deploy.

The gate is intentionally not required to live in a specific step or under a
specific step name — it may be appended to the same run block as the update
command, or placed in a following step. What matters is that *some* readiness
check exists after the update call and actually gates job success.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEPLOY_YML = Path(__file__).parent.parent / ".github" / "workflows" / "deploy.yml"

# Matches the prod deploy call specifically — "az containerapp update" (no
# "job" in between), as distinct from the earlier "az containerapp job
# update" call used to pin the migrations job image.
_UPDATE_CMD_RE = re.compile(r"az\s+containerapp\s+update\b[\s\S]{0,400}?ca-mom-bot", re.MULTILINE)

# Either signal from the issue's acceptance criteria ("and/or") counts as a
# readiness check: latestReadyRevisionName == latestRevisionName, and/or the
# new revision's healthState == Healthy.
_READY_SIGNAL_RE = re.compile(r"latestReadyRevisionName|healthState")

# Top-level GitHub Actions step headers, e.g. "      - name: Some step".
_STEP_HEADER_RE = re.compile(r"^([ ]+)- name:[ \t]*(.+?)[ \t]*$", re.MULTILINE)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_workflow_text() -> str:
    """Return the full contents of deploy.yml.

    Returns:
        The workflow file's text, decoded as UTF-8.
    """
    return _DEPLOY_YML.read_text(encoding="utf-8")


def _step_spans(text: str) -> list[tuple[str, int, int]]:
    """Return (name, start, end) character spans for every top-level step.

    Args:
        text: Full workflow YAML text.

    Returns:
        A list of (step name, start offset, end offset) tuples covering the
        whole file, in document order. ``end`` is the start of the next step
        header or ``len(text)`` for the last step.
    """
    headers = list(_STEP_HEADER_RE.finditer(text))
    spans: list[tuple[str, int, int]] = []
    for i, match in enumerate(headers):
        name = match.group(2)
        start = match.start()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        spans.append((name, start, end))
    return spans


def _locate_revision_ready_gate(text: str) -> str:
    """Return the text of the step containing the post-update readiness gate.

    Searches for ``az containerapp update`` targeting ``ca-mom-bot``, then
    looks for a readiness-check reference (``latestReadyRevisionName`` and/or
    ``healthState``) anywhere after it — whether appended to the same run
    block as the update command, or placed in a later step. This keeps the
    test satisfiable by more than one correct implementation shape.

    Args:
        text: Full deploy.yml text.

    Returns:
        The text of the step block that contains the readiness-check
        reference.

    Raises:
        AssertionError: If the ``az containerapp update ... ca-mom-bot``
            anchor itself cannot be found (a sign the test's assumption
            about the file is stale, not that the gate is missing).
    """
    update_match = _UPDATE_CMD_RE.search(text)
    assert update_match is not None, (
        "Could not locate 'az containerapp update ... ca-mom-bot' in "
        "deploy.yml — this test's anchor assumption is stale relative to "
        "the current workflow file; update the test's anchor pattern."
    )

    remainder = text[update_match.end() :]
    gate_match = _READY_SIGNAL_RE.search(remainder)
    if gate_match is None:
        pytest.fail(
            "deploy.yml has no readiness check (referencing "
            "'latestReadyRevisionName' and/or 'healthState') anywhere after "
            "'az containerapp update ... ca-mom-bot'. Per issue #332, the "
            "workflow must poll until the new revision is actually "
            "ready/healthy before reporting the job successful — today it "
            "returns as soon as the update API call is merely accepted, "
            "leaving the old revision serving traffic for ~60-90s after "
            "the workflow goes green."
        )

    gate_offset = update_match.end() + gate_match.start()
    for _name, start, end in _step_spans(text):
        if start <= gate_offset < end:
            return text[start:end]

    pytest.fail(
        "Found a readiness-check reference "
        f"({gate_match.group(0)!r}) after the update command, but it is "
        "not inside a recognizable workflow step body — cannot confirm it "
        "actually gates the job."
    )
    raise AssertionError("unreachable")  # pragma: no cover — pytest.fail raises


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRevisionReadyGateExists:
    """The core regression test: a readiness gate must exist at all."""

    def test_gate_exists_after_update_command(self) -> None:
        """A readiness check must follow the containerapp update call.

        Covers issue #332's acceptance criteria: deploy.yml must poll for
        ``latestReadyRevisionName == latestRevisionName`` and/or the new
        revision's ``healthState == Healthy`` after ``az containerapp
        update``, instead of reporting success as soon as the update API
        call is merely accepted.
        """
        text = _read_workflow_text()
        gate_step = _locate_revision_ready_gate(text)
        assert gate_step.strip(), "located gate step body is unexpectedly empty"


class TestRevisionReadyGatePolls:
    """The gate must actually poll, not do a single racy check."""

    def test_gate_polls_with_sleep_and_bounded_failure_path(self) -> None:
        """The gate must sleep-and-retry and fail loudly, not hang forever.

        A one-shot check on the first not-ready response would just move
        the same race earlier in the workflow. And a poll loop with no
        bound would hang the job indefinitely instead of failing it when
        the revision never becomes ready — both are unacceptable per issue
        #332's requirement to *wait* for readiness, not merely *check* it
        once.
        """
        text = _read_workflow_text()
        gate_step = _locate_revision_ready_gate(text)

        assert "sleep" in gate_step, (
            "Gate step has no 'sleep' call — a single, immediate check "
            "races the same ~60-90s handoff window issue #332 describes; "
            "the gate must retry with a poll cadence instead of checking "
            "once."
        )
        assert re.search(r"exit\s+1\b", gate_step), (
            "Gate step has no non-zero exit path — an unbounded poll that "
            "never fails would hang the deploy job forever instead of "
            "failing it when the new revision never becomes ready/healthy."
        )


class TestRevisionReadyGateBlocksSuccess:
    """The gate must be wired so its failure actually fails the job."""

    def test_gate_is_not_neutered_with_continue_on_error(self) -> None:
        """The gate step must not be marked ``continue-on-error: true``.

        A gate that exists and polls correctly but is marked
        ``continue-on-error: true`` would still let the job report overall
        success even when the new revision never becomes ready — silently
        reintroducing the exact bug issue #332 reports.
        """
        text = _read_workflow_text()
        gate_step = _locate_revision_ready_gate(text)

        assert not re.search(r"continue-on-error:\s*true", gate_step, re.IGNORECASE), (
            "Gate step is marked 'continue-on-error: true' — this lets the "
            "job report success even when the revision-ready poll fails, "
            "defeating the purpose of the gate."
        )
