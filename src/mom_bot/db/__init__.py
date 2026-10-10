"""SQLAlchemy declarative base and engine factory for mom-bot.

This module establishes the shared ``Base`` that all ORM models in Epic 1+
must inherit from, and provides ``build_session_factory`` for constructing
a SQLAlchemy session factory with AAD-token injection for Postgres.

Usage::

    from mom_bot.db import Base, build_session_factory

    class MyModel(Base):
        __tablename__ = "my_model"
        ...
"""

from __future__ import annotations

import os
from typing import Any

from azure.identity import ManagedIdentityCredential
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

__all__ = ["Base", "build_engine", "build_session_factory"]


class Base(DeclarativeBase):
    """Shared declarative base for all mom-bot ORM models.

    All SQLAlchemy model classes should inherit from this ``Base`` so that
    Alembic can discover them via ``Base.metadata`` and generate accurate
    autogenerate migrations.

    This class is intentionally empty — model definitions live in their
    respective feature modules (Epic 1+).
    """


# ---------------------------------------------------------------------------
# Engine factory — added in Phase 3 (#91). The Base class above remains
# unchanged; migrations/env.py imports it as `from mom_bot.db import Base`.
# ---------------------------------------------------------------------------

_OSSDB_AAD_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"

# pool_recycle ceiling: observed token TTL is 86 min (5160 s). Use 4800 s
# (80 min) for a 6-min safety margin. Citation:
# docs/spike/2026-05-17-postgres-aad-findings.md § Charge 3.
_POOL_RECYCLE_SECONDS = 4800


def build_engine(
    db_url: str,
    *,
    aad_client_id: str | None = None,
    authentication: str | None = None,
    **engine_options: Any,
) -> Engine:
    """Create an engine with an explicitly selected PostgreSQL authentication path.

    MOM_BOT_DATABASE_AUTH defaults to azure_identity, preserving runtime behavior.
    Password mode never constructs Azure credentials or installs a token hook.
    No authentication failure triggers a fallback. SQLite remains available for tests.
    """
    mode = (
        authentication
        if authentication is not None
        else os.environ.get("MOM_BOT_DATABASE_AUTH", "azure_identity")
    )
    if mode not in {"azure_identity", "password"}:
        raise RuntimeError("MOM_BOT_DATABASE_AUTH must be azure_identity or password")
    url = make_url(db_url)
    postgres = url.get_backend_name() == "postgresql"
    options: dict[str, Any] = {"echo": False, **engine_options}
    client_id = aad_client_id or os.environ.get("AZURE_CLIENT_ID")
    if postgres:
        if mode == "azure_identity" and not client_id:
            raise RuntimeError(
                "AZURE_CLIENT_ID must be set (or aad_client_id passed) for Azure PostgreSQL"
            )
        if mode == "password" and (not url.username or not url.password):
            raise RuntimeError("Password-authenticated PostgreSQL requires a username and password")
        options.setdefault("pool_pre_ping", True)
        if "poolclass" not in options:
            # Two overlapping runtime revisions use at most 20 connections,
            # leaving headroom below Azure B1ms's 35-connection ceiling.
            options.setdefault("pool_size", 5)
            options.setdefault("max_overflow", 5)
        if mode == "azure_identity":
            # Recycle physical connections before the observed 86-minute token
            # TTL. A session checkout alone does not invoke the do_connect hook.
            options.setdefault("pool_recycle", _POOL_RECYCLE_SECONDS)
    engine = create_engine(db_url, **options)
    if postgres and mode == "azure_identity":
        credential = ManagedIdentityCredential(client_id=client_id)

        @event.listens_for(engine, "do_connect")
        def _inject_aad_token(  # type: ignore[no-untyped-def]
            dialect, conn_rec, cargs, cparams
        ) -> None:
            token = credential.get_token(_OSSDB_AAD_SCOPE)
            cparams["password"] = token.token

    return engine


def build_session_factory(
    db_url: str,
    *,
    aad_client_id: str | None = None,
    authentication: str | None = None,
) -> sessionmaker[Session]:
    """Build runtime sessions using the shared explicit authentication selection."""
    return sessionmaker(
        bind=build_engine(db_url, aad_client_id=aad_client_id, authentication=authentication)
    )
