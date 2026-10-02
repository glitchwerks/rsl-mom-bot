"""Regression tests for the infra deployment-recency guardrail (#318)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

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

    assert "environment=prod-infra" in text
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
    assert "SLACK_INFRA_ALERT_WEBHOOK_URL" in text
    assert "DISCORD_INFRA_ALERT_WEBHOOK_URL" not in text
    assert "'{text: $text}'" in text
    assert "az login" not in text
    assert "az deployment" not in text
    assert "infra-deploy.yml from main" in text


@pytest.fixture
def notification_script() -> str:
    """Execute the actual workflow block rather than a duplicate implementation."""
    step = _workflow_text().split("      - name: Alert Slack on infra drift\n", 1)[1]
    run = step.split("        run: |\n", 1)[1]
    lines = []
    for line in run.splitlines():
        if line and not line.startswith("          "):
            break
        lines.append(line)
    script = textwrap.dedent("\n".join(lines))
    assert script.strip(), "Slack notification step must contain a shell script"
    return script


@pytest.fixture
def notification_env(tmp_path: Path) -> dict[str, str]:
    """Replace curl with a recorder so tests can never contact a Slack webhook."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text(
        f"#!{sys.executable}\n" + textwrap.dedent("""\
            import json
            import os
            import sys
            from pathlib import Path

            args = sys.argv[1:]
            Path(os.environ["CURL_ARGS"]).write_text(json.dumps(args))
            Path(args[args.index("-o") + 1]).write_text("webhook response")
            print(os.environ["HTTP_STATUS"], end="")
            sys.exit(int(os.environ["CURL_EXIT_CODE"]))
            """),
        encoding="utf-8",
    )
    curl.chmod(0o755)
    (tmp_path / "infra-drift-files.txt").write_text(
        "infra/main.bicep\ninfra/modules/database.bicep\n", encoding="utf-8"
    )
    return {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "TMPDIR": str(tmp_path),
        "CURL_ARGS": str(tmp_path / "curl-args.json"),
        "HTTP_STATUS": "200",
        "CURL_EXIT_CODE": "0",
        "SLACK_WEBHOOK_URL": "https://example.invalid/test-webhook",
        "BASELINE_SHA": "a" * 40,
        "CURRENT_SHA": "b" * 40,
        "DRIFT_REASON": "changed-files",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_REPOSITORY": "example/mom-bot",
        "GITHUB_RUN_ID": "12345",
    }


def _run_notification(
    script: str, env: dict[str, str], tmp_path: Path
) -> subprocess.CompletedProcess[str]:
    # Match GitHub Actions' fail-fast bash behavior, including transport errors.
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


def _sent_message(env: dict[str, str]) -> str:
    args = json.loads(Path(env["CURL_ARGS"]).read_text(encoding="utf-8"))
    assert args[-1] == env["SLACK_WEBHOOK_URL"]
    assert args[args.index("-H") + 1] == "Content-Type: application/json"
    assert args[args.index("-w") + 1] == "%{http_code}"
    payload = json.loads(args[args.index("-d") + 1])
    assert set(payload) == {"text"}
    assert isinstance(payload["text"], str)
    return payload["text"]


@pytest.mark.parametrize("http_status", ["200", "204", "299"])
def test_slack_notification_sends_payload(
    notification_script: str,
    notification_env: dict[str, str],
    tmp_path: Path,
    http_status: str,
) -> None:
    notification_env["HTTP_STATUS"] = http_status
    result = _run_notification(notification_script, notification_env, tmp_path)

    assert result.returncode == 0, result.stderr
    assert f"delivered to Slack (HTTP {http_status})" in result.stdout
    message = _sent_message(notification_env)
    assert message.startswith(":warning: *mom-bot infrastructure drift detected*")
    assert "differ from the last recorded prod-infra deployment" in message
    assert f"*Last deployed:* `{notification_env['BASELINE_SHA']}`" in message
    assert f"*Current main:* `{notification_env['CURRENT_SHA']}`" in message
    assert "```• infra/main.bicep\n• infra/modules/database.bicep```" in message
    assert "<https://github.com/example/mom-bot/actions/runs/12345|View workflow run>" in message


def test_slack_notification_handles_missing_baseline(
    notification_script: str, notification_env: dict[str, str], tmp_path: Path
) -> None:
    notification_env.update(DRIFT_REASON="no-baseline", BASELINE_SHA="none")
    (tmp_path / "infra-drift-files.txt").write_text("No deployed baseline is recorded.\n")

    result = _run_notification(notification_script, notification_env, tmp_path)

    assert result.returncode == 0, result.stderr
    message = _sent_message(notification_env)
    assert "No successful prod-infra deployment baseline is recorded." in message
    assert "Run infra-deploy.yml from main to establish one." in message
    assert "*Last deployed:* `none`" in message
    assert "• No deployed baseline is recorded." in message


def test_slack_notification_escapes_json_and_limits_files(
    notification_script: str, notification_env: dict[str, str], tmp_path: Path
) -> None:
    files = ['infra/a "quoted" \\ unicode-☃.bicep'] + [
        f"infra/module-{number:02}.bicep" for number in range(1, 55)
    ]
    (tmp_path / "infra-drift-files.txt").write_text("\n".join(files) + "\n", encoding="utf-8")

    result = _run_notification(notification_script, notification_env, tmp_path)

    assert result.returncode == 0, result.stderr
    message = _sent_message(notification_env)
    assert "\n".join(f"• {name}" for name in files[:50]) in message
    assert all(name not in message for name in files[50:])


def test_slack_notification_fails_before_curl_without_secret(
    notification_script: str, notification_env: dict[str, str], tmp_path: Path
) -> None:
    notification_env["SLACK_WEBHOOK_URL"] = ""

    result = _run_notification(notification_script, notification_env, tmp_path)

    assert result.returncode != 0
    assert "::error::SLACK_INFRA_ALERT_WEBHOOK_URL is not configured" in result.stdout
    assert not Path(notification_env["CURL_ARGS"]).exists()
    assert "delivered to Slack" not in result.stdout


@pytest.mark.parametrize("http_status", ["199", "300", "400", "429", "500"])
def test_slack_notification_fails_on_non_success_http_status(
    notification_script: str,
    notification_env: dict[str, str],
    tmp_path: Path,
    http_status: str,
) -> None:
    notification_env["HTTP_STATUS"] = http_status

    result = _run_notification(notification_script, notification_env, tmp_path)

    assert result.returncode != 0
    assert f"::error::Slack webhook returned HTTP {http_status}" in result.stdout
    assert "webhook response" in result.stdout
    assert "delivered to Slack" not in result.stdout
    _sent_message(notification_env)


def test_slack_notification_fails_on_transport_error(
    notification_script: str, notification_env: dict[str, str], tmp_path: Path
) -> None:
    notification_env.update(HTTP_STATUS="000", CURL_EXIT_CODE="7")

    result = _run_notification(notification_script, notification_env, tmp_path)

    assert result.returncode == 7
    assert "delivered to Slack" not in result.stdout
    assert Path(notification_env["CURL_ARGS"]).exists()
