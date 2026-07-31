"""
Legends Tunisia — Server Stats Bot
Updates locked voice channels: member count + live clock.
"""
import asyncio
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, cast
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

from config import (
    BOT_VOICE_CHANNEL_ID,
    VERIFICATION_VOICE_CHANNEL_IDS,
    CLOCK_ROTATION_SECONDS,
    CLOCK_UPDATE_SECONDS,
    MEMBER_COUNT_EXCLUDE_BOTS,
    STATS_CATEGORY_NAME,
    TOKEN,
    VERIFICATION_CHANNEL_KEYWORDS,
    VERIFICATION_MUSIC_URL,
    WORLD_CLOCKS,
    load_stats_config,
    save_stats_config,
    load_nick_config,
    save_nick_config,
)

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

YTDL_OPTIONS: dict[str, Any] = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "default_search": "auto",
    "source_address": "0.0.0.0",
    "noplaylist": True,
    "http_headers": {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://www.youtube.com/",
    },
    "extractor_args": {
        "youtube": {
            "player_client": "android",
        }
    },
}

YT_DLP_COOKIE_FILE = os.getenv("YTDL_COOKIE_FILE")
if YT_DLP_COOKIE_FILE:
    YTDL_OPTIONS["cookiefile"] = YT_DLP_COOKIE_FILE

FFMPEG_OPTIONS: dict[str, Any] = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

load_dotenv()

if not TOKEN:
    raise SystemExit(
        "Missing bot token. Set DISCORD_BOT_TOKEN or LEVELS_BOT_TOKEN in .env"
    )

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.voice_states = True
intents.message_content = True

class StatsBot(commands.Bot):
    async def setup_hook(self) -> None:
        self.add_view(NicknameRequestView())


bot = StatsBot(
    command_prefix="?",
    intents=intents,
    activity=discord.Activity(type=discord.ActivityType.watching, name="server stats"),
)

_last_member_count: int | None = None
_last_stats_status: str | None = None
_stats_config: dict[str, int | None] = load_stats_config()
_nick_config: dict[str, int | None] = load_nick_config()


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


def _parse_duration(duration: str) -> timedelta | None:
    duration = duration.strip().lower()
    if not duration:
        return None
    match = re.fullmatch(r"(\d+)\s*([smhd])", duration)
    if not match:
        return None
    value = int(match.group(1))
    unit = match.group(2)
    seconds = {
        "s": 1,
        "m": 60,
        "h": 3600,
        "d": 86400,
    }[unit]
    return timedelta(seconds=value * seconds)


def _get_timeout_until(duration: str) -> datetime | None:
    parsed = _parse_duration(duration)
    if parsed is None:
        return None
    return datetime.now(timezone.utc) + parsed


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

    def __init__(self, client: discord.Client, channel: discord.VoiceChannel):
        self.client = client
        self.channel = channel
        self._connected = False

    async def connect(
        self,
        *,
        timeout: float,
        reconnect: bool,
        self_deaf: bool = False,
        self_mute: bool = False,
    ) -> None:
        # discord.py passes self_deaf=False and self_mute=False by default.
        voice_channel = cast(discord.VoiceChannel, self.channel)
        await voice_channel.guild.change_voice_state(
            channel=voice_channel,
            self_deaf=False,
            self_mute=False,
        )
        self._connected = True

    async def disconnect(self, *, force: bool = False) -> None:
        voice_channel = cast(discord.VoiceChannel, self.channel)
        await voice_channel.guild.change_voice_state(channel=None)
        self._connected = False
        try:
            key_id, _ = cast(Any, self.channel)._get_voice_client_key()
            client_connection = cast(Any, getattr(self.client, "_connection", None))
            if client_connection is not None:
                client_connection._remove_voice_client(key_id)
        except Exception:
            pass

    async def on_voice_state_update(self, data: Any) -> None:
        pass

    async def on_voice_server_update(self, data: Any) -> None:
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


def _is_verification_channel(channel: discord.abc.GuildChannel | None) -> bool:
    if not isinstance(channel, discord.VoiceChannel):
        return False
    if VERIFICATION_VOICE_CHANNEL_IDS:
        result = channel.id in VERIFICATION_VOICE_CHANNEL_IDS
    else:
        result = any(keyword in channel.name.lower() for keyword in VERIFICATION_CHANNEL_KEYWORDS)
    print(f"_is_verification_channel: {channel.name} ({channel.id}) -> {result}")
    return result


def _get_guild_voice_client(guild: discord.Guild) -> discord.VoiceClient | None:
    for client in bot.voice_clients:
        if isinstance(client, discord.VoiceClient) and client.guild.id == guild.id:
            return client
    return None


async def _disconnect_guild_voice_client(guild: discord.Guild) -> None:
    client = _get_guild_voice_client(guild)
    if client is None:
        return
    try:
        await client.disconnect(force=True)
    except Exception as exc:
        print(f"Failed to disconnect voice client: {exc}")


