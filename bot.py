"""
Legends Tunisia — Levels Bot
Handles voice XP, level-up cards, level roles, and Discord backup.
Run with LEVELS_BOT_TOKEN — separate from the main bot.
"""
import asyncio
import io
import json
import math
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

from level_up_card import build_level_up_card
from config import (
    BACKUP_STATE_FILE,
    BOT_VOICE_CHANNEL_ID,
    CREATE_CHANNEL_ID,
    DATA_BACKUP_CHANNEL_ID,
    DATA_DIR,
    DB_FILE,
    DISCORD_BACKUP_INTERVAL_MINUTES,
    LEGACY_DB_FILE,
    LEVEL_LOG_CHANNEL_ID,
    LEVEL_MINUTES_BASE,
    LEVEL_ROLE_IDS,
    LEVEL_ROLE_TIERS,
    LEVEL_UP_ANNOUNCE_CAP,
    LEVELS_BOT_BUILD_ID,
    MAX_VOICE_LEVEL,
    SUPPORT_CHANNEL_ID,
    VERIFICATION_1_ID,
    VERIFICATION_2_ID,
)

load_dotenv()

TOKEN = os.getenv("LEVELS_BOT_TOKEN")
if not TOKEN:
    raise SystemExit("Missing LEVELS_BOT_TOKEN. Create a second bot app on Discord.")

user_levels: dict[int, dict] = {}
_discord_backup_message_id: int | None = None
_level_up_announce_count = 0
_level_up_announce_bucket = 0
_startup_done = False

intents = discord.Intents.default()
intents.voice_states = True
intents.guilds = True
intents.message_content = True
intents.members = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    status=discord.Status.online,
    activity=discord.Activity(type=discord.ActivityType.watching, name="voice levels"),
)


def voice_minutes_for_level(level: int) -> int:
    return LEVEL_MINUTES_BASE * level * (level + 1) // 2


