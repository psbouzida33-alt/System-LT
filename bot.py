"""
Legends Tunisia — Server Stats Bot
Updates locked voice channels: member count + live clock.
"""
import asyncio
import os
import re
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.ext.commands.view import StringView
from dotenv import load_dotenv

from config import (
    BOT_VOICE_CHANNEL_ID,
    CLOCK_ROTATION_SECONDS,
    CLOCK_UPDATE_SECONDS,
    MEMBER_COUNT_EXCLUDE_BOTS,
    NICK_PANEL_CHANNEL_ID,
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
        self.add_view(AdminApproveView(requester_id=0, requested_nick=""))
        self.add_view(StaffAppView(self))
        self.add_view(
            StaffApplicationReviewView(
                applicant_id=0,
                applicant_name="",
                applicant_mention="",
            )
        )
        self.add_view(CommentPanelView())
        try:
            synced = await self.tree.sync()
            print(f"Synced {len(synced)} slash command(s).")
        except Exception as exc:
            print(f"Slash command sync failed: {exc}")


bot = StatsBot(
    command_prefix=["?", "!"],
    intents=intents,
    activity=discord.Activity(type=discord.ActivityType.watching, name="server stats"),
)

_last_member_count: int | None = None
_last_stats_status: str | None = None
_stats_config: dict[str, int | None] = load_stats_config()
_nick_config: dict[str, int | None] = load_nick_config()

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
        admin_channel: discord.TextChannel | None = None
        if interaction.guild:
            saved_id = _nick_config.get("review_channel_id")
            if saved_id:
                saved_channel = interaction.guild.get_channel(int(saved_id))
                if isinstance(saved_channel, discord.TextChannel):
                    admin_channel = saved_channel

        try:
            await interaction.response.send_modal(
                NicknameRequestModal(
                    requester=interaction.user,
                    admin_channel=admin_channel,
                )
            )
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

    if custom_id and custom_id.startswith("comment_panel:"):
        if await _handle_legacy_panel_interaction(interaction, custom_id):
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


async def _get_configured_text_channel(
    client_or_interaction: "discord.Client | discord.Interaction",
    channel_id: int,
    guild: discord.Guild | None = None,
) -> discord.TextChannel | None:
    """Resolve a configured channel from cache or the API.

    Accepts either a Client/Bot (pass `guild` separately, e.g. ctx.guild) or a
    discord.Interaction (guild is read off it automatically).
    """
    if isinstance(client_or_interaction, discord.Interaction):
        client = client_or_interaction.client
        guild = guild or client_or_interaction.guild
    else:
        client = client_or_interaction

    channel = client.get_channel(channel_id)
    if isinstance(channel, discord.TextChannel):
        return channel

    if guild is not None:
        guild_channel = guild.get_channel(channel_id)
        if isinstance(guild_channel, discord.TextChannel):
            return guild_channel

    try:
        fetched = await client.fetch_channel(channel_id)
    except (discord.HTTPException, discord.NotFound):
        return None
    return fetched if isinstance(fetched, discord.TextChannel) else None


def _parse_applicant_from_embed(embed: discord.Embed) -> tuple[int, str, str] | None:
    for field in embed.fields:
        if field.name != "Applicant":
            continue
        value = field.value or ""
        id_match = re.search(r"\((\d+)\)\s*$", value)
        if not id_match:
            continue
        applicant_id = int(id_match.group(1))
        mention_match = re.search(r"<@!?(\d+)>", value)
        applicant_mention = mention_match.group(0) if mention_match else f"<@{applicant_id}>"
        applicant_name = value.split("(")[0].strip() or applicant_mention
        return applicant_id, applicant_name, applicant_mention
    return None


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
    pick_channel: discord.TextChannel,
    answers: dict[str, str],
) -> bool:
    """Publish a submitted application to pick-a-new-staff for staff review."""
    embed = _build_staff_application_embed(interaction, answers=answers)

    try:
        guild = interaction.guild
        bot_member = guild.me if guild is not None else None
        if bot_member is not None:
            perms = pick_channel.permissions_for(bot_member)
            if not perms.send_messages or not perms.embed_links:
                print(
                    f"[staff apply] missing send/embed permissions in pick channel {pick_channel.id}"
                )
                return False
    except Exception as perm_exc:
        print(f"[staff apply] permission check failed: {perm_exc}")

    review_view = StaffApplicationReviewView(
        applicant_id=interaction.user.id,
        applicant_name=str(interaction.user),
        applicant_mention=interaction.user.mention,
    )

    try:
        pick_message = await pick_channel.send(embed=embed, view=review_view)
        bot.add_view(review_view, message_id=pick_message.id)
        return True
    except Exception as pick_exc:
        print(f"[staff apply] failed to send to pick channel: {pick_exc}")
        return False


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
        pick_channel = await _get_configured_text_channel(interaction, STAFF_PICK_CHANNEL_ID)
        if pick_channel is None:
            await interaction.response.send_message(
                "Staff review channel (pick-a-new-staff) is not configured or could not be found.",
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

        await interaction.response.defer(ephemeral=True)
        published = await _publish_staff_application(
            interaction,
            pick_channel=pick_channel,
            answers=answers,
        )
        if published:
            await interaction.followup.send(
                "Your staff application was submitted successfully. Staff will review it soon.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "Could not submit your application right now. Please contact staff or try again later.",
                ephemeral=True,
            )

class StaffApplicationReviewView(discord.ui.View):
    def __init__(self, applicant_id: int, applicant_name: str, applicant_mention: str):
        super().__init__(timeout=None)
        self.applicant_id = applicant_id
        self.applicant_name = applicant_name
        self.applicant_mention = applicant_mention

    def _ensure_applicant(self, interaction: discord.Interaction) -> bool:
        if self.applicant_id:
            return True
        message = interaction.message
        if message is None or not message.embeds:
            return False
        parsed = _parse_applicant_from_embed(message.embeds[0])
        if parsed is None:
            return False
        self.applicant_id, self.applicant_name, self.applicant_mention = parsed
        return True

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
        if not self._ensure_applicant(interaction):
            return

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
            if guild is not None and self.applicant_id:
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

    app_channel = bot.get_channel(STAFF_APP_CHANNEL_ID)
    review_channel = bot.get_channel(STAFF_PICK_CHANNEL_ID)

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
            panel_message = await app_channel.send(embed=embed, view=view)
            bot.add_view(view, message_id=panel_message.id)
        except Exception as exc:
            await ctx.send(f"Failed to send panel to staff-app channel: {exc}")

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
            review_message = await review_channel.send(embed=embed2, view=review_view)
            bot.add_view(review_view, message_id=review_message.id)
        except Exception as exc:
            await ctx.send(f"Failed to send review to pick-a-new-staff channel: {exc}")

    await ctx.send("Posted staff panels.", delete_after=8)


def _member_is_authorized(member: discord.Member | discord.User | None) -> bool:
    if not isinstance(member, discord.Member):
        return False
    return (
        member.guild_permissions.manage_guild
        or member.guild_permissions.manage_roles
        or member.id in ADMIN_USER_IDS
        or member.id in STAFF_USER_IDS
    )


def _interaction_is_authorized(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        return False
    return _member_is_authorized(interaction.user)

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


def _panel_user_can_run(
    interaction: discord.Interaction,
    *,
    require_manage_guild: bool = False,
    require_auth: bool = False,
) -> tuple[bool, str | None]:
    if interaction.guild is None:
        return False, "This control panel must be used inside a server."
    if require_manage_guild and not _interaction_has_manage_guild(interaction):
        return False, "You need **Manage Server** permission to use this action."
    if require_auth and not _interaction_is_authorized(interaction):
        return False, "You must be staff or an admin to use this action."
    return True, None


def _build_control_panel_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🛠️ Panel System — Staff Commands",
        description=(
            "> Run bot commands with one click — no need to type anything manually.\n\n"
            "**How it works**\n"
            "🔹 Browse the sections below to find what you need\n"
            "🔹 Click a button to run the action instantly\n"
            "🔹 Some buttons open a short form when extra details are required\n"
            "🔹 You'll get a private confirmation after each action"
        ),
        color=discord.Color.from_rgb(88, 101, 242),
    )
    embed.add_field(
        name="📋 Management",
        value=(
            "**Post Panel** — Repost this control panel here\n"
            "**Setup Stats** — Create locked stats voice channels\n"
            "**Staff App** — Publish the staff application panel\n"
            "**Refresh Stats** — Force-update member count and clocks"
        ),
        inline=True,
    )
    embed.add_field(
        name="🛡️ Moderation",
        value=(
            "**Verify Reminder** — DM members with the not-verify role\n"
            "**Custom DM** — Send your own reminder message"
        ),
        inline=True,
    )
    embed.add_field(name="\u200b", value="\u200b", inline=False)
    embed.add_field(
        name="🔧 Utility",
        value=(
            "**Restart Lounge** — Reconnect the bot to the voice lounge\n"
            "**Nick Review** — Show the configured nickname review channel\n"
            "**Ping** — Check bot latency\n"
            "**Nick Panel** — Post the public nickname request button\n"
            "**Set Review Channel** — Choose where nick requests are reviewed"
        ),
        inline=False,
    )
    embed.set_footer(text="Staff & admin controls only  •  Buttons marked with a form open a popup")
    return embed


class ChangeNameModal(discord.ui.Modal, title="Change Your Nickname"):
    new_nick: discord.ui.TextInput["ChangeNameModal"] = discord.ui.TextInput(
        label="What nickname would you like?",
        placeholder="Example: Ahmed — keep it respectful and easy to read (max 32 characters)",
        max_length=32,
        required=True,
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
            await interaction.followup.send(
                f"Your nickname was updated to **{self.new_nick.value.strip()}**.",
                ephemeral=True,
            )
        except PermissionError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"Could not change nickname: {exc}", ephemeral=True)


class SendNotVerifyModal(discord.ui.Modal, title="Send Custom Verification Reminder"):
    custom_message: discord.ui.TextInput["SendNotVerifyModal"] = discord.ui.TextInput(
        label="Message for unverified members",
        style=discord.TextStyle.long,
        required=True,
        placeholder=(
            "Example: Please read the rules and press Verify in #welcome to unlock the rest of the server."
        ),
        max_length=2000,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        allowed, error = _panel_user_can_run(interaction, require_auth=True)
        if not allowed:
            await interaction.response.send_message(error or "Not allowed.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            await _invoke_existing_command(
                interaction,
                send_not_verify_cmd,
                custom_message=self.custom_message.value,
            )
            await interaction.followup.send(
                "Custom verification reminder sent to members with the not-verify role.",
                ephemeral=True,
            )
        except PermissionError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"Failed to send reminder: {exc}", ephemeral=True)


class PostStaffPanelsModal(discord.ui.Modal, title="Post Staff Application Panels"):
    applicant_reference: discord.ui.TextInput["PostStaffPanelsModal"] = discord.ui.TextInput(
        label="Applicant (optional)",
        style=discord.TextStyle.short,
        required=False,
        placeholder="Leave blank to use yourself, or paste @member / user ID",
        max_length=100,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        allowed, error = _panel_user_can_run(interaction, require_manage_guild=True)
        if not allowed:
            await interaction.response.send_message(error or "Not allowed.", ephemeral=True)
            return

        if interaction.guild is None:
            await interaction.response.send_message("This must be used inside a server.", ephemeral=True)
            return

        member: discord.Member | None = None
        raw_applicant = self.applicant_reference.value.strip()
        if raw_applicant:
            member = _parse_member_reference(interaction.guild, raw_applicant)
            if member is None:
                await interaction.response.send_message(
                    "Could not find that member. Paste a valid @mention or numeric user ID.",
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
            await interaction.followup.send(
                "Staff application panels posted to the configured channels.",
                ephemeral=True,
            )
        except PermissionError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"Failed to post staff panels: {exc}", ephemeral=True)


class SetNickReviewModal(discord.ui.Modal, title="Set Nickname Review Channel"):
    review_channel: discord.ui.TextInput["SetNickReviewModal"] = discord.ui.TextInput(
        label="Review channel",
        style=discord.TextStyle.short,
        required=True,
        placeholder="Paste #channel mention or the channel ID where staff review nick requests",
        max_length=100,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        allowed, error = _panel_user_can_run(interaction, require_auth=True)
        if not allowed:
            await interaction.response.send_message(error or "Not allowed.", ephemeral=True)
            return

        if interaction.guild is None:
            await interaction.response.send_message("This must be used inside a server.", ephemeral=True)
            return

        channel = _parse_channel_reference(interaction.guild, self.review_channel.value)
        if channel is None:
            await interaction.response.send_message(
                "Please enter a valid text channel mention (like #channel) or channel ID.",
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
                f"Nickname review channel saved: {channel.mention}",
                ephemeral=True,
            )
        except PermissionError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"Failed to set review channel: {exc}", ephemeral=True)


def _panel_command_context(interaction: discord.Interaction) -> commands.Context[StatsBot]:
    """Build a real command context so panel buttons can invoke existing commands."""
    if interaction.channel_id is None:
        raise RuntimeError("This action requires a channel.")

    channel = interaction.channel
    if channel is None:
        channel = discord.PartialMessageable(
            state=interaction.client._connection,
            guild_id=interaction.guild_id,
            id=interaction.channel_id,
        )

    message = discord.Message(
        state=interaction.client._connection,
        channel=channel,
        data={
            "id": interaction.id,
            "reactions": [],
            "embeds": [],
            "mention_everyone": False,
            "tts": False,
            "pinned": False,
            "edited_timestamp": None,
            "type": discord.MessageType.default.value,
            "flags": 0,
            "content": "",
            "mentions": [],
            "mention_roles": [],
            "attachments": [],
        },
    )
    message.author = interaction.user

    return commands.Context[StatsBot](
        message=message,
        bot=bot,
        view=StringView(""),
        prefix="?",
        interaction=interaction,
    )


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
    ctx = _panel_command_context(interaction)
    try:
        # Command.invoke() only ever accepts `ctx` — it does not forward extra
        # positional/keyword arguments. Calling it with args from a modal used
        # to raise a TypeError and silently break those panel buttons. Calling
        # the command's underlying .callback directly accepts normal args.
        # Permission checks normally run inside invoke(); the panel already
        # re-checks permissions via _panel_user_can_run()/require_* before
        # ever reaching this point, so nothing is skipped.
        if isinstance(command_callback, commands.Command):
            return await command_callback.callback(ctx, *args, **kwargs)
        return await command_callback(ctx, *args, **kwargs)
    except commands.MissingPermissions as exc:
        missing = ", ".join(exc.missing_permissions) if exc.missing_permissions else "required permissions"
        raise PermissionError(f"You need **{missing}** for this command.") from exc
    except commands.CheckFailure as exc:
        raise PermissionError("You are not allowed to run this command.") from exc
    except commands.MissingRequiredArgument as exc:
        raise ValueError(f"Missing required argument: {exc.param.name}") from exc


class CommentPanelView(discord.ui.View):
    """Persistent staff control panel with categorized command shortcuts."""

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
            await self._reply(interaction, "This action must be used inside a server.")
            return
        if require_text_channel and not isinstance(interaction.channel, discord.TextChannel):
            await self._reply(interaction, "This action must be used in a text channel.")
            return

        allowed, error = _panel_user_can_run(
            interaction,
            require_auth=require_auth,
            require_manage_guild=require_manage_guild,
        )
        if not allowed:
            await self._reply(interaction, error or "You are not allowed to use this action.")
            return

        await interaction.response.defer(ephemeral=True)
        try:
            await _invoke_existing_command(interaction, command, **command_kwargs)
            await interaction.followup.send(success_message or "Done.", ephemeral=True)
        except PermissionError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"Action failed: {exc}", ephemeral=True)

    async def _open_modal(
        self,
        interaction: discord.Interaction,
        modal: discord.ui.Modal,
        *,
        require_auth: bool = False,
        require_manage_guild: bool = False,
    ) -> None:
        allowed, error = _panel_user_can_run(
            interaction,
            require_auth=require_auth,
            require_manage_guild=require_manage_guild,
        )
        if not allowed:
            await self._reply(interaction, error or "You are not allowed to use this action.")
            return
        await interaction.response.send_modal(modal)

    # --- Management ---
    @discord.ui.button(
        label="Post Panel",
        style=discord.ButtonStyle.secondary,
        custom_id="control_panel:mgmt:post_panel",
        emoji="📋",
        row=0,
    )
    async def post_panel_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        await self._run_command(
            interaction,
            setup_command_spanel_cmd,
            require_auth=True,
            require_guild=True,
            require_text_channel=True,
            success_message="Control panel posted in this channel.",
        )

    @discord.ui.button(
        label="Setup Stats",
        style=discord.ButtonStyle.primary,
        custom_id="control_panel:mgmt:setup_stats",
        emoji="📊",
        row=0,
    )
    async def setup_stats_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        await self._run_command(
            interaction,
            setup_stats_cmd,
            require_auth=True,
            require_guild=True,
            require_text_channel=True,
            success_message="Stats channels created and configured.",
        )

    @discord.ui.button(
        label="Staff App",
        style=discord.ButtonStyle.primary,
        custom_id="control_panel:mgmt:staff_app",
        emoji="🧑‍💼",
        row=0,
    )
    async def staff_app_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        await self._run_command(
            interaction,
            setup_staff_app_cmd,
            require_manage_guild=True,
            success_message="Staff application panel posted.",
        )

    @discord.ui.button(
        label="Refresh Stats",
        style=discord.ButtonStyle.success,
        custom_id="control_panel:mgmt:refresh_stats",
        emoji="🔄",
        row=0,
    )
    async def refresh_stats_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        await self._run_command(
            interaction,
            refresh_stats_cmd,
            require_auth=True,
            success_message="Stats channel refreshed.",
        )

    # --- Moderation ---
    @discord.ui.button(
        label="Verify Reminder",
        style=discord.ButtonStyle.danger,
        custom_id="control_panel:mod:verify_reminder",
        emoji="📩",
        row=1,
    )
    async def verify_reminder_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        await self._run_command(
            interaction,
            send_not_verify_cmd,
            require_auth=True,
            require_guild=True,
            success_message="Default verification reminder sent to not-verify members.",
        )

    @discord.ui.button(
        label="Custom DM",
        style=discord.ButtonStyle.primary,
        custom_id="control_panel:mod:custom_dm",
        emoji="✉️",
        row=1,
    )
    async def custom_dm_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        await self._open_modal(interaction, SendNotVerifyModal(), require_auth=True)

    # --- Utility ---
    @discord.ui.button(
        label="Restart Lounge",
        style=discord.ButtonStyle.danger,
        custom_id="control_panel:util:restart_lounge",
        emoji="🔁",
        row=2,
    )
    async def restart_lounge_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        await self._run_command(
            interaction,
            restart_voice_lounge_cmd,
            require_auth=True,
            require_guild=True,
            success_message="Voice lounge restart requested.",
        )

    @discord.ui.button(
        label="Nick Review",
        style=discord.ButtonStyle.secondary,
        custom_id="control_panel:util:nick_review",
        emoji="🔎",
        row=2,
    )
    async def nick_review_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        await self._run_command(
            interaction,
            get_nick_review_cmd,
            require_auth=True,
            require_guild=True,
        )

    @discord.ui.button(
        label="Ping",
        style=discord.ButtonStyle.secondary,
        custom_id="control_panel:util:ping",
        emoji="📡",
        row=2,
    )
    async def ping_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        await self._run_command(interaction, ping_cmd)

    @discord.ui.button(
        label="Nick Panel",
        style=discord.ButtonStyle.secondary,
        custom_id="control_panel:util:nick_panel",
        emoji="🧾",
        row=3,
    )
    async def nick_panel_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        await self._run_command(
            interaction,
            setup_nick_cmd,
            require_auth=True,
            require_text_channel=True,
            success_message="Nickname request panel posted.",
        )

    @discord.ui.button(
        label="Set Review Channel",
        style=discord.ButtonStyle.secondary,
        custom_id="control_panel:util:set_review_channel",
        emoji="⚙️",
        row=3,
    )
    async def set_review_channel_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        del button
        await self._open_modal(interaction, SetNickReviewModal(), require_auth=True)


async def _post_control_panel(channel: discord.abc.Messageable) -> discord.Message:
    """Build and send the Panel System (staff control panel) embed + view."""
    embed = _build_control_panel_embed()
    if bot.user is not None:
        embed.set_author(
            name="Legends Tunisia Bot",
            icon_url=bot.user.display_avatar.url,
        )
    view = CommentPanelView()
    message = await channel.send(embed=embed, view=view)
    bot.add_view(view, message_id=message.id)
    return message


async def setup_command_spanel_cmd(ctx: commands.Context[StatsBot]) -> None:
    """Legacy bridge so old panel buttons can still repost the panel via a Context."""
    await _post_control_panel(ctx.channel)


@bot.tree.command(
    name="panel-system",
    description="Post the staff control panel (staff commands) in this channel.",
)
@app_commands.guild_only()
async def panel_system_command(interaction: discord.Interaction) -> None:
    """Slash-command entry point for the Panel System — staff/admin only."""
    if not _interaction_is_authorized(interaction):
        await _reply_ephemeral(interaction, "You need to be staff or an admin to use this command.")
        return
    if not isinstance(interaction.channel, discord.TextChannel):
        await _reply_ephemeral(interaction, "This command must be used in a text channel.")
        return

    try:
        await _post_control_panel(interaction.channel)
        await _reply_ephemeral(interaction, "✅ **Panel System** posted in this channel.")
    except Exception as exc:
        await _reply_ephemeral(interaction, f"Failed to post control panel: {exc}")


async def _handle_legacy_panel_interaction(
    interaction: discord.Interaction,
    custom_id: str,
) -> bool:
    """Handle older panels that still use comment_panel:* button IDs."""
    panel = CommentPanelView()

    if custom_id == "comment_panel:changename":
        if interaction.guild is None:
            await _reply_ephemeral(interaction, "This action must be used inside a server.")
            return True
        await interaction.response.send_modal(ChangeNameModal())
        return True

    legacy_modals: dict[str, tuple[discord.ui.Modal, dict[str, Any]]] = {
        "comment_panel:customnotice": (
            SendNotVerifyModal(),
            {"require_auth": True},
        ),
        "comment_panel:poststaffpanels": (
            PostStaffPanelsModal(),
            {"require_manage_guild": True},
        ),
        "comment_panel:setnickreview": (
            SetNickReviewModal(),
            {"require_auth": True},
        ),
    }
    if custom_id in legacy_modals:
        modal, opts = legacy_modals[custom_id]
        allowed, error = _panel_user_can_run(interaction, **opts)
        if not allowed:
            await _reply_ephemeral(interaction, error or "You are not allowed to use this action.")
            return True
        await interaction.response.send_modal(modal)
        return True

    legacy_commands: dict[str, tuple[Any, dict[str, Any]]] = {
        "comment_panel:postpanel": (
            setup_command_spanel_cmd,
            {
                "require_auth": True,
                "require_guild": True,
                "require_text_channel": True,
                "success_message": "Control panel posted in this channel.",
            },
        ),
        "comment_panel:setupstats": (
            setup_stats_cmd,
            {
                "require_auth": True,
                "require_guild": True,
                "require_text_channel": True,
                "success_message": "Stats channels created and configured.",
            },
        ),
        "comment_panel:setupstaffapp": (
            setup_staff_app_cmd,
            {
                "require_manage_guild": True,
                "success_message": "Staff application panel posted.",
            },
        ),
        "comment_panel:refreshstats": (
            refresh_stats_cmd,
            {
                "require_auth": True,
                "success_message": "Stats channel refreshed.",
            },
        ),
        "comment_panel:sendnotice": (
            send_not_verify_cmd,
            {
                "require_auth": True,
                "require_guild": True,
                "success_message": "Default verification reminder sent to not-verify members.",
            },
        ),
        "comment_panel:restartlounge": (
            restart_voice_lounge_cmd,
            {
                "require_auth": True,
                "require_guild": True,
                "success_message": "Voice lounge restart requested.",
            },
        ),
        "comment_panel:getnickreview": (
            get_nick_review_cmd,
            {
                "require_auth": True,
                "require_guild": True,
            },
        ),
        "comment_panel:ping": (ping_cmd, {}),
        "comment_panel:setupnick": (
            setup_nick_cmd,
            {
                "require_auth": True,
                "require_text_channel": True,
                "success_message": "Nickname request panel posted.",
            },
        ),
    }
    if custom_id in legacy_commands:
        command, opts = legacy_commands[custom_id]
        await panel._run_command(interaction, command, **opts)
        return True

    return False


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
        label="What nickname would you like?",
        placeholder="Example: Ahmed — keep it respectful and easy to read (max 32 characters)",
        max_length=32,
        required=True,
    )

    def __init__(
        self,
        requester: discord.Member | discord.User,
        *,
        admin_channel: discord.TextChannel | None,
    ):
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

        if not isinstance(self.requester, discord.Member):
            await interaction.response.send_message(
                "Could not resolve your member profile.",
                ephemeral=True,
            )
            return

        try:
            await self.requester.edit(nick=requested, reason="Self-service nickname change")
            await interaction.response.send_message(
                f"Your nickname has been changed to **{requested}**.",
                ephemeral=True,
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "I don't have permission to change your nickname.",
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.response.send_message(
                f"Could not change your nickname: {exc}",
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

        admin_channel = self.admin_channel
        if admin_channel is None:
            saved_id = _nick_config.get("review_channel_id")
            if saved_id:
                saved_channel = interaction.guild.get_channel(int(saved_id))
                if isinstance(saved_channel, discord.TextChannel):
                    admin_channel = saved_channel

        try:
            await interaction.response.send_modal(
                NicknameRequestModal(
                    requester=interaction.user,
                    admin_channel=admin_channel,
                )
            )
        except Exception as exc:
            print(f"[nickname] failed to open modal: {exc}")
            try:
                await interaction.response.send_message(
                    "Unable to open nickname request modal right now. Please try again.",
                    ephemeral=True,
                )
            except Exception:
                pass


class AdminApproveView(discord.ui.View):
    def __init__(self, requester_id: int, requested_nick: str):
        super().__init__(timeout=None)
        self.requester_id = requester_id
        self.requested_nick = requested_nick

    def _ensure_request(self, interaction: discord.Interaction) -> bool:
        if self.requester_id and self.requested_nick:
            return True
        message = interaction.message
        if message is None or not message.embeds:
            return False
        embed = message.embeds[0]
        footer_text = embed.footer.text if embed.footer else ""
        if footer_text.startswith("Requester ID: "):
            try:
                self.requester_id = int(footer_text.removeprefix("Requester ID: ").strip())
            except ValueError:
                return False
        description = embed.description or ""
        match = re.search(r"Requested nickname: \*\*(.+?)\*\*", description)
        if match:
            self.requested_nick = match.group(1)
        return bool(self.requester_id and self.requested_nick)

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
            await interaction.response.send_message(
                "You are not allowed to approve nickname requests.",
                ephemeral=True,
            )
            return

        if not self._ensure_request(interaction):
            await interaction.response.send_message("Could not read this request.", ephemeral=True)
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
            await member.edit(
                nick=self.requested_nick,
                reason=f"Approved by {interaction.user}",
            )
            await interaction.response.send_message(
                f"Nickname for {member.mention} changed to **{self.requested_nick}**.",
            )
            for child in self.children:
                if isinstance(child, discord.ui.Button):
                    child.disabled = True
            if interaction.message is not None:
                await interaction.message.edit(view=self)
        except discord.Forbidden:
            await interaction.response.send_message(
                "I don't have permission to change that member's nickname.",
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.response.send_message(f"Failed to change nickname: {exc}", ephemeral=True)

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger, custom_id="nickrequest:reject")
    async def reject(self, interaction: discord.Interaction, button: Any) -> None:
        del button
        if not await self._is_authorized(interaction.user):
            await interaction.response.send_message(
                "You are not allowed to reject nickname requests.",
                ephemeral=True,
            )
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
    """Post the nickname-request panel in the configured change-nickname channel.

    Usage: `?setupnick` to post in the configured panel channel, or
    `?setupnick #review` to also set the staff review channel for requests.
    """
    panel_channel = await _get_configured_text_channel(bot, NICK_PANEL_CHANNEL_ID, ctx.guild)
    if panel_channel is None:
        await ctx.send("Could not find the configured nickname panel channel.")
        return

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
    message = await panel_channel.send(embed=embed, view=view)
    bot.add_view(view, message_id=message.id)
    await ctx.send(
        f"Nickname request panel posted in {panel_channel.mention}.",
        delete_after=10,
    )


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