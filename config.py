"""Server stats channels — member counter + live clock."""
import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DATA_DIR = Path("data")
STATS_CONFIG_FILE = DATA_DIR / "stats_config.json"

TOKEN = os.getenv("DISCORD_BOT_TOKEN") or os.getenv("LEVELS_BOT_TOKEN")

STATS_TIMEZONE = os.getenv("STATS_TIMEZONE", "Africa/Casablanca")
STATS_TIME_LABEL = os.getenv("STATS_TIME_LABEL", "Morocco")
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
    with open(STATS_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
