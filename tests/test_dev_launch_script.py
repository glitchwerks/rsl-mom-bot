"""Subprocess tests for scripts/dev-launch.sh — the local dev launcher.

Runs ``dev-launch.sh`` via ``bash`` inside a synthetic sandbox directory
that mirrors the real repo layout (a ``scripts/`` dir holding a copy of
the script, an optional ``.env.dev`` at the sandbox root, and a fake
project venv). A fake ``az`` executable is placed first on ``PATH``. The
fake Python interpreter is deliberately **not** on ``PATH`` — it lives at
the OS-appropriate venv-relative interpreter path
(``<sandbox>/.venv/Scripts/python.exe`` on Windows,
``<sandbox>/.venv/bin/python`` on POSIX), because the script is expected
to invoke that specific path rather than resolve a bare ``python`` off
``PATH``. A decoy ``python`` stub is also placed on ``PATH`` that logs a
distinct, poisoned marker and exits non-zero, so any test that
accidentally falls back to a PATH-resolved ``python`` fails loudly rather
than silently passing. Every stub logs its invocation (argv + relevant
env vars) to a file the tests inspect afterward, so no real Azure CLI or
Python interpreter is ever invoked.

The sandbox intentionally supports both a cwd-relative and a
script-directory-relative ``.env.dev`` lookup: the copied script lives at
``<sandbox>/scripts/dev-launch.sh`` and the subprocess ``cwd`` is set to
``<sandbox>``, so either resolution strategy finds the same
``<sandbox>/.env.dev`` file. The spec does not mandate one strategy over
the other. Out of contract: the sandbox is not a git repository, so a
``git rev-parse --show-toplevel``-based root resolution is NOT covered
and will fail every test here — use cwd-relative or script-dir-relative
resolution. The fake venv is built at the same sandbox root (one level up
from ``scripts/``, i.e. ``SCRIPT_DIR/..``), matching where ``.env.dev``
lives.

Tested behaviours (see issue #348 for the full spec)
-----------------------------------------------------
1. Missing ``.env.dev`` -> non-zero exit with a clear error message.
2. ``az account show`` tenant matches ``AZURE_TENANT_ID`` -> ``az login``
   is skipped entirely.
3. Tenant mismatch, command failure, or empty output from
   ``az account show`` -> ``az login --tenant "$AZURE_TENANT_ID"`` runs.
4. ``az account set --subscription "$AZURE_SUBSCRIPTION_ID"`` always runs,
   on both the skip-login and login branches.
5. The project's venv-relative Python interpreter (``.venv/Scripts/
   python.exe`` on Windows, ``.venv/bin/python`` on POSIX, resolved
   relative to the repo root) is invoked with ``-m mom_bot`` and
   ``MOM_BOT_ENV=dev`` in its environment, after a successful login
   sequence — never a bare ``python`` resolved off ``PATH``.
6. A non-zero exit from any ``az`` step (``login`` or ``account set``)
   aborts the script before the venv Python interpreter is ever invoked.

Windows footgun handled here: a Windows path like ``C:/Users/...`` breaks
bash's colon-delimited ``PATH`` (the drive-letter colon is parsed as a
separator). ``_to_posix_for_path_var`` converts only the stub directory
used inside the ``PATH`` env var; the script path, sandbox cwd, and paths
written into stub bodies use plain ``Path.as_posix()``, which bash
accepts natively as a drive-relative path.

A second Windows footgun specific to the venv-interpreter stub: MSYS
bash resolves a shebang (``#!/bin/sh``) at exec time regardless of file
extension, so a shell-script stub can be named ``python.exe`` and still
run correctly under Git Bash — confirmed by hand before trusting it here.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Script + bash resolution
# ---------------------------------------------------------------------------

SCRIPT = Path(__file__).parents[1] / "scripts" / "dev-launch.sh"

# Resolve bash — prefer Git for Windows bash over WSL shim (mirrors the
# resolution used by tests/test_extract_highlights.py). On CI
# (ubuntu-latest) /usr/bin/bash is the standard location.
_GIT_BASH = Path("C:/Program Files/Git/usr/bin/bash.exe")
_BASH_ON_PATH = shutil.which("bash")
BASH = str(_GIT_BASH) if _GIT_BASH.exists() else (_BASH_ON_PATH or "bash")

pytestmark = pytest.mark.skipif(
    not _GIT_BASH.exists() and _BASH_ON_PATH is None,
    reason="bash not found on PATH — dev-launch.sh tests require bash",
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TENANT_ID = "11111111-2222-3333-4444-555555555555"
_OTHER_TENANT_ID = "99999999-8888-7777-6666-555555555555"
_SUBSCRIPTION_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

_ENV_DEV_CONTENT = {
    "AZURE_TENANT_ID": _TENANT_ID,
    "AZURE_SUBSCRIPTION_ID": _SUBSCRIPTION_ID,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_posix_for_path_var(path: Path) -> str:
    """Convert a Windows path to the ``/c/...`` form bash's PATH needs.

    ``PATH`` is colon-delimited, so a drive-letter path like
    ``C:/Users/...`` is parsed as two bogus entries (``C`` and
    ``/Users/...``). Bash (MSYS) expects drive letters spelled as
    ``/c/...`` inside ``PATH`` specifically. On POSIX this is a no-op
    beyond normalizing separators.

    Args:
        path: The path to convert.

    Returns:
        The POSIX/MSYS-style path string, safe to join into ``PATH``.
    """
    text = path.as_posix()
    if len(text) >= 2 and text[1] == ":":
        text = "/" + text[0].lower() + text[2:]
    return text


def _venv_python_path(root: Path) -> Path:
    """Return the OS-appropriate venv-relative Python interpreter path.

    Mirrors the resolution the script is expected to perform relative to
    its own location (``SCRIPT_DIR/..``, the same directory ``.env.dev``
    lives in): ``.venv/Scripts/python.exe`` on Windows, ``.venv/bin/
    python`` on POSIX. The sandbox and the script under test always run
    on the same host OS, so matching ``os.name`` here mirrors whatever
    OS-detection the script performs at run time.

    Args:
        root: The sandbox root (equivalent to the real repo root).

    Returns:
        The absolute path the fake interpreter must be written to, and
        the path the script is expected to invoke.
    """
    if os.name == "nt":
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python"


def _write_stub(path: Path, body: str) -> Path:
    """Write an executable POSIX shell stub script.

    Args:
        path: Destination path for the stub file.
        body: Shell script body (must include a shebang line).

    Returns:
        ``path`` — the written file, for chaining.
    """
    path.write_text(body, encoding="utf-8", newline="\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


class Sandbox:
    """A synthetic repo-root directory for running dev-launch.sh in isolation.

    Attributes:
        root: The synthetic repo root. Contains ``scripts/dev-launch.sh``
            (copied from the real script) and, optionally, ``.env.dev``.
        script: Path to the copied ``dev-launch.sh`` inside ``root``.
        log: Path to the stub invocation log file.
        bin_dir: Directory holding the fake ``az`` stub (and a decoy,
            poisoned ``python`` stub), placed first on ``PATH`` when the
            sandbox is run.
        venv_python: The OS-appropriate venv-relative interpreter path
            (see ``_venv_python_path``) where the real fake Python stub
            lives — deliberately outside ``bin_dir``/``PATH``.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.script = root / "scripts" / "dev-launch.sh"
        self.log = root / "log.txt"
        self.bin_dir = root / "bin"
        self.venv_python = _venv_python_path(root)

    def log_lines(self) -> list[str]:
        """Return the stub invocation log as a list of lines (or empty).

        Returns:
            Each logged invocation line, in call order. Empty if no stub
            was ever invoked or the log file was never created.
        """
        if not self.log.exists():
            return []
        return self.log.read_text(encoding="utf-8").splitlines()

    def python_was_invoked(self) -> bool:
        """Whether the venv Python stub was ever called.

        Returns:
            ``True`` if any logged line starts with ``python ``.
        """
        return any(line.startswith("python ") for line in self.log_lines())

    def python_argv0_lines(self) -> list[str]:
        """The exact path(s) the venv Python stub observed as ``$0``.

        Returns:
            One entry per invocation of the venv Python stub, in call
            order, with the ``argv0 `` prefix stripped.
        """
        return [line[len("argv0 ") :] for line in self.log_lines() if line.startswith("argv0 ")]

    def path_python_was_invoked(self) -> bool:
        """Whether the decoy, PATH-resolved ``python`` stub was ever called.

        This stub lives in ``bin_dir`` (on ``PATH``) rather than at the
        venv-relative interpreter path, and always exits non-zero. Any
        call to it proves the script fell back to a bare ``python``
        lookup instead of resolving the project's venv interpreter.

        Returns:
            ``True`` if any logged line starts with ``path-python ``.
        """
        return any(line.startswith("path-python ") for line in self.log_lines())

    def az_login_was_invoked(self) -> bool:
        """Whether the fake ``az login`` stub was ever called.

        Returns:
            ``True`` if any logged line starts with ``az login``.
        """
        return any(line.startswith("az login") for line in self.log_lines())


