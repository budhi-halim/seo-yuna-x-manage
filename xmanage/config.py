"""Strict non-secret settings and secret environment-variable loading."""
import json
from pathlib import Path
import re

from .errors import ManagerError

DEFAULTS = {
    "max_replies_per_run": 10, "reply_interval_seconds": 15,
    "max_comment_length": 100, "emoji_min": 1, "emoji_max": 3,
    "page_size": 100, "max_mention_pages": 5, "max_manual_reply_pages": 10,
    "request_budget": 60, "lookback_hours": 48,
}
SECRET_NAMES = ("X_API_KEY", "X_API_KEY_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET")


def load_config(base):
    path = Path(base) / "config.json"
    try:
        settings = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise ManagerError("Cannot read config.json as valid UTF-8 JSON.") from error
    if not isinstance(settings, dict) or set(settings) != set(DEFAULTS):
        raise ManagerError("config.json must contain exactly the documented settings.")
    bounds = {
        "max_replies_per_run": (1, 50), "reply_interval_seconds": (1, 300),
        "max_comment_length": (1, 500), "emoji_min": (1, 3), "emoji_max": (1, 3),
        "page_size": (5, 100), "max_mention_pages": (1, 8),
        "max_manual_reply_pages": (1, 32), "request_budget": (5, 500),
        "lookback_hours": (1, 168),
    }
    for key, (low, high) in bounds.items():
        value = settings[key]
        if type(value) is not int or not low <= value <= high:
            raise ManagerError(f"config.json: {key} must be an integer from {low} to {high}.")
    if settings["emoji_min"] > settings["emoji_max"]:
        raise ManagerError("emoji_min must not exceed emoji_max.")
    return settings


def credentials_from_env(env):
    values = {key: env.get(key, "").strip() for key in SECRET_NAMES}
    missing = [key for key, value in values.items() if not value or value.startswith("PASTE_")]
    if missing:
        raise ManagerError("Missing GitHub Actions secrets: " + ", ".join(missing))
    return values


def expected_username(env):
    value = env.get("X_EXPECTED_USERNAME", "").strip().lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", value):
        raise ManagerError("Set repository variable X_EXPECTED_USERNAME to the intended X handle (without @).")
    return value


def load_list(path):
    try:
        lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    except OSError as error:
        raise ManagerError(f"Cannot read {Path(path).name}.") from error
    result = []
    for line in lines:
        result.extend(part.strip() for part in line.split("#", 1)[0].split(",") if part.strip())
    return result