def level_from_voice_minutes(minutes: int) -> int:
    if minutes <= 0:
        return 0
    level = int((-1 + math.sqrt(1 + 8 * minutes / LEVEL_MINUTES_BASE)) // 2)
    return min(level, MAX_VOICE_LEVEL)


def minutes_for_next_level(current_level: int) -> int:
    return LEVEL_MINUTES_BASE * (current_level + 1)


def _level_role_id_for_level(level: int) -> int | None:
    if level < LEVEL_ROLE_TIERS[0][0]:
        return None
    role_id = None
    for threshold, rid in LEVEL_ROLE_TIERS:
        if level >= threshold:
            role_id = rid
    return role_id


async def _sync_level_voice_roles(member: discord.Member, level: int) -> None:
    guild = member.guild
    target_role_id = _level_role_id_for_level(level)
    to_remove = [
        role
        for role_id in LEVEL_ROLE_IDS
        if role_id != target_role_id and (role := guild.get_role(role_id)) and role in member.roles
    ]
    if to_remove:
        await member.remove_roles(*to_remove, reason=f"Voice level role sync (Lv {level})")
    if target_role_id:
        target_role = guild.get_role(target_role_id)
        if target_role and target_role not in member.roles:
            await member.add_roles(target_role, reason=f"Reached voice level {level}")


def _normalize_user_level_data(raw: dict) -> dict:
    if "voice_minutes" in raw:
        minutes = int(raw["voice_minutes"])
    else:
        minutes = int(raw.get("xp", 0)) // 10
    return {
        "voice_minutes": minutes,
        "level": level_from_voice_minutes(minutes),
    }


def _get_user_level_data(user_id: int) -> dict:
    return _normalize_user_level_data(user_levels.get(user_id, {}))


def _format_level_stats(user_data: dict) -> str:
    level = user_data["level"]
    minutes = user_data["voice_minutes"]
    remaining = max(0, voice_minutes_for_level(level + 1) - minutes)
    return (
        f"• **Current Level:** `Level {level}`\n"
        f"• **Voice Time:** `{minutes} min`\n"
        f"• **Next Level:** `{remaining} min` remaining"
    )


def _iter_level_voice_channels(guild: discord.Guild):
    excluded = {
        CREATE_CHANNEL_ID,
        SUPPORT_CHANNEL_ID,
        VERIFICATION_1_ID,
        VERIFICATION_2_ID,
        BOT_VOICE_CHANNEL_ID,
    }
    for channel in (*guild.voice_channels, *guild.stage_channels):
        if channel.id in excluded or not channel.members:
            continue
        yield channel


def _levels_data_score(data: dict) -> int:
    return sum(entry.get("voice_minutes", 0) for entry in data.values())


def _normalize_levels_payload(raw: dict) -> dict:
    return {int(k): _normalize_user_level_data(v) for k, v in raw.items()}


def _read_levels_file(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return _normalize_levels_payload(json.load(f))
    except Exception as e:
        print(f"Could not read {path}: {e}")
        return {}


def _migrate_legacy_db_if_needed():
    legacy = Path(LEGACY_DB_FILE)
    target = Path(DB_FILE)
    if legacy.is_file() and not target.exists():
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        legacy.replace(target)
        print(f"Migrated {LEGACY_DB_FILE} -> {DB_FILE}")


def _save_local_database_sync() -> bool:
    if not user_levels:
        return False
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        formatted_data = {str(k): v for k, v in user_levels.items()}
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(formatted_data, f, ensure_ascii=False, indent=4)
        return True
    except Exception as e:
        print(f"Local level save failed: {e}")
        return False


def _pick_best_levels_data(*datasets: dict) -> dict:
    best: dict = {}
    best_score = -1
    for data in datasets:
        if not data:
            continue
        score = _levels_data_score(data)
        if score > best_score:
            best = data
            best_score = score
    return best


def _load_backup_state() -> None:
    global _discord_backup_message_id
    path = Path(BACKUP_STATE_FILE)
    if not path.is_file():
        _discord_backup_message_id = None
        return
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        message_id = int(data.get("message_id", 0) or 0)
        _discord_backup_message_id = message_id or None
    except Exception as e:
        print(f"Could not load backup state: {e}")
        _discord_backup_message_id = None


def _save_backup_state(message_id: int | None) -> None:
    global _discord_backup_message_id
    _discord_backup_message_id = message_id
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(BACKUP_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {"message_id": message_id, "channel_id": DATA_BACKUP_CHANNEL_ID},
                f,
                ensure_ascii=False,
                indent=2,
            )
    except Exception as e:
        print(f"Could not save backup state: {e}")


def _should_announce_level_up() -> bool:
    global _level_up_announce_count, _level_up_announce_bucket
    bucket = int(time.time() // 60)
    if bucket != _level_up_announce_bucket:
        _level_up_announce_bucket = bucket
        _level_up_announce_count = 0
    if _level_up_announce_count >= LEVEL_UP_ANNOUNCE_CAP:
        return False
    _level_up_announce_count += 1
    return True


async def _await_rate_limit(exc: discord.HTTPException, *, label: str) -> None:
    wait = float(getattr(exc, "retry_after", 5) or 5) + 0.5
    print(f"{label}: rate limited, waiting {wait:.1f}s")
    await asyncio.sleep(wait)


async def _read_levels_from_discord_backup() -> dict:
    channel = bot.get_channel(DATA_BACKUP_CHANNEL_ID)
    if not channel:
        return {}
    try:
        async for message in channel.history(limit=10):
            if message.author != bot.user:
                continue
            for attachment in message.attachments:
                if attachment.filename.lower().endswith(".json"):
                    try:
                        payload = await attachment.read()
                        return _normalize_levels_payload(json.loads(payload.decode("utf-8")))
                    except Exception as e:
                        print(f"Error reading backup attachment: {e}")
            if message.content.startswith("```json"):
                clean = message.content.strip("```json").strip("```")
                return _normalize_levels_payload(json.loads(clean))
    except Exception as e:
        print(f"Error loading cloud db: {e}")
    return {}


async def load_database_from_discord():
    global user_levels
    await bot.wait_until_ready()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _migrate_legacy_db_if_needed()
    local_data = _read_levels_file(DB_FILE)
    discord_data = await _read_levels_from_discord_backup()
    user_levels = _pick_best_levels_data(local_data, discord_data)
    if user_levels:
        _save_local_database_sync()
        source = "local file" if _levels_data_score(local_data) >= _levels_data_score(discord_data) else "Discord backup"
        print(f"Loaded {len(user_levels)} user levels from {source}.")
    else:
        print("No level data found — starting fresh.")


async def save_database_to_discord() -> bool:
    if not user_levels:
        return False
    if not _save_local_database_sync():
        return False
    channel = bot.get_channel(DATA_BACKUP_CHANNEL_ID)
    if not channel:
        return False

    json_bytes = json.dumps(
        {str(k): v for k, v in user_levels.items()},
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")
    backup_embed = discord.Embed(
        title="AUTOMATIC DATA BACKUP SECURED",
        description=(
            "Automated backup for server voice levels (Levels Bot).\n"
            "**DO NOT DELETE THIS MESSAGE.**\n"
            f"Users tracked: `{len(user_levels)}` • Attachment: `levels_database.json`"
        ),
        color=discord.Color.green(),
    )

    if _discord_backup_message_id:
        try:
            old = await channel.fetch_message(_discord_backup_message_id)
            await old.delete()
        except discord.NotFound:
            _save_backup_state(None)
        except discord.HTTPException as exc:
            if exc.status == 429:
                await _await_rate_limit(exc, label="Cloud backup delete")

    for attempt in range(3):
        try:
            backup_file = discord.File(io.BytesIO(json_bytes), filename="levels_database.json")
            backup_message = await channel.send(embed=backup_embed, file=backup_file)
            _save_backup_state(backup_message.id)
            print(f"Cloud backup saved ({len(user_levels)} users).")
            return True
        except discord.HTTPException as exc:
            if exc.status == 429 and attempt < 2:
                await _await_rate_limit(exc, label="Cloud backup send")
                continue
            print(f"Cloud backup failed: {exc}")
            return False
        except Exception as e:
            print(f"Cloud backup failed: {e}")
            return False
    return False


class DummyVoiceClient(discord.VoiceProtocol):
    """Join voice without audio — keeps the bot visible in the lounge channel."""

    def __init__(self, client, channel):
        self.client = client
        self.channel = channel
        self._connected = False

    async def connect(self, *, timeout: float, reconnect: bool, self_deaf: bool = True, self_mute: bool = True) -> None:
        await self.channel.guild.change_voice_state(channel=self.channel, self_deaf=self_deaf, self_mute=self_mute)
        self._connected = True

    async def disconnect(self, *, force: bool = False) -> None:
        await self.channel.guild.change_voice_state(channel=None)
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


def _start_background_tasks():
    if not update_levels_task.is_running():
        update_levels_task.start()
    if not discord_backup_task.is_running():
        discord_backup_task.start()


@bot.event
async def on_ready():
    global _startup_done
    print(f"Levels bot logged in as {bot.user} (build {LEVELS_BOT_BUILD_ID})")

    if _startup_done:
        print("Reconnect — skipping heavy startup (avoids Discord rate limits).")
        return

    _startup_done = True
    os.environ.pop("LEVELS_LOGIN_ATTEMPT", None)

    await load_database_from_discord()
    _load_backup_state()
    print(
        f"Rate-limit settings: cloud backup every {DISCORD_BACKUP_INTERVAL_MINUTES}m, "
        f"level-up cap {LEVEL_UP_ANNOUNCE_CAP}/min"
    )

    voice_channel = bot.get_channel(BOT_VOICE_CHANNEL_ID)
    if voice_channel is None:
        for guild in bot.guilds:
            voice_channel = guild.get_channel(BOT_VOICE_CHANNEL_ID)
            if voice_channel is not None:
                break
    if voice_channel:
        try:
            await voice_channel.connect(cls=DummyVoiceClient)
            print(f"Levels bot connected to voice lounge: {voice_channel.name}")
        except Exception as e:
            print(f"Levels bot failed to join static channel: {e}")
    else:
        print(f"WARNING: voice lounge channel {BOT_VOICE_CHANNEL_ID} not found.")

    _start_background_tasks()
    print("Levels system online.")


@tasks.loop(minutes=DISCORD_BACKUP_INTERVAL_MINUTES)
async def discord_backup_task():
    try:
        await save_database_to_discord()
    except Exception as e:
        print(f"Scheduled cloud backup failed: {e}")


@discord_backup_task.before_loop
async def before_discord_backup_task():
    await bot.wait_until_ready()


@tasks.loop(minutes=1.0)
async def update_levels_task():
    data_changed = False
    for guild in bot.guilds:
        log_channel = guild.get_channel(LEVEL_LOG_CHANNEL_ID)
        for voice_channel in _iter_level_voice_channels(guild):
            for member in voice_channel.members:
                if member.bot:
                    continue
                voice = member.voice
                if voice is None or voice.self_deaf:
                    continue
                try:
                    user_id = member.id
                    user_data = _get_user_level_data(user_id)
                    old_lvl = user_data["level"]
                    user_data["voice_minutes"] += 1
                    new_calculated_level = level_from_voice_minutes(user_data["voice_minutes"])
                    user_data["level"] = new_calculated_level
                    user_levels[user_id] = user_data
                    data_changed = True

                    if new_calculated_level > old_lvl:
                        current_lvl = new_calculated_level
                        if log_channel and _should_announce_level_up():
                            try:
                                buffer = await build_level_up_card(member, old_lvl, current_lvl)
                                image_file = discord.File(buffer, filename="level_up.png")
                                embed_lvl = discord.Embed(
                                    title="LEVEL UP!",
                                    description=(
                                        f"{member.mention} reached **Level {current_lvl}**.\n"
                                        f"*Voice Time: {user_data['voice_minutes']} min*"
                                    ),
                                    color=discord.Color.from_rgb(231, 76, 60),
                                )
                                embed_lvl.set_image(url="attachment://level_up.png")
                                await log_channel.send(file=image_file, embed=embed_lvl)
                            except discord.HTTPException as exc:
                                if exc.status != 429:
                                    print(f"Level up card failed: {exc}")
                            except Exception as e:
                                print(f"Level up card failed: {e}")

                        if _level_role_id_for_level(old_lvl) != _level_role_id_for_level(current_lvl):
                            try:
                                await _sync_level_voice_roles(member, current_lvl)
                            except discord.HTTPException as exc:
                                if exc.status == 429:
                                    print(f"Level role sync rate limited for {member.id}")
                                else:
                                    print(f"Level role sync failed for {member.id}: {exc.text}")
                except Exception as e:
                    print(f"Level update failed for {member.id}: {e}")

    if data_changed:
        _save_local_database_sync()


@update_levels_task.before_loop
async def before_update_levels_task():
    await bot.wait_until_ready()


@bot.command(name="level")
async def check_user_level_cmd(ctx, member: discord.Member = None):
    """Show voice level stats. Usage: !level [@user]"""
    target_member = member or ctx.author
    if target_member.bot:
        return await ctx.send("Bots do not have voice leveling profiles.")
    user_data = _get_user_level_data(target_member.id)
    embed_cmd = discord.Embed(
        title="ARENA LEVEL REGISTRY",
        description=f"Stats for: {target_member.mention}\n\n{_format_level_stats(user_data)}",
        color=discord.Color.from_rgb(46, 204, 113),
    )
    embed_cmd.set_thumbnail(url=target_member.display_avatar.url)
    embed_cmd.set_footer(text=f"Requested by {ctx.author.name}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed_cmd)


@bot.command(name="testlevelup")
@commands.has_permissions(manage_guild=True)
async def test_levelup_cmd(ctx, member: discord.Member = None, old: int = None, new: int = None):
    """Preview level-up card. Example: !testlevelup @user 2 3"""
    target = member or ctx.author
    user_data = _get_user_level_data(target.id)
    old_lvl = old if old is not None else max(0, user_data["level"] - 1)
    new_lvl = new if new is not None else user_data["level"]
    try:
        buffer = await build_level_up_card(target, old_lvl, new_lvl)
        image_file = discord.File(buffer, filename="level_up.png")
        embed = discord.Embed(
            title="LEVEL UP! (TEST)",
            description=f"Preview: **{old_lvl} → {new_lvl}** for {target.mention}",
            color=discord.Color.from_rgb(231, 76, 60),
        )
        embed.set_image(url="attachment://level_up.png")
        await ctx.send(file=image_file, embed=embed)
    except Exception as e:
        await ctx.send(f"Level up preview failed: {e}")


@bot.command(name="ping")
async def ping_cmd(ctx):
    await ctx.send(
        f"Levels bot — `{round(bot.latency * 1000)}ms` • build `{LEVELS_BOT_BUILD_ID}`",
        delete_after=10,
    )


class _HealthHandler(BaseHTTPRequestHandler):
    def _send_ok(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()

    def do_GET(self):
        self._send_ok()
        self.wfile.write(b"Legends Tunisia levels bot is running")

    def do_HEAD(self):
        self._send_ok()

    def log_message(self, format, *args):
        pass


def _start_health_server() -> None:
    port = int(os.environ.get("PORT", "8080"))
    HTTPServer(("0.0.0.0", port), _HealthHandler).serve_forever()


_original_bot_close = bot.close


async def _close_with_level_save():
    if user_levels:
        if _save_local_database_sync():
            print("Levels saved locally before shutdown.")
    await _original_bot_close()


bot.close = _close_with_level_save


def _login_retry_wait(attempt: int) -> int:
    return min(900, max(90, 60 * attempt))


def _handle_login_rate_limit(exc: discord.HTTPException) -> None:
    attempt = int(os.environ.get("LEVELS_LOGIN_ATTEMPT", "0")) + 1
    max_attempts = 12
    if attempt >= max_attempts:
        raise SystemExit(
            "Discord login still rate-limited after multiple waits. "
            "Pause Render auto-deploy, wait 30 min, ensure only ONE service uses "
            "LEVELS_BOT_TOKEN, then deploy once."
        ) from exc

    retry_after = float(getattr(exc, "retry_after", 0) or 0)
    wait = max(_login_retry_wait(attempt), int(retry_after) + 30)
    print(
        f"Discord login rate limit (attempt {attempt}/{max_attempts}). "
        f"Waiting {wait}s before retry — do not spam redeploy."
    )
    time.sleep(wait)
    os.environ["LEVELS_LOGIN_ATTEMPT"] = str(attempt)
    os.execv(sys.executable, [sys.executable, *sys.argv])


if __name__ == "__main__":
    print(f"Starting Levels bot (PID {os.getpid()}, build {LEVELS_BOT_BUILD_ID})")

    if os.environ.get("PORT"):
        threading.Thread(target=_start_health_server, daemon=True).start()

    try:
        bot.run(TOKEN, log_handler=None)
    except discord.LoginFailure as exc:
        raise SystemExit(
            "Invalid LEVELS_BOT_TOKEN — reset the token in Discord Developer Portal "
            "and update Render Environment."
        ) from exc
    except RuntimeError as exc:
        if "Session is closed" in str(exc):
            print("Discord session closed during login — restarting with a fresh process.")
            os.execv(sys.executable, [sys.executable, *sys.argv])
        raise
    except discord.HTTPException as exc:
        if exc.status == 429:
            _handle_login_rate_limit(exc)
        raise