async def _restore_lounge_voice(guild: discord.Guild) -> None:
    voice_channel = _find_voice_lounge_channel()
    if voice_channel is None or voice_channel.guild.id != guild.id:
        return
    await _disconnect_guild_voice_client(guild)
    await _join_voice_lounge()


def _extract_ytdl_info(url: str) -> dict[str, Any]:
    if yt_dlp is None:
        raise RuntimeError("yt_dlp is not installed")
    ytdl = yt_dlp.YoutubeDL(cast(Any, YTDL_OPTIONS))
    try:
        return cast(dict[str, Any], ytdl.extract_info(url, download=False))
    except Exception as exc:
        message = str(exc)
        if "Sign in to confirm you\'re not a bot" in message or "Sign in to confirm you are not a bot" in message:
            print(
                "YouTube blocked playback for this video. "
                "Set YTDL_COOKIE_FILE to a valid cookies.txt file in the environment "
                "or use a different song URL."
            )
        raise


async def _get_stream_url(url: str) -> tuple[str, str]:
    loop = asyncio.get_running_loop()
    info = await loop.run_in_executor(None, lambda: _extract_ytdl_info(url))
    if "entries" in info:
        info = info["entries"][0]
    return str(info["url"]), str(info.get("title", url))


def _schedule_verification_music_replay(channel: discord.VoiceChannel, error: Exception | None) -> None:
    if error is not None:
        print(f"Verification music playback ended with error: {error}")
        return
    try:
        bot.loop.call_soon_threadsafe(asyncio.create_task, _play_verification_music(channel))
    except Exception as exc:
        print(f"Failed to schedule verification replay: {exc}")


def _make_verification_replay_callback(channel: discord.VoiceChannel):
    def _callback(error: Exception | None) -> None:
        _schedule_verification_music_replay(channel, error)

    return _callback


async def _play_verification_music(channel: discord.VoiceChannel) -> None:
    guild = channel.guild
    current_vc = _get_guild_voice_client(guild)
    if current_vc is not None:
        current_channel = getattr(current_vc, "channel", None)
        if current_channel == channel:
            vc = current_vc
            if not vc.is_playing():
                try:
                    stream_url, title = await _get_stream_url(VERIFICATION_MUSIC_URL)
                    source = discord.PCMVolumeTransformer(
                        cast(Any, discord.FFmpegPCMAudio(stream_url, **FFMPEG_OPTIONS)),
                        volume=0.25,
                    )
                    vc.play(source, after=_make_verification_replay_callback(channel))
                    print(f"Playing verification music in {channel.name}: {title}")
                except Exception as exc:
                    print(f"Failed to play verification music: {exc}")
            return
        try:
            if current_vc.is_connected():
                await current_vc.move_to(channel)
                print(f"Moved existing voice client to verification channel {channel.name}.")
            else:
                await _disconnect_guild_voice_client(guild)
                raise RuntimeError("Existing voice client not connected")
        except Exception as exc:
            print(f"Failed to move existing voice client: {exc}")
            await _disconnect_guild_voice_client(guild)

    try:
        stream_url, title = await _get_stream_url(VERIFICATION_MUSIC_URL)
    except Exception as exc:
        print(f"Failed to resolve verification music URL: {exc}")
        return

    try:
        voice_client = await channel.connect(self_deaf=False, self_mute=False)
        print(f"Connected to verification channel {channel.name}.")
    except Exception as exc:
        print(f"Failed to connect to verification channel {channel.name}: {exc}")
        return

    try:
        source = discord.PCMVolumeTransformer(
            cast(Any, discord.FFmpegPCMAudio(stream_url, **FFMPEG_OPTIONS)),
            volume=0.25,
        )
        voice_client.play(source, after=_make_verification_replay_callback(channel))
        print(f"Connected to {channel.name} and playing verification music: {title}")
    except Exception as exc:
        print(f"Failed to play verification music in {channel.name}: {exc}")


async def _ensure_voice_unmuted(guild: discord.Guild, channel: discord.VoiceChannel | discord.StageChannel) -> None:
    await guild.change_voice_state(channel=channel, self_deaf=False, self_mute=False)


