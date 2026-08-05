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

    custom_id = interaction.data.get("custom_id") if isinstance(interaction.data, dict) else None
    if custom_id == "nickrequest:open":
        if interaction.guild is None:
            await interaction.response.send_message(
                "This button can only be used inside a server.",
                ephemeral=True,
            )
            return

        print(f"[nickname] raw button interaction received by {interaction.user} ({interaction.user.id})")
        modal = NicknameRequestModal(requester=interaction.user, admin_channel=None)
        try:
            await interaction.response.send_modal(modal)
        except Exception as exc:
            print(f"[nickname] raw on_interaction failed to open modal: {exc}")
            try:
                await interaction.response.send_message(
                    "Unable to open nickname request modal right now. Please try again.",
                    ephemeral=True,
                )
            except Exception:
                pass
        return

    if custom_id == "staff_app.apply":
        if interaction.guild is None:
            await interaction.response.send_message(
                "This button can only be used inside a server.",
                ephemeral=True,
            )
            return

        print(f"[staff apply] raw button interaction received by {interaction.user} ({interaction.user.id})")
        modal = StaffApplicationModal()
        try:
            await interaction.response.send_modal(modal)
        except Exception as exc:
            print(f"[staff apply] raw on_interaction failed to open modal: {exc}")
            try:
                await interaction.response.send_message(
                    "Unable to open the staff application form right now. Please try again.",
                    ephemeral=True,
                )
            except Exception as inner_exc:
                print(f"[staff apply] raw fallback response failed: {inner_exc}")
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


