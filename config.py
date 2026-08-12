"""Legends Tunisia — bot settings (edit this file, not Render env)."""
# Pushed: 2026-07-31 — non-functional metadata stamp
import json
import os
import re
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# --- Token only (secret — keep in .env locally or Render dashboard) ---
TOKEN = os.getenv("DISCORD_BOT_TOKEN") or os.getenv("LEVELS_BOT_TOKEN")

# --- Server IDs & stats layout ---
BOT_VOICE_CHANNEL_ID = 1533639367590019183
STAFF_PICK_CHANNEL_ID = 1534586562447282258
STAFF_APP_CHANNEL_ID = 1534583845884792874
NICK_PANEL_CHANNEL_ID = 1532349544417984693

def _get_env_int(name: str, default: int | None = None) -> int | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value.strip())
    except ValueError:
        print(f"Invalid integer value for {name}: {value!r}")
        return default

# --- Server IDs & stats layout ---
# --- Not-verify role notifications ---
NOT_VERIFY_ROLE_ID = _get_env_int("NOT_VERIFY_ROLE_ID", 1517593118399139840)
NOT_VERIFY_ROLE_NAME = os.getenv("NOT_VERIFY_ROLE_NAME", "not verify")
NOT_VERIFY_DM_MESSAGE = os.getenv(
    "NOT_VERIFY_DM_MESSAGE",
    "Hello {member_name}, you currently have the {role_name} role in {guild_name}. Please review the rules and press the verify button or contact the staff. Don't forget to visit https://discord.com/channels/1511674199914320082/1517597478378143937"
)

STATS_CATEGORY_NAME = "─── ❖ ── ⚙️ SYSTEM ZONE ⚙️ ── ❖ ───"

# Rotating world clocks — (flag, country name, timezone)
WORLD_CLOCKS = (
    ("🇹🇳", "Tunisia", "Africa/Tunis"),
    ("🇲🇦", "Morocco", "Africa/Casablanca"),
    ("🇩🇿", "Algeria", "Africa/Algiers"),
    ("🇱🇾", "Tripoli", "Africa/Tripoli"),
    ("🇮🇹", "Italy", "Europe/Rome"),
)

CLOCK_ROTATION_SECONDS = 60  # switch country every 1 minutes
CLOCK_UPDATE_SECONDS = 60       # refresh displayed time every second
MEMBER_COUNT_EXCLUDE_BOTS = False

DATA_DIR = Path("data")
STATS_CONFIG_FILE = DATA_DIR / "stats_config.json"


def load_stats_config() -> dict[str, int | None]:
    """Channel IDs saved by ?setupstats (persisted in data/stats_config.json)."""
    empty: dict[str, int | None] = {
        "guild_id": None,
        "category_id": None,
        "stats_channel_id": None,
    }
    if not STATS_CONFIG_FILE.is_file():
        return empty
    try:
        with open(STATS_CONFIG_FILE, encoding="utf-8") as f:
            saved = json.load(f)

        stats_channel_id = saved.get("stats_channel_id")
        if not stats_channel_id:
            stats_channel_id = saved.get("member_channel_id") or saved.get("clock_channel_id")

        return {
            "guild_id": int(saved["guild_id"]) if saved.get("guild_id") else None,
            "category_id": int(saved["category_id"]) if saved.get("category_id") else None,
            "stats_channel_id": int(stats_channel_id) if stats_channel_id else None,
        }
    except (json.JSONDecodeError, OSError, TypeError, ValueError, KeyError) as exc:
        print(f"Could not read {STATS_CONFIG_FILE}: {exc}")
        return empty


def save_stats_config(config: dict[str, int | None]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    guild_id = config["guild_id"]
    stats_channel_id = config["stats_channel_id"]
    if guild_id is None or stats_channel_id is None:
        raise ValueError("Missing required stats config values")
    payload: dict[str, int] = {
        "guild_id": guild_id,
        "stats_channel_id": stats_channel_id,
    }
    category_id = config.get("category_id")
    if category_id is not None:
        payload["category_id"] = category_id
    with open(STATS_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


# --- Nickname review channel persistence ---
NICK_CONFIG_FILE = DATA_DIR / "nick_config.json"


def load_nick_config() -> dict[str, int | None]:
    empty: dict[str, int | None] = {"review_channel_id": None, "guild_id": None}
    if not NICK_CONFIG_FILE.is_file():
        return empty
    try:
        with open(NICK_CONFIG_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        return {
            "review_channel_id": int(saved["review_channel_id"]) if saved.get("review_channel_id") else None,
            "guild_id": int(saved["guild_id"]) if saved.get("guild_id") else None,
        }
    except (json.JSONDecodeError, OSError, TypeError, ValueError, KeyError) as exc:
        print(f"Could not read {NICK_CONFIG_FILE}: {exc}")
        return empty


def save_nick_config(config: dict[str, int | None]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload: dict[str, int] = {}
    review_channel_id = config.get("review_channel_id")
    if review_channel_id is not None:
        payload["review_channel_id"] = review_channel_id
    guild_id = config.get("guild_id")
    if guild_id is not None:
        payload["guild_id"] = guild_id
    with open(NICK_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


# ============================================================
# Staff moderation (link/badword auto-delete + DM report)
# ============================================================

# Channels mora9bin — ken fihom link/badword mel members eli 3andhom
# rôle mel MOD_MONITORED_ROLE_IDS, el message yet7a5a.
MOD_WATCHED_CHANNELS = {
    1518043545712463902,  # chat support
    1517949169132769452,  # general
    1518560304903094272,  # memes
    1511674200543199334,  # clips and highlights
    1524882989312512262,  # giveaway feedback
    1524463663447146727,  # event chat
    1518395048582971474,  # team up ping
    1517597975856025854,  # chat of verify
}

# Regex eli te3raf ay link (http/https/discord invite/www.)
MOD_LINK_REGEX = re.compile(
    r"(https?://\S+)|(www\.\S+)|(discord\.gg/\S+)", re.IGNORECASE
)

# Liste kalmet mamnou3a — zid/na9ess kif te7eb.
MOD_BANNED_WORDS = [
    # --- Anglais ---
    "fuck", "fucking", "fucker", "motherfucker", "shit", "bullshit",
    "bitch", "asshole", "bastard", "cunt", "dick", "pussy", "whore",
    "slut", "nigger", "nigga", "faggot", "retard", "rape",

    # --- 3arbi/Franco (Tounsi) ---
    "nik", "nik omk", "nik oukhtk", "9a7ba",
    "zebi", "zeb", "kess", "3ahra", "sharmouta", "sharmuta", "mnayek",
    "manyouk", "kahba",

    # --- 3arbi (script 3arbi) ---
    "قحبة", "شرموطة", "منيوك", "زبي", "عرص",
    "كس اختك", "نيك امك",
]

# El bot ye7ass BISS b members eli 3andhom wa7ed mel had el rôles —
# members el 3adiyin (ma3andhomch rôle) matet7assbouch.
MOD_MONITORED_ROLE_IDS = {
    1511828976732209252,  # Head Admin
    1534781116722974772,  # Moderator
    1517586424306598140,  # Staff Team
    1536024113892687892,  # Trial Staff
}

# El bot yeb3ath el DM report l KOL member 3andou wa7ed mel had el rôles
# (mch chakhs wa7ed) — nafss el rôles el mora9bin, momken tbaddel ken t7eb.
MOD_REPORT_ROLE_IDS = {
    1511828976732209252,  # Head Admin
    1534781116722974772,  # Moderator
    1517586424306598140,  # Staff Team
    1536024113892687892,  # Trial Staff
}
