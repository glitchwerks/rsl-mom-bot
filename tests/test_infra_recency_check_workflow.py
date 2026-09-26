"""Regression tests for the infra deployment-recency guardrail (#318)."""

from __future__ import annotations

import re
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOW = _ROOT / ".github" / "workflows" / "infra-recency-check.yml"


def _workflow_text() -> str:
    return _WORKFLOW.read_text(encoding="utf-8")


def test_guardrail_runs_daily_and_on_deployable_infra_pushes() -> None:
    text = _workflow_text()

    assert "schedule:" in text
    assert "push:" in text
    assert "workflow_dispatch:" in text
    assert "- main" in text
    assert "- 'infra/**'" in text


def test_push_filter_does_not_realert_for_non_deployable_changes() -> None:
    """Docs, generated JSON, and operator scripts must not retrigger drift alerts."""
    text = _workflow_text()

    assert "- '!infra/**/*.md'" in text
    assert "- '!infra/main.json'" in text
    assert "- '!infra/scripts/**'" in text


def test_diff_pathspec_matches_push_exclusions() -> None:
    """The scheduled check and push trigger must agree about deployable files."""
    text = _workflow_text()

    assert "'infra/**'" in text
    assert "':(exclude)infra/**/*.md'" in text
    assert "':(exclude)infra/main.json'" in text
    assert "':(exclude)infra/scripts/**'" in text


def test_latest_prod_infra_deployment_is_the_baseline() -> None:
    text = _workflow_text()

    assert 'environment=prod-infra' in text
    assert "task='deploy:infra'" in text
    assert "--jq '.[0].sha'" in text
    assert "git cat-file -e" in text


def test_empty_deployment_query_handles_literal_null() -> None:
    """gh --jq emits the string ``null`` for an empty deployments list."""
    text = _workflow_text()

    assert re.search(r'\[ "\$DEPLOYED_SHA" = "null" \]', text)
    assert "reason=no-baseline" in text
    assert "detected=true" in text


def test_guardrail_is_read_only_and_alerts_to_dedicated_webhook() -> None:
    text = _workflow_text()

    assert "deployments: read" in text
    assert "DISCORD_INFRA_ALERT_WEBHOOK_URL" in text
    assert "az login" not in text
    assert "az deployment" not in text
    assert "infra-deploy.yml from main" in text
