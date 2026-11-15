"""Disposable reconciliation of joins missed during a portable pause."""

import asyncio
import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from mom_bot.db import Base
from mom_bot.main import MomBot, build_intents
from mom_bot.member_activity.service import MemberActivityService


@pytest.mark.asyncio
async def test_resume_scan_claims_only_missed_join_once():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    bot = MomBot(build_intents())
    bot.guild = discord.Object(id=123)
    bot._db_session_factory = factory
    resumed = datetime.datetime(2026, 11, 15, 12, 0, tzinfo=datetime.UTC)
    window = (resumed - datetime.timedelta(minutes=10), resumed)

    missed = MagicMock(spec=discord.Member)
    missed.id = 456
    missed.bot = False
    missed.mention = "<@456>"
    missed.joined_at = resumed - datetime.timedelta(minutes=5)
    missed.guild.id = 123
    old = MagicMock(spec=discord.Member)
    old.id = 789
    old.bot = False
    old.joined_at = resumed - datetime.timedelta(days=2)
    old.guild.id = 123

    guild = MagicMock(spec=discord.Guild)
    guild.id = 123

    async def members(*, limit):
        assert limit is None
        yield old
        yield missed

    guild.fetch_members = members
    channel = MagicMock()
    channel.send = AsyncMock()
    with (
        patch("mom_bot.main.maintenance_gate.enabled", True),
        patch("mom_bot.main.maintenance_gate.begin", return_value=True),
        patch("mom_bot.main.maintenance_gate.end"),
        patch("mom_bot.main.maintenance_gate.reconciliation_window", return_value=window),
        patch("mom_bot.main.load_secret", return_value="999"),
        patch.object(bot, "get_channel", return_value=channel),
        patch("mom_bot.main.NewMemberAlertService.list_subscriber_ids", return_value=[]),
    ):
        assert await bot._reconcile_joins_once(guild, window) is True
        assert await bot._reconcile_joins_once(guild, window) is True

    restarted = MomBot(build_intents())
    restarted.guild = discord.Object(id=123)
    restarted._db_session_factory = factory
    with (
        patch("mom_bot.main.maintenance_gate.enabled", True),
        patch("mom_bot.main.maintenance_gate.begin", return_value=True),
        patch("mom_bot.main.maintenance_gate.end"),
        patch("mom_bot.main.maintenance_gate.reconciliation_window", return_value=window),
        patch("mom_bot.main.load_secret", return_value="999"),
        patch.object(restarted, "get_channel", return_value=channel),
        patch("mom_bot.main.NewMemberAlertService.list_subscriber_ids", return_value=[]),
    ):
        assert await restarted._reconcile_joins_once(guild, window) is True

    channel.send.assert_awaited_once()
    tracked = MemberActivityService(factory).get_tracking(123, 456)
    assert tracked is not None
    assert tracked.joined_at == missed.joined_at.replace(tzinfo=None)
    assert MemberActivityService(factory).get_tracking(123, 789) is None


@pytest.mark.asyncio
async def test_slow_welcome_does_not_block_second_join_activity():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    bot = MomBot(build_intents())
    bot.guild = discord.Object(id=123)
    bot._db_session_factory = factory
    first, second = MagicMock(spec=discord.Member), MagicMock(spec=discord.Member)
    for member, identifier in ((first, 456), (second, 789)):
        member.id = identifier
        member.bot = False
        member.mention = f"<@{identifier}>"
        member.guild.id = 123
        member.joined_at = datetime.datetime.now(datetime.UTC)
    first_send_started, release_first = asyncio.Event(), asyncio.Event()

    async def send(content):
        if "<@456>" in content:
            first_send_started.set()
            await release_first.wait()

    channel = MagicMock()
    channel.send = AsyncMock(side_effect=send)
    with (
        patch("mom_bot.main.load_secret", return_value="999"),
        patch.object(bot, "get_channel", return_value=channel),
        patch("mom_bot.main.NewMemberAlertService.list_subscriber_ids", return_value=[]),
    ):
        first_task = asyncio.create_task(bot.on_member_join(first))
        await asyncio.wait_for(first_send_started.wait(), timeout=2)
        await asyncio.wait_for(bot.on_member_join(second), timeout=2)
        message = MagicMock(spec=discord.Message)
        message.author.id = 789
        message.author.bot = False
        message.guild.id = 123
        message.created_at = datetime.datetime.now(datetime.UTC)
        await bot.on_message(message)
        release_first.set()
        await first_task

    tracked = MemberActivityService(factory).get_tracking(123, 789)
    assert tracked is not None and tracked.first_message_at is not None