async def _join_voice_lounge() -> None:
    voice_channel = _find_voice_lounge_channel()
    if voice_channel is None:
        print(f"WARNING: voice lounge channel {BOT_VOICE_CHANNEL_ID} not found.")
        return

    member = voice_channel.guild.me
    if member and member.voice and member.voice.channel:
        if member.voice.channel.id == BOT_VOICE_CHANNEL_ID:
            if member.voice.self_deaf or member.voice.self_mute:
                try:
                    await _ensure_voice_unmuted(voice_channel.guild, voice_channel)
                    print("Re-applied unmute/undeaf in voice lounge.")
                except Exception as exc:
                    print(f"Failed to restore audio state in voice lounge: {exc}")
            current_vc = _get_guild_voice_client(voice_channel.guild)
            if current_vc is not None and not current_vc.is_playing():
                await _play_verification_music(voice_channel)
            return

    try:
        await voice_channel.connect(self_deaf=False, self_mute=False)
        print(f"Connected to voice lounge (unmuted): {voice_channel.name}")
        await _play_verification_music(voice_channel)
    except discord.ClientException:
        pass
    except Exception as exc:
        print(f"Failed to join voice lounge: {exc}")


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
    if custom_id != "nickrequest:open":
        return

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


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
):
    if bot.user is None:
        return

    if member.id == bot.user.id:
        if after.channel and after.channel.id == BOT_VOICE_CHANNEL_ID:
            if after.self_deaf or after.self_mute:
                await asyncio.sleep(0.5)
                try:
                    await _ensure_voice_unmuted(member.guild, after.channel)
                except Exception as exc:
                    print(f"Failed to enforce audio state in lounge: {exc}")
            return

        if before.channel and before.channel.id == BOT_VOICE_CHANNEL_ID:
            if after.channel is None or not _is_verification_channel(after.channel):
                await asyncio.sleep(2)
                await _join_voice_lounge()
        return

    if after.channel and isinstance(after.channel, discord.VoiceChannel):
        if _is_verification_channel(after.channel):
            print(
                f"Voice update: {member} entered verification channel {after.channel.name} "
                f"({after.channel.id}); starting verification music with {VERIFICATION_MUSIC_URL}"
            )
            await _play_verification_music(after.channel)
            return

    if before.channel and _is_verification_channel(before.channel):
        channel = member.guild.get_channel(before.channel.id)
        if isinstance(channel, discord.VoiceChannel):
            human_members = [m for m in channel.members if not m.bot]
            if not human_members:
                await _restore_lounge_voice(member.guild)


@bot.event
async def on_member_join(member: discord.Member):
    if member.guild.id != _stats_config.get("guild_id"):
        return
    await update_stats_channel(member.guild)


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


@bot.command(name="setupstats")
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
async def setup_stats_cmd(ctx: commands.Context[StatsBot]):
    """Create one stats voice channel: name = members, status = world clock."""
    guild = ctx.guild
    if guild is None:
        await ctx.send("This command can only be used in a server.")
        return
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


@bot.command(name="refreshstats")
@commands.has_permissions(manage_guild=True)
async def refresh_stats_cmd(ctx: commands.Context[StatsBot]):
    """Force-refresh the stats channel."""
    await refresh_all_stats(force_members=True)
    await ctx.send("Stats channel refreshed.", delete_after=8)


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


@bot.command(name="ban")
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
async def ban_cmd(ctx: commands.Context[StatsBot], member: discord.Member, *, reason: str):
    await member.ban(reason=reason, delete_message_days=0)
    await ctx.send(f"{member.mention} has been banned for: {reason}")


@bot.command(name="timeout")
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
async def timeout_cmd(ctx: commands.Context[StatsBot], member: discord.Member, duration: str, *, reason: str):
    until = _get_timeout_until(duration)
    if until is None:
        await ctx.send("Invalid duration format. Use 30m, 1h, or 1d.")
        return
    await member.edit(timed_out_until=until, reason=reason)
    await ctx.send(f"{member.mention} timed out until {until.isoformat()} UTC for: {reason}")


@bot.command(name="chatmute")
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
async def chatmute_cmd(ctx: commands.Context[StatsBot], member: discord.Member, duration: str, *, reason: str):
    until = _get_timeout_until(duration)
    if until is None:
        await ctx.send("Invalid duration format. Use 30m, 1h, or 1d.")
        return
    await member.edit(timed_out_until=until, reason=f"Chat mute — {reason}")
    await ctx.send(f"{member.mention} chat-muted until {until.isoformat()} UTC for: {reason}")


@bot.command(name="voicemute")
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
async def voicemute_cmd(ctx: commands.Context[StatsBot], member: discord.Member, duration: str, *, reason: str):
    until = _get_timeout_until(duration)
    if until is None:
        await ctx.send("Invalid duration format. Use 30m, 1h, or 1d.")
        return
    if member.voice and member.voice.channel:
        await member.edit(mute=True, reason=f"Voice mute — {reason}")
        await ctx.send(f"{member.mention} voice-muted in channel for: {reason}")
    else:
        await member.edit(timed_out_until=until, reason=f"Voice mute — {reason}")
        await ctx.send(f"{member.mention} voice-muted until {until.isoformat()} UTC for: {reason}")


@bot.command(name="warn")
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
async def warn_cmd(ctx: commands.Context[StatsBot], member: discord.Member, *, reason: str):
    guild_name = ctx.guild.name if ctx.guild else "this server"
    dm_text = f"You have been warned in {guild_name}.\nReason: {reason}"
    try:
        await member.send(dm_text)
    except Exception:
        pass
    await ctx.send(f"{member.mention} has been warned for: {reason}")


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
@commands.has_permissions(manage_guild=True)
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
@commands.has_permissions(manage_guild=True)
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
@commands.has_permissions(manage_guild=True)
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

    def log_message(self, format: str, *args: Any) -> None:
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
