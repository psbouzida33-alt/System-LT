"""Server stats channels — member counter + live clock."""
import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DATA_DIR = Path("data")
STATS_CONFIG_FILE = DATA_DIR / "stats_config.json"

TOKEN = os.getenv("DISCORD_BOT_TOKEN") or os.getenv("LEVELS_BOT_TOKEN")

# Static voice lounge — bot stays visible here (no audio).
BOT_VOICE_CHANNEL_ID = int(os.getenv("BOT_VOICE_CHANNEL_ID", "1518025649225470072"))

STATS_TIMEZONE = os.getenv("STATS_TIMEZONE", "Africa/Tunis")
STATS_TIME_LABEL = os.getenv("STATS_TIME_LABEL", "Tunisia")
STATS_CLOCK_EMOJI = os.getenv("STATS_CLOCK_EMOJI", "🇹🇳")
STATS_CATEGORY_NAME = os.getenv("STATS_CATEGORY_NAME", "Legends Tunisia")
CLOCK_UPDATE_SECONDS = max(1, int(os.getenv("CLOCK_UPDATE_SECONDS", "1")))
MEMBER_COUNT_EXCLUDE_BOTS = os.getenv("MEMBER_COUNT_EXCLUDE_BOTS", "false").lower() in {
    "1",
    "true",
    "yes",
}


def _env_int(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def load_stats_config() -> dict:
    """Merge saved setup file with optional .env overrides."""
    config: dict = {
        "guild_id": _env_int("GUILD_ID"),
        "category_id": _env_int("STATS_CATEGORY_ID"),
        "member_channel_id": _env_int("MEMBER_STATS_CHANNEL_ID"),
        "clock_channel_id": _env_int("CLOCK_STATS_CHANNEL_ID"),
    }

    if STATS_CONFIG_FILE.is_file():
        try:
            with open(STATS_CONFIG_FILE, encoding="utf-8") as f:
                saved = json.load(f)
            for key in config:
                if config[key] is None and saved.get(key):
                    config[key] = int(saved[key])
        except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
            print(f"Could not read {STATS_CONFIG_FILE}: {exc}")

    return config


def save_stats_config(config: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "guild_id": int(config["guild_id"]),
        "member_channel_id": int(config["member_channel_id"]),
        "clock_channel_id": int(config["clock_channel_id"]),
    }
    if config.get("category_id"):
        payload["category_id"] = int(config["category_id"])
    with open(STATS_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
