import os

# ============================================
# BOT
# ============================================
BOT_PREFIX = os.getenv("BOT_PREFIX", ".")
SUPER_ADMIN_ID = int(os.getenv("SUPER_ADMIN_ID", 0))

# ============================================
# VOICE
# ============================================
MUSIC_EMPTY_CHANNEL_TIMEOUT = 60
MUSIC_VOICE_CONNECT_TIMEOUT = 20
MUSIC_VOTE_REQUIRED_RATIO = 0.5

# ============================================
# PATHS
# ============================================
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# ============================================
# COMMANDS / ALIASES (configurable skeleton)
# ============================================
# Edit the aliases here to reconfigure command triggers.
# Do NOT change the primary command name unless you also
# rename the decorator in music.py.

COMMANDS = {
    "play": {
        "aliases": ["p"],
        "description": "Play a YouTube track or search",
    },
    "search": {
        "aliases": [],
        "description": "Search YouTube and pick a result",
    },
    "searchspotify": {
        "aliases": ["ssearch", "spotifysearch"],
        "description": "Search Spotify and pick a track",
    },
    "insert": {
        "aliases": ["i"],
        "description": "Queue a song to play next",
    },
    "join": {
        "aliases": ["j"],
        "description": "Join your voice channel",
    },
    "leave": {
        "aliases": ["l", "stop", "disconnect", "dc"],
        "description": "Leave voice channel and clear queue",
    },
    "loop": {
        "aliases": ["lp"],
        "description": "Loop current track or queue",
    },
    "skip": {
        "aliases": ["next", "s"],
        "description": "Skip the current track",
    },
    "seek": {
        "aliases": ["goto"],
        "description": "Jump to a position in the current track",
    },
    "queue": {
        "aliases": ["q"],
        "description": "Show the queue or manage it",
    },
    "pause": {
        "aliases": ["ps"],
        "description": "Pause playback",
    },
    "resume": {
        "aliases": ["unpause"],
        "description": "Resume playback",
    },
    "nowplaying": {
        "aliases": ["np"],
        "description": "Show the current track info",
    },
    "playagain": {
        "aliases": ["rewind"],
        "description": "Replay the last song",
    },
    "shuffle": {
        "aliases": ["mix"],
        "description": "Shuffle the queue",
    },
    "removeduplicates": {
        "aliases": ["rmdup", "rmduplicate", "rmduplicates"],
        "description": "Remove duplicate songs from the queue",
    },
    "volume": {
        "aliases": ["v"],
        "description": "Set the playback volume",
    },
    "dj": {
        "aliases": [],
        "description": "Set the DJ role for this server",
    },
    "help": {
        "aliases": [],
        "description": "Show all music commands",
    },
}

# ============================================
# DERIVED MAPS (auto-built from COMMANDS)
# ============================================
ALIAS_TO_COMMAND = {}
for _cmd, _meta in COMMANDS.items():
    for _alias in _meta.get("aliases", []):
        ALIAS_TO_COMMAND[_alias] = _cmd


def resolve_command(name: str) -> str | None:
    """Return the primary command name for an alias or raw input, or None."""
    key = name.lstrip("!").lower()
    if key in COMMANDS:
        return key
    return ALIAS_TO_COMMAND.get(key)
