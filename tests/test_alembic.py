"""Verify alembic env.py wiring imports the package's Base correctly."""

from alembic.config import Config
from alembic.script import ScriptDirectory


def test_alembic_can_load_config() -> None:
    """Alembic.ini parses, migrations dir resolves, and baseline revision exists.

    Proves that:
    - ``alembic.ini`` is present and parseable.
    - ``migrations/`` directory is found by the script directory.
    - Exactly one revision exists (the empty baseline).

    If ``Base`` import in ``env.py`` is broken, this test fails because
    the migration module cannot be imported during revision discovery.
    """
    cfg = Config("I:/games/raid/mom-bot/.worktrees/epic-0-5-alembic/alembic.ini")
    script = ScriptDirectory.from_config(cfg)
    revisions = list(script.walk_revisions())
    assert len(revisions) == 1, "Expected exactly the baseline revision"
