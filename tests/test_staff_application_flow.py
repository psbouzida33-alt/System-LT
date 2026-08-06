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
            is_done=lambda: False,
        )


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
            staff_channel=staff_channel,
            pick_channel=pick_channel,
            answers={"Q1": "answer"},
        )
    finally:
        bot.bot.add_view = original_add_view

    assert len(staff_channel.sent) == 1
    assert len(pick_channel.sent) == 1
    assert "embed" in staff_channel.sent[0]
    assert "view" in staff_channel.sent[0]
    assert "embed" in pick_channel.sent[0]
    assert "view" in pick_channel.sent[0]