def _make_sandbox(
    tmp_path: Path,
    *,
    write_env_dev: bool,
    az_show_tenant: str | None = _TENANT_ID,
    az_show_exit: int = 0,
    az_login_exit: int = 0,
    az_set_exit: int = 0,
) -> Sandbox:
    """Build a synthetic repo root with a stubbed ``az`` and venv Python.

    The fake ``az`` lives on ``PATH`` (in ``bin_dir``). The fake Python
    interpreter lives at the OS-appropriate venv-relative path
    (``_venv_python_path``), *not* on ``PATH`` — a separate, poisoned
    ``python`` decoy sits in ``bin_dir`` instead, so a script that falls
    back to a bare ``python`` lookup fails loudly (non-zero exit, a
    distinguishable ``path-python`` log line) rather than silently
    passing.

    Args:
        tmp_path: Pytest-provided temp directory to build the sandbox in.
        write_env_dev: Whether to write ``.env.dev`` at the sandbox root
            (with ``AZURE_TENANT_ID``/``AZURE_SUBSCRIPTION_ID``). Pass
            ``False`` to test the missing-file path.
        az_show_tenant: Tenant ID the fake ``az account show`` prints to
            stdout, or ``None`` to have it print nothing (an empty
            response, as if no account is logged in).
        az_show_exit: Exit code for the fake ``az account show`` call.
        az_login_exit: Exit code for the fake ``az login`` call.
        az_set_exit: Exit code for the fake ``az account set`` call.

    Returns:
        A populated ``Sandbox`` ready to run via ``_run_dev_launch``.
    """
    assert SCRIPT.exists(), (
        "scripts/dev-launch.sh does not exist yet (implementation pending, "
        "see issue #348) — this assertion is the expected red until it "
        "is written"
    )

    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    dest_script = scripts_dir / "dev-launch.sh"
    shutil.copy2(SCRIPT, dest_script)
    dest_script.chmod(dest_script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    if write_env_dev:
        lines = "".join(f"{key}={value}\n" for key, value in _ENV_DEV_CONTENT.items())
        (tmp_path / ".env.dev").write_text(lines, encoding="utf-8", newline="\n")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = (tmp_path / "log.txt").as_posix()

    show_echo = f'echo "{az_show_tenant}"' if az_show_tenant is not None else ":"
    az_stub_body = f"""#!/bin/sh
LOGFILE="{log_path}"
echo "az $*" >> "$LOGFILE"
if [ "$1" = "account" ] && [ "$2" = "show" ]; then
    {show_echo}
    exit {az_show_exit}
fi
if [ "$1" = "login" ]; then
    exit {az_login_exit}
fi
if [ "$1" = "account" ] && [ "$2" = "set" ]; then
    exit {az_set_exit}
fi
exit 0
"""
    _write_stub(bin_dir / "az", az_stub_body)

    # Decoy: a bare `python` resolved off PATH. Poisoned — logs a
    # distinguishable marker and fails — so an implementation that never
    # moves off `python -m mom_bot` produces a loud, positive failure
    # signal instead of an ambiguous "command not found".
    path_python_stub_body = f"""#!/bin/sh
LOGFILE="{log_path}"
echo "path-python $*" >> "$LOGFILE"
exit 1
"""
    _write_stub(bin_dir / "python", path_python_stub_body)

    # The real fake interpreter: placed at the venv-relative path the
    # script is expected to resolve and invoke directly, never via PATH.
    venv_python = _venv_python_path(tmp_path)
    venv_python.parent.mkdir(parents=True, exist_ok=True)
    venv_python_stub_body = f"""#!/bin/sh
LOGFILE="{log_path}"
echo "argv0 $0" >> "$LOGFILE"
echo "python $* MOM_BOT_ENV=${{MOM_BOT_ENV-<unset>}}" >> "$LOGFILE"
exit 0
"""
    _write_stub(venv_python, venv_python_stub_body)

    return Sandbox(tmp_path)


def _run_dev_launch(sandbox: Sandbox) -> subprocess.CompletedProcess[str]:
    """Run ``dev-launch.sh`` in the sandbox with a stubbed ``az`` and venv Python.

    Uses a hermetic environment (only ``PATH``) so the script cannot
    accidentally pass by reading ambient ``AZURE_TENANT_ID`` /
    ``AZURE_SUBSCRIPTION_ID`` / ``MOM_BOT_ENV`` from this test process —
    it must source them from ``.env.dev`` and set them itself. ``PATH``
    carries the fake ``az`` and the poisoned ``python`` decoy only — the
    real fake interpreter lives off-PATH at the venv-relative path the
    script must resolve itself (see ``Sandbox.venv_python``).

    Args:
        sandbox: The sandbox built by ``_make_sandbox``.

    Returns:
        ``CompletedProcess`` with ``returncode``, ``stdout``, ``stderr``.
    """
    stub_dir = _to_posix_for_path_var(sandbox.bin_dir)
    # A minimal but functional PATH — mirrors tests/test_migrate_sh.py's
    # _base_env(), which relies on the same "PATH is enough for sh/bash
    # builtins" property.
    env = {"PATH": f"{stub_dir}:/usr/bin:/bin"}
    return subprocess.run(
        [BASH, str(sandbox.script)],
        cwd=sandbox.root,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


# ---------------------------------------------------------------------------
# 1. Missing .env.dev
# ---------------------------------------------------------------------------


class TestMissingEnvDev:
    """Behaviour when ``.env.dev`` is absent from the repo root."""

    def test_exits_nonzero(self, tmp_path: Path) -> None:
        """A missing ``.env.dev`` causes a non-zero exit.

        Without ``AZURE_TENANT_ID``/``AZURE_SUBSCRIPTION_ID``, the script
        cannot proceed and must fail loudly rather than falling through
        to unset-variable errors deeper in the script.
        """
        sandbox = _make_sandbox(tmp_path, write_env_dev=False)
        result = _run_dev_launch(sandbox)
        assert result.returncode != 0

    def test_prints_clear_error_message(self, tmp_path: Path) -> None:
        """The error output names ``.env.dev`` so the fix is obvious.

        Checked on combined stdout+stderr since the spec doesn't mandate
        which stream carries the message.
        """
        sandbox = _make_sandbox(tmp_path, write_env_dev=False)
        result = _run_dev_launch(sandbox)
        combined = result.stdout + result.stderr
        assert ".env.dev" in combined

    def test_does_not_invoke_az_or_python(self, tmp_path: Path) -> None:
        """Neither stub is invoked when ``.env.dev`` is missing.

        The script must fail before attempting any Azure or Python step.
        """
        sandbox = _make_sandbox(tmp_path, write_env_dev=False)
        _run_dev_launch(sandbox)
        assert sandbox.log_lines() == []


# ---------------------------------------------------------------------------
# 2. Tenant matches -> skip az login
# ---------------------------------------------------------------------------


class TestTenantMatchesSkipsLogin:
    """When the current tenant already matches, ``az login`` is skipped."""

    def test_checks_current_tenant_via_account_show(self, tmp_path: Path) -> None:
        """The script calls ``az account show --query tenantId -o tsv``.

        This is the check that determines whether login can be skipped.
        """
        sandbox = _make_sandbox(tmp_path, write_env_dev=True, az_show_tenant=_TENANT_ID)
        _run_dev_launch(sandbox)
        assert "az account show --query tenantId -o tsv" in sandbox.log_lines()

    def test_does_not_call_az_login(self, tmp_path: Path) -> None:
        """``az login`` is never invoked when the tenant already matches."""
        sandbox = _make_sandbox(tmp_path, write_env_dev=True, az_show_tenant=_TENANT_ID)
        _run_dev_launch(sandbox)
        assert not sandbox.az_login_was_invoked()

    def test_exits_zero_on_happy_path(self, tmp_path: Path) -> None:
        """The full skip-login happy path completes with exit code 0."""
        sandbox = _make_sandbox(tmp_path, write_env_dev=True, az_show_tenant=_TENANT_ID)
        result = _run_dev_launch(sandbox)
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# 3. Tenant mismatch / failure / empty output -> az login runs
# ---------------------------------------------------------------------------


class TestTenantMismatchTriggersLogin:
    """``az login`` runs whenever the current tenant check doesn't confirm a match."""

    def test_logs_in_when_tenant_differs(self, tmp_path: Path) -> None:
        """A different current tenant triggers ``az login --tenant <id>``."""
        sandbox = _make_sandbox(tmp_path, write_env_dev=True, az_show_tenant=_OTHER_TENANT_ID)
        _run_dev_launch(sandbox)
        assert f"az login --tenant {_TENANT_ID}" in sandbox.log_lines()

    def test_logs_in_when_account_show_fails(self, tmp_path: Path) -> None:
        """A failing ``az account show`` (non-zero exit) triggers login too."""
        sandbox = _make_sandbox(tmp_path, write_env_dev=True, az_show_tenant=None, az_show_exit=1)
        _run_dev_launch(sandbox)
        assert sandbox.az_login_was_invoked()

    def test_logs_in_when_account_show_output_empty(self, tmp_path: Path) -> None:
        """An empty (but zero-exit) ``az account show`` triggers login too.

        Covers the "not logged in yet" case where the command succeeds
        but prints nothing.
        """
        sandbox = _make_sandbox(tmp_path, write_env_dev=True, az_show_tenant=None, az_show_exit=0)
        _run_dev_launch(sandbox)
        assert sandbox.az_login_was_invoked()

    def test_login_uses_tenant_id_from_env_dev(self, tmp_path: Path) -> None:
        """``az login`` is called with the exact tenant from ``.env.dev``.

        Guards against a hardcoded or blank ``--tenant`` value.
        """
        sandbox = _make_sandbox(tmp_path, write_env_dev=True, az_show_tenant=_OTHER_TENANT_ID)
        _run_dev_launch(sandbox)
        login_lines = [line for line in sandbox.log_lines() if line.startswith("az login")]
        assert login_lines == [f"az login --tenant {_TENANT_ID}"]


# ---------------------------------------------------------------------------
# 4. az account set always runs
# ---------------------------------------------------------------------------


class TestAccountSetAlwaysRuns:
    """``az account set --subscription`` runs on both the skip and login branches."""

    def test_runs_on_skip_login_branch(self, tmp_path: Path) -> None:
        """``account set`` runs even when login was skipped."""
        sandbox = _make_sandbox(tmp_path, write_env_dev=True, az_show_tenant=_TENANT_ID)
        _run_dev_launch(sandbox)
        assert f"az account set --subscription {_SUBSCRIPTION_ID}" in sandbox.log_lines()

    def test_runs_on_login_branch(self, tmp_path: Path) -> None:
        """``account set`` runs after a completed login too."""
        sandbox = _make_sandbox(tmp_path, write_env_dev=True, az_show_tenant=_OTHER_TENANT_ID)
        _run_dev_launch(sandbox)
        assert f"az account set --subscription {_SUBSCRIPTION_ID}" in sandbox.log_lines()


# ---------------------------------------------------------------------------
# 5. venv-relative python invocation
# ---------------------------------------------------------------------------


class TestPythonInvocation:
    """The final step invokes the project's venv Python with ``-m mom_bot``.

    The interpreter path is resolved relative to the script's own
    location (``SCRIPT_DIR/..``, the same directory ``.env.dev`` lives
    in) — OS-appropriate: ``.venv/Scripts/python.exe`` on Windows,
    ``.venv/bin/python`` on POSIX. A bare ``python`` resolved off
    ``PATH`` is explicitly not acceptable — see
    ``test_never_falls_back_to_path_resolved_python``.
    """

    def test_python_invoked_with_module_args(self, tmp_path: Path) -> None:
        """The venv Python is invoked with exactly ``-m mom_bot``."""
        sandbox = _make_sandbox(tmp_path, write_env_dev=True, az_show_tenant=_TENANT_ID)
        _run_dev_launch(sandbox)
        assert sandbox.python_was_invoked()
        python_lines = [line for line in sandbox.log_lines() if line.startswith("python ")]
        assert len(python_lines) == 1
        assert python_lines[0].startswith("python -m mom_bot ")

    def test_mom_bot_env_set_to_dev(self, tmp_path: Path) -> None:
        """``MOM_BOT_ENV=dev`` is present in the venv Python stub's environment."""
        sandbox = _make_sandbox(tmp_path, write_env_dev=True, az_show_tenant=_TENANT_ID)
        _run_dev_launch(sandbox)
        python_lines = [line for line in sandbox.log_lines() if line.startswith("python ")]
        assert python_lines == ["python -m mom_bot MOM_BOT_ENV=dev"]

    def test_python_invoked_after_login_branch_too(self, tmp_path: Path) -> None:
        """The venv Python also runs after a successful login sequence."""
        sandbox = _make_sandbox(tmp_path, write_env_dev=True, az_show_tenant=_OTHER_TENANT_ID)
        _run_dev_launch(sandbox)
        assert sandbox.python_was_invoked()

    def test_invokes_venv_relative_interpreter_path(self, tmp_path: Path) -> None:
        """The script execs the OS-appropriate venv-relative interpreter path.

        Asserted via the stub's own ``$0`` — a tail match, not exact
        equality, since a correct implementation may or may not
        normalize the ``SCRIPT_DIR/../.venv/...`` path before invoking
        it; only the resolved location matters, not the string shape.
        """
        sandbox = _make_sandbox(tmp_path, write_env_dev=True, az_show_tenant=_TENANT_ID)
        _run_dev_launch(sandbox)
        argv0_lines = sandbox.python_argv0_lines()
        assert len(argv0_lines) == 1
        expected_tail = ".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python"
        assert argv0_lines[0].replace("\\", "/").endswith(expected_tail)

    def test_never_falls_back_to_path_resolved_python(self, tmp_path: Path) -> None:
        """A bare ``python`` resolved off ``PATH`` is never invoked as a fallback.

        Only the venv-relative interpreter stub may be called. The decoy
        ``python`` on ``PATH`` is poisoned (exits non-zero, logs a
        distinct ``path-python`` marker) precisely so a PATH fallback
        fails loudly instead of silently passing.
        """
        sandbox = _make_sandbox(tmp_path, write_env_dev=True, az_show_tenant=_TENANT_ID)
        _run_dev_launch(sandbox)
        assert not sandbox.path_python_was_invoked()


# ---------------------------------------------------------------------------
# 6. Any az failure aborts before python runs
# ---------------------------------------------------------------------------


class TestAzFailureAbortsBeforePython:
    """A non-zero exit from any ``az`` step must prevent ``python -m mom_bot``."""

    def test_az_login_failure_aborts(self, tmp_path: Path) -> None:
        """A failing ``az login`` stops the script before ``python`` runs."""
        sandbox = _make_sandbox(
            tmp_path,
            write_env_dev=True,
            az_show_tenant=_OTHER_TENANT_ID,
            az_login_exit=1,
        )
        result = _run_dev_launch(sandbox)
        assert result.returncode != 0
        assert not sandbox.python_was_invoked()

    def test_az_account_set_failure_aborts(self, tmp_path: Path) -> None:
        """A failing ``az account set`` stops the script before ``python`` runs.

        Exercised on the skip-login branch so ``account set`` is reached
        directly (isolating this failure from the login failure above).
        """
        sandbox = _make_sandbox(
            tmp_path,
            write_env_dev=True,
            az_show_tenant=_TENANT_ID,
            az_set_exit=1,
        )
        result = _run_dev_launch(sandbox)
        assert result.returncode != 0
        assert not sandbox.python_was_invoked()
