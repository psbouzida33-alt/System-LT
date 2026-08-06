"""
Legends Tunisia — Server Stats Bot
Updates locked voice channels: member count + live clock.
"""
import asyncio
import os
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

from config import (
    BOT_VOICE_CHANNEL_ID,
    CLOCK_ROTATION_SECONDS,
    CLOCK_UPDATE_SECONDS,
    MEMBER_COUNT_EXCLUDE_BOTS,
    STAFF_APP_CHANNEL_ID,
    STAFF_PICK_CHANNEL_ID,
    STATS_CATEGORY_NAME,
    TOKEN,
    WORLD_CLOCKS,
    NOT_VERIFY_DM_MESSAGE,
    NOT_VERIFY_ROLE_ID,
    NOT_VERIFY_ROLE_NAME,
    load_stats_config,
    save_stats_config,
    load_nick_config,
    save_nick_config,
)

load_dotenv()

if not TOKEN:
    raise SystemExit(
        "Missing bot token. Set DISCORD_BOT_TOKEN or LEVELS_BOT_TOKEN in .env"
    )

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.voice_states = True
intents.guild_messages = True
intents.message_content = True

class StatsBot(commands.Bot):
    async def setup_hook(self) -> None:
        self.add_view(NicknameRequestView())
        self.add_view(StaffAppView(self))
        self.add_view(CommentPanelView())


bot = StatsBot(
    command_prefix=["?", "!"],
    intents=intents,
    activity=discord.Activity(type=discord.ActivityType.watching, name="server stats"),
)

_last_member_count: int | None = None
_last_stats_status: str | None = None
_stats_config: dict[str, int | None] = load_stats_config()
_nick_config: dict[str, int | None] = load_nick_config()
_staff_view_registered = False

ADMIN_USER_IDS = {1511828976732209252}
STAFF_USER_IDS = {1517586424306598140}


def _can_manage_bot(ctx: commands.Context[StatsBot]) -> bool:
    if ctx.guild is None:
        return False
    author = ctx.author
    if isinstance(author, discord.Member):
        if author.guild_permissions.manage_guild or author.guild_permissions.manage_roles:
            return True
        return author.id in ADMIN_USER_IDS or author.id in STAFF_USER_IDS
    return False


def _guild_member_count(guild: discord.Guild) -> int:
    if MEMBER_COUNT_EXCLUDE_BOTS:
        return sum(1 for member in guild.members if not member.bot)
    return guild.member_count or len(guild.members)


def _stats_channel_name(count: int) -> str:
    return f"👥 • Members: {count}"


