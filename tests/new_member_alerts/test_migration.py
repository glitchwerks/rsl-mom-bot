"""Alembic migration tests for the ``new_member_alert_subscription`` table.

Verifies that running ``alembic upgrade head`` creates a
``new_member_alert_subscription`` table with ``guild_id``/``user_id``
columns and a UNIQUE constraint on ``(guild_id, user_id)`` — the
"Subscription state persisted per (guild_id, user_id)" requirement of
issue #301 — mirroring the schema-test conventions in
``tests/test_alembic.py`` (``TestRemindersSchema``) and the
``member_notification``/``member_notification_sent`` table pattern
(migration ``b3_member_notifications``).

Deliberately storage-shape neutral: only column *presence* (``issubset``,
not an exact set) and the UNIQUE pair are asserted, not a full column
list or the absence of any particular column — so an implementer who adds
e.g. a soft ``enabled`` flag isn't broken by a correct implementation
(mirrors the test_alembic.py convention of asserting ``expected.issubset``
rather than set equality).

Binding assumption for the UNIQUE-constraint test below: the raw INSERT
supplies only ``guild_id``/``user_id``, so any other NOT NULL column the
migration adds must carry a server default or be nullable — consistent
with how ``member_notification``'s ``id``/``created_at``/``updated_at``
(and ``enabled``, via ``server_default``) already work in migration
``b3_member_notifications``.
"""

from __future__ import annotations

import logging.config
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

# Resolve alembic.ini relative to this test file (two levels under tests/)
# so the path works on both Windows dev machines and Linux CI runners.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_ALEMBIC_INI = str(_REPO_ROOT / "alembic.ini")


# ---------------------------------------------------------------------------
# Module-scoped autouse fixture: prevent alembic from disabling loggers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True, scope="module")
def _no_disable_existing_loggers() -> Any:
    """Force ``disable_existing_loggers=False`` on every ``fileConfig`` call.

    ``alembic.command.upgrade``/``downgrade`` invoke ``migrations/env.py``,
    which calls ``logging.config.fileConfig(alembic_ini)``.  The stdlib
    default for ``disable_existing_loggers`` is ``True``, which sets
    ``.disabled = True`` on every logger that already existed at call time —
    process-wide, for the rest of the pytest session. This silently breaks
    unrelated tests elsewhere in the suite that rely on ``caplog`` capturing
    records from loggers such as ``mom_bot.roles``/``mom_bot.sidecar``.

    This fixture wraps ``fileConfig`` so the ``disable_existing_loggers``
    argument is always ``False``, without touching ``env.py`` or the
    production logging configuration. Mirrors the identical fixture in
    ``tests/test_alembic.py``, kept as a local copy here so this module
    independently neutralizes the side effect regardless of test-collection
    order or which other alembic test modules are present.
    """
    _real_fileConfig = logging.config.fileConfig

    def _patched_fileConfig(fname: Any, *args: Any, **kwargs: Any) -> None:
        """Delegate to real fileConfig with disable_existing_loggers=False."""
        kwargs["disable_existing_loggers"] = False
        _real_fileConfig(fname, *args, **kwargs)

    with patch("logging.config.fileConfig", side_effect=_patched_fileConfig):
        yield


def _make_alembic_config(db_path: str) -> Config:
    """Create an Alembic Config pointed at a temp SQLite database.

    Args:
        db_path: Filesystem path to the SQLite database file.

    Returns:
        An Alembic ``Config`` instance with the URL override applied.
    """
    cfg = Config(_ALEMBIC_INI)
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _get_table_names(engine: sa.Engine) -> list[str]:
    """Return all table names in the database."""
    inspector = sa.inspect(engine)
    return sorted(inspector.get_table_names())


def _get_column_names(engine: sa.Engine, table: str) -> list[str]:
    """Return column names for ``table``."""
    inspector = sa.inspect(engine)
    return [col["name"] for col in inspector.get_columns(table)]


def test_new_member_alert_subscription_table_created_after_upgrade_head(
    tmp_path: Path,
) -> None:
    """``new_member_alert_subscription`` exists after ``alembic upgrade head``.

    Args:
        tmp_path: pytest-supplied temp directory.
    """
    db_file = str(tmp_path / "test.db")
    cfg = _make_alembic_config(db_file)
    command.upgrade(cfg, "head")

    engine = sa.create_engine(f"sqlite:///{db_file}")
    tables = _get_table_names(engine)
    engine.dispose()

    assert (
        "new_member_alert_subscription" in tables
    ), f"Expected 'new_member_alert_subscription' table; found: {tables}"


def test_new_member_alert_subscription_has_guild_and_user_columns(
    tmp_path: Path,
) -> None:
    """``guild_id`` and ``user_id`` columns are present after upgrade.

    Args:
        tmp_path: pytest-supplied temp directory.
    """
    db_file = str(tmp_path / "test.db")
    cfg = _make_alembic_config(db_file)
    command.upgrade(cfg, "head")

    engine = sa.create_engine(f"sqlite:///{db_file}")
    columns = _get_column_names(engine, "new_member_alert_subscription")
    engine.dispose()

    expected = {"guild_id", "user_id"}
    assert expected.issubset(set(columns)), (
        f"Missing columns in 'new_member_alert_subscription': " f"{expected - set(columns)}"
    )


def test_new_member_alert_subscription_unique_guild_and_user(
    tmp_path: Path,
) -> None:
    """UNIQUE(guild_id, user_id) rejects a duplicate row.

    Args:
        tmp_path: pytest-supplied temp directory.
    """
    db_file = str(tmp_path / "test.db")
    cfg = _make_alembic_config(db_file)
    command.upgrade(cfg, "head")

    engine = sa.create_engine(f"sqlite:///{db_file}")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO new_member_alert_subscription (guild_id, user_id) "
                "VALUES ('300000000000000001', '111111111111111111')"
            )
        )

    with pytest.raises(sa.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO new_member_alert_subscription (guild_id, user_id) "
                    "VALUES ('300000000000000001', '111111111111111111')"
                )
            )
    engine.dispose()


def test_upgrade_then_downgrade_removes_table(tmp_path: Path) -> None:
    """``upgrade head`` then ``downgrade base`` leaves no leftover table.

    Args:
        tmp_path: pytest-supplied temp directory.
    """
    db_file = str(tmp_path / "test.db")
    cfg = _make_alembic_config(db_file)

    command.upgrade(cfg, "head")
    engine = sa.create_engine(f"sqlite:///{db_file}")
    tables_after_upgrade = _get_table_names(engine)
    engine.dispose()
    assert "new_member_alert_subscription" in tables_after_upgrade

    command.downgrade(cfg, "base")
    engine = sa.create_engine(f"sqlite:///{db_file}")
    tables_after_downgrade = _get_table_names(engine)
    engine.dispose()
    assert (
        "new_member_alert_subscription" not in tables_after_downgrade
    ), "'new_member_alert_subscription' should not exist after downgrade to base"