class StaffApplicationModal(discord.ui.Modal, title="Staff Application Form"):
    q1 = discord.ui.TextInput(
        label="Tnajem tkoun daily active w supportive ?",
        style=discord.TextStyle.long,
        required=True,
        placeholder="Write how you stay active and supportive",
        max_length=2000,
    )
    q2 = discord.ui.TextInput(
        label="Afkar mte3ek lel team w teamwork ?",
        style=discord.TextStyle.long,
        required=True,
        placeholder="Your ideas for the team and teamwork",
        max_length=2000,
    )
    q3 = discord.ui.TextInput(
        label="Ta9der tekhou 9rarat s3iba under pressure?",
        style=discord.TextStyle.long,
        required=True,
        placeholder="Explain how you handle pressure",
        max_length=2000,
    )
    q4 = discord.ui.TextInput(
        label="Kifech tnajem tdhif lel community ?",
        style=discord.TextStyle.long,
        required=True,
        placeholder="Describe how you would improve the community",
        max_length=2000,
    )
    q5 = discord.ui.TextInput(
        label="Zaref b rohek fi fa9ra sghira :",
        style=discord.TextStyle.long,
        required=True,
        placeholder="A short personal message or summary",
        max_length=2000,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        staff_channel = interaction.client.get_channel(STAFF_APP_CHANNEL_ID)
        if not isinstance(staff_channel, discord.TextChannel):
            await interaction.response.send_message(
                "Staff application channel is not configured or could not be found.",
                ephemeral=True,
            )
            return

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
        embed.add_field(name=self.q1.label, value=self.q1.value, inline=False)
        embed.add_field(name=self.q2.label, value=self.q2.value, inline=False)
        embed.add_field(name=self.q3.label, value=self.q3.value, inline=False)
        embed.add_field(name=self.q4.label, value=self.q4.value, inline=False)
        embed.add_field(name=self.q5.label, value=self.q5.value, inline=False)

        await staff_channel.send(embed=embed)
        await interaction.response.send_message(
            "Thank you — your staff application has been submitted.",
            ephemeral=True,
        )


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

# --- Nickname request UI ---
class NicknameRequestModal(discord.ui.Modal, title="Change Your Nickname"):
    new_nick: discord.ui.TextInput["NicknameRequestModal"] = discord.ui.TextInput(
        label="What should be your new nickname?",
        placeholder="Enter your new nickname...",
        max_length=32,
    )

    def __init__(self, requester: discord.Member | discord.User, *, admin_channel: discord.abc.GuildChannel | None):
        super().__init__()
        self.requester = requester
        self.admin_channel = admin_channel

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "This nickname request must be made inside a server.",
                ephemeral=True,
            )
            return

        requested = self.new_nick.value.strip()
        if not requested:
            await interaction.response.send_message("Nickname cannot be empty.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        if not isinstance(self.requester, discord.Member):
            await interaction.followup.send(
                "Could not change your nickname because you are not a server member.",
                ephemeral=True,
            )
            return

        try:
            await self.requester.edit(
                nick=requested,
                reason="Nickname changed via bot request",
            )
            await interaction.followup.send(
                f"Your nickname has been changed to **{requested}**.",
                ephemeral=True,
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "I don't have permission to change your nickname.",
                ephemeral=True,
            )
        except Exception as exc:
            print(f"[nickname] failed to change nickname: {exc}")
            await interaction.followup.send(
                "Could not change your nickname right now. Please try again later.",
                ephemeral=True,
            )


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

        print(f"[nickname] open_modal pressed by {interaction.user} ({interaction.user.id})")
        admin_channel = self.admin_channel
        if admin_channel is None and interaction.guild:
            saved_id = _nick_config.get("review_channel_id")
            if saved_id:
                saved_channel = interaction.guild.get_channel(int(saved_id))
                if isinstance(saved_channel, discord.TextChannel):
                    admin_channel = saved_channel

        # Opt-in debug quick-response: set DEBUG_NICK_QUICK_RESP=1 in the environment
        # (Render env vars) to make the button reply immediately with an ephemeral
        # confirmation. This helps determine whether interactions reach the deployed
        # instance (useful for diagnosing cold-start / timeout issues).
        if os.environ.get("DEBUG_NICK_QUICK_RESP") == "1":
            try:
                await interaction.response.send_message(
                    "Debug: button press received by bot.",
                    ephemeral=True,
                )
                return
            except Exception as exc:
                print(f"[nickname] debug quick response failed: {exc}")

        modal = NicknameRequestModal(requester=interaction.user, admin_channel=admin_channel)
        try:
            await interaction.response.send_modal(modal)
        except Exception as exc:
            print(f"[nickname] failed to open modal: {exc}")
            try:
                await interaction.response.send_message(
                    "Unable to open nickname request modal right now. Please try again.",
                    ephemeral=True,
                )
            except Exception:
                pass


# Punishment panel removed per user request — modal and view code deleted


class AdminApproveView(discord.ui.View):
    def __init__(self, requester_id: int, requested_nick: str):
        super().__init__(timeout=None)
        self.requester_id = requester_id
        self.requested_nick = requested_nick

    async def _is_authorized(self, member: discord.Member | discord.User) -> bool:
        if not isinstance(member, discord.Member):
            return False
        return (
            member.guild_permissions.manage_nicknames
            or member.guild_permissions.manage_roles
            or member.guild_permissions.manage_guild
        )

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, custom_id="nickrequest:approve")
    async def approve(self, interaction: discord.Interaction, button: Any) -> None:
        del button
        if not await self._is_authorized(interaction.user):
            await interaction.response.send_message("You are not allowed to approve nickname requests.", ephemeral=True)
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Could not resolve guild.", ephemeral=True)
            return

        member = guild.get_member(self.requester_id)
        if member is None:
            await interaction.response.send_message("Member not found.", ephemeral=True)
            return

        try:
            await member.edit(nick=self.requested_nick, reason=f"Approved by {interaction.user}")
            await interaction.response.send_message(f"Nickname for {member.mention} changed to **{self.requested_nick}**.")
            # disable buttons after action
            for child in self.children:
                if isinstance(child, discord.ui.Button):
                    child.disabled = True
            if interaction.message is not None:
                await interaction.message.edit(view=self)
        except discord.Forbidden:
            await interaction.response.send_message("I don't have permission to change that member's nickname.", ephemeral=True)
        except Exception as exc:
            await interaction.response.send_message(f"Failed to change nickname: {exc}", ephemeral=True)

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger, custom_id="nickrequest:reject")
    async def reject(self, interaction: discord.Interaction, button: Any) -> None:
        del button
        if not await self._is_authorized(interaction.user):
            await interaction.response.send_message("You are not allowed to reject nickname requests.", ephemeral=True)
            return

        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        if interaction.message is not None:
            await interaction.message.edit(view=self)
        await interaction.response.send_message("Request rejected.")


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
