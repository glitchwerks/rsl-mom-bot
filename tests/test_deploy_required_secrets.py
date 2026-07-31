"""Regression test: deploy.yml REQUIRED_SECRETS must match load_secret() call sites.

Issue #320: ``.github/workflows/deploy.yml``'s hand-maintained
``REQUIRED_SECRETS`` bash array (the "Verify required KV secrets are
present" preflight step) is a manually curated list with no mechanism
tying it back to the actual ``load_secret(...)`` call sites in ``src/``.
It has already drifted — e.g. ``prod-new-members-channel-id`` is missing
even though ``main.py`` loads a ``"new-members-channel-id"`` secret at
runtime (through a local variable, not a literal argument, which is why a
naive text search for the literal wouldn't have caught it either).

Equality vs. superset
----------------------
This test asserts **exact equality** between the two sets, not merely that
the workflow list is a superset of what the code needs. A pure superset
check would only catch secrets the preflight *fails to guard*, but the
issue explicitly calls out "no mechanism to catch future drift" in
general — that includes stale entries left behind after a
``load_secret()`` call site is removed from the code. An operator who
keeps provisioning (and rotating) a KV secret nobody reads anymore is
exactly the kind of silent drift this preflight step should prevent, so
equality is the stronger and more useful invariant here. If a legitimate
reason ever arises for the workflow list to carry an intentional buffer
entry, narrow this assertion to a superset check at that time and record
the reason in this docstring.

Parsing approach
-----------------
- **src/ side** (:func:`_extract_load_secret_names`): AST-based. Walks
  every ``*.py`` file under ``src/``, finds ``Call`` nodes whose callee is
  named (or ends in an attribute) ``load_secret``, and resolves the first
  argument. A string literal is used directly. A bare ``Name`` reference
  is resolved by building a flat, file-wide map of
  ``simple_name -> string_constant`` from every ``Assign`` node of the
  form ``name = "literal"`` found anywhere in the file (module or
  function scope) and looking the argument name up in that map — this is
  what resolves ``main.py``'s ``channel_secret = "new-members-channel-id"``
  /  ``load_secret(channel_secret)`` pattern. The map is intentionally
  *not* scope- or order-aware (a single flat dict per file): the codebase
  does not currently reassign a load_secret-feeding variable to two
  different literals within one file, and scope-awareness would add
  complexity with no present payoff. An argument that resolves to neither
  a literal nor a mapped local name (e.g. a function parameter, as in
  ``reminders/seed.py``'s ``_load_int_secret(secret_name)`` wrapper) is
  silently skipped — safe here because every secret name that only
  reaches ``load_secret`` through such a wrapper is *also* passed as a
  literal at the wrapper's own call site elsewhere in the codebase.
- **deploy.yml side** (:func:`_extract_required_secrets`): regex-based.
  Finds the ``REQUIRED_SECRETS=( ... )`` bash-array literal in the
  workflow YAML (it lives inside a plain ``run: |`` shell block, not YAML
  structure, so a YAML parser would not help) and pulls out every
  double-quoted string within the parens.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent
_SRC_ROOT = _REPO_ROOT / "src"
_DEPLOY_YML = _REPO_ROOT / ".github" / "workflows" / "deploy.yml"


# ---------------------------------------------------------------------------
# src/ side: extract every secret name passed to load_secret(...)
# ---------------------------------------------------------------------------


def _is_load_secret_callee(func_node: ast.expr) -> bool:
    """Return True if *func_node* is a call target named ``load_secret``.

    Matches both a bare name (``load_secret(...)``, the pattern used
    throughout this codebase after ``from mom_bot.config import
    load_secret``) and an attribute access (``config.load_secret(...)``)
    so the check is robust to either import style.

    Args:
        func_node: The ``Call.func`` node to inspect.

    Returns:
        True if the node calls something literally named ``load_secret``.
    """
    if isinstance(func_node, ast.Name):
        return func_node.id == "load_secret"
    if isinstance(func_node, ast.Attribute):
        return func_node.attr == "load_secret"
    return False


def _string_literal_assignments(tree: ast.AST) -> dict[str, str]:
    """Build a flat ``name -> literal value`` map for one module's AST.

    Collects every ``Assign`` node of the exact shape ``name = "literal"``
    (single simple target, string constant value) found anywhere in the
    tree, regardless of scope or source order. See the module docstring
    for why a flat, order-agnostic map is sufficient for this codebase.

    Args:
        tree: The parsed module AST to scan.

    Returns:
        A dict mapping the assigned variable name to its literal string
        value. Later assignments to the same name overwrite earlier ones
        (last-write-wins is an implementation detail, not a guarantee
        this test relies on).
    """
    assignments: dict[str, str] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            assignments[node.targets[0].id] = node.value.value
    return assignments


def _extract_load_secret_names(src_root: Path) -> set[str]:
    """Return every secret name reachable from a ``load_secret(...)`` call.

    Args:
        src_root: Root directory to search (recursively) for ``*.py``
            files, e.g. the repo's ``src/`` directory.

    Returns:
        The set of unprefixed secret names (e.g. ``"discord-token"``,
        without the ``dev-``/``prod-`` environment prefix that
        ``mom_bot.config.load_secret`` applies at call time).
    """
    names: set[str] = set()
    for py_file in sorted(src_root.rglob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        local_literals = _string_literal_assignments(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and _is_load_secret_callee(node.func)):
                continue
            if not node.args:
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                names.add(arg.value)
            elif isinstance(arg, ast.Name) and arg.id in local_literals:
                names.add(local_literals[arg.id])
            # Any other argument shape (f-string, function param with no
            # local literal, attribute access, ...) is a dynamic secret
            # name this test cannot statically resolve; it is skipped
            # rather than raising, per the module docstring.
    return names


# ---------------------------------------------------------------------------
# deploy.yml side: extract the REQUIRED_SECRETS bash array
# ---------------------------------------------------------------------------

_REQUIRED_SECRETS_BLOCK_RE = re.compile(r"REQUIRED_SECRETS=\((.*?)\)", re.DOTALL)
_QUOTED_ENTRY_RE = re.compile(r'"([^"]+)"')


def _extract_required_secrets(deploy_yml: Path) -> set[str]:
    """Parse the ``REQUIRED_SECRETS`` bash array out of deploy.yml.

    The array lives inside a plain shell ``run: |`` block (not YAML
    structure), so it is extracted with a regex rather than a YAML
    parser: find the ``REQUIRED_SECRETS=( ... )`` literal, then collect
    every double-quoted string between the parens.

    Args:
        deploy_yml: Path to ``.github/workflows/deploy.yml``.

    Returns:
        The set of fully-qualified secret names as written in the
        workflow (e.g. ``"prod-discord-token"``).

    Raises:
        AssertionError: If the ``REQUIRED_SECRETS=( ... )`` block cannot
            be found at all — signals the workflow's shell syntax changed
            in a way this regex no longer understands, which is a parser
            problem this test must surface loudly rather than silently
            returning an empty set.
    """
    text = deploy_yml.read_text(encoding="utf-8")
    match = _REQUIRED_SECRETS_BLOCK_RE.search(text)
    assert match is not None, (
        "Could not locate a REQUIRED_SECRETS=( ... ) bash array in "
        f"{deploy_yml}; the workflow's preflight step syntax may have "
        "changed in a way this test's regex no longer understands."
    )
    return set(_QUOTED_ENTRY_RE.findall(match.group(1)))


# ---------------------------------------------------------------------------
# Self-tests for the extraction helpers (guard against a silently-broken
# parser producing a false-green drift check)
# ---------------------------------------------------------------------------


class TestExtractLoadSecretNames:
    """Unit tests for :func:`_extract_load_secret_names` in isolation."""

    def test_resolves_direct_string_literal_argument(self, tmp_path: Path) -> None:
        """A literal ``load_secret("foo")`` call resolves to ``"foo"``.

        This is the common case used throughout ``main.py`` and
        ``post_conditions/``.
        """
        module = tmp_path / "direct.py"
        module.write_text(
            'from mom_bot.config import load_secret\n\ntoken = load_secret("foo-secret")\n'
        )

        result = _extract_load_secret_names(tmp_path)

        assert result == {"foo-secret"}

    def test_resolves_name_argument_via_local_string_assignment(self, tmp_path: Path) -> None:
        """``x = "foo"; load_secret(x)`` resolves to ``"foo"``.

        Mirrors the real ``main.py`` pattern:
        ``channel_secret = "new-members-channel-id"`` followed by
        ``load_secret(channel_secret)`` inside a ``try`` block. This is
        the exact shape that let ``prod-new-members-channel-id`` drift
        out of sync silently — a naive literal-only scanner would miss
        it entirely.
        """
        module = tmp_path / "indirect.py"
        module.write_text(
            "from mom_bot.config import load_secret\n\n"
            "def handler():\n"
            '    channel_secret = "new-members-channel-id"\n'
            "    try:\n"
            "        channel_id = int(load_secret(channel_secret))\n"
            "    except Exception:\n"
            "        return None\n"
            "    return channel_id\n"
        )

        result = _extract_load_secret_names(tmp_path)

        assert result == {"new-members-channel-id"}

    def test_skips_unresolvable_dynamic_argument(self, tmp_path: Path) -> None:
        """A function-parameter argument with no local literal is skipped.

        Mirrors ``reminders/seed.py``'s ``_load_int_secret(secret_name)``
        wrapper, where ``secret_name`` is a parameter, not a local
        assignment. This must not raise and must not add a spurious
        empty/None-ish entry to the result set.
        """
        module = tmp_path / "wrapper.py"
        module.write_text(
            "from mom_bot.config import load_secret\n\n"
            "def _load_int_secret(secret_name):\n"
            "    return int(load_secret(secret_name))\n"
        )

        result = _extract_load_secret_names(tmp_path)

        assert result == set()

    def test_ignores_calls_to_other_functions(self, tmp_path: Path) -> None:
        """A call to an unrelated function named ``load`` is not matched."""
        module = tmp_path / "unrelated.py"
        module.write_text("def load(x):\n    return x\n\nvalue = load('not-a-secret')\n")

        result = _extract_load_secret_names(tmp_path)

        assert result == set()

    def test_aggregates_across_multiple_files(self, tmp_path: Path) -> None:
        """Secret names are collected across every .py file under the root."""
        (tmp_path / "a.py").write_text(
            "from mom_bot.config import load_secret\nload_secret('secret-a')\n"
        )
        nested = tmp_path / "pkg"
        nested.mkdir()
        (nested / "b.py").write_text(
            "from mom_bot.config import load_secret\nload_secret('secret-b')\n"
        )

        result = _extract_load_secret_names(tmp_path)

        assert result == {"secret-a", "secret-b"}


class TestExtractRequiredSecrets:
    """Unit tests for :func:`_extract_required_secrets` in isolation."""

    def test_parses_bash_array_of_quoted_entries(self, tmp_path: Path) -> None:
        """A synthetic workflow with the same array shape parses correctly."""
        workflow = tmp_path / "fake-deploy.yml"
        workflow.write_text(
            "steps:\n"
            "  - run: |\n"
            "      REQUIRED_SECRETS=(\n"
            '        "prod-foo"\n'
            '        "prod-bar"\n'
            "      )\n"
        )

        result = _extract_required_secrets(workflow)

        assert result == {"prod-foo", "prod-bar"}

    def test_real_deploy_yml_parses_without_error(self) -> None:
        """Sanity check: the real deploy.yml is parseable and non-empty.

        Guards against the regex silently matching nothing (e.g. after an
        unrelated edit to the workflow's shell syntax) and this test
        suite reporting a vacuous pass.
        """
        result = _extract_required_secrets(_DEPLOY_YML)

        assert result, "Expected at least one REQUIRED_SECRETS entry in deploy.yml"
        assert "prod-discord-token" in result


# ---------------------------------------------------------------------------
# The regression test
# ---------------------------------------------------------------------------


def test_required_secrets_matches_load_secret_call_sites() -> None:
    """deploy.yml's REQUIRED_SECRETS must exactly match src/'s load_secret() calls.

    This is the drift guard requested in issue #320. It currently fails
    because ``REQUIRED_SECRETS`` in ``.github/workflows/deploy.yml`` is
    missing at least ``prod-new-members-channel-id`` (loaded indirectly
    via a local variable in ``main.py``'s ``_send_welcome_message``).

    See the module docstring for why exact-equality (not superset) is the
    asserted invariant.
    """
    secret_names = _extract_load_secret_names(_SRC_ROOT)
    expected_required_secrets = {f"prod-{name}" for name in secret_names}

    actual_required_secrets = _extract_required_secrets(_DEPLOY_YML)

    missing = expected_required_secrets - actual_required_secrets
    extra = actual_required_secrets - expected_required_secrets

    assert not missing and not extra, (
        "deploy.yml REQUIRED_SECRETS has drifted from src/'s load_secret() "
        f"call sites.\nMissing from REQUIRED_SECRETS (code needs these, "
        f"workflow doesn't check them): {sorted(missing)}\nExtra in "
        f"REQUIRED_SECRETS (workflow checks these, no code loads them): "
        f"{sorted(extra)}"
    )
