"""Join-to-Create temporary voice rooms for Legends Tunisia."""
import asyncio
import random
from typing import Callable

import discord
from discord.ext import commands, tasks

from config import (
    BOT_VOICE_CHANNEL_ID,
    BOY_ROLE_ID,
    COMMAND_PREFIX,
    CREATE_CHANNEL_ID,
    EMPTY_ROOM_CLEANUP_MINUTES,
    GIRL_ROLE_ID,
    OWNER_ABSENCE_SECONDS,
    STAFF_ROLE_ID,
    SUPPORT_CHANNEL_ID,
    VERIFICATION_1_ID,
    VERIFICATION_2_ID,
)

_bot: commands.Bot | None = None
_get_user_level_data: Callable[[int], dict] | None = None
_format_level_stats: Callable[[dict], str] | None = None

owners: dict[int, int] = {}
room_kinds: dict[int, str] = {}
room_nsfw_enabled: dict[int, bool] = {}
room_nsfw_pending: dict[int, tuple[str, bool]] = {}
room_nsfw_retry_tasks: dict[int, asyncio.Task] = {}
owner_transfer_tasks: dict[int, asyncio.Task] = {}
locked_rooms: set[int] = set()

LOUNGE_ROOM_NAME_PREFIX = "🎙️|"
LOUNGE_ROOM_NAME_SUFFIX = " ✓"
NSFW_ROOM_NAME_PREFIX = "🔞"

JOIN_TO_CREATE_CHANNELS = {
    CREATE_CHANNEL_ID: {
        "name": "{member}",
        "title": "HUB CENTRAL INTERFACE",
        "kind": "lounge",
    },
    SUPPORT_CHANNEL_ID: {
        "name": "Support | {member}",
        "title": "SUPPORT ROOM",
        "kind": "support",
    },
    VERIFICATION_1_ID: {
        "name": "Verify | {member}",
        "title": "VERIFICATION ROOM",
        "kind": "verification",
    },
    VERIFICATION_2_ID: {
        "name": "Verify | {member}",
        "title": "VERIFICATION ROOM",
        "kind": "verification",
    },
}

_JOIN_TO_CREATE_HUB_IDS = frozenset(JOIN_TO_CREATE_CHANNELS.keys())

PANEL_EMOJI_LOCK = discord.PartialEmoji(name="50376", id=1518983212066668675)
PANEL_EMOJI_UNLOCK = discord.PartialEmoji(name="50375", id=1518983208224559214)
PANEL_EMOJI_RENAME = discord.PartialEmoji(name="50377", id=1518983214511820850)
PANEL_EMOJI_KICK = discord.PartialEmoji(name="50378", id=1518983216575414292)
PANEL_EMOJI_LEVEL = discord.PartialEmoji(name="50379", id=1518983219372884038)
PANEL_EMOJI_CROWN = discord.PartialEmoji(name="50399", id=1519035786002038915)

PANEL_EMOJI_FALLBACKS = {
    "lock": "🔒",
    "unlock": "🔓",
    "rename": "📝",
    "kick": "👞",
    "level": "📊",
    "transfer": "👑",
}

PANEL_EMOJI_CUSTOM = {
    "lock": PANEL_EMOJI_LOCK,
    "unlock": PANEL_EMOJI_UNLOCK,
    "rename": PANEL_EMOJI_RENAME,
    "kick": PANEL_EMOJI_KICK,
    "level": PANEL_EMOJI_LEVEL,
    "transfer": PANEL_EMOJI_CROWN,
}


def format_lounge_room_name(member_name: str) -> str:
    return f"{LOUNGE_ROOM_NAME_PREFIX}{member_name}{LOUNGE_ROOM_NAME_SUFFIX}"


def _get_panel_emojis(_guild=None):
    return dict(PANEL_EMOJI_CUSTOM)


def _build_room_panel_embed(member, channel, kind="lounge"):
    description = (
        f"👑 • **Owner:** {member.mention}\n"
        f"🔊 • **Channel:** {channel.mention}\n\n"
        f"📋 • **Status:** Fully functional. Use the buttons below to manage your channel."
    )
    if kind == "support":
        description += "\n\n🛡️ • **Staff:** Staff members can join this room to help you."
    elif kind == "verification":
        description += "\n\n🛡️ • **Staff:** Please wait — a staff member will join to verify you."

    embed = discord.Embed(
        title="➕ Temporary Voice Control Panel",
        description=description,
        color=discord.Color.red(),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=f"ID: {member.id} • Temp Voice System")
    return embed


