# Music Bot

A plain Discord music bot built with discord.py and pytubefix. Plays YouTube audio in voice channels with queue management, loop, seek, and DJ-only controls.

## Setup

### Prerequisites

- Python 3.11+
- [FFmpeg](https://ffmpeg.org/) installed and on `PATH`
- A Discord bot token — [create one at the Developer Portal](https://discord.com/developers/applications)

### Installation

1. Create and activate a virtual environment:

```bash
python -m venv .venv
.venv\Scripts\activate   # Windows
source .venv/bin/activate   # macOS / Linux
```

2. Upgrade pip and install dependencies:

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### Configuration

Copy `.env.example` to `.env` and fill in your credentials:

```
DISCORD_BOT_TOKEN=your-discord-bot-token-here
SUPER_ADMIN_ID=your-discord-user-id-here
```

### Running

```bash
python bot.py
```

Or use the launcher:

```bash
run.bat
```

## Commands

| Command | Aliases | Description |
|---------|---------|-------------|
| `!play` | `p` | Play a YouTube track or search |
| `!search` | — | Search YouTube and pick a result |
| `!insert` | `i` | Queue a song to play next |
| `!pause` | `ps` | Pause playback |
| `!resume` | — | Resume playback |
| `!skip` | `s` | Skip current track |
| `!seek` | `goto` | Jump to a position in the track |
| `!queue` | `q` | Show/manage the queue |
| `!nowplaying` | `np` | Show current track info |
| `!loop` | `lp` | Loop track or queue |
| `!shuffle` | `mix` | Shuffle the queue |
| `!leave` | `dc` | Disconnect from voice |
| `!dj` | — | Set the DJ role for this server |
| `!playagain` | `rewind` | Replay the last song |
| `!removeduplicates` | `rmdup` | Remove duplicate songs from the queue |
| `!help` | — | Show all commands |

## Customizing Commands

All commands and aliases are defined in `config.py` under the `COMMANDS` dict. You can rename triggers or add/remove aliases there without touching `music.py`.

```python
COMMANDS = {
    "play": {
        "aliases": ["p"],
        "description": "Play a YouTube track or search",
    },
    # ...
}
```

## Requirements

- `discord.py` 2.7+
- `pytubefix` — YouTube stream extraction
- `PyNaCl` — voice encryption for Discord voice
- `ffmpeg` — audio transcoding
