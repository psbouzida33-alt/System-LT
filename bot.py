"""
Legends Tunisia — Server Stats Bot
Updates locked voice channels: member count + Morocco live clock.
"""
import asyncio
import os
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

from config import (
    CLOCK_UPDATE_SECONDS,
    MEMBER_COUNT_EXCLUDE_BOTS,
    STATS_TIME_LABEL,
    STATS_TIMEZONE,
    TOKEN,
    load_stats_config,
    save_stats_config,
)

load_dotenv()

if not TOKEN:
    raise SystemExit(
        "Missing bot token. Set DISCORD_BOT_TOKEN or LEVELS_BOT_TOKEN in .env"
    )

intents = discord.Intents.default()
intents.guilds = True
intents.members = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    activity=discord.Activity(type=discord.ActivityType.watching, name="server stats"),
)

_last_member_count: int | None = None
_stats_config = load_stats_config()


def _guild_member_count(guild: discord.Guild) -> int:
    if MEMBER_COUNT_EXCLUDE_BOTS:
        return sum(1 for member in guild.members if not member.bot)
    return guild.member_count or len(guild.members)


def _member_channel_name(count: int) -> str:
    return f"👤 | Membres: {count}"


def _clock_channel_name(now: datetime) -> str:
    return f"🇲🇦 {STATS_TIME_LABEL}: {now.strftime('%H:%M:%S')}"


def _stat_channel_overwrites(guild: discord.Guild) -> dict:
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


async def _safe_edit_channel_name(
    channel: discord.abc.GuildChannel | None,
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


async def update_member_stats_channel(guild: discord.Guild, *, force: bool = False) -> None:
    global _last_member_count

    channel_id = _stats_config.get("member_channel_id")
    if not channel_id:
        return

    count = _guild_member_count(guild)
    if not force and count == _last_member_count:
        return

    channel = guild.get_channel(int(channel_id))
    if await _safe_edit_channel_name(channel, _member_channel_name(count), label="member count"):
        _last_member_count = count


async def update_clock_stats_channel(guild: discord.Guild) -> None:
    channel_id = _stats_config.get("clock_channel_id")
    if not channel_id:
        return

    now = datetime.now(ZoneInfo(STATS_TIMEZONE))
    channel = guild.get_channel(int(channel_id))
    await _safe_edit_channel_name(channel, _clock_channel_name(now), label="clock")


async def refresh_all_stats(*, force_members: bool = False) -> None:
    guild = _get_configured_guild()
    if guild is None:
        return
    await update_member_stats_channel(guild, force=force_members)
    await update_clock_stats_channel(guild)


@bot.event
async def on_ready():
    print(f"Stats bot online as {bot.user} ({len(bot.guilds)} server(s))")
    if not _stats_config.get("member_channel_id") or not _stats_config.get("clock_channel_id"):
        print("No stat channels configured yet. Run !setupstats in your server.")
    if not update_clock_task.is_running():
        update_clock_task.start()
    await refresh_all_stats(force_members=True)


@bot.event
async def on_member_join(member: discord.Member):
    if member.guild.id != _stats_config.get("guild_id"):
        return
    await update_member_stats_channel(member.guild)


@bot.event
async def on_member_remove(member: discord.Member):
    if member.guild.id != _stats_config.get("guild_id"):
        return
    global _last_member_count
    _last_member_count = None
    await update_member_stats_channel(member.guild, force=True)


@tasks.loop(seconds=CLOCK_UPDATE_SECONDS)
async def update_clock_task():
    guild = _get_configured_guild()
    if guild is None:
        return
    await update_clock_stats_channel(guild)


@update_clock_task.before_loop
async def before_clock_task():
    await bot.wait_until_ready()


@bot.command(name="setupstats")
@commands.has_permissions(manage_guild=True)
async def setup_stats_cmd(ctx: commands.Context):
    """Create the two stat voice channels (locked, display-only)."""
    guild = ctx.guild
    overwrites = _stat_channel_overwrites(guild)
    tz = ZoneInfo(STATS_TIMEZONE)
    now = datetime.now(tz)
    count = _guild_member_count(guild)

    status = await ctx.send("Creating stat channels…")

    try:
        clock_channel = await guild.create_voice_channel(
            name=_clock_channel_name(now),
            overwrites=overwrites,
            reason="Stats bot setup — live clock",
        )
        member_channel = await guild.create_voice_channel(
            name=_member_channel_name(count),
            overwrites=overwrites,
            reason="Stats bot setup — member counter",
        )

        try:
            await member_channel.edit(position=0)
            await clock_channel.edit(position=0)
        except discord.HTTPException:
            pass

        global _stats_config, _last_member_count
        _stats_config = {
            "guild_id": guild.id,
            "member_channel_id": member_channel.id,
            "clock_channel_id": clock_channel.id,
        }
        save_stats_config(_stats_config)
        _last_member_count = count

        embed = discord.Embed(
            title="Stat channels ready",
            description=(
                f"Member counter: {member_channel.mention}\n"
                f"Live clock: {clock_channel.mention}\n\n"
                "Both channels are locked — display only.\n"
                "The bot will update them automatically."
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
async def refresh_stats_cmd(ctx: commands.Context):
    """Force-refresh member count and clock channels."""
    await refresh_all_stats(force_members=True)
    await ctx.send("Stats channels refreshed.", delete_after=8)


@bot.command(name="ping")
async def ping_cmd(ctx: commands.Context):
    await ctx.send(f"Pong — `{round(bot.latency * 1000)}ms`", delete_after=10)


@setup_stats_cmd.error
@refresh_stats_cmd.error
async def admin_cmd_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You need **Manage Server** permission for this command.")


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
