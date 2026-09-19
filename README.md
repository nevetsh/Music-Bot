# Music Bot

A Discord music bot built with discord.py, pytubefix, and yt-dlp. Plays YouTube and SoundCloud audio in voice channels with queue management, playlists, loop, seek, and DJ-only controls.

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
BOT_PREFIX=.
DISCORD_GUILD_ID=
SUPER_ADMIN_ID=your-discord-user-id-here
```

### Slash commands

All music commands are available as both prefix commands and slash commands. The bot syncs slash commands globally by default. For immediate updates in a development server, set `DISCORD_GUILD_ID` in `.env` to that server's ID; global command registration can take up to an hour to propagate.

### Running

```bash
python bot.py
```

Or use the launcher:

```bash
run.bat
```

## Commands

The default command prefix is `.`, and can be changed with `BOT_PREFIX` in `.env`. Use `.play` with a SoundCloud track URL or set URL for direct playback/playlist queueing.

| Command | Aliases | Description |
|---------|---------|-------------|
| `.play` | `p` | Play a YouTube/SoundCloud track, search, or playlist |
| `.search` | — | Search YouTube and pick a result |
| `.searchsoundcloud` | `scsearch` | Search SoundCloud and pick a result |
| `.play` | `p` | Play a YouTube track or search |
| `.search` | — | Search YouTube and pick a result |
| `.insert` | `i` | Queue a song to play next |
| `.pause` | `ps` | Pause playback |
| `.resume` | — | Resume playback |
| `.skip` | `s` | Skip current track |
| `.seek` | `goto` | Jump to a position in the track |
| `.queue` | `q` | Show/manage the queue |
| `.nowplaying` | `np` | Show current track info |
| `.loop` | `lp` | Loop track or queue |
| `.shuffle` | `mix` | Shuffle the queue |
| `.leave` | `dc` | Disconnect from voice |
| `.dj` | — | Set the DJ role for this server |
| `.playagain` | `rewind` | Replay the last song |
| `.removeduplicates` | `rmdup` | Remove duplicate songs from the queue |
| `.help` | — | Show all commands |

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
- `pytubefix` — YouTube stream extraction and playlists
- `yt-dlp` — SoundCloud links, searches, and playlists
- `PyNaCl` — voice encryption for Discord voice
- `ffmpeg` — audio transcoding