def _cancel_owner_transfer(channel_id: int):
    task = owner_transfer_tasks.pop(channel_id, None)
    if task and not task.done():
        task.cancel()


def _lounge_access_roles(guild):
    return [r for rid in (BOY_ROLE_ID, GIRL_ROLE_ID) if (r := guild.get_role(rid))]


async def _restore_room_join_permissions(channel, guild):
    kind = room_kinds.get(channel.id, "lounge")
    if kind != "lounge":
        return

    everyone = guild.default_role
    await channel.set_permissions(
        everyone,
        view_channel=False,
        connect=False,
        send_messages=False,
    )
    for role in _lounge_access_roles(guild):
        await channel.set_permissions(
            role,
            view_channel=True,
            connect=True,
            speak=True,
            send_messages=True,
        )


async def _set_room_locked(channel, *, locked: bool):
    guild = channel.guild

    if locked:
        everyone = guild.default_role
        everyone_ow = channel.overwrites_for(everyone)
        everyone_ow.connect = False
        await channel.set_permissions(everyone, overwrite=everyone_ow)

        for role in _lounge_access_roles(guild):
            role_ow = channel.overwrites_for(role)
            role_ow.connect = False
            await channel.set_permissions(role, overwrite=role_ow)

        staff_role = guild.get_role(STAFF_ROLE_ID)
        if staff_role:
            await channel.set_permissions(
                staff_role,
                view_channel=True,
                connect=True,
                speak=True,
            )

        owner_id = owners.get(channel.id)
        if owner_id:
            owner = guild.get_member(owner_id)
            if owner:
                await channel.set_permissions(
                    owner,
                    view_channel=True,
                    connect=True,
                    speak=True,
                    send_messages=True,
                    manage_channels=True,
                )

        locked_rooms.add(channel.id)
    else:
        locked_rooms.discard(channel.id)
        await _restore_room_join_permissions(channel, guild)


def _strip_nsfw_room_prefix(channel_name: str) -> str:
    if channel_name.startswith(NSFW_ROOM_NAME_PREFIX):
        return channel_name[len(NSFW_ROOM_NAME_PREFIX) :]
    return channel_name


def _channel_has_nsfw_label(channel_name: str) -> bool:
    return channel_name.startswith(NSFW_ROOM_NAME_PREFIX)


async def _apply_nsfw_room_mark(channel: discord.VoiceChannel, *, new_name: str, enabled: bool, reason: str) -> None:
    await channel.edit(name=new_name, reason=reason)
    room_nsfw_enabled[channel.id] = enabled


