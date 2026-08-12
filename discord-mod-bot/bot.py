# ============================================================
# Bot moderation (discord.py) — yefsa5 links/kalmet mamnou3a
# fi channels me7ddin, w yeb3ath report kamel fi DM lel owner.
# ============================================================

import os
import logging
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import discord
from discord.ext import commands
from dotenv import load_dotenv

from config import WATCHED_CHANNELS, LINK_REGEX, BANNED_WORDS, MONITORED_ROLE_IDS

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
OWNER_ID = os.getenv("OWNER_ID")

if not TOKEN or not OWNER_ID:
    raise SystemExit(
        "[ERROR] 5ali te3mer DISCORD_TOKEN w OWNER_ID fi fichier .env (chouf .env.example)."
    )

OWNER_ID = int(OWNER_ID)

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
log = logging.getLogger("mod-bot")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


def find_banned_word(content: str):
    lower = content.lower()
    for w in BANNED_WORDS:
        if w.lower() in lower:
            return w
    return None


@bot.event
async def on_ready():
    log.info(f"Connecté kif {bot.user}")
    log.info(f"Ye7ares 3la {len(WATCHED_CHANNELS)} channels.")
    log.info(f"Ye7ass biss b {len(MONITORED_ROLE_IDS)} rôles (staff/admins).")


@bot.event
async def on_message(message: discord.Message):
    try:
        # Zid bots ma3endehomch check
        if message.author.bot:
            return

        # Ken el channel mahouch fi liste el mora9aba, matdi rou7ek
        if message.channel.id not in WATCHED_CHANNELS:
            await bot.process_commands(message)
            return

        # El bot ye7ass BISS b members eli 3andhom wa7ed mel MONITORED_ROLE_IDS.
        # Member 3adi (mafamech rôle staff) → matdi rou7ek, matet7assbouch.
        member = message.author
        if not isinstance(member, discord.Member):
            await bot.process_commands(message)
            return
        role_ids = {r.id for r in member.roles}
        if not (role_ids & MONITORED_ROLE_IDS):
            await bot.process_commands(message)
            return

        content = message.content or ""
        has_link = bool(LINK_REGEX.search(content))
        banned_word = find_banned_word(content)

        if not has_link and not banned_word:
            await bot.process_commands(message)
            return

        reason = "Link" if has_link else f'Kelma mamnou3a: "{banned_word}"'

        info = {
            "username": str(message.author),
            "user_id": message.author.id,
            "channel": message.channel.name,
            "channel_id": message.channel.id,
            "content": content,
            "reason": reason,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # Efsa5 el message
        try:
            await message.delete()
        except discord.Forbidden:
            log.warning("Ma3andich permission nefsa5 el message.")
        except discord.HTTPException as e:
            log.warning(f"Ma9dertch nefsa5 el message: {e}")

        # Eb3ath report fi DM lel owner
        owner = await bot.fetch_user(OWNER_ID)
        if owner:
            embed = discord.Embed(
                title="🚨 Message et7a5a",
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(
                name="User",
                value=f"{info['username']} (`{info['user_id']}`)",
                inline=False,
            )
            embed.add_field(
                name="Channel",
                value=f"#{info['channel']} (`{info['channel_id']}`)",
                inline=False,
            )
            embed.add_field(name="Sbeb", value=info["reason"], inline=False)
            embed.add_field(
                name="Contenu",
                value=(info["content"][:1000] or "*(vide/attachment)*"),
                inline=False,
            )
            try:
                await owner.send(embed=embed)
            except discord.Forbidden:
                log.warning("Ma9dertch neb3ath DM lel owner (DM mgha5).")

        log.info(
            f"Et7a5a message mte3 {info['username']} fi #{info['channel']} — {reason}"
        )

        await bot.process_commands(message)
    except Exception as e:
        log.exception(f"Erreur fi on_message: {e}")


class _HealthHandler(BaseHTTPRequestHandler):
    """Minimal HTTP server so Render (free web service) health checks pass."""

    def _send_ok(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()

    def do_GET(self):
        self._send_ok()
        self.wfile.write(b"discord-mod-bot is running")

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

    bot.run(TOKEN)
