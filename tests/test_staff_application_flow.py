import asyncio
import types
from unittest.mock import AsyncMock, Mock

import pytest

import bot


class DummyChannel:
    def __init__(self, channel_id: int):
        self.id = channel_id
        self.sent = []
        self.permissions_for_result = types.SimpleNamespace(send_messages=True, embed_links=True)

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return types.SimpleNamespace(id=1234, channel=self)

    def permissions_for(self, member):
        return self.permissions_for_result


class DummyTextChannel(DummyChannel):
    pass


class DummyInteraction:
    def __init__(self):
        self.user = types.SimpleNamespace(id=42, mention="<@42>", __str__=lambda self: "Tester")
        self.guild = None
        self.channel = DummyChannel(1)
        self.client = types.SimpleNamespace(get_channel=lambda _id: self.channel)
        self.response = types.SimpleNamespace(
            send_message=AsyncMock(),
            defer=AsyncMock(),
            is_done=lambda: False,
        )
        self.followup = types.SimpleNamespace(send=AsyncMock())


@pytest.mark.asyncio
async def test_publish_staff_application_posts_to_staff_and_pick_channels(monkeypatch):
    monkeypatch.setattr(bot.discord, "TextChannel", DummyTextChannel)

    staff_channel = DummyTextChannel(1)
    pick_channel = DummyTextChannel(2)
    interaction = DummyInteraction()
    interaction.client = types.SimpleNamespace(get_channel=lambda channel_id: {
        bot.STAFF_APP_CHANNEL_ID: staff_channel,
        bot.STAFF_PICK_CHANNEL_ID: pick_channel,
    }.get(channel_id))

    original_add_view = bot.bot.add_view
    bot.bot.add_view = Mock()
    try:
        await bot._publish_staff_application(
            interaction,
            pick_channel=pick_channel,
            answers={"Q1": "answer"},
        )
    finally:
        bot.bot.add_view = original_add_view

    assert len(staff_channel.sent) == 0
    assert len(pick_channel.sent) == 1
    assert "embed" in pick_channel.sent[0]
    assert "view" in pick_channel.sent[0]


@pytest.mark.asyncio
async def test_change_name_modal_allows_any_member(monkeypatch):
    """Any member should be able to change their own nickname, not just staff/admins."""
    interaction = DummyInteraction()
    interaction.guild = types.SimpleNamespace(id=1)
    interaction.user = types.SimpleNamespace(
        id=42,
        mention="<@42>",
        guild_permissions=types.SimpleNamespace(manage_guild=False, manage_roles=False),
    )

    invoked = AsyncMock(return_value=None)
    monkeypatch.setattr(bot, "_invoke_existing_command", invoked)

    modal = bot.ChangeNameModal()
    modal.new_nick._value = "NewNick"
    await modal.on_submit(interaction)

    interaction.response.send_message.assert_not_awaited()
    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    invoked.assert_awaited_once()
    interaction.followup.send.assert_awaited_once()
    args, _kwargs = interaction.followup.send.await_args
    assert "staff" not in args[0].lower() and "admin" not in args[0].lower()
