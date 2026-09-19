"""Minimal JSON document store for the music bot.

Replaces the full economy/fishing SQLite database with two simple files:
- music_settings.json  -> DJ roles per guild
- music_state.json     -> queue/current/loop/volume per guild
"""
import json
import os
from config import DATA_DIR

SETTINGS_FILE = os.path.join(DATA_DIR, "voice", "music_settings.json")
STATE_FILE = os.path.join(DATA_DIR, "voice", "music_state.json")


def _load(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return default


def _save(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(value, f, indent=2, ensure_ascii=False)


def get_kv(key, default=None):
    if key == "music_settings":
        return _load(SETTINGS_FILE, default)
    if key == "music_state":
        return _load(STATE_FILE, default)
    return default


def set_kv(key, value) -> None:
    if key == "music_settings":
        _save(SETTINGS_FILE, value)
    elif key == "music_state":
        _save(STATE_FILE, value)


def migrate_json_file_to_kv(key, json_path):
    if get_kv(key) is not None:
        return False
    if not os.path.exists(json_path):
        return False
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            value = json.load(f)
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return False
    set_kv(key, value)
    return True


def discard_legacy_file(path):
    try:
        os.remove(path)
    except OSError:
        pass