def _stats_channel_status() -> str:
    index = int(time.time() // CLOCK_ROTATION_SECONDS) % len(WORLD_CLOCKS)
    emoji, label, tz_name = WORLD_CLOCKS[index]
    now = datetime.now(ZoneInfo(tz_name))
    return f"{emoji} {label}: {now.strftime('%H:%M:%S')}"


def _extract_text_from_content(content_value: Any) -> str | None:
    if isinstance(content_value, list):
        parts: list[str] = []
        for raw_item in cast(list[Any], content_value):
            if isinstance(raw_item, dict):
                raw_dict = cast(dict[str, Any], raw_item)
                text_item = raw_dict.get("text") or raw_dict.get("content")
                text = _extract_text_from_content(text_item)
                if text:
                    parts.append(text)
            elif isinstance(raw_item, str):
                parts.append(raw_item)
        return "".join(parts).strip() if parts else None

    if isinstance(content_value, dict):
        content_dict = cast(dict[str, Any], content_value)
        text_value = content_dict.get("text")
        if isinstance(text_value, str):
            return text_value.strip()
        parts_value = content_dict.get("parts")
        if isinstance(parts_value, list):
            return _extract_text_from_content(parts_value)
        content_item = content_dict.get("content")
        return _extract_text_from_content(content_item)

    if isinstance(content_value, str):
        return content_value.strip()

    return None



def _stat_channel_overwrites(guild: discord.Guild) -> dict[discord.Role | discord.Member | discord.Object, discord.PermissionOverwrite]:
    return {
        guild.default_role: discord.PermissionOverwrite(
            connect=False,
            speak=False,
            view_channel=True,
        )
    }


def _get_configured_guild() -> discord.Guild | None:
    guild_id = _stats_config.get("guild_id")
    if not guild_id:
        return bot.guilds[0] if len(bot.guilds) == 1 else None
    return bot.get_guild(int(guild_id))


# `_resolve_member` was removed — it was previously used by the punishment modal
# which was deleted. Keep this note to avoid accidental re-adds.


async def _safe_edit_channel_name(
    channel: discord.VoiceChannel | None,
    new_name: str,
    *,
    label: str,
) -> bool:
    if channel is None:
        return False
    if channel.name == new_name:
        return True
    if len(new_name) > 100:
        new_name = new_name[:100]

    try:
        await channel.edit(name=new_name, reason=f"Stats bot — {label}")
        return True
    except discord.HTTPException as exc:
        if exc.status == 429:
            wait = float(getattr(exc, "retry_after", 5) or 5) + 0.5
            print(f"{label}: rate limited, waiting {wait:.1f}s")
            await asyncio.sleep(wait)
            try:
                await channel.edit(name=new_name, reason=f"Stats bot — {label}")
                return True
            except discord.HTTPException as retry_exc:
                print(f"{label}: rename failed after retry — {retry_exc}")
        else:
            print(f"{label}: rename failed — {exc}")
    except Exception as exc:
        print(f"{label}: rename failed — {exc}")
    return False


async def update_stats_channel(guild: discord.Guild, *, force_members: bool = False) -> None:
    """Update one voice channel: name = members, status = world clock."""
    global _last_member_count, _last_stats_status

    channel_id = _stats_config.get("stats_channel_id")
    if not channel_id:
        return

    channel = guild.get_channel(int(channel_id))
    if not isinstance(channel, discord.VoiceChannel):
        return

    count = _guild_member_count(guild)
    name = _stats_channel_name(count)
    status = _stats_channel_status()
    update_name = force_members or count != _last_member_count
    update_status = status != _last_stats_status

    if not update_name and not update_status:
        return

    try:
        if update_name:
            await channel.edit(
                name=name,
                status=status,
                reason="Stats bot — members + clock",
            )
            _last_member_count = count
            _last_stats_status = status
        else:
            await channel.edit(status=status, reason="Stats bot — clock")
            _last_stats_status = status
    except discord.HTTPException as exc:
        if exc.status == 429:
            wait = float(getattr(exc, "retry_after", 5) or 5) + 0.5
            print(f"stats channel: rate limited, waiting {wait:.1f}s")
            await asyncio.sleep(wait)
            try:
                if update_name:
                    await channel.edit(name=name, status=status, reason="Stats bot — members + clock")
                    _last_member_count = count
                    _last_stats_status = status
                else:
                    await channel.edit(status=status, reason="Stats bot — clock")
                    _last_stats_status = status
            except discord.HTTPException as retry_exc:
                print(f"stats channel update failed after retry — {retry_exc}")
        else:
            print(f"stats channel update failed — {exc}")
            combined = f"{name} | {status}"[:100]
            if await _safe_edit_channel_name(channel, combined, label="stats fallback"):
                _last_member_count = count
                _last_stats_status = status
    except TypeError:
        combined = f"{name} | {status}"[:100]
        if await _safe_edit_channel_name(channel, combined, label="stats fallback"):
            _last_member_count = count
            _last_stats_status = status
    except Exception as exc:
        print(f"stats channel update failed — {exc}")


async def refresh_all_stats(*, force_members: bool = False) -> None:
    guild = _get_configured_guild()
    if guild is None:
        return
    await update_stats_channel(guild, force_members=force_members)


class DummyVoiceClient(discord.VoiceProtocol):
    """Join voice without audio — keeps the bot visible in the lounge channel."""

    channel: discord.abc.Connectable

    def __init__(self, client, channel: discord.abc.Connectable):
        self.client = client
        self.channel = channel
        self._connected = False

    async def connect(
        self,
        *,
        timeout: float,
        reconnect: bool,
        self_deaf: bool = True,
        self_mute: bool = True,
    ) -> None:
        # discord.py passes self_deaf=False by default — always join deafened + muted.
        channel = cast(discord.abc.GuildChannel, self.channel)
        await channel.guild.change_voice_state(
            channel=channel,
            self_deaf=True,
            self_mute=True,
        )
        self._connected = True

    async def disconnect(self, *, force: bool = False) -> None:
        channel = cast(discord.abc.GuildChannel, self.channel)
        await channel.guild.change_voice_state(channel=None)
        self._connected = False
        try:
            key_id, _ = self.channel._get_voice_client_key()
            self.client._connection._remove_voice_client(key_id)
        except Exception:
            pass

    async def on_voice_state_update(self, data):
        pass

    async def on_voice_server_update(self, data):
        pass

    def is_connected(self):
        return self._connected

    def is_playing(self):
        return False

    def stop(self):
        pass


def _find_voice_lounge_channel() -> discord.VoiceChannel | None:
    channel = bot.get_channel(BOT_VOICE_CHANNEL_ID)
    if isinstance(channel, discord.VoiceChannel):
        return channel
    for guild in bot.guilds:
        found = guild.get_channel(BOT_VOICE_CHANNEL_ID)
        if isinstance(found, discord.VoiceChannel):
            return found
    return None


async def _ensure_voice_deafened(guild: discord.Guild, channel: discord.abc.GuildChannel) -> None:
    await guild.change_voice_state(channel=channel, self_deaf=True, self_mute=True)


async def _ensure_voice_unmuted(guild: discord.Guild, channel: discord.abc.GuildChannel) -> None:
    await guild.change_voice_state(channel=channel, self_deaf=False, self_mute=False)


async def _disconnect_guild_voice_client(guild: discord.Guild) -> None:
    for voice_client in bot.voice_clients:
        if getattr(voice_client, "guild", None) == guild:
            try:
                await voice_client.disconnect(force=True)
            except Exception as exc:
                print(f"Failed to disconnect voice client in {guild.id}: {exc}")
            return


async def _join_voice_lounge() -> None:
    voice_channel = _find_voice_lounge_channel()
    if voice_channel is None:
        print(f"WARNING: voice lounge channel {BOT_VOICE_CHANNEL_ID} not found.")
        return

    member = voice_channel.guild.me
    if member and member.voice and member.voice.channel:
        if member.voice.channel.id == BOT_VOICE_CHANNEL_ID:
            if not member.voice.self_deaf or not member.voice.self_mute:
                try:
                    await _ensure_voice_unmuted(voice_channel.guild, voice_channel)
                    print("Re-applied unmute/undeaf in voice lounge.")
                except Exception as exc:
                    print(f"Failed to restore audio state in voice lounge: {exc}")
            return

    try:
        await voice_channel.connect(self_deaf=False, self_mute=False)
        print(f"Connected to voice lounge (unmuted): {voice_channel.name}")
    except discord.ClientException:
        pass
    except Exception as exc:
        print(f"Failed to join voice lounge: {exc}")


async def _send_not_verify_dm(member: discord.Member) -> None:
    if member.bot or NOT_VERIFY_ROLE_ID is None:
        return
    if not any(role.id == NOT_VERIFY_ROLE_ID for role in member.roles):
        return

    try:
        message = NOT_VERIFY_DM_MESSAGE.format(
            member_name=member.display_name,
            role_name=NOT_VERIFY_ROLE_NAME,
            guild_name=member.guild.name,
        )
        await member.send(message)
        print(f"[not-verify] DM sent to {member} ({member.id})")
    except discord.Forbidden:
        print(f"[not-verify] Could not DM {member} ({member.id}) because DMs are closed")
    except discord.HTTPException as exc:
        print(f"[not-verify] Failed to DM {member} ({member.id}): {exc}")
    except Exception as exc:
        print(f"[not-verify] Unexpected DM error for {member} ({member.id}): {exc}")


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return
    await bot.process_commands(message)


@bot.event
async def on_ready():
    await bot.change_presence(status=discord.Status.dnd)
    print('Bot is ready and on DND!')
    print(f"Stats bot online as {bot.user} ({len(bot.guilds)} server(s))")
    if not _stats_config.get("stats_channel_id"):
        print("No stat channel configured yet. Run ?setupstats in your server.")
    if not update_stats_task.is_running():
        update_stats_task.start()
    await _join_voice_lounge()
    await refresh_all_stats(force_members=True)


@bot.event
async def on_interaction(interaction: discord.Interaction) -> None:
    if interaction.type != discord.InteractionType.component:
        return

    if interaction.response.is_done():
        return

    custom_id = interaction.data.get("custom_id") if isinstance(interaction.data, dict) else None
    if custom_id == "staff_app.apply":
        if interaction.guild is None:
            await interaction.response.send_message(
                "This button can only be used inside a server.",
                ephemeral=True,
            )
            return
        try:
            await interaction.response.send_modal(StaffApplicationModal())
        except Exception as exc:
            import traceback

            print(f"[staff apply] failed to open modal: {exc}")
            traceback.print_exc()
            try:
                await interaction.response.send_message(
                    f"Unable to open the staff application form right now. Error: {type(exc).__name__}",
                    ephemeral=True,
                )
            except Exception:
                pass
        return

    if custom_id == "nickrequest:open":
        if interaction.guild is None:
            await interaction.response.send_message(
                "This button can only be used inside a server.",
                ephemeral=True,
            )
            return
        try:
            await interaction.response.send_modal(ChangeNameModal())
        except Exception as exc:
            print(f"[nickname] failed to open modal: {exc}")
            try:
                await interaction.response.send_message(
                    "Unable to open nickname request modal right now. Please try again.",
                    ephemeral=True,
                )
            except Exception:
                pass
        return


@bot.event
async def on_voice_state_update(
    member: discord.Member | None,
    before: discord.VoiceState,
    after: discord.VoiceState,
):
    if member is None or bot.user is None or member.id != bot.user.id:
        return

    if after.channel and after.channel.id == BOT_VOICE_CHANNEL_ID:
        if not after.self_deaf or not after.self_mute:
            await asyncio.sleep(0.5)
            try:
                await _ensure_voice_deafened(member.guild, after.channel)
            except Exception as exc:
                print(f"Failed to enforce deafen in lounge: {exc}")
        return

    if before.channel and before.channel.id == BOT_VOICE_CHANNEL_ID:
        await asyncio.sleep(2)
        await _join_voice_lounge()


@bot.event
async def on_member_join(member: discord.Member):
    if member.guild.id != _stats_config.get("guild_id"):
        return
    await update_stats_channel(member.guild)
    await _send_not_verify_dm(member)


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    if before.guild.id != _stats_config.get("guild_id"):
        return

    had_role = any(role.id == NOT_VERIFY_ROLE_ID for role in before.roles)
    has_role = any(role.id == NOT_VERIFY_ROLE_ID for role in after.roles)
    if not had_role and has_role:
        await _send_not_verify_dm(after)


@bot.event
async def on_member_remove(member: discord.Member):
    if member.guild.id != _stats_config.get("guild_id"):
        return
    global _last_member_count
    _last_member_count = None
    await update_stats_channel(member.guild, force_members=True)


@tasks.loop(seconds=CLOCK_UPDATE_SECONDS)
async def update_stats_task():
    guild = _get_configured_guild()
    if guild is None:
        return
    await update_stats_channel(guild)


@update_stats_task.before_loop
async def before_update_stats_task():
    await bot.wait_until_ready()


@bot.event
async def on_command_error(ctx: commands.Context[StatsBot], error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You need **Manage Server** permission for this command.")
        return
    if isinstance(error, commands.CheckFailure):
        await ctx.send("You are not allowed to run this command.")
        return
    print(f"Command error ({getattr(ctx.command, 'name', '?')}): {error}")
    await ctx.send(f"Command failed: {error}")


@bot.command(name="sendnotverify")
@commands.guild_only()
@commands.check(_can_manage_bot)
async def send_not_verify_cmd(ctx: commands.Context[StatsBot], *, custom_message: str | None = None):
    """DM every member who currently has the configured not-verify role.

    Usage: `?sendnotverify` to resend the default not-verify message,
    or `?sendnotverify <message>` to send a custom DM to the role members.
    """
    guild = ctx.guild
    if guild is None:
        await ctx.send("This command can only be used in a server.")
        return

    if NOT_VERIFY_ROLE_ID is None:
        await ctx.send("No not-verify role ID is configured.")
        return

    role = guild.get_role(NOT_VERIFY_ROLE_ID)
    if role is None:
        await ctx.send("The configured not-verify role was not found in this server.")
        return

    if custom_message is None or not custom_message.strip():
        custom_message = NOT_VERIFY_DM_MESSAGE.format(
            member_name="{member_name}",
            role_name=NOT_VERIFY_ROLE_NAME,
            guild_name=guild.name,
        )
        use_template = True
    else:
        custom_message = custom_message.strip()
        use_template = False

    sent = 0
    failed = 0
    for member in role.members:
        if member.bot:
            continue
        try:
            if use_template:
                await member.send(
                    NOT_VERIFY_DM_MESSAGE.format(
                        member_name=member.display_name,
                        role_name=NOT_VERIFY_ROLE_NAME,
                        guild_name=guild.name,
                    )
                )
            else:
                await member.send(custom_message)
            sent += 1
        except discord.Forbidden:
            failed += 1
        except discord.HTTPException:
            failed += 1

    await ctx.send(f"Sent message to {sent} members. Failed: {failed}.")


@bot.command(name="setupstats")
@commands.guild_only()
@commands.check(_can_manage_bot)
async def setup_stats_cmd(ctx: commands.Context[StatsBot]):
    """Create one stats voice channel: name = members, status = world clock."""
    guild = ctx.guild
    assert guild is not None
    overwrites = _stat_channel_overwrites(guild)
    count = _guild_member_count(guild)

    status = await ctx.send(f"Creating **{STATS_CATEGORY_NAME}**…")

    try:
        category = await guild.create_category(
            name=STATS_CATEGORY_NAME,
            overwrites=overwrites,
            reason="Stats bot setup — stats category",
        )

        stats_channel = await guild.create_voice_channel(
            name=_stats_channel_name(count),
            category=category,
            overwrites=overwrites,
            reason="Stats bot setup — server stats",
        )
        await stats_channel.edit(
            status=_stats_channel_status(),
            reason="Stats bot setup — clock status",
        )

        try:
            await category.edit(position=0)
        except discord.HTTPException:
            pass

        global _stats_config, _last_member_count, _last_stats_status
        _stats_config = cast(dict[str, int | None], {
            "guild_id": guild.id,
            "category_id": category.id,
            "stats_channel_id": stats_channel.id,
        })
        save_stats_config(_stats_config)
        _last_member_count = count
        _last_stats_status = _stats_channel_status()

        embed = discord.Embed(
            title=f"{STATS_CATEGORY_NAME} — ready",
            description=(
                f"Category: **{category.name}**\n"
                f"Stats channel: {stats_channel.mention}\n"
                f"• **Name:** member count\n"
                f"• **Status:** rotating world clock\n\n"
                "Channel is locked — display only."
            ),
            color=discord.Color.green(),
        )
        await status.edit(content=None, embed=embed)
    except discord.Forbidden:
        await status.edit(
            content="I need **Manage Channels** permission to create stat channels."
        )
    except Exception as exc:
        await status.edit(content=f"Setup failed: {exc}")


def _build_staff_application_embed(interaction: discord.Interaction, *, answers: dict[str, str]) -> discord.Embed:
    embed = discord.Embed(
        title="New Staff Application",
        color=discord.Color.blue(),
        timestamp=datetime.now(),
    )
    embed.add_field(
        name="Applicant",
        value=f"{interaction.user.mention} ({interaction.user.id})",
        inline=False,
    )

    origin = "Unknown"
    if isinstance(interaction.channel, discord.TextChannel):
        origin = interaction.channel.mention
    elif isinstance(interaction.channel, discord.VoiceChannel):
        origin = interaction.channel.mention
    elif isinstance(interaction.channel, discord.Thread):
        origin = interaction.channel.mention
    embed.add_field(name="Origin channel", value=origin, inline=False)

    for label, value in answers.items():
        embed.add_field(name=label, value=value, inline=False)

    return embed


async def _publish_staff_application(
    interaction: discord.Interaction,
    *,
    staff_channel: discord.TextChannel | None,
    pick_channel: discord.TextChannel | None,
    answers: dict[str, str],
) -> None:
    if staff_channel is None:
        await interaction.response.send_message(
            "Staff application channel is not configured or could not be found.",
            ephemeral=True,
        )
        return

    embed = _build_staff_application_embed(interaction, answers=answers)

    # Do not send an ephemeral confirmation message in the channel.
    # The application is published directly to the configured review channels.
    try:
        guild = interaction.guild
        bot_member = guild.me if guild is not None else None
        if isinstance(staff_channel, discord.TextChannel) and bot_member is not None:
            perms = staff_channel.permissions_for(bot_member)
            if not perms.send_messages or not perms.embed_links:
                print(f"[staff apply] missing send/embed permissions in channel {staff_channel.id}")
                return
    except Exception as perm_exc:
        print(f"[staff apply] permission check failed: {perm_exc}")

    review_view = StaffApplicationReviewView(
        applicant_id=interaction.user.id,
        applicant_name=str(interaction.user),
        applicant_mention=interaction.user.mention,
    )

    if isinstance(staff_channel, discord.TextChannel):
        try:
            staff_message = await staff_channel.send(embed=embed, view=review_view)
            bot.add_view(review_view, message_id=staff_message.id)
        except Exception as staff_exc:
            print(f"[staff apply] failed to send to staff channel: {staff_exc}")
    else:
        print("[staff apply] staff channel is not configured or could not be found.")

    if isinstance(pick_channel, discord.TextChannel):
        try:
            pick_message = await pick_channel.send(embed=embed, view=review_view)
            bot.add_view(review_view, message_id=pick_message.id)
        except Exception as pick_exc:
            print(f"[staff apply] failed to send to pick channel: {pick_exc}")
    else:
        print("[staff apply] pick channel is not configured or could not be found.")


class StaffApplicationModal(discord.ui.Modal, title="Staff Application Form"):
    q1 = discord.ui.TextInput(
        label="Can you be active daily and supportive?",
        style=discord.TextStyle.long,
        required=True,
        placeholder="Write how you stay active and supportive",
        max_length=2000,
    )
    q2 = discord.ui.TextInput(
        label="What ideas would you bring to the team?",
        style=discord.TextStyle.long,
        required=True,
        placeholder="Your ideas for the team and teamwork",
        max_length=2000,
    )
    q3 = discord.ui.TextInput(
        label="Can you make tough decisions under pressure?",
        style=discord.TextStyle.long,
        required=True,
        placeholder="Explain how you handle pressure",
        max_length=2000,
    )
    q4 = discord.ui.TextInput(
        label="How would you improve the community?",
        style=discord.TextStyle.long,
        required=True,
        placeholder="Describe how you would improve the community",
        max_length=2000,
    )
    q5 = discord.ui.TextInput(
        label="A short introduction about yourself:",
        style=discord.TextStyle.long,
        required=True,
        placeholder="A short personal message or summary",
        max_length=2000,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        staff_channel = interaction.client.get_channel(STAFF_APP_CHANNEL_ID)
        pick_channel = interaction.client.get_channel(STAFF_PICK_CHANNEL_ID)
        if not isinstance(staff_channel, discord.TextChannel):
            await interaction.response.send_message(
                "Staff application channel is not configured or could not be found.",
                ephemeral=True,
            )
            return

        answers: dict[str, str] = {
            str(self.q1.label or "Question 1"): str(self.q1.value or ""),
            str(self.q2.label or "Question 2"): str(self.q2.value or ""),
            str(self.q3.label or "Question 3"): str(self.q3.value or ""),
            str(self.q4.label or "Question 4"): str(self.q4.value or ""),
            str(self.q5.label or "Question 5"): str(self.q5.value or ""),
        }
        await _publish_staff_application(
            interaction,
            staff_channel=staff_channel,
            pick_channel=pick_channel if isinstance(pick_channel, discord.TextChannel) else None,
            answers=answers,
        )

class StaffApplicationReviewView(discord.ui.View):
    def __init__(self, applicant_id: int, applicant_name: str, applicant_mention: str):
        super().__init__(timeout=None)
        self.applicant_id = applicant_id
        self.applicant_name = applicant_name
        self.applicant_mention = applicant_mention

    async def _is_authorized(self, member: discord.Member | discord.User) -> bool:
        if not isinstance(member, discord.Member):
            return False
        return (
            member.guild_permissions.manage_guild
            or member.guild_permissions.manage_roles
            or member.guild_permissions.manage_channels
            or member.guild_permissions.manage_messages
        )

    async def _finish_review(self, interaction: discord.Interaction, status: str) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

        # Safely update the interaction message if it exists. `interaction.message`
        # can be None (for some types of interactions), so guard before calling
        # `.edit` to avoid runtime errors and satisfy static analysis.
        if interaction.message is not None:
            await interaction.message.edit(view=self)

        try:
            guild = interaction.guild
            if guild is not None:
                member = guild.get_member(self.applicant_id)
                if member is not None:
                    if status == "Accepted":
                        await member.send(
                            "Congratulations! You have been accepted to join our Staff team. "
                            "Please head over to the Support channel so we can talk more about the details and get you set up."
                        )
                    else:
                        await member.send(
                            "We're sorry, but your application has not been accepted at this time. "
                            "Don't be discouraged, and feel free to try again later!"
                        )
        except Exception:
            pass

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, custom_id="staff_app:accept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        if not await self._is_authorized(interaction.user):
            await interaction.response.send_message("You are not allowed to accept staff applications.", ephemeral=True)
            return

        await self._finish_review(interaction, "Accepted")
        await interaction.response.send_message("Staff application accepted.", ephemeral=True)

    @discord.ui.button(label="Refuse", style=discord.ButtonStyle.danger, custom_id="staff_app:refuse")
    async def refuse(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        if not await self._is_authorized(interaction.user):
            await interaction.response.send_message("You are not allowed to refuse staff applications.", ephemeral=True)
            return

        await self._finish_review(interaction, "Refused")
        await interaction.response.send_message("Staff application refused.", ephemeral=True)


class StaffAppView(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="Apply", style=discord.ButtonStyle.primary, custom_id="staff_app.apply")
    async def apply_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.send_modal(StaffApplicationModal())


@bot.command(name="setupstaffapp")
@commands.has_permissions(manage_guild=True)
async def setup_staff_app_cmd(ctx: commands.Context):
    """Create the staff application panel in the configured staff app channel."""
    app_channel = bot.get_channel(STAFF_APP_CHANNEL_ID)
    if not isinstance(app_channel, discord.TextChannel):
        await ctx.send(
            "Could not find the configured staff application channel."
        )
        return

    embed = discord.Embed(
        title="STAFF APPLICATION",
        description=(
            "We’re looking for active, respectful, and dedicated members to join our staff team. "
            "If you enjoy helping others, keeping the community safe, and contributing to a positive environment, "
            "this is your chance to apply."
        ),
        color=discord.Color.red(),
    )

    embed.add_field(
        name="Why apply?",
        value=(
            "• Exclusive staff role\n"
            "• Experience & teamwork\n"
            "• Support the community"
        ),
        inline=False,
    )

    image_url = "https://i.imgur.com/WbOpn5L.jpeg"
    image_path = Path("staff_app_banner.png")
    if image_path.is_file():
        embed.set_image(url="attachment://staff_app_banner.png")
    else:
        embed.set_image(url=image_url)

    embed.set_footer(text="Press Apply to send your staff application.")

    view = StaffAppView(bot)
    if image_path.is_file():
        message = await app_channel.send(embed=embed, view=view, file=discord.File(image_path, filename="staff_app_banner.png"))
    else:
        message = await app_channel.send(embed=embed, view=view)
    bot.add_view(view, message_id=message.id)
    await ctx.send(
        f"Staff application panel created in {app_channel.mention}.",
        delete_after=10,
    )


@bot.command(name="poststaffpanels")
@commands.has_permissions(manage_guild=True)
async def post_staff_panels_cmd(ctx: commands.Context, member: discord.Member | None = None):
    """Post two staff application panels to the requested channels.

    Usage: `?poststaffpanels` to post with the command author as the applicant,
    or `?poststaffpanels @member` to post as a specific member.
    """
    if member is None:
        member = ctx.author if isinstance(ctx.author, discord.Member) else None

    applicant_mention = member.mention if isinstance(member, discord.Member) else str(member or ctx.author)

    # Channel IDs requested by the user:
    channel_a_id = 1534583845884792874  # STAFF APPLICATION
    channel_b_id = 1534586562447282258  # New Staff Application (review)

    app_channel = bot.get_channel(channel_a_id)
    review_channel = bot.get_channel(channel_b_id)

    if isinstance(app_channel, discord.TextChannel):
        embed = discord.Embed(
            title="STAFF APPLICATION",
            description=(
                "We’re looking for active, respectful, and dedicated members to join our staff team. "
                "If you enjoy helping others, keeping the community safe, and contributing to a positive environment, "
                "this is your chance to apply."
            ),
            color=discord.Color.red(),
        )
        embed.add_field(name="Why apply?", value=("• Exclusive staff role\n" "• Experience & teamwork\n" "• Support the community"), inline=False)
        embed.add_field(name="Applicant", value=applicant_mention, inline=False)
        view = StaffAppView(bot)
        try:
            await app_channel.send(embed=embed, view=view)
        except Exception as exc:
            await ctx.send(f"Failed to send panel to channel A: {exc}")

    if isinstance(review_channel, discord.TextChannel):
        embed2 = discord.Embed(
            title="New Staff Application",
            color=discord.Color.blue(),
            timestamp=datetime.now(),
        )
        embed2.add_field(name="Applicant", value=applicant_mention, inline=False)
        embed2.add_field(name="Origin channel", value=app_channel.mention if isinstance(app_channel, discord.TextChannel) else "Unknown", inline=False)
        # include placeholder answers so staff see the fields
        embed2.add_field(name="Q1", value="—", inline=False)
        embed2.add_field(name="Q2", value="—", inline=False)
        embed2.add_field(name="Q3", value="—", inline=False)
        review_view = StaffApplicationReviewView(applicant_id=member.id if isinstance(member, discord.Member) else 0, applicant_name=str(member or ctx.author), applicant_mention=applicant_mention)
        try:
            await review_channel.send(embed=embed2, view=review_view)
        except Exception as exc:
            await ctx.send(f"Failed to send review panel to channel B: {exc}")

    await ctx.send("Posted staff panels.", delete_after=8)


def _interaction_is_authorized(interaction: discord.Interaction) -> bool:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return False
    author = interaction.user
    return (
        author.guild_permissions.manage_guild
        or author.guild_permissions.manage_roles
        or author.id in ADMIN_USER_IDS
        or author.id in STAFF_USER_IDS
    )

async def _reply_ephemeral(interaction: discord.Interaction, message: str) -> None:
    try:
        await interaction.response.send_message(message, ephemeral=True)
    except Exception:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)


def _parse_channel_reference(guild: discord.Guild, raw_value: str) -> discord.TextChannel | None:
    raw_value = raw_value.strip()
    if raw_value.startswith("<#") and raw_value.endswith(">"):
        raw_value = raw_value[2:-1]

    if raw_value.isdigit():
        channel = guild.get_channel(int(raw_value))
        if isinstance(channel, discord.TextChannel):
            return channel
    return None


def _parse_member_reference(guild: discord.Guild, raw_value: str) -> discord.Member | None:
    raw_value = raw_value.strip()
    if not raw_value:
        return None
    if raw_value.startswith("<@") and raw_value.endswith(">"):
        raw_value = raw_value[2:-1]
        if raw_value.startswith("!"):
            raw_value = raw_value[1:]
    if raw_value.isdigit():
        return guild.get_member(int(raw_value))
    return None


class ChangeNameModal(discord.ui.Modal, title="Change Your Nickname"):
    new_nick: discord.ui.TextInput["ChangeNameModal"] = discord.ui.TextInput(
        label="What should be your new nickname?",
        placeholder="Enter your new nickname...",
        max_length=32,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This must be used inside a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            await _invoke_existing_command(
                interaction,
                change_name_cmd,
                new_nick=self.new_nick.value,
            )
            await interaction.followup.send("?changename executed.", ephemeral=True)
        except PermissionError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"Failed to change nickname: {exc}", ephemeral=True)


class SendNotVerifyModal(discord.ui.Modal, title="Send Not-Verify Notice"):
    custom_message: discord.ui.TextInput["SendNotVerifyModal"] = discord.ui.TextInput(
        label="Message to send",
        style=discord.TextStyle.long,
        required=True,
        placeholder="Type the message to send to members with the not-verify role.",
        max_length=2000,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "This action must be performed in a server.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            await _invoke_existing_command(
                interaction,
                send_not_verify_cmd,
                custom_message=self.custom_message.value,
            )
            await interaction.followup.send("?sendnotverify executed.", ephemeral=True)
        except PermissionError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"Failed to send custom notice: {exc}", ephemeral=True)


class PostStaffPanelsModal(discord.ui.Modal, title="Post Staff Panels"):
    applicant_reference: discord.ui.TextInput["PostStaffPanelsModal"] = discord.ui.TextInput(
        label="Member mention or ID",
        style=discord.TextStyle.short,
        required=False,
        placeholder="Leave blank to use your own name",
        max_length=100,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This must be used inside a server.", ephemeral=True)
            return

        member: discord.Member | None = None
        raw_applicant = self.applicant_reference.value.strip()
        if raw_applicant:
            member = _parse_member_reference(interaction.guild, raw_applicant)
            if member is None:
                await interaction.response.send_message(
                    "Could not find that member. Use a mention or user ID.",
                    ephemeral=True,
                )
                return

        await interaction.response.defer(ephemeral=True)
        try:
            await _invoke_existing_command(
                interaction,
                post_staff_panels_cmd,
                member=member,
            )
            await interaction.followup.send("?poststaffpanels executed.", ephemeral=True)
        except PermissionError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"Failed to post staff panels: {exc}", ephemeral=True)


class SetNickReviewModal(discord.ui.Modal, title="Set Nickname Review Channel"):
    review_channel: discord.ui.TextInput["SetNickReviewModal"] = discord.ui.TextInput(
        label="Channel mention or ID",
        style=discord.TextStyle.short,
        required=True,
        placeholder="Enter a channel mention like #name or the channel ID",
        max_length=100,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This must be used inside a server.", ephemeral=True)
            return

        channel = _parse_channel_reference(interaction.guild, self.review_channel.value)
        if channel is None:
            await interaction.response.send_message(
                "Please enter a valid text channel mention or channel ID.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            await _invoke_existing_command(
                interaction,
                set_nick_review_cmd,
                channel,
            )
            await interaction.followup.send(
                f"?setnickreview executed for {channel.mention}.",
                ephemeral=True,
            )
        except PermissionError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"Failed to set review channel: {exc}", ephemeral=True)


async def _delete_message_after(message: discord.Message, delay: float) -> None:
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except (discord.Forbidden, discord.HTTPException):
        pass


class _CommandContextProxy:
    def __init__(self, interaction: discord.Interaction):
        self.bot = bot
        self.guild = interaction.guild
        self.channel = interaction.channel
        self.author = interaction.user
        self.message = None
        self.prefix = "?"
        self._interaction = interaction

    async def send(self, content: str | None = None, **kwargs: Any) -> Any:
        if not isinstance(self._interaction.channel, discord.TextChannel):
            raise RuntimeError("This action requires a text channel.")
        delete_after = kwargs.pop("delete_after", None)
        message = await self._interaction.channel.send(content=content, **kwargs)
        if delete_after is not None:
            asyncio.create_task(_delete_message_after(message, float(delete_after)))
        return message


def _interaction_has_manage_guild(interaction: discord.Interaction) -> bool:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return False
    author = interaction.user
    return author.guild_permissions.manage_guild or author.id in ADMIN_USER_IDS


async def _invoke_existing_command(
    interaction: discord.Interaction,
    command_callback: Any,
    *args: Any,
    **kwargs: Any,
) -> Any:
    ctx = _CommandContextProxy(interaction)
    try:
        if isinstance(command_callback, commands.Command):
            return await command_callback.invoke(ctx, *args, **kwargs)
        return await command_callback(ctx, *args, **kwargs)
    except commands.MissingPermissions as exc:
        missing = ", ".join(exc.missing_permissions) if exc.missing_permissions else "required permissions"
        raise PermissionError(f"You need **{missing}** for this command.") from exc
    except commands.CheckFailure as exc:
        raise PermissionError("You are not allowed to run this command.") from exc
    except commands.MissingRequiredArgument as exc:
        raise ValueError(f"Missing required argument: {exc.param.name}") from exc


class CommentPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _reply(self, interaction: discord.Interaction, message: str) -> None:
        await _reply_ephemeral(interaction, message)

    async def _run_command(
        self,
        interaction: discord.Interaction,
        command: Any,
        *,
        require_auth: bool = False,
        require_manage_guild: bool = False,
        require_guild: bool = False,
        require_text_channel: bool = False,
        success_message: str | None = None,
        **command_kwargs: Any,
    ) -> None:
        if require_guild and interaction.guild is None:
            await self._reply(interaction, "This button must be used in a server.")
            return
        if require_text_channel and not isinstance(interaction.channel, discord.TextChannel):
            await self._reply(interaction, "This button must be used in a server text channel.")
            return
        if require_manage_guild:
            if not _interaction_has_manage_guild(interaction):
                await self._reply(interaction, "You need **Manage Server** permission for this button.")
                return
        elif require_auth:
            if not _interaction_is_authorized(interaction):
                await self._reply(interaction, "You must be staff or an admin to use this button.")
                return

        await interaction.response.defer(ephemeral=True)
        try:
            await _invoke_existing_command(interaction, command, **command_kwargs)
            if success_message:
                await interaction.followup.send(success_message, ephemeral=True)
        except PermissionError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"Command failed: {exc}", ephemeral=True)

    @discord.ui.button(label="?panel", style=discord.ButtonStyle.secondary, custom_id="comment_panel:postpanel", emoji="📋", row=0)
    async def postpanel_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        await self._run_command(
            interaction,
            setup_command_spanel_cmd,
            require_auth=True,
            require_guild=True,
            require_text_channel=True,
            success_message="?panel executed — command panel posted.",
        )

    @discord.ui.button(label="?setupstats", style=discord.ButtonStyle.primary, custom_id="comment_panel:setupstats", emoji="📊", row=0)
    async def setupstats_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        await self._run_command(
            interaction,
            setup_stats_cmd,
            require_auth=True,
            require_guild=True,
            require_text_channel=True,
            success_message="?setupstats executed.",
        )

    @discord.ui.button(label="?setupstaffapp", style=discord.ButtonStyle.primary, custom_id="comment_panel:setupstaffapp", emoji="🧑‍💼", row=0)
    async def setupstaffapp_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        await self._run_command(
            interaction,
            setup_staff_app_cmd,
            require_manage_guild=True,
            success_message="?setupstaffapp executed.",
        )

    @discord.ui.button(label="?refreshstats", style=discord.ButtonStyle.success, custom_id="comment_panel:refreshstats", emoji="🔄", row=0)
    async def refreshstats_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        await self._run_command(
            interaction,
            refresh_stats_cmd,
            require_auth=True,
            success_message="?refreshstats executed.",
        )

    @discord.ui.button(label="?sendnotverify", style=discord.ButtonStyle.danger, custom_id="comment_panel:sendnotice", emoji="📩", row=1)
    async def sendnotice_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        await self._run_command(
            interaction,
            send_not_verify_cmd,
            require_auth=True,
            require_guild=True,
            success_message="?sendnotverify executed.",
        )

    @discord.ui.button(label="?sendnotverify msg", style=discord.ButtonStyle.primary, custom_id="comment_panel:customnotice", emoji="✉️", row=1)
    async def customnotice_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        if not _interaction_is_authorized(interaction):
            await self._reply(interaction, "You must be staff or an admin to use this button.")
            return
        await interaction.response.send_modal(SendNotVerifyModal())

    @discord.ui.button(label="?poststaffpanels", style=discord.ButtonStyle.secondary, custom_id="comment_panel:poststaffpanels", emoji="📝", row=1)
    async def poststaffpanels_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        if not _interaction_has_manage_guild(interaction):
            await self._reply(interaction, "You need **Manage Server** permission for this button.")
            return
        await interaction.response.send_modal(PostStaffPanelsModal())

    @discord.ui.button(label="?restartvoicelounge", style=discord.ButtonStyle.danger, custom_id="comment_panel:restartlounge", emoji="🔁", row=2)
    async def restartlounge_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        await self._run_command(
            interaction,
            restart_voice_lounge_cmd,
            require_auth=True,
            require_guild=True,
            success_message="?restartvoicelounge executed.",
        )

    @discord.ui.button(label="?getnickreview", style=discord.ButtonStyle.secondary, custom_id="comment_panel:getnickreview", emoji="🔎", row=2)
    async def getnickreview_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        await self._run_command(
            interaction,
            get_nick_review_cmd,
            require_auth=True,
            require_guild=True,
        )

    @discord.ui.button(label="?ping", style=discord.ButtonStyle.secondary, custom_id="comment_panel:ping", emoji="📡", row=2)
    async def ping_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        await self._run_command(interaction, ping_cmd)

    @discord.ui.button(label="?changename", style=discord.ButtonStyle.success, custom_id="comment_panel:changename", emoji="✏️", row=3)
    async def changename_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        if interaction.guild is None:
            await self._reply(interaction, "This button must be used in a server.")
            return
        await interaction.response.send_modal(ChangeNameModal())

    @discord.ui.button(label="?setupnick", style=discord.ButtonStyle.secondary, custom_id="comment_panel:setupnick", emoji="🧾", row=3)
    async def setupnick_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        await self._run_command(
            interaction,
            setup_nick_cmd,
            require_auth=True,
            require_text_channel=True,
            success_message="?setupnick executed.",
        )

    @discord.ui.button(label="?setnickreview", style=discord.ButtonStyle.secondary, custom_id="comment_panel:setnickreview", emoji="⚙️", row=3)
    async def setnickreview_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        if not _interaction_is_authorized(interaction):
            await self._reply(interaction, "You must be staff or an admin to use this button.")
            return
        await interaction.response.send_modal(SetNickReviewModal())


@bot.command(name="setupcommandspanel", aliases=["setupcommentpanel", "panel"])
@commands.guild_only()
@commands.check(_can_manage_bot)
async def setup_command_spanel_cmd(ctx: commands.Context[StatsBot]):
    """Post a large commands panel in the current channel."""
    embed = discord.Embed(
        title="Server Control Panel",
        description=(
            "Each button runs an existing bot command (shortcut only).\n"
            "Click a button to execute `?command`, or fill out the form when extra details are needed."
        ),
        color=discord.Color.blurple(),
    )

    embed.add_field(
        name="How to use",
        value=(
            "• Each button label shows the exact command it runs.\n"
            "• Buttons that need extra details open a popup form first.\n"
            "• Protected buttons require staff/admin access."
        ),
        inline=False,
    )
    embed.add_field(
        name="Management",
        value=(
            "`?panel` · `?setupstats` · `?setupstaffapp` · `?refreshstats`"
        ),
        inline=False,
    )
    embed.add_field(
        name="Moderation",
        value=(
            "`?sendnotverify` · `?sendnotverify msg` · `?poststaffpanels`"
        ),
        inline=False,
    )
    embed.add_field(
        name="Utility",
        value=(
            "`?restartvoicelounge` · `?getnickreview` · `?ping` · `?changename` · `?setupnick` · `?setnickreview`"
        ),
        inline=False,
    )
    embed.set_footer(text="Modern Discord-style control panel · Staff/Admin only")

    view = CommentPanelView()
    try:
        image_path = Path("staff_app_banner.png")
        if image_path.is_file():
            message = await ctx.send(
                embed=embed,
                view=view,
                file=discord.File(image_path, filename="staff_app_banner.png"),
            )
        else:
            message = await ctx.send(embed=embed, view=view)
        bot.add_view(view, message_id=message.id)
    except Exception as exc:
        await ctx.send(f"Failed to post command panel: {exc}")


@bot.command(name="refreshstats")
@commands.check(_can_manage_bot)
async def refresh_stats_cmd(ctx: commands.Context[StatsBot]):
    """Force-refresh the stats channel."""
    await refresh_all_stats(force_members=True)
    await ctx.send("Stats channel refreshed.", delete_after=8)


@bot.command(name="restartvoicelounge")
@commands.guild_only()
@commands.check(_can_manage_bot)
async def restart_voice_lounge_cmd(ctx: commands.Context[StatsBot]):
    """Restart the bot's voice connection in the lounge channel."""
    guild = ctx.guild
    if guild is None:
        await ctx.send("This command can only be used inside a server.")
        return
    await _disconnect_guild_voice_client(guild)
    await _join_voice_lounge()
    await ctx.send("Voice lounge restart requested.", delete_after=10)


@bot.command(name="ping")
async def ping_cmd(ctx: commands.Context[StatsBot]):
    await ctx.send(f"Pong — `{round(bot.latency * 1000)}ms`", delete_after=10)

@bot.command(name="changename")
@commands.guild_only()
async def change_name_cmd(ctx: commands.Context[StatsBot], *, new_nick: str):
    author = ctx.author
    if not isinstance(author, discord.Member):
        await ctx.send("This command must be used in a server.", delete_after=10)
        return
    new_nick = new_nick.strip()
    if not new_nick:
        await ctx.send("Please provide a new nickname.", delete_after=10)
        return

    if len(new_nick) > 32:
        await ctx.send("Nickname must be 32 characters or less.", delete_after=10)
        return

    try:
        await cast(discord.Member, ctx.author).edit(nick=new_nick, reason="Changed via bot command")
        await ctx.send(f"Your nickname has been changed to **{new_nick}**.", delete_after=10)
    except discord.Forbidden:
        await ctx.send("I don't have permission to change your nickname.", delete_after=10)
    except Exception as exc:
        await ctx.send(f"Could not change your nickname: {exc}", delete_after=10)

# --- Nickname request UI (button opens ChangeNameModal → ?changename) ---
class NicknameRequestView(discord.ui.View):
    def __init__(self, *, admin_channel: discord.TextChannel | None = None):
        super().__init__(timeout=None)
        self.admin_channel = admin_channel

    @discord.ui.button(label="Change Nickname", style=discord.ButtonStyle.danger, custom_id="nickrequest:open")
    async def open_modal(self, interaction: discord.Interaction, button: Any):
        del button
        if interaction.guild is None:
            await interaction.response.send_message(
                "This button can only be used inside a server.",
                ephemeral=True,
            )
            return

        try:
            await interaction.response.send_modal(ChangeNameModal())
        except Exception as exc:
            print(f"[nickname] failed to open modal: {exc}")
            try:
                await interaction.response.send_message(
                    "Unable to open nickname request modal right now. Please try again.",
                    ephemeral=True,
                )
            except Exception:
                pass


# `set_punishment` command removed per user request


@bot.command(name="setupnick")
@commands.guild_only()
@commands.check(_can_manage_bot)
async def setup_nick_cmd(ctx: commands.Context[StatsBot], admin_channel: discord.TextChannel | None = None):
    """Post a nickname-request panel in this channel or a specified admin review channel.

    Usage: `?setupnick` to post in current channel, or `?setupnick #requests` to configure an admin channel.
    """
    # If no admin_channel passed, check persisted config for this guild
    if admin_channel is None:
        saved_id = _nick_config.get("review_channel_id")
        if saved_id and ctx.guild:
            saved_channel = ctx.guild.get_channel(int(saved_id))
            if isinstance(saved_channel, discord.TextChannel):
                admin_channel = saved_channel

    view = NicknameRequestView(admin_channel=admin_channel)
    embed = discord.Embed(
        title="Nickname Request",
        description=(
            "Use the button below to request a nickname change.\n\n"
            "Please make sure your nickname follows the server rules:\n"
            "• Must be appropriate and respectful\n"
            "• No offensive, abusive, or explicit language\n"
            "• No impersonation of members or staff\n"
            "• Keep it readable and avoid excessive symbols\n"
            "• Follow all community rules\n\n"
            "Requests that break the rules will be rejected."
        ),
        color=discord.Color.blurple(),
    )
    await ctx.send(embed=embed, view=view)


# Do-Not-Disturb support removed per user request


@bot.command(name="setnickreview")
@commands.guild_only()
@commands.check(_can_manage_bot)
async def set_nick_review_cmd(ctx: commands.Context[StatsBot], channel: discord.TextChannel):
    """Set the persistent review channel for nickname requests."""
    global _nick_config
    if ctx.guild is None:
        await ctx.send("This command must be run in a guild.")
        return
    _nick_config = cast(dict[str, int | None], {"review_channel_id": channel.id, "guild_id": ctx.guild.id})
    save_nick_config(_nick_config)
    await ctx.send(f"Nickname review channel saved: {channel.mention}")


@bot.command(name="getnickreview")
@commands.guild_only()
@commands.check(_can_manage_bot)
async def get_nick_review_cmd(ctx: commands.Context[StatsBot]):
    """Show the configured review channel for the server."""
    saved_id = _nick_config.get("review_channel_id")
    if not saved_id:
        await ctx.send("No nickname review channel configured.")
        return
    channel = ctx.guild.get_channel(int(saved_id)) if ctx.guild else None
    if channel:
        await ctx.send(f"Configured review channel: {channel.mention}")
    else:
        await ctx.send("Configured review channel is not available in this server.")


class _HealthHandler(BaseHTTPRequestHandler):
    def _send_ok(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()

    def do_GET(self):
        self._send_ok()
        self.wfile.write(b"Legends Tunisia stats bot is running")

    def do_HEAD(self):
        self._send_ok()

    def log_message(self, format, *args):
        pass


def _start_health_server() -> None:
    port = int(os.environ.get("PORT", "8080"))
    HTTPServer(("0.0.0.0", port), _HealthHandler).serve_forever()


if __name__ == "__main__":
    if os.environ.get("PORT"):
        threading.Thread(target=_start_health_server, daemon=True).start()

    try:
        bot.run(TOKEN, log_handler=None)
    except discord.LoginFailure as exc:
        raise SystemExit("Invalid bot token — check your .env file.") from exc
