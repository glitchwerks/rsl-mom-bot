"""Provider selection, failure isolation, and migration compatibility."""

from __future__ import annotations

import importlib.util
from unittest.mock import MagicMock, patch

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from mom_bot import config
from mom_bot.db import build_engine, build_session_factory


def portable_config(monkeypatch: pytest.MonkeyPatch, source: str):
    monkeypatch.setenv("MOM_BOT_SECRET_SOURCE", source)
    monkeypatch.setenv("MOM_BOT_ENV", "prod")
    spec = importlib.util.spec_from_file_location("portable_config_test", config.__file__)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_environment_secrets_do_not_contact_azure(monkeypatch: pytest.MonkeyPatch):
    selected = portable_config(monkeypatch, "environment")
    monkeypatch.setenv("MOM_BOT_SECRET_DISCORD_TOKEN", "synthetic-token")
    with patch.object(selected, "_get_secret_client", side_effect=AssertionError("Azure invoked")):
        assert selected.load_secret("discord-token") == "synthetic-token"
        monkeypatch.setenv("MOM_BOT_SECRET_DISCORD_TOKEN", "changed")
        assert selected.load_secret("discord-token") == "synthetic-token"


def test_missing_portable_secret_does_not_fall_back(monkeypatch: pytest.MonkeyPatch):
    selected = portable_config(monkeypatch, "environment")
    monkeypatch.delenv("MOM_BOT_SECRET_DISCORD_TOKEN", raising=False)
    with patch.object(selected, "_get_secret_client", side_effect=AssertionError("Azure invoked")):
        with pytest.raises(selected.ConfigError, match="missing or empty"):
            selected.load_secret("discord-token")


def test_files_preserve_content_and_remove_terminal_newline(monkeypatch, tmp_path):
    selected = portable_config(monkeypatch, "files")
    monkeypatch.setenv("MOM_BOT_SECRET_DIR", str(tmp_path))
    (tmp_path / "prod-discord-token").write_text(" synthetic-token \n")
    assert selected.load_secret("discord-token") == " synthetic-token "


@pytest.mark.parametrize("name", ["../token", "/etc/passwd", "", "bad/name"])
def test_file_secret_name_cannot_escape_directory(monkeypatch, tmp_path, name):
    selected = portable_config(monkeypatch, "files")
    monkeypatch.setenv("MOM_BOT_SECRET_DIR", str(tmp_path))
    with pytest.raises(selected.ConfigError, match="Invalid portable secret name"):
        selected.load_secret(name)


def test_file_symlink_cannot_escape_directory(monkeypatch, tmp_path):
    selected = portable_config(monkeypatch, "files")
    base = tmp_path / "secrets"
    base.mkdir()
    external = tmp_path / "outside"
    external.write_text("synthetic-token")
    (base / "prod-discord-token").symlink_to(external)
    monkeypatch.setenv("MOM_BOT_SECRET_DIR", str(base))
    with pytest.raises(selected.ConfigError, match="inside its directory"):
        selected.load_secret("discord-token")


def test_unknown_secret_provider_fails(monkeypatch):
    with pytest.raises(ValueError, match="MOM_BOT_SECRET_SOURCE"):
        portable_config(monkeypatch, "unexpected")


@pytest.mark.parametrize("content", [None, "", " \n"])
def test_file_secret_missing_or_empty_fails_without_azure(monkeypatch, tmp_path, content):
    selected = portable_config(monkeypatch, "files")
    monkeypatch.setenv("MOM_BOT_SECRET_DIR", str(tmp_path))
    if content is not None:
        (tmp_path / "prod-discord-token").write_text(content)
    with patch.object(selected, "_get_secret_client", side_effect=AssertionError("Azure invoked")):
        with pytest.raises(selected.ConfigError):
            selected.load_secret("discord-token")


def test_file_secret_requires_directory(monkeypatch):
    selected = portable_config(monkeypatch, "files")
    monkeypatch.delenv("MOM_BOT_SECRET_DIR", raising=False)
    with pytest.raises(selected.ConfigError, match="MOM_BOT_SECRET_DIR"):
        selected.load_secret("discord-token")


def test_password_postgres_never_constructs_azure_credentials(monkeypatch):
    monkeypatch.setenv("MOM_BOT_DATABASE_AUTH", "password")
    monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
    with patch("mom_bot.db.ManagedIdentityCredential") as credential:
        engine = build_engine("postgresql+psycopg://test:synthetic-password@localhost/test")
        assert engine.url.password == "synthetic-password"
        credential.assert_not_called()
        engine.dispose()


@pytest.mark.parametrize(
    "url", ["postgresql+psycopg://test@localhost/test", "postgresql+psycopg://localhost/test"]
)
def test_password_postgres_requires_credentials(monkeypatch, url):
    monkeypatch.setenv("MOM_BOT_DATABASE_AUTH", "password")
    with pytest.raises(RuntimeError, match="username and password"):
        build_engine(url)


def test_unknown_database_auth_fails_before_engine_creation(monkeypatch):
    monkeypatch.setenv("MOM_BOT_DATABASE_AUTH", "typo")
    with patch("mom_bot.db.create_engine") as create:
        with pytest.raises(RuntimeError, match="MOM_BOT_DATABASE_AUTH"):
            build_engine("sqlite:///:memory:")
        create.assert_not_called()


def test_azure_token_failure_does_not_use_url_password(monkeypatch):
    monkeypatch.setenv("MOM_BOT_DATABASE_AUTH", "azure_identity")
    monkeypatch.setenv("AZURE_CLIENT_ID", "synthetic-client")
    callbacks = []
    with (
        patch("mom_bot.db.create_engine", return_value=MagicMock()),
        patch("mom_bot.db.ManagedIdentityCredential") as credential,
        patch(
            "mom_bot.db.event.listens_for",
            side_effect=lambda *a: lambda fn: callbacks.append(fn) or fn,
        ),
    ):
        credential.return_value.get_token.side_effect = RuntimeError("Token acquisition failed")
        build_engine("postgresql+psycopg://test:synthetic-password@localhost/test")
        params = {"password": "synthetic-password"}
        with pytest.raises(RuntimeError, match="Token acquisition failed"):
            callbacks[0](None, None, (), params)
        assert (
            params["password"] == "synthetic-password"
        )  # hook raises; DB connection never proceeds


def test_selected_auth_migrations_share_engine_with_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("MOM_BOT_DATABASE_AUTH", "password")
    db_url = f"sqlite:///{tmp_path / 'portable.db'}"
    monkeypatch.setenv("MOM_BOT_DATABASE_URL", db_url)
    cfg = Config("alembic.ini")
    with patch("mom_bot.db.build_engine", wraps=build_engine) as shared:
        command.upgrade(cfg, "head")
        assert shared.call_count == 1
        assert shared.call_args.args[0] == db_url
    factory = build_session_factory(db_url)
    with factory() as session:
        assert session.execute(text("SELECT version_num FROM alembic_version")).scalar()
    factory.kw["bind"].dispose()