async def _schedule_nsfw_room_retry(guild_id: int, channel_id: int, new_name: str, enabled: bool, delay: float) -> None:
    existing = room_nsfw_retry_tasks.pop(channel_id, None)
    if existing and not existing.done():
        existing.cancel()

    room_nsfw_pending[channel_id] = (new_name, enabled)

    async def _retry() -> None:
        try:
            await asyncio.sleep(max(delay, 1))
            pending = room_nsfw_pending.get(channel_id)
            if pending != (new_name, enabled):
                return
            guild = _bot.get_guild(guild_id) if _bot else None
            if guild is None:
                return
            fresh = await guild.fetch_channel(channel_id)
            if not isinstance(fresh, discord.VoiceChannel):
                return
            await _apply_nsfw_room_mark(
                fresh,
                new_name=new_name,
                enabled=enabled,
                reason="Room 18+ label retry",
            )
            room_nsfw_pending.pop(channel_id, None)
            print(f"NSFW label retry ok for channel {channel_id}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"NSFW label retry failed for {channel_id}: {exc}")
        finally:
            room_nsfw_retry_tasks.pop(channel_id, None)

    room_nsfw_retry_tasks[channel_id] = asyncio.create_task(_retry())


async def _toggle_room_nsfw_mark(channel: discord.VoiceChannel) -> tuple[bool, str, float | None]:
    fresh = await channel.guild.fetch_channel(channel.id)
    if not isinstance(fresh, discord.VoiceChannel):
        raise TypeError("Channel is not a voice channel")

    has_label = _channel_has_nsfw_label(fresh.name)
    enabled = not has_label
    base_name = _strip_nsfw_room_prefix(fresh.name)
    new_name = f"{NSFW_ROOM_NAME_PREFIX}{base_name}"[:100] if enabled else base_name[:100]

    pending = room_nsfw_pending.pop(fresh.id, None)
    if pending:
        retry_task = room_nsfw_retry_tasks.pop(fresh.id, None)
        if retry_task and not retry_task.done():
            retry_task.cancel()

    if fresh.name == new_name:
        room_nsfw_enabled[fresh.id] = enabled
        return enabled, new_name, None

    try:
        await _apply_nsfw_room_mark(
            fresh,
            new_name=new_name,
            enabled=enabled,
            reason="Room owner toggled 18+ label",
        )
        return enabled, new_name, None
    except discord.HTTPException as exc:
        if exc.status != 429:
            raise
        retry_after = float(getattr(exc, "retry_after", 0) or 600)
        await _schedule_nsfw_room_retry(channel.guild.id, fresh.id, new_name, enabled, retry_after + 1)
        return enabled, new_name, retry_after


async def _create_join_to_create_room(member, trigger_channel):
    config = JOIN_TO_CREATE_CHANNELS.get(trigger_channel.id)
    if not config:
        return

    guild = member.guild
    category = trigger_channel.category
    everyone_role = guild.default_role
    staff_role = guild.get_role(STAFF_ROLE_ID)
    boy_role = guild.get_role(BOY_ROLE_ID)
    girl_role = guild.get_role(GIRL_ROLE_ID)

    overwrites = {
        everyone_role: discord.PermissionOverwrite(view_channel=False, connect=False, send_messages=False),
    }

    if config["kind"] == "lounge":
        if boy_role:
            overwrites[boy_role] = discord.PermissionOverwrite(
                view_channel=True, connect=True, speak=True, send_messages=True
            )
        if girl_role:
            overwrites[girl_role] = discord.PermissionOverwrite(
                view_channel=True, connect=True, speak=True, send_messages=True
            )
    elif config["kind"] in ("support", "verification") and staff_role:
        overwrites[staff_role] = discord.PermissionOverwrite(
            view_channel=True,
            connect=True,
            speak=True,
            send_messages=True,
            move_members=True,
        )

    overwrites[member] = discord.PermissionOverwrite(
        view_channel=True,
        connect=True,
        speak=True,
        send_messages=True,
        manage_channels=True,
    )

    me = guild.me
    if me:
        overwrites[me] = discord.PermissionOverwrite(
            view_channel=True,
            connect=True,
            send_messages=True,
            manage_channels=True,
            read_message_history=True,
        )

    if config["kind"] == "lounge":
        channel_name = format_lounge_room_name(member.name)
    else:
        channel_name = config["name"].format(member=member.name)

    new_channel = await guild.create_voice_channel(
        name=channel_name[:100],
        category=category,
        overwrites=overwrites,
        reason=f"Temp {config['kind']} room for {member}",
    )
    cat_name = getattr(category, "name", "none") if category else "none"
    print(f"Temp room created: {new_channel.name} → category {cat_name}")
    owners[new_channel.id] = member.id
    room_kinds[new_channel.id] = config["kind"]
    room_nsfw_enabled[new_channel.id] = False
    await member.move_to(new_channel)
    await _send_room_control_panel(new_channel, member, kind=config["kind"])


async def _apply_owner_permissions(channel, old_owner_id: int, new_owner: discord.Member):
    guild = channel.guild
    member_overwrite = discord.PermissionOverwrite(
        view_channel=True,
        connect=True,
        speak=True,
        send_messages=True,
    )
    owner_overwrite = discord.PermissionOverwrite(
        view_channel=True,
        connect=True,
        speak=True,
        send_messages=True,
        manage_channels=True,
    )
    old_member = guild.get_member(old_owner_id)
    if old_member:
        await channel.set_permissions(old_member, overwrite=member_overwrite)
    await channel.set_permissions(new_owner, overwrite=owner_overwrite)


async def _transfer_room_ownership(
    channel: discord.VoiceChannel,
    new_owner: discord.Member,
    *,
    former_owner_id: int | None = None,
    transfer_reason: str = "auto",
):
    former_owner_id = former_owner_id or owners.get(channel.id)
    _cancel_owner_transfer(channel.id)
    owners[channel.id] = new_owner.id
    if former_owner_id:
        await _apply_owner_permissions(channel, former_owner_id, new_owner)
    kind = room_kinds.get(channel.id, "lounge")
    await _send_room_control_panel(channel, new_owner, kind=kind)
    if transfer_reason == "manual":
        notice = f"👑 {new_owner.mention} is now the room owner (transferred by the previous owner)."
    else:
        notice = (
            f"👑 {new_owner.mention} is now the room owner "
            f"(previous owner did not return within {OWNER_ABSENCE_SECONDS}s)."
        )
    try:
        await channel.send(notice)
    except discord.HTTPException:
        pass


async def _owner_absence_countdown(guild_id: int, channel_id: int, former_owner_id: int):
    try:
        await asyncio.sleep(OWNER_ABSENCE_SECONDS)
    except asyncio.CancelledError:
        return

    owner_transfer_tasks.pop(channel_id, None)
    guild = _bot.get_guild(guild_id) if _bot else None
    if not guild:
        return
    channel = guild.get_channel(channel_id)
    if not channel or channel_id not in owners:
        return
    if owners.get(channel_id) != former_owner_id:
        return

    former = guild.get_member(former_owner_id)
    if former and former.voice and former.voice.channel and former.voice.channel.id == channel_id:
        return

    candidates = [m for m in channel.members if not m.bot and m.id != former_owner_id]
    if not candidates:
        return

    new_owner = random.choice(candidates)
    await _transfer_room_ownership(channel, new_owner, former_owner_id=former_owner_id)


def _schedule_owner_transfer(channel: discord.VoiceChannel, former_owner_id: int):
    if room_kinds.get(channel.id) != "lounge":
        return
    _cancel_owner_transfer(channel.id)
    task = asyncio.create_task(_owner_absence_countdown(channel.guild.id, channel.id, former_owner_id))
    owner_transfer_tasks[channel.id] = task


async def _send_room_control_panel(channel, owner, embed=None, *, kind="lounge"):
    if embed is None:
        embed = _build_room_panel_embed(owner, channel, kind)

    guild = channel.guild
    view = ControlPanelView(channel.id, emojis=_get_panel_emojis(guild))
    if _bot:
        _bot.add_view(view)

    try:
        await channel.send(
            content=f"{owner.mention} — open **chat** here to use the panel below.",
            embed=embed,
            view=view,
        )
    except discord.Forbidden:
        print(f"Cannot send control panel in {channel.name}: missing Send Messages permission")
        try:
            await owner.send(
                embed=discord.Embed(
                    title="Room created",
                    description=(
                        f"Your room **{channel.name}** is ready, but I could not post the control panel.\n"
                        "Move my bot role **above** other roles and give **Send Messages** in voice channels.\n"
                        f"Then use `{COMMAND_PREFIX}panel` inside the room chat to repost it."
                    ),
                    color=discord.Color.orange(),
                )
            )
        except discord.Forbidden:
            pass
    except discord.HTTPException as exc:
        print(f"Control panel send failed in {channel.name}: {exc.text}")
        fallback_view = ControlPanelView(channel.id, emojis=PANEL_EMOJI_FALLBACKS)
        if _bot:
            _bot.add_view(fallback_view)
        try:
            await channel.send(
                content=f"{owner.mention} — open **chat** here to use the panel below.",
                embed=embed,
                view=fallback_view,
            )
        except discord.HTTPException as retry_exc:
            print(f"Control panel fallback send failed in {channel.name}: {retry_exc.text}")


def _temp_room_kind_from_name(channel_name: str) -> str | None:
    name = _strip_nsfw_room_prefix(channel_name)
    if name.startswith(LOUNGE_ROOM_NAME_PREFIX) and name.endswith(LOUNGE_ROOM_NAME_SUFFIX):
        return "lounge"
    if name.endswith("'s Lounge"):
        return "lounge"
    if name.startswith("Support | "):
        return "support"
    if name.startswith("Verify | "):
        return "verification"
    return None


def _is_tracked_temp_room(channel_id: int) -> bool:
    return channel_id in owners or channel_id in room_kinds


def _clear_temp_room_tracking(channel_id: int):
    _cancel_owner_transfer(channel_id)
    owners.pop(channel_id, None)
    room_kinds.pop(channel_id, None)
    room_nsfw_enabled.pop(channel_id, None)
    room_nsfw_pending.pop(channel_id, None)
    retry_task = room_nsfw_retry_tasks.pop(channel_id, None)
    if retry_task and not retry_task.done():
        retry_task.cancel()
    locked_rooms.discard(channel_id)


def _infer_room_owner(channel: discord.VoiceChannel) -> discord.Member | None:
    for target, overwrite in channel.overwrites.items():
        if isinstance(target, discord.Member) and not target.bot and overwrite.manage_channels:
            return target
    humans = [m for m in channel.members if not m.bot]
    return humans[0] if humans else None


async def _delete_temp_voice_channel(channel: discord.VoiceChannel, *, reason: str):
    channel_id = channel.id
    _clear_temp_room_tracking(channel_id)
    try:
        await channel.delete(reason=reason)
    except discord.HTTPException as exc:
        print(f"Failed to delete temp room {channel.name}: {exc.text}")


async def _purge_empty_temp_rooms(guild, *, reason: str = "Empty temp voice room cleanup"):
    skip_ids = _JOIN_TO_CREATE_HUB_IDS | {BOT_VOICE_CHANNEL_ID}
    deleted_ids = set()

    for channel_id in set(owners) | set(room_kinds):
        if channel_id in skip_ids:
            continue
        channel = guild.get_channel(channel_id)
        if channel is None:
            _clear_temp_room_tracking(channel_id)
            continue
        if len(channel.members) == 0:
            await _delete_temp_voice_channel(channel, reason=reason)
            deleted_ids.add(channel_id)
            print(f"Deleted empty tracked temp room: {channel.name}")

    scanned_categories = set()
    for hub_id in _JOIN_TO_CREATE_HUB_IDS:
        hub = guild.get_channel(hub_id)
        if not hub or not hub.category:
            continue
        category = hub.category
        if category.id in scanned_categories:
            continue
        scanned_categories.add(category.id)

        for voice_channel in category.voice_channels:
            if voice_channel.id in skip_ids or voice_channel.id in deleted_ids:
                continue
            kind = _temp_room_kind_from_name(voice_channel.name)
            if kind is None:
                continue
            if len(voice_channel.members) == 0:
                await _delete_temp_voice_channel(voice_channel, reason=reason)
                print(f"Deleted empty temp room: {voice_channel.name}")


async def _cleanup_and_register_temp_rooms(guild):
    await _purge_empty_temp_rooms(guild, reason="Empty temp voice room cleanup after bot restart")

    skip_ids = _JOIN_TO_CREATE_HUB_IDS | {BOT_VOICE_CHANNEL_ID}
    scanned_categories = set()

    for hub_id in _JOIN_TO_CREATE_HUB_IDS:
        hub = guild.get_channel(hub_id)
        if not hub or not hub.category:
            continue
        category = hub.category
        if category.id in scanned_categories:
            continue
        scanned_categories.add(category.id)

        for voice_channel in category.voice_channels:
            if voice_channel.id in skip_ids:
                continue
            kind = _temp_room_kind_from_name(voice_channel.name)
            if kind is None:
                continue
            if len(voice_channel.members) == 0:
                continue

            owner = _infer_room_owner(voice_channel)
            if owner:
                owners[voice_channel.id] = owner.id
            room_kinds[voice_channel.id] = kind
            room_nsfw_enabled[voice_channel.id] = voice_channel.name.startswith(NSFW_ROOM_NAME_PREFIX)
            print(
                f"Re-registered temp room: {voice_channel.name} "
                f"(kind={kind}, owner={owner.display_name if owner else 'unknown'})"
            )


def _is_bot_managed_voice_room(channel):
    return isinstance(channel, discord.VoiceChannel) and (
        channel.id in owners or channel.id in room_kinds
    )


async def _purge_channel_messages(channel):
    total = 0
    while True:
        deleted = await channel.purge(limit=100)
        total += len(deleted)
        if len(deleted) < 100:
            break
    return total


class KickUserSelect(discord.ui.Select):
    def __init__(self, channel):
        self.channel = channel
        options = [
            discord.SelectOption(label=member.display_name, value=str(member.id), emoji="👤")
            for member in channel.members
            if not member.bot
        ]
        if not options:
            options = [discord.SelectOption(label="No other members inside the room", value="none", disabled=True)]
        super().__init__(placeholder="Select a member to kick out...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            return await interaction.response.send_message("There is no one here to kick!", ephemeral=True)
        member_id = int(self.values[0])
        member = interaction.guild.get_member(member_id)
        if member and member.voice and member.voice.channel.id == self.channel.id:
            await member.move_to(None)
            await interaction.response.send_message(f"**{member.display_name}** has been kicked.", ephemeral=True)
        else:
            await interaction.response.send_message("User is no longer in your room.", ephemeral=True)


class KickView(discord.ui.View):
    def __init__(self, channel):
        super().__init__(timeout=60)
        self.add_item(KickUserSelect(channel))


class TransferOwnerSelect(discord.ui.Select):
    def __init__(self, channel, owner_id: int):
        self.channel = channel
        self.owner_id = owner_id
        options = [
            discord.SelectOption(label=member.display_name, value=str(member.id), emoji=PANEL_EMOJI_CROWN)
            for member in channel.members
            if not member.bot and member.id != owner_id
        ]
        if not options:
            options = [discord.SelectOption(label="No other members in the room", value="none", disabled=True)]
        super().__init__(placeholder="Select the new room owner...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            return await interaction.response.send_message(
                "There is no one else in the room to transfer ownership to.",
                ephemeral=True,
            )
        if interaction.user.id != owners.get(self.channel.id):
            return await interaction.response.send_message(
                "Only the room owner can transfer ownership.",
                ephemeral=True,
            )
        member_id = int(self.values[0])
        new_owner = interaction.guild.get_member(member_id)
        if not new_owner or not new_owner.voice or new_owner.voice.channel.id != self.channel.id:
            return await interaction.response.send_message("That user is no longer in your room.", ephemeral=True)
        await _transfer_room_ownership(
            self.channel,
            new_owner,
            former_owner_id=interaction.user.id,
            transfer_reason="manual",
        )
        await interaction.response.send_message(
            f"Ownership transferred to **{new_owner.display_name}**.",
            ephemeral=True,
        )


class TransferOwnerView(discord.ui.View):
    def __init__(self, channel, owner_id: int):
        super().__init__(timeout=60)
        self.add_item(TransferOwnerSelect(channel, owner_id))


class RenameModal(discord.ui.Modal, title="Change Room Name"):
    channel_name = discord.ui.TextInput(label="New Room Name", placeholder="Enter channel name...", max_length=30, required=True)

    def __init__(self, channel):
        super().__init__()
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        await self.channel.edit(name=self.channel_name.value)
        await interaction.response.send_message(f"Room name updated to: **{self.channel_name.value}**", ephemeral=True)


class ControlPanelView(discord.ui.View):
    def __init__(self, channel_id: int, emojis=None):
        super().__init__(timeout=None)
        self.channel_id = channel_id
        emojis = emojis or PANEL_EMOJI_FALLBACKS

        lock_btn = discord.ui.Button(
            label="Lock", style=discord.ButtonStyle.secondary, emoji=emojis["lock"],
            custom_id=f"ltvoice:lock:{channel_id}", row=0,
        )
        lock_btn.callback = self.lock_button
        self.add_item(lock_btn)

        unlock_btn = discord.ui.Button(
            label="Unlock", style=discord.ButtonStyle.secondary, emoji=emojis["unlock"],
            custom_id=f"ltvoice:unlock:{channel_id}", row=0,
        )
        unlock_btn.callback = self.unlock_button
        self.add_item(unlock_btn)

        rename_btn = discord.ui.Button(
            label="Rename", style=discord.ButtonStyle.secondary, emoji=emojis["rename"],
            custom_id=f"ltvoice:rename:{channel_id}", row=0,
        )
        rename_btn.callback = self.rename_button
        self.add_item(rename_btn)

        kick_btn = discord.ui.Button(
            label="Kick", style=discord.ButtonStyle.secondary, emoji=emojis["kick"],
            custom_id=f"ltvoice:kick:{channel_id}", row=1,
        )
        kick_btn.callback = self.kick_button
        self.add_item(kick_btn)

        transfer_btn = discord.ui.Button(
            label="Transfer", style=discord.ButtonStyle.secondary, emoji=PANEL_EMOJI_CROWN,
            custom_id=f"ltvoice:transfer:{channel_id}", row=1,
        )
        transfer_btn.callback = self.transfer_button
        self.add_item(transfer_btn)

        level_btn = discord.ui.Button(
            label="Check Level", style=discord.ButtonStyle.secondary, emoji=emojis["level"],
            custom_id=f"ltvoice:level:{channel_id}", row=2,
        )
        level_btn.callback = self.check_level_button
        self.add_item(level_btn)

        nsfw_btn = discord.ui.Button(
            label="18+", style=discord.ButtonStyle.secondary, emoji="🔞",
            custom_id=f"ltvoice:nsfw:{channel_id}", row=2,
        )
        nsfw_btn.callback = self.nsfw_button
        self.add_item(nsfw_btn)

    def _get_channel(self, interaction: discord.Interaction):
        return interaction.guild.get_channel(self.channel_id)

    async def lock_button(self, interaction: discord.Interaction):
        channel = self._get_channel(interaction)
        if not channel:
            return await interaction.response.send_message("Room no longer exists.", ephemeral=True)
        if interaction.user.id != owners.get(self.channel_id):
            return await interaction.response.send_message("Only the room creator can use these controls.", ephemeral=True)
        await _set_room_locked(channel, locked=True)
        await interaction.response.send_message(
            "Room locked — **@everyone** cannot join. Only **Staff** and the room owner can connect.",
            ephemeral=True,
        )

    async def unlock_button(self, interaction: discord.Interaction):
        channel = self._get_channel(interaction)
        if not channel:
            return await interaction.response.send_message("Room no longer exists.", ephemeral=True)
        if interaction.user.id != owners.get(self.channel_id):
            return await interaction.response.send_message("Only the room creator can use these controls.", ephemeral=True)
        await _set_room_locked(channel, locked=False)
        await interaction.response.send_message("Room unlocked — others can join again.", ephemeral=True)

    async def rename_button(self, interaction: discord.Interaction):
        channel = self._get_channel(interaction)
        if not channel:
            return await interaction.response.send_message("Room no longer exists.", ephemeral=True)
        if interaction.user.id != owners.get(self.channel_id):
            return await interaction.response.send_message("Only the room creator can use these controls.", ephemeral=True)
        await interaction.response.send_modal(RenameModal(channel))

    async def kick_button(self, interaction: discord.Interaction):
        channel = self._get_channel(interaction)
        if not channel:
            return await interaction.response.send_message("Room no longer exists.", ephemeral=True)
        if interaction.user.id != owners.get(self.channel_id):
            return await interaction.response.send_message("Only the room creator can use these controls.", ephemeral=True)
        await interaction.response.send_message("Choose who to disconnect:", view=KickView(channel), ephemeral=True)

    async def transfer_button(self, interaction: discord.Interaction):
        channel = self._get_channel(interaction)
        if not channel:
            return await interaction.response.send_message("Room no longer exists.", ephemeral=True)
        if interaction.user.id != owners.get(self.channel_id):
            return await interaction.response.send_message("Only the room owner can use these controls.", ephemeral=True)
        others = [m for m in channel.members if not m.bot and m.id != interaction.user.id]
        if not others:
            return await interaction.response.send_message(
                "There is no one else in the room to transfer ownership to.",
                ephemeral=True,
            )
        await interaction.response.send_message(
            "Choose who gets ownership:",
            view=TransferOwnerView(channel, interaction.user.id),
            ephemeral=True,
        )

    async def check_level_button(self, interaction: discord.Interaction):
        if _get_user_level_data and _format_level_stats:
            user_data = _get_user_level_data(interaction.user.id)
            embed_stats = discord.Embed(
                title="YOUR LIVE STATS",
                description=f"Hello {interaction.user.mention}!\n\n{_format_level_stats(user_data)}",
                color=discord.Color.blue(),
            )
            embed_stats.set_thumbnail(url=interaction.user.display_avatar.url)
            await interaction.response.send_message(embed=embed_stats, ephemeral=True)
        else:
            await interaction.response.send_message(
                f"Use `{COMMAND_PREFIX}level` in any text channel.",
                ephemeral=True,
            )

    async def nsfw_button(self, interaction: discord.Interaction):
        channel = self._get_channel(interaction)
        if not channel:
            return await interaction.response.send_message("Room no longer exists.", ephemeral=True)
        if interaction.user.id != owners.get(self.channel_id):
            return await interaction.response.send_message("Only the room creator can use these controls.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        try:
            enabled, new_name, retry_after = await _toggle_room_nsfw_mark(channel)
        except discord.Forbidden:
            return await interaction.followup.send(
                "I cannot rename this room (check **Manage Channels**).",
                ephemeral=True,
            )
        except discord.HTTPException as exc:
            return await interaction.followup.send(f"Could not update room: {exc.text}", ephemeral=True)
        except Exception as exc:
            print(f"NSFW toggle failed for {self.channel_id}: {exc}")
            return await interaction.followup.send(f"Could not update room: {exc}", ephemeral=True)
        if retry_after is not None:
            mins = max(1, int((retry_after + 59) // 60))
            action = "ytetzad" if enabled else "yetsala7"
            return await interaction.followup.send(
                f"⏳ Discord y7eb limit esm el room (2 marrat / 10 min).\n"
                f"🔞 Label **{action}** automatiquement ba3d ~**{mins} min**. Ma tclickich kter.",
                ephemeral=True,
            )
        if enabled:
            msg = f"🔞 **18+** label added — room is now **{new_name}**"
        else:
            msg = f"🔞 **18+** label removed — room is now **{new_name}**"
        await interaction.followup.send(msg, ephemeral=True)


@tasks.loop(minutes=EMPTY_ROOM_CLEANUP_MINUTES)
async def empty_temp_rooms_cleanup_task():
    for guild in _bot.guilds if _bot else []:
        try:
            await _purge_empty_temp_rooms(guild, reason="Empty temp voice room periodic cleanup")
        except Exception as e:
            print(f"Empty temp room cleanup failed for {guild.name}: {e}")


@empty_temp_rooms_cleanup_task.before_loop
async def before_empty_temp_rooms_cleanup_task():
    if _bot:
        await _bot.wait_until_ready()


async def startup_voice_rooms(bot: commands.Bot):
    for guild in bot.guilds:
        try:
            await _cleanup_and_register_temp_rooms(guild)
            for channel_id in list(room_kinds.keys()):
                ch = guild.get_channel(channel_id)
                emojis = _get_panel_emojis(guild) if ch else PANEL_EMOJI_FALLBACKS
                bot.add_view(ControlPanelView(channel_id, emojis=emojis))
        except Exception as e:
            print(f"Voice rooms init failed for {guild.name}: {e}")
    if not empty_temp_rooms_cleanup_task.is_running():
        empty_temp_rooms_cleanup_task.start()
    print("Voice rooms system online.")


async def handle_voice_state_update(member, before, after):
    if after.channel and after.channel.id in JOIN_TO_CREATE_CHANNELS:
        await _create_join_to_create_room(member, after.channel)

    if after.channel and after.channel.id in owners and member.id == owners[after.channel.id]:
        _cancel_owner_transfer(after.channel.id)

    if (
        before.channel
        and before.channel.id in owners
        and member.id == owners.get(before.channel.id)
        and len(before.channel.members) > 0
    ):
        _schedule_owner_transfer(before.channel, member.id)

    if (
        before.channel
        and _is_tracked_temp_room(before.channel.id)
        and len(before.channel.members) == 0
    ):
        await _delete_temp_voice_channel(before.channel, reason="Empty temp voice room cleanup")


def setup_voice_rooms(
    bot: commands.Bot,
    *,
    get_user_level_data: Callable[[int], dict] | None = None,
    format_level_stats: Callable[[dict], str] | None = None,
):
    global _bot, _get_user_level_data, _format_level_stats
    _bot = bot
    _get_user_level_data = get_user_level_data
    _format_level_stats = format_level_stats

    @bot.listen("on_voice_state_update")
    async def _voice_rooms_listener(member, before, after):
        await handle_voice_state_update(member, before, after)

    @bot.command(name="panel", aliases=["controlpanel", "roompanel"])
    async def repost_panel_cmd(ctx):
        if not ctx.author.voice or not ctx.author.voice.channel:
            return await ctx.send(
                f"Join your voice room first, then run `{COMMAND_PREFIX}panel` in its chat.",
                delete_after=10,
            )
        channel = ctx.author.voice.channel
        if channel.id not in owners and channel.id not in room_kinds:
            return await ctx.send("This voice channel is not a bot-managed room.", delete_after=10)
        owner_id = owners.get(channel.id, ctx.author.id)
        if ctx.author.id != owner_id:
            return await ctx.send("Only the room owner can repost the control panel.", delete_after=10)
        owners.setdefault(channel.id, ctx.author.id)
        config_kind = room_kinds.get(channel.id, "lounge")
        await _send_room_control_panel(channel, ctx.author, kind=config_kind)
        try:
            await ctx.message.delete()
        except discord.Forbidden:
            pass

    @bot.command(name="clear", aliases=["clearchat", "purgechat", "purge"])
    async def clear_chat_cmd(ctx):
        channel = ctx.channel
        if not isinstance(channel, (discord.TextChannel, discord.VoiceChannel, discord.Thread)):
            return await ctx.send("Use this in a text or voice chat.", delete_after=8)

        if _is_bot_managed_voice_room(channel):
            owner_id = owners.get(channel.id, ctx.author.id)
            if ctx.author.id != owner_id and not ctx.author.guild_permissions.manage_messages:
                return await ctx.send("Only the room owner can clear this chat.", delete_after=8)
        elif not ctx.author.guild_permissions.manage_messages:
            return await ctx.send("You need **Manage Messages** to clear this channel.", delete_after=8)

        if not ctx.guild.me.guild_permissions.manage_messages:
            return await ctx.send("I need **Manage Messages** to clear chat.", delete_after=8)

        try:
            try:
                await ctx.message.delete()
            except discord.Forbidden:
                pass
            removed = await _purge_channel_messages(channel)
            msg = f"Chat cleared by **{ctx.author.display_name}** ({removed} message(s) removed)."
            if _is_bot_managed_voice_room(channel):
                msg += f"\nUse `{COMMAND_PREFIX}panel` to repost the control panel."
            await channel.send(msg, delete_after=5)
        except discord.Forbidden:
            await channel.send("I cannot delete messages here.", delete_after=8)
        except discord.HTTPException as exc:
            await channel.send(f"Could not clear chat: {exc.text}", delete_after=8)
