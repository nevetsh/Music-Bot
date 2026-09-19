"""
Music cog: plays YouTube audio in a voice channel using pytubefix.

Requirements (see modules/assets/requirements.txt):
- pip install pytubefix PyNaCl
- An ffmpeg binary on PATH (or set the FFMPEG_BINARY env var to its full path)

Commands: `!play <link|search>`, `!skip`, `!stop`, `!leave`, `!join`, `!loop`, `!queue`, `!pause`, `!resume`, `!nowplaying`, `!dj <role>`
"""

import asyncio
import math
import os
import random
import re
import shutil
import time
import discord
import pytubefix as ptf
from discord.ext import commands, tasks

from config import SUPER_ADMIN_ID, MUSIC_EMPTY_CHANNEL_TIMEOUT, MUSIC_VOICE_CONNECT_TIMEOUT, MUSIC_VOTE_REQUIRED_RATIO


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
from config import DATA_DIR
import database as player_db

MUSIC_SETTINGS_KEY = "music_settings"
MUSIC_STATE_KEY = "music_state"
MUSIC_SETTINGS_FILE = os.path.join(DATA_DIR, "voice", "music_settings.json")  # legacy import source
MUSIC_STATE_FILE = os.path.join(DATA_DIR, "voice", "music_state.json")        # legacy import source


def _load_music_settings() -> dict[int, int]:
    try:
        player_db.migrate_json_file_to_kv(MUSIC_SETTINGS_KEY, MUSIC_SETTINGS_FILE)
        data = player_db.get_kv(MUSIC_SETTINGS_KEY, {}) or {}
        return {int(guild_id): int(role_id) for guild_id, role_id in data.get("dj_roles", {}).items()}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def _save_music_settings(dj_roles: dict[int, int]) -> None:
    try:
        player_db.set_kv(
            MUSIC_SETTINGS_KEY,
            {"dj_roles": {str(guild_id): role_id for guild_id, role_id in dj_roles.items()}},
        )
        player_db.discard_legacy_file(MUSIC_SETTINGS_FILE)
    except Exception as exc:
        print(f"[music] settings save failed: {exc!r}")


def _load_music_state() -> dict[int, dict]:
    try:
        player_db.migrate_json_file_to_kv(MUSIC_STATE_KEY, MUSIC_STATE_FILE)
        data = player_db.get_kv(MUSIC_STATE_KEY, {}) or {}
        return {int(guild_id): state for guild_id, state in data.get("guilds", {}).items()}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def _save_music_state(states: dict[int, dict]) -> None:
    try:
        serializable = {}
        for guild_id, state in states.items():
            serializable[str(guild_id)] = {
                "queue": state.get("queue", []),
                "current": state.get("current"),
                "last_played": state.get("last_played"),
                "loop": state.get("loop", "off"),
                "volume": state.get("volume", 1.0),
                "elapsed": state.get("elapsed", 0.0),
                "channel_id": state.get("channel_id"),
                "text_channel_id": state.get("text_channel_id"),
                "paused": bool(state.get("paused")),
            }
        player_db.set_kv(MUSIC_STATE_KEY, {"guilds": serializable})
        player_db.discard_legacy_file(MUSIC_STATE_FILE)
    except Exception as exc:
        print(f"[music] state save failed: {exc!r}")


def _ffmpeg_binary():
    """Return a path to a working ffmpeg binary.

    Priority:
        1. FFMPEG_BINARY env var (if set and the file exists).
        2. 'ffmpeg' on PATH.
        3. The WinGet-managed ffmpeg.exe under the user's AppData if present.
    """
    env = os.getenv("FFMPEG_BINARY")
    if env:
        if os.path.isfile(env):
            return env
        if shutil.which(env):
            return env
        print("[music] ⚠️ FFMPEG_BINARY env var set but not found:", env)
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    winnode = (
        os.environ.get("LOCALAPPDATA", "") + "\\Microsoft\\WinGet\\Links\\ffmpeg.exe"
    )
    if os.path.isfile(winnode):
        return winnode
    return "ffmpeg"


FFMPEG_BINARY = _ffmpeg_binary()

# ---------- Spotify link support ----------
# Spotify links (open.spotify.com/<type>/<id>) carry no playable audio, so the
# metadata is scraped from the public embed page (no auth/API keys needed) and
# each track is searched on YouTube instead. Playlists queue lazily: the queue
# stores the search query and the audio URL is only resolved when the track
# actually starts playing (see _play_next).

_SPOTIFY_URL_RE = re.compile(
    r"https?://open\.spotify\.com/(?:intl-[a-z]{2}/)?(track|album|playlist)/([A-Za-z0-9]+)",
    re.IGNORECASE,
)
_SPOTIFY_TYPE_NAMES = {"track": "track", "album": "album", "playlist": "playlist"}


def _spotify_parse(url: str):
    """Return (spotify_type, spotify_id) for a supported Spotify link, or None."""
    match = _SPOTIFY_URL_RE.search(url or "")
    if match is None:
        return None
    return _SPOTIFY_TYPE_NAMES[match.group(1).lower()], match.group(2)


def _spotify_fetch(url: str, entry_type: str) -> dict | None:
    """Fetch title/tracks for a Spotify entry from its public embed page.

    Uses the base64-encoded initialState JSON blob (entities.items -> content)
    plus the og: meta tags, so no Spotify API credentials or extra
    dependencies are needed. Albums/tracklists are best-effort: the embed page
    only ships the first page of items (30 for playlists, none for albums).
    """
    try:
        import requests
    except ImportError:
        print("[music] ⚠️ requests not installed — can't resolve Spotify links.")
        return None

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
    except Exception as exc:
        print(f"[music] spotify fetch failed for {url!r}: {exc!r}")
        return None

    html = resp.text

    def _meta(prop: str) -> str:
        m = re.search(
            r'<meta[^>]+(?:property|name)="' + prop + r'"[^>]+content="([^"]*)"',
            html,
        )
        if m is None:
            m = re.search(
                r'<meta[^>]+content="([^"]*)"[^>]+(?:property|name)="' + prop + r'"',
                html,
            )
        if m is None:
            return ""
        return (
            m.group(1)
            .replace("&amp;", "&")
            .replace("&#39;", "'")
            .replace("&quot;", '"')
        )

    # og:title: "Track Name" (tracks) or "Album - Album by Artist | Spotify".
    og_title = _meta("og:title")
    og_title = re.sub(r"\s*\|\s*Spotify\s*$", "", og_title)
    name = re.sub(r"\s*-\s*Album by .*$", "", og_title).strip()
    description = _meta("og:description")

    blob_tracks: list[dict] = []
    blob_name = ""
    blob_cover = None
    m = re.search(r'<script id="initialState"[^>]*>(.*?)</script>', html, re.DOTALL)
    if m:
        try:
            import base64 as _base64
            import json as _json

            payload = _json.loads(_base64.b64decode(m.group(1).strip()))
            for uri, ent in (payload.get("entities", {}).get("items") or {}).items():
                if not isinstance(ent, dict):
                    continue
                if blob_name == "" and ent.get("name") and uri.rsplit(":", 1)[-1] == url.rstrip("/").rsplit("/", 1)[-1]:
                    blob_name = ent.get("name") or ""
                    cover_sources = ((ent.get("albumOfTrack") or {}).get("coverArt") or {}).get("sources") or []
                    if cover_sources:
                        blob_cover = cover_sources[-1].get("url")
                content = ent.get("content") or {}
                for item in content.get("items") or []:
                    data = (item.get("itemV2") or {}).get("data") or {}
                    t_title = data.get("name") or ""
                    artists = ", ".join(
                        (a.get("profile") or {}).get("name", "")
                        for a in (data.get("artists") or {}).get("items") or []
                        if (a.get("profile") or {}).get("name")
                    )
                    if t_title:
                        blob_tracks.append(
                            {
                                "title": t_title,
                                "artists": artists,
                                "duration": int(((data.get("duration") or {}).get("totalMilliseconds") or 0) / 1000),
                                "cover": (((data.get("albumOfTrack") or {}).get("coverArt") or {}).get("sources") or [{}])[-1].get("url"),
                            }
                        )
        except Exception as exc:
            print(f"[music] spotify initialState parse failed for {url!r}: {exc!r}")

    if blob_name:
        name = blob_name

    tracks: list[dict] = []
    if entry_type in ("playlist", "album"):
        tracks = blob_tracks
        if not tracks and name:
            # No tracklist shipped (albums): search the album + artist name on
            # YouTube, which usually surfaces the full album upload.
            artists = description.split("·")[0].strip()
            tracks = [{"title": name, "artists": artists, "duration": 0, "cover": None}]
    else:  # single track
        if blob_tracks:
            tracks = blob_tracks
        elif name:
            # og:description is "Artist · Album · Song · Year".
            artists = description.split("·")[0].strip()
            tracks = [{"title": name, "artists": artists, "duration": 0, "cover": None}]

    if not tracks:
        return None

    display_artists = description.split("·")[0].strip()
    return {
        "entry_type": entry_type,
        "title": name,
        "artists": display_artists,
        "cover": blob_cover,
        "tracks": tracks,
    }


def _spotify_search_query(track_title: str, artists: str) -> str:
    """Build the YouTube search string for a Spotify track."""
    return f"{track_title} {artists}".strip()


# ffmpeg: keep the connection alive on live streams, strip video.
FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}


class _ResumeCtx:
    """Minimal stand-in for a command context, used when resuming playback
    after a restart (there is no invoking command to build a ctx from)."""

    def __init__(self, guild, voice_client, channel):
        self.guild = guild
        self.voice_client = voice_client
        self.channel = channel

    async def send(self, *args, **kwargs):
        if self.channel is not None:
            await self.channel.send(*args, **kwargs)


class Music(commands.Cog):
    """Plays music in voice channels."""

    def __init__(self, bot):
        self.bot = bot
        self.states: dict[int, dict] = _load_music_state()
        for guild_id, state in self.states.items():
            state.setdefault("queue", [])
            state.setdefault("current", None)
            state.setdefault("last_played", None)
            state.setdefault("loop", "off")
            state.setdefault("volume", 1.0)
            state.setdefault("started_at", None)
            state.setdefault("elapsed", 0.0)
            state.setdefault("voice", None)
            state.setdefault("source", None)
            state.setdefault("channel_id", None)
            state.setdefault("text_channel_id", None)
            state.setdefault("paused", False)
        self.dj_roles: dict[int, int] = _load_music_settings()
        self.votes: dict[int, dict] = {}
        self.search_sessions: dict[int, dict] = {}
        self.empty_timers: dict[int, asyncio.Task] = {}
        self.kick_checks: dict[int, asyncio.Task] = {}
        self._resumed = False
        if shutil.which(FFMPEG_BINARY) is None:
            print(
                "[music] ⚠️  ffmpeg not found on PATH — music won't play. "
                "Install ffmpeg or set the FFMPEG_BINARY env var to its full path."
            )

    async def cog_load(self):
        # Keep the saved position/channel fresh so a restart (graceful or
        # not) can pick playback back up at roughly the right spot.
        self._persist_loop.start()

    # ---------- helpers ----------

    def get_state(self, guild_id: int) -> dict:
        state = self.states.setdefault(
            guild_id,
            {
                "queue": [],
                "current": None,
                "last_played": None,
                "loop": "off",
                "suppress_loop": False,
                "volume": 1.0,
                "started_at": None,
                "elapsed": 0.0,
                "voice": None,
                "source": None,
            },
        )
        state.setdefault("queue", [])
        state.setdefault("current", None)
        state.setdefault("loop", "off")
        state.setdefault("suppress_loop", False)
        state.setdefault("volume", 1.0)
        state.setdefault("started_at", None)
        state.setdefault("elapsed", 0.0)
        state.setdefault("voice", None)
        state.setdefault("source", None)
        state.setdefault("channel_id", None)
        state.setdefault("text_channel_id", None)
        state.setdefault("paused", False)
        return state

    def _reset_playback_state(self, state: dict) -> None:
        state["current"] = None
        state["started_at"] = None
        state["elapsed"] = 0.0
        state["voice"] = None
        state["source"] = None

    def _persist(self) -> None:
        """Snapshot live playback (position, channel) and save all guild states."""
        now = time.monotonic()
        for state in self.states.values():
            voice = state.get("voice")
            if state.get("current") and voice is not None and voice.is_playing():
                # Fold the live position into `elapsed` AND restart the clock.
                # _elapsed_seconds() adds (now - started_at) on top of `elapsed`,
                # so leaving started_at alone made every 10s persist bank the
                # same span twice: the reported timestamp (and the position
                # saved for a restart) raced ahead of the audio.
                state["elapsed"] = self._elapsed_raw(state)
                state["started_at"] = now
            if voice is not None and voice.is_connected() and getattr(voice, "channel", None):
                state["channel_id"] = voice.channel.id
        _save_music_state(self.states)

    def persist_now(self) -> None:
        """Save immediately with the freshest position - used before restart."""
        self._persist()

    @tasks.loop(seconds=10)
    async def _persist_loop(self):
        try:
            self._persist()
        except Exception as exc:
            print(f"[music] state persist failed: {exc!r}")

    def _clear_resume_markers(self, state: dict) -> None:
        state["channel_id"] = None
        state["paused"] = False

    async def _resume_guild(self, guild_id: int) -> None:
        """Rejoin the voice channel and resume the current track at its saved position."""
        state = self.states.get(guild_id)
        if state is None or not state.get("current") or not state.get("channel_id"):
            return
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            self._clear_resume_markers(state)
            return
        channel = guild.get_channel(state["channel_id"])
        if channel is None:
            self._clear_resume_markers(state)
            return
        try:
            voice = await channel.connect(timeout=MUSIC_VOICE_CONNECT_TIMEOUT)
        except Exception as exc:
            print(f"[music] resume join failed (guild {guild_id}): {exc!r}")
            self._clear_resume_markers(state)
            return

        text_channel = self.bot.get_channel(state.get("text_channel_id") or 0)
        shim = _ResumeCtx(guild, voice, text_channel)
        elapsed = float(state.get("elapsed", 0.0) or 0.0)
        duration = int(state.get("current", {}).get("duration", 0) or 0)
        if duration and elapsed >= duration:
            elapsed = max(0.0, duration - 1)
        was_paused = bool(state.get("paused"))

        await self._play_next(shim, resume_at=elapsed, force_track=state["current"])
        self._clear_resume_markers(state)

        if was_paused:
            voice = state.get("voice") or guild.voice_client
            if voice is not None and voice.is_playing():
                state["started_at"] = None
                voice.pause()

        await self._start_empty_timer(guild_id)

    @commands.Cog.listener()
    async def on_ready(self):
        """Resume any guild whose playback was saved before a restart."""
        if self._resumed:
            return
        self._resumed = True
        for guild_id in list(self.states.keys()):
            try:
                await self._resume_guild(guild_id)
            except Exception as exc:
                print(f"[music] resume failed for guild {guild_id}: {exc!r}")

    def _is_dj_or_admin(self, ctx) -> bool:
        """Check if user can use music control commands without voting."""
        if ctx.guild is None:
            return False
        if getattr(ctx.author, "guild_permissions", None) and ctx.author.guild_permissions.manage_guild:
            return True
        if ctx.author.id == SUPER_ADMIN_ID:
            return True
        dj_role_id = self.dj_roles.get(ctx.guild.id)
        if dj_role_id and any(role.id == dj_role_id for role in getattr(ctx.author, "roles", [])):
            return True
        return False

    async def _require_dj(self, ctx) -> bool:
        """Require the configured DJ role or Manage Server for a control."""
        if self._is_dj_or_admin(ctx):
            return True
        await ctx.reply("You need the configured DJ role or Manage Server permission to do that.", mention_author=False)
        return False

    def _elapsed_raw(self, state: dict) -> float:
        """Unrounded, unclamped elapsed seconds for the current track."""
        elapsed = float(state.get("elapsed", 0.0) or 0.0)
        started_at = state.get("started_at")
        voice = state.get("voice")
        if started_at is not None and voice is not None and voice.is_playing():
            elapsed += max(0.0, time.monotonic() - started_at)
        return max(0.0, elapsed)

    def _elapsed_seconds(self, state: dict) -> int:
        """Return the current track's elapsed playback time."""
        elapsed = self._elapsed_raw(state)
        current = state.get("current") or {}
        return min(max(0, int(elapsed)), int(current.get("duration", 0) or 0)) if current.get("duration") else max(0, int(elapsed))

    @staticmethod
    def _progress_bar(elapsed: int, duration: int, width: int = 20) -> str:
        if duration <= 0:
            return "─" * width
        filled = min(width, max(0, int(width * elapsed / duration)))
        return "▬" * filled + "—" * (width - filled)

    def _loop_label(self, mode: str) -> str:
        return {"off": "Off", "track": "Track", "queue": "Queue"}.get(mode, "Off")

    def _now_playing_embed(self, guild_id: int) -> discord.Embed | None:
        state = self.get_state(guild_id)
        current = state.get("current")
        if not current:
            return None

        duration = int(current.get("duration", 0) or 0)
        elapsed = self._elapsed_seconds(state)
        progress = f"{self._fmt_duration(elapsed)} / {self._fmt_duration(duration)}" if duration else self._fmt_duration(elapsed)
        embed = discord.Embed(title="🎵 Now Playing", description=f"**{current.get('title', 'Unknown')}**", color=0x00BFFF)
        if current.get("thumbnail"):
            embed.set_thumbnail(url=current["thumbnail"])
        embed.add_field(name="Duration", value=self._fmt_duration(duration) if duration else "Unknown", inline=True)
        embed.add_field(name="Progress", value=f"`{self._progress_bar(elapsed, duration)}`\n{progress}", inline=False)
        embed.add_field(name="Requester", value=current.get("requester", "Unknown"), inline=True)
        embed.add_field(name="Loop", value=self._loop_label(state.get("loop", "off")), inline=True)
        voice = state.get("voice")
        status = "⏸️ Paused" if voice and voice.is_paused() else "▶️ Playing" if voice and voice.is_playing() else "⏹️ Stopped"
        embed.set_footer(text=f"{status} • Queue: {len(state.get('queue', []))} waiting")
        return embed

    async def _send_now_playing(self, ctx):
        embed = self._now_playing_embed(ctx.guild.id)
        if embed is not None:
            await ctx.send(embed=embed)

    def _get_human_voice_members(self, ctx):
        """Return list of non-bot members in the bot's current voice channel."""
        voice = ctx.voice_client
        if voice is None or not voice.is_connected() or not voice.channel:
            return []
        return [m for m in voice.channel.members if not m.bot]

    def _cancel_empty_timer(self, guild_id: int):
        task = self.empty_timers.pop(guild_id, None)
        if task and not task.done():
            task.cancel()

    async def _start_empty_timer(self, guild_id: int):
        """Start a 60-second timer to leave if the voice channel stays empty."""
        self._cancel_empty_timer(guild_id)

        async def _wait_and_leave():
            try:
                await asyncio.sleep(MUSIC_EMPTY_CHANNEL_TIMEOUT)
                voice = None
                for vc in self.bot.voice_clients:
                    if getattr(vc, "guild", None) and vc.guild.id == guild_id:
                        voice = vc
                        break
                if voice is not None and voice.is_connected() and getattr(voice, "channel", None):
                    humans = [m for m in voice.channel.members if not m.bot]
                    if not humans:
                        try:
                            await voice.disconnect()
                        except Exception:
                            pass
                        self.states.pop(guild_id, None)
            except asyncio.CancelledError:
                pass
            finally:
                current = self.empty_timers.get(guild_id)
                if current is asyncio.current_task():
                    self.empty_timers.pop(guild_id, None)

        self.empty_timers[guild_id] = asyncio.create_task(_wait_and_leave())

    def _cancel_all_empty_timers(self):
        for guild_id in list(self.empty_timers):
            self._cancel_empty_timer(guild_id)

    def _cancel_kick_check(self, guild_id: int) -> None:
        task = self.kick_checks.pop(guild_id, None)
        if task and not task.done():
            task.cancel()

    async def _confirm_external_kick(self, guild_id: int) -> None:
        """Wipe state 10s after a "no channel" event, unless we're back."""
        try:
            await asyncio.sleep(10)
            voice = None
            for vc in self.bot.voice_clients:
                if getattr(vc, "guild", None) and vc.guild.id == guild_id:
                    voice = vc
                    break
            if voice is not None and (voice.is_connected() or voice.is_playing()):
                return  # false alarm - the client reconnected and kept playing
            # Still playing audibly through a reconnect? Definitely not a kick.
            state = self.get_state(guild_id)
            if state.get("current") is not None and (voice is None or not getattr(voice, "is_connected", lambda: False)()):
                src = state.get("source")
                if src is not None and getattr(src, "is_playing", lambda: False)():
                    return  # audio is live - the "no channel" event was a blip
            state = self.get_state(guild_id)
            state["queue"].clear()
            state["current"] = None
            state["last_played"] = None
            self._clear_resume_markers(state)
            self._persist()  # wipe the saved state so a restart can't resurrect the kicked session
            self._cancel_empty_timer(guild_id)
        except asyncio.CancelledError:
            pass
        finally:
            current = self.kick_checks.get(guild_id)
            if current is asyncio.current_task():
                self.kick_checks.pop(guild_id, None)

    async def _ensure_voice(self, ctx):
        """Connect to or move into the sender's voice channel."""
        if ctx.guild is None:
            await ctx.reply("Music only works in a server.", mention_author=False)
            return None
        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.reply("You need to be in a voice channel first!", mention_author=False)
            return None

        channel = ctx.author.voice.channel
        try:
            voice = ctx.voice_client
            if voice is None:
                voice = await channel.connect(timeout=MUSIC_VOICE_CONNECT_TIMEOUT)
            elif voice.channel.id != channel.id:
                await voice.move_to(channel)
            return voice
        except Exception as e:
            print(f"[music] join error: {e!r}")
            await ctx.reply(
                "Couldn't join the voice channel. Make sure PyNaCl is installed "
                "(`pip install PyNaCl`) and the bot has voice permissions.",
                mention_author=False,
            )
            return None

    def _resolve(self, query: str):
        """Return a pytubefix YouTube object for a link/playlist/search, or None."""
        query = (query or "").strip()
        if not query:
            return None

        lowered = query.lower()
        is_playlist = (
            "youtube.com/playlist" in lowered
            or "youtu.be/playlist" in lowered
            or "/playlists" in lowered
        )
        is_url = (
            "youtube.com" in lowered
            or "youtu.be" in lowered
            or "music.youtube.com" in lowered
        )

        try:
            if is_playlist:
                playlist = ptf.Playlist(query)
                for video in playlist.videos:
                    if video is not None:
                        return video
                return None
            if is_url:
                return ptf.YouTube(query)
            for video in ptf.Search(query).videos:
                if video is not None:
                    return video
            return None
        except Exception as e:
            print(f"[music] resolve error for {query!r}: {e!r}")
            return None

    def _playlist_entries(self, playlist_url: str, requester: str) -> list[dict]:
        """Expand a YouTube playlist URL into lazily-resolved queue entries.

        Like Spotify entries, each one stores the video URL as its query;
        _play_next extracts the actual stream only when the track starts.
        """
        import pytubefix as ptf
        entries = []
        playlist = ptf.Playlist(playlist_url)
        for video in playlist.videos:
            url = getattr(video, "watch_url", None) or getattr(video, "url", None)
            if not url:
                continue
            # Length/metadata can raise on unavailable videos (age-restricted,
            # deleted, private); the stream check at play time will skip them.
            try:
                title = video.title or "Unknown"
            except Exception:
                title = "Unknown"
            try:
                length = max(0, int(video.length or 0))
            except Exception:
                length = 0
            try:
                thumbnail = video.thumbnail_url
            except Exception:
                thumbnail = None
            try:
                author = video.author or ""
            except Exception:
                author = ""
            entries.append(
                {
                    "title": title,
                    "url": None,
                    "duration": length,
                    "thumbnail": thumbnail,
                    "uploader": author,
                    "query": url,
                    "requester": requester,
                }
            )
        return entries

    def _youtube_search_results(self, query: str, limit: int = 5) -> list[dict]:
        """Search YouTube and return lightweight result cards (no stream extraction)."""
        results = []
        try:
            search = ptf.Search(query)
            for video in search.videos[:limit]:
                if video is None:
                    continue
                results.append(
                    {
                        "title": getattr(video, "title", None) or "Unknown",
                        "url": getattr(video, "watch_url", None) or getattr(video, "url", None),
                        "duration": max(0, int(getattr(video, "length", 0) or 0)),
                        "thumbnail": getattr(video, "thumbnail_url", None),
                        "sub": getattr(video, "author", None) or "",
                    }
                )
        except Exception as e:
            print(f"[music] youtube search error for {query!r}: {e!r}")
        return [r for r in results if r["url"]]

    def _spotify_search_results(self, query: str, limit: int = 5) -> list[dict]:
        """Search for tracks via Deezer's public API (no auth) — the closest
        legal metadata match to Spotify's catalogue. Each result maps to the
        YouTube search string that !play's Spotify path would have used."""
        import requests
        try:
            resp = requests.get(
                "https://api.deezer.com/search",
                params={"q": query, "limit": limit},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json().get("data", [])
        except Exception as e:
            print(f"[music] spotify(deezer) search error for {query!r}: {e!r}")
            return []
        results = []
        for t in data:
            title = t.get("title") or "Unknown"
            artists = (t.get("artist") or {}).get("name") or ""
            results.append(
                {
                    "title": f"{title} — {artists}".rstrip(" —"),
                    "sub": (t.get("album") or {}).get("title") or "",
                    "duration": max(0, int(t.get("duration") or 0)),
                    "thumbnail": (t.get("album") or {}).get("cover_medium"),
                    # Queue key: the "title artists" YouTube search string,
                    # resolved lazily at play time like every Spotify entry.
                    "url": _spotify_search_query(title, artists),
                }
            )
        return results

    def _spotify_track_dicts(self, data: dict, requester: str) -> list[dict]:
        """Turn a _spotify_fetch result into lazily-resolved queue entries.

        Spotify links carry no playable audio, so each queue entry stores the
        YouTube *search query*; _play_next resolves the actual stream only when
        the track starts (the same lazy path regular queued tracks use).
        """
        entries = []
        for track in data["tracks"]:
            search = _spotify_search_query(track.get("title", ""), track.get("artists", ""))
            if not search:
                continue
            entries.append(
                {
                    "title": f"{track.get('title', 'Unknown')} — {track.get('artists', '')}".rstrip(" —"),
                    "url": None,
                    "duration": int(track.get("duration") or 0),
                    "thumbnail": track.get("cover"),
                    "uploader": "Spotify",
                    "query": search,
                    "requester": requester,
                    "spotify": True,
                }
            )
        return entries

    def _extract(self, query: str):
        """Resolve audio info for play/queue. Returns a track dict or None."""
        yt = self._resolve(query)
        if yt is None:
            return None
        try:
            stream = yt.streams.get_audio_only()
        except Exception as e:
            print(f"[music] no audio stream for {query!r}: {e!r}")
            return None
        if stream is None or not getattr(stream, "url", None):
            return None
        return {
            "title": getattr(yt, "title", None) or "Unknown",
            "url": stream.url,
            "duration": max(0, int(getattr(yt, "length", 0) or 0)),
            "thumbnail": getattr(yt, "thumbnail_url", None),
            "uploader": getattr(yt, "author", None) or "",
            "query": query,
            "requester": "Unknown",
        }
    def _fmt_duration(self, seconds: int) -> str:
        seconds = max(0, int(seconds))
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    # ---------- search pickers ----------

    SEARCH_EMOJIS = ("1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣")
    SEARCH_TIMEOUT = 60

    async def _run_search_picker(self, ctx, source: str, results: list[dict], source_label: str):
        """Show a numbered result picker and queue the chosen track.

        results: list of dicts with title/sub/duration/thumbnail; how a pick
        becomes a queue entry is decided by `source` ('youtube' queues the
        video URL, 'spotify' queues the YouTube search string lazily).
        """
        if ctx.guild is None:
            await ctx.reply("Music only works in a server.", mention_author=False)
            return

        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.reply("You need to be in a voice channel first!", mention_author=False)
            return

        # One active search per user.
        for sess in list(self.search_sessions.values()):
            if sess["user_id"] == ctx.author.id and sess["guild_id"] == ctx.guild.id:
                await ctx.reply("You already have a search open — pick a result or wait for it to expire.", mention_author=False)
                return

        if not results:
            await ctx.reply(f"Couldn't find anything for that on {source_label}.", mention_author=False)
            return

        lines = [f"**{i}.** {r['title']}" for i, r in enumerate(results, start=1)]
        embed = discord.Embed(
            title=f"🔎 {source_label} results for `{ctx.kwargs.get('query', '') if hasattr(ctx, 'kwargs') else ''}`".replace("``", "`"),
            description="\n".join(lines),
            color=0x00AAFF,
        )
        embed.set_footer(text=f"React 1-{len(results)} within {self.SEARCH_TIMEOUT}s to queue")
        if results[0].get("thumbnail"):
            embed.set_thumbnail(url=results[0]["thumbnail"])
        msg = await ctx.reply(embed=embed, mention_author=False)

        session = {
            "user_id": ctx.author.id,
            "guild_id": ctx.guild.id,
            "results": results,
            "source": source,
            "expires_at": time.time() + self.SEARCH_TIMEOUT,
            "message": msg,
        }
        self.search_sessions[msg.id] = session

        for emoji in self.SEARCH_EMOJIS[: len(results)]:
            try:
                await msg.add_reaction(emoji)
            except Exception:
                pass

        asyncio.get_running_loop().create_task(self._expire_search(msg.id, self.SEARCH_TIMEOUT))

    async def _expire_search(self, message_id: int, delay: int):
        await asyncio.sleep(delay)
        session = self.search_sessions.pop(message_id, None)
        if session is None:
            return
        msg = session.get("message")
        if msg is not None:
            try:
                await msg.edit(embed=discord.Embed(
                    title="🔎 Search expired",
                    description="No pick was made in time. Run the search again to retry.",
                    color=0x808080,
                ))
                for emoji in self.SEARCH_EMOJIS[: len(session["results"])]:
                    try:
                        await msg.remove_reaction(emoji, self.bot.user)
                    except Exception:
                        pass
            except Exception:
                pass

    async def _handle_search_pick(self, payload):
        """Resolve a number-reaction on an active search message into a queue add."""
        session = self.search_sessions.get(payload.message_id)
        if session is None:
            return False
        if payload.user_id != session["user_id"]:
            return True  # consumed by an active session, just not theirs
        if time.time() > session["expires_at"]:
            self.search_sessions.pop(payload.message_id, None)
            return True

        emoji_str = str(payload.emoji)
        if emoji_str not in self.SEARCH_EMOJIS:
            return True
        index = self.SEARCH_EMOJIS.index(emoji_str)
        if index >= len(session["results"]):
            return True

        self.search_sessions.pop(payload.message_id, None)
        picked = session["results"][index]

        # Build the queue entry / enqueue path per source.
        if session["source"] == "youtube":
            track = {
                "title": picked["title"],
                "url": None,
                "duration": picked.get("duration", 0),
                "thumbnail": picked.get("thumbnail"),
                "uploader": picked.get("sub", ""),
                "query": picked["url"],
                "requester": f"<@{payload.user_id}>",
            }
        else:  # spotify: queue the YouTube search string, resolved lazily
            track = {
                "title": picked["title"],
                "url": None,
                "duration": picked.get("duration", 0),
                "thumbnail": picked.get("thumbnail"),
                "uploader": "Spotify",
                "query": picked["url"],
                "requester": f"<@{payload.user_id}>",
                "spotify": True,
            }

        guild_id = session["guild_id"]
        state = self.get_state(guild_id)
        state["text_channel_id"] = payload.channel_id

        msg = session.get("message")
        try:
            await msg.edit(embed=discord.Embed(
                description=f"✅ Queued **{track['title']}**",
                color=0x00FF00,
            ))
            for emoji in self.SEARCH_EMOJIS[: len(session["results"])]:
                try:
                    await msg.remove_reaction(emoji, self.bot.user)
                except Exception:
                    pass
        except Exception:
            pass

        # Find (or join) the picker's voice channel, then start/queue.
        voice = None
        for vc in self.bot.voice_clients:
            if getattr(vc, "guild", None) and vc.guild.id == guild_id:
                voice = vc
                break

        if voice is None or not voice.is_connected():
            # Try to join the picker's current voice channel.
            member = getattr(payload, "member", None)
            vch = getattr(getattr(member, "voice", None), "channel", None)
            if vch is not None:
                try:
                    voice = await vch.connect(timeout=MUSIC_VOICE_CONNECT_TIMEOUT)
                except Exception as exc:
                    print(f"[music] search-pick join failed: {exc!r}")
                    voice = None
            if voice is None:
                channel = self.bot.get_channel(payload.channel_id)
                if channel:
                    await channel.send("🔊 Join a voice channel, then run the search again.")
                return True

        state["queue"].append(track)
        if voice.is_playing() or voice.is_paused():
            channel = self.bot.get_channel(payload.channel_id)
            if channel:
                pos = len(state["queue"])
                dur = f" ({self._fmt_duration(track['duration'])})" if track["duration"] else ""
                await channel.send(f"➕ Queued **{track['title']}**{dur} — position **{pos}**")
        else:
            shim = _ResumeCtx(self.bot.get_guild(guild_id), voice, self.bot.get_channel(payload.channel_id))
            await self._play_next(shim)
        self._start_empty_timer(guild_id)
        return True

    async def _handle_track_end(self, ctx):
        """Apply the selected loop mode, then start the next queued track."""
        state = self.get_state(ctx.guild.id)
        current = state["current"]
        suppress_loop = state.pop("suppress_loop", False)

        if current is not None and not suppress_loop:
            if state["loop"] == "track":
                state["queue"].insert(0, current)
            elif state["loop"] == "queue":
                state["queue"].append(current)

        if current is not None:
            state["last_played"] = current
        state["current"] = None
        state["started_at"] = None
        state["elapsed"] = 0.0
        state["source"] = None
        await self._play_next(ctx)

    async def _play_next(self, ctx, resume_at: float = 0.0, force_track: dict | None = None):
        """Pop the next queued track and play it (commands + after-callback).

        resume_at > 0 starts playback at that offset (restart resume);
        force_track plays that exact track without touching the queue (the
        resume path - the track is already "current").
        """
        state = self.get_state(ctx.guild.id)
        voice = ctx.voice_client
        if voice is None or not voice.is_connected():
            self._reset_playback_state(state)
            return

        if force_track is None:
            if not state["queue"]:
                self._reset_playback_state(state)
                return
            if state["current"] is not None:
                state["last_played"] = state["current"]
            track = state["queue"].pop(0)
            state["current"] = track
        else:
            track = force_track
            state["current"] = track

        info = await asyncio.to_thread(self._extract, track["query"])
        if info is None or not info.get("url"):
            await ctx.send(embed=discord.Embed(description=f"⚠️ Couldn't fetch audio for **{track['title']}** — skipping.", color=0xFF0000))
            if force_track is not None:
                self._reset_playback_state(state)
                return
            await self._play_next(ctx)
            return

        state["voice"] = voice
        state["elapsed"] = max(0.0, resume_at)
        state["started_at"] = time.monotonic()
        state["channel_id"] = voice.channel.id

        before_options = FFMPEG_OPTS["before_options"]
        if resume_at > 0:
            # Fast seek to the saved position (keyframe accuracy - standard
            # for music bots) so a restart picks up where it left off.
            before_options = f"-ss {int(resume_at)} " + before_options

        source = discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(
                info["url"],
                executable=FFMPEG_BINARY,
                before_options=before_options,
                options=FFMPEG_OPTS["options"],
            ),
            volume=float(state.get("volume", 1.0)),
        )

        def _after(error):
            if error:
                print(f"[music] playback error: {error!r}")
            asyncio.run_coroutine_threadsafe(self._handle_track_end(ctx), self.bot.loop)

        voice.play(source, after=_after)
        state["source"] = source
        await self._send_now_playing(ctx)

    @commands.command(name="join", aliases=["j"])
    async def join(self, ctx):
        """Join the voice channel of the command sender."""
        voice = await self._ensure_voice(ctx)
        if voice is not None:
            await ctx.reply(f"🔊 Joined **{voice.channel.name}**.", mention_author=False)
            await self._start_empty_timer(ctx.guild.id)

    @commands.command(name="loop", aliases=["lp"])
    async def loop(self, ctx, mode: str = "track"):
        """Loop the current track or the whole queue."""
        if ctx.guild is None:
            await ctx.reply("Music only works in a server.", mention_author=False)
            return
        if not await self._require_dj(ctx):
            return

        mode = mode.casefold()
        if mode in ("off", "none", "disable", "disabled"):
            loop_mode = "off"
        elif mode in ("track", "song"):
            loop_mode = "track"
        elif mode == "queue":
            loop_mode = "queue"
        else:
            await ctx.reply("Usage: `!loop [track|queue]` (or `!loop off` to disable)", mention_author=False)
            return

        self.get_state(ctx.guild.id)["loop"] = loop_mode
        if loop_mode == "track":
            await ctx.reply("🔂 Track looping enabled.", mention_author=False)
        elif loop_mode == "queue":
            await ctx.reply("🔁 Queue looping enabled.", mention_author=False)
        else:
            await ctx.reply("➡️ Looping disabled.", mention_author=False)

    @commands.command(name="volume", aliases=["v"])
    async def volume(self, ctx, value: str = ""):
        """Show or set playback volume from 0 to 100 percent."""
        if ctx.guild is None:
            await ctx.reply("Music only works in a server.", mention_author=False)
            return
        state = self.get_state(ctx.guild.id)
        if not value:
            await ctx.reply(f"🔊 Volume is **{round(state['volume'] * 100)}%**.", mention_author=False)
            return
        if not await self._require_dj(ctx):
            return
        try:
            percent = int(value.rstrip("%"))
        except ValueError:
            await ctx.reply("Volume must be a whole number from 0 to 100.", mention_author=False)
            return
        if not 0 <= percent <= 100:
            await ctx.reply("Volume must be between 0 and 100.", mention_author=False)
            return
        state["volume"] = percent / 100
        source = state.get("source")
        if source is not None:
            source.volume = state["volume"]
        await ctx.reply(f"🔊 Volume set to **{percent}%**.", mention_author=False)
        if state.get("current"):
            await self._send_now_playing(ctx)

    async def _enqueue(self, ctx, query: str, insert: bool = False):
        """Resolve a link/search and add it to the queue.

        insert=False appends at the end (!play); insert=True splices right
        after the currently playing track (!insert). Playlists are expanded
        lazily and keep their order starting at the splice point.
        """
        if ctx.guild is None:
            await ctx.reply("Music only works in a server.", mention_author=False)
            return

        if not query.strip():
            cmd = "insert" if insert else "play"
            await ctx.reply(f"Usage: `!{cmd} <link or search>`", mention_author=False)
            return

        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.reply("You need to be in a voice channel first!", mention_author=False)
            return

        try:
            voice = await self._ensure_voice(ctx)
            if voice is None:
                return
        except Exception as e:
            print(f"[music] join error: {e!r}")
            await ctx.reply(
                "Couldn't join the voice channel. Make sure PyNaCl is installed "
                "(`pip install PyNaCl`) and the bot has voice permissions.",
                mention_author=False,
            )
            return

        query = query.strip()

        # Spotify links (track/album/playlist) are metadata-only: fetch the
        # tracklist from the embed page and queue YouTube searches for each
        # track instead of trying to resolve the link directly.
        spotify_match = _spotify_parse(query)
        if spotify_match is not None:
            spotify_type, spotify_id = spotify_match
            spotify_url = f"https://open.spotify.com/{spotify_type}/{spotify_id}"
            async with ctx.typing():
                data = await asyncio.to_thread(_spotify_fetch, spotify_url, spotify_type)
            if data is None or not data["tracks"]:
                await ctx.reply("Couldn't read that Spotify link. Is it public?", mention_author=False)
                return
            entries = self._spotify_track_dicts(data, ctx.author.mention)
            if not entries:
                await ctx.reply("Couldn't find any playable tracks on that Spotify link.", mention_author=False)
                return

            name = data.get("title") or spotify_type
            icon = {"track": "🎵", "album": "💿", "playlist": "📃"}.get(spotify_type, "🎵")
            if spotify_type == "track":
                single = f"{icon} **{name}**{f' — {data.get("artists", "")}' if data.get('artists') else ''} (Spotify)"
                plural = f"{icon} **{len(entries)}** tracks from **{name}** (Spotify {spotify_type})"
            else:
                single = plural = f"{icon} **{len(entries)}** tracks from **{name}** (Spotify {spotify_type})"
            await self._add_entries(ctx, voice, entries, single, plural, insert)
            return

        # YouTube playlist links expand to the full tracklist (lazily resolved),
        # same as Spotify playlists.
        lowered = query.lower()
        if (
            "youtube.com/playlist" in lowered
            or "youtu.be/playlist" in lowered
            or "/playlists" in lowered
        ):
            async with ctx.typing():
                try:
                    entries = await asyncio.to_thread(self._playlist_entries, query, ctx.author.mention)
                except Exception as e:
                    print(f"[music] playlist expand error for {query!r}: {e!r}")
                    entries = []
            if not entries:
                await ctx.reply("Couldn't find any videos in that playlist.", mention_author=False)
                return

            playlist_name = None
            try:
                import pytubefix as ptf
                playlist_name = ptf.Playlist(query).title
            except Exception:
                pass
            label = f"📃 **{len(entries)}** videos from **{playlist_name or 'YouTube playlist'}**"
            await self._add_entries(ctx, voice, entries, label, label, insert)
            return

        async with ctx.typing():
            info = await asyncio.to_thread(self._extract, query)
        if info is None or not info.get("url"):
            await ctx.reply("Couldn't find anything to play for that.", mention_author=False)
            return

        state = self.get_state(ctx.guild.id)
        state["text_channel_id"] = ctx.channel.id
        info["requester"] = ctx.author.mention

        playing = voice.is_playing() or voice.is_paused()
        if insert and playing:
            state["queue"].insert(0, info)
            pos = 1
            line = f"⏭️ Inserted **{info['title']}**"
            if info["duration"]:
                line += f" ({self._fmt_duration(info['duration'])})"
            line += " — playing **next**"
            await ctx.reply(line, mention_author=False)
        elif insert and not playing:
            state["queue"].append(info)
            await self._play_next(ctx)
        elif playing or len(state["queue"]) >= 1:
            state["queue"].append(info)
            line = f"➕ Queued **{info['title']}**"
            if info["duration"]:
                line += f" ({self._fmt_duration(info['duration'])})"
            line += f" — position **{len(state['queue'])}**"
            await ctx.reply(line, mention_author=False)
        else:
            state["queue"].append(info)
            await self._play_next(ctx)

        await self._start_empty_timer(ctx.guild.id)

    async def _add_entries(self, ctx, voice, entries: list[dict], single_label: str, plural_label: str, insert: bool):
        """Add pre-built queue entries (playlists) with the right reply, splicing
        after the current track when insert=True."""
        state = self.get_state(ctx.guild.id)
        state["text_channel_id"] = ctx.channel.id
        playing = voice.is_playing() or voice.is_paused()
        if insert and playing:
            state["queue"][0:0] = entries
            first_pos = 1
            line = f"⏭️ Inserted {plural_label} — playing **next**"
            await ctx.reply(line, mention_author=False)
        else:
            state["queue"].extend(entries)
            was_idle = not playing
            start_now = was_idle and len(state["queue"]) == len(entries)
            if start_now:
                await ctx.reply(single_label, mention_author=False)
                await self._play_next(ctx)
            else:
                first_pos = len(state["queue"]) - len(entries) + 1
                await ctx.reply(f"{plural_label} — starting at position **{first_pos}**", mention_author=False)
        await self._start_empty_timer(ctx.guild.id)

    @commands.command(name="play", aliases=["p"])
    async def play(self, ctx, *, query: str = ""):
        """Play a song from a link or search. Usage: `!play <link|search>`"""
        if not query.strip():
            # Bare !play with something paused/recently played acts as resume.
            voice = ctx.voice_client
            if voice is not None and voice.is_paused():
                await self.resume(ctx)
                return
            state = self.get_state(ctx.guild.id)
            last = state.get("last_played")
            if last is not None and voice is not None and voice.is_connected():
                state["queue"].insert(0, dict(last))
                await ctx.reply(f"▶️ Restarting **{last['title']}**.", mention_author=False)
                await self._play_next(ctx)
                return
        await self._enqueue(ctx, query, insert=False)

    @commands.command(name="playagain", aliases=["rewind"])
    async def playagain(self, ctx):
        """Replay the last finished song right after the current one."""
        voice = ctx.voice_client
        if voice is None or not voice.is_connected():
            await ctx.reply("I'm not in a voice channel.", mention_author=False)
            return
        state = self.get_state(ctx.guild.id)
        last = state.get("last_played")
        if last is None:
            await ctx.reply("Nothing has finished playing yet this session.", mention_author=False)
            return
        if not (voice.is_playing() or voice.is_paused()):
            # Idle: just start the last track again.
            state["queue"].insert(0, dict(last))
            await ctx.reply(f"⏪ Replaying **{last['title']}**.", mention_author=False)
            await self._play_next(ctx)
            return
        state["queue"].insert(0, dict(last))
        await ctx.reply(f"⏪ Queued **{last['title']}** again — playing **next**.", mention_author=False)

    @commands.command(name="shuffle", aliases=["mix"])
    async def shuffle(self, ctx):
        """Shuffle the current queue."""
        if ctx.guild is None:
            await ctx.reply("Music only works in a server.", mention_author=False)
            return
        if not await self._require_dj(ctx):
            return
        state = self.get_state(ctx.guild.id)
        if len(state["queue"]) < 2:
            await ctx.reply("Not enough songs in the queue to shuffle.", mention_author=False)
            return
        random.shuffle(state["queue"])
        await ctx.reply(f"🔀 Shuffled **{len(state['queue'])}** songs.", mention_author=False)

    @staticmethod
    def _parse_seek_timestamp(raw: str, current_elapsed: float, duration: int) -> float | None:
        """Parse a !seek argument into a target position in seconds.

        Accepts absolute forms (90, 1:30, 1:02:03) and relative forms
        (+30, -15, +1:00) that offset from the current position. Returns
        None for garbage; clamps the result into [0, duration].
        """
        text = (raw or "").strip()
        if not text:
            return None
        relative = text[0] in "+-"
        sign = -1 if text.startswith("-") else 1
        body = text[1:] if relative else text
        if not body.replace(":", "").isdigit():
            return None
        parts = body.split(":")
        if len(parts) > 3 or any(p == "" for p in parts):
            return None
        try:
            seconds = sum(int(p) * 60 ** i for i, p in enumerate(reversed(parts)))
        except ValueError:
            return None
        if relative:
            seconds = current_elapsed + sign * seconds
        return max(0.0, min(float(seconds), float(duration))) if duration > 0 else max(0.0, float(seconds))

    @commands.command(name="seek", aliases=["goto"])
    async def seek(self, ctx, *, timestamp: str = ""):
        """Jump to a position in the current track (DJ).

        Usage: `!seek 1:23` (absolute), `!seek +30` / `!seek -15` (relative).
        """
        if ctx.guild is None:
            await ctx.reply("Music only works in a server.", mention_author=False)
            return
        if not await self._require_dj(ctx):
            return
        state = self.get_state(ctx.guild.id)
        voice = ctx.voice_client
        current = state.get("current")
        if voice is None or not voice.is_connected() or current is None:
            await ctx.reply("Nothing is playing to seek in.", mention_author=False)
            return
        elapsed = self._elapsed_raw(state)
        duration = int(current.get("duration", 0) or 0)
        target = self._parse_seek_timestamp(timestamp, elapsed, duration)
        if target is None:
            await ctx.reply(
                "Usage: `!seek <time>` — e.g. `!seek 1:23` (jump), `!seek +30` / `!seek -15` (relative).",
                mention_author=False,
            )
            return
        if duration and elapsed >= duration - 1:
            await ctx.reply("The track is already at its end — use `!skip` or `!playagain`.", mention_author=False)
            return
        if duration and target >= duration - 1:
            await ctx.reply(
                f"Can't seek past the end — the track is only **{self._fmt_duration(duration)}** long.",
                mention_author=False,
            )
            return

        was_paused = bool(state.get("paused"))
        # Fold any pause into elapsed so the position math stays correct, then
        # restart the same track at the target offset. suppress_loop stops
        # _handle_track_end from re-queuing the track when we stop it.
        state["elapsed"] = elapsed
        state["started_at"] = None
        state["paused"] = False
        state["suppress_loop"] = True
        if voice.is_playing() or voice.is_paused():
            voice.stop()
        await self._play_next(ctx, resume_at=target, force_track=current)
        verb = (
            "jumped to"
            if not timestamp[:1] in "+-"
            else ("stepped forward to" if timestamp[0] == "+" else "stepped back to")
        )
        await ctx.reply(
            f"⏩ {verb.capitalize()} **{self._fmt_duration(int(target))}** in **{current.get('title', 'Unknown')}**.",
            mention_author=False,
        )
        if was_paused:
            # Seeking while paused leaves the new position audible (simplest
            # predictable behavior), so just clear the stale paused flag.
            state["paused"] = False

    @commands.command(name="removeduplicates", aliases=["rmdup", "rmduplicate", "rmduplicates"])
    async def removeduplicates(self, ctx):
        """Remove duplicate songs from the queue (keeps the first occurrence)."""
        if ctx.guild is None:
            await ctx.reply("Music only works in a server.", mention_author=False)
            return
        if not await self._require_dj(ctx):
            return
        state = self.get_state(ctx.guild.id)
        seen = set()
        kept = []
        removed = 0
        for track in state["queue"]:
            key = (track.get("title") or "").casefold().strip()
            if key in seen:
                removed += 1
                continue
            seen.add(key)
            kept.append(track)
        if removed == 0:
            await ctx.reply("No duplicates found — the queue is already clean.", mention_author=False)
            return
        state["queue"][:] = kept
        await ctx.reply(f"🧹 Removed **{removed}** duplicate song(s); **{len(kept)}** remain.", mention_author=False)

    @commands.command(name="search")
    async def search(self, ctx, *, query: str = ""):
        """Search YouTube and pick a result to queue. Usage: `!search <query>`"""
        if not query.strip():
            await ctx.reply("Usage: `!search <query>`", mention_author=False)
            return
        if ctx.guild is None:
            await ctx.reply("Music only works in a server.", mention_author=False)
            return
        async with ctx.typing():
            results = await asyncio.to_thread(self._youtube_search_results, query.strip(), 5)
        await self._run_search_picker(ctx, "youtube", results, "YouTube")

    @commands.command(name="searchspotify", aliases=["ssearch", "spotifysearch"])
    async def searchspotify(self, ctx, *, query: str = ""):
        """Search Spotify's catalogue and pick a track to queue. Usage: `!searchspotify <query>`"""
        if not query.strip():
            await ctx.reply("Usage: `!searchspotify <query>`", mention_author=False)
            return
        if ctx.guild is None:
            await ctx.reply("Music only works in a server.", mention_author=False)
            return
        async with ctx.typing():
            results = await asyncio.to_thread(self._spotify_search_results, query.strip(), 5)
        await self._run_search_picker(ctx, "spotify", results, "Spotify")

    @commands.command(name="insert", aliases=["i"])
    async def insert(self, ctx, *, query: str = ""):
        """Insert a song right after the current one. Usage: `!insert <link|search>`"""
        await self._enqueue(ctx, query, insert=True)

    @commands.command(name="skip", aliases=["next", "s"])
    async def skip(self, ctx):
        """Skip the current track."""
        voice = ctx.voice_client
        if voice is None or not voice.is_connected():
            await ctx.reply("I'm not in a voice channel.", mention_author=False)
            return

        if self._is_dj_or_admin(ctx):
            state = self.get_state(ctx.guild.id)
            if voice.is_playing() or voice.is_paused():
                state["suppress_loop"] = True
                voice.stop()
                await ctx.reply("⏭️ Skipped!", mention_author=False)
                return

            await self._play_next(ctx)
            if self.get_state(ctx.guild.id)["current"] is None:
                await ctx.reply("Nothing is playing and the queue is empty.", mention_author=False)
            return

        humans = self._get_human_voice_members(ctx)
        if not humans:
            await ctx.reply("You need to be in the voice channel to vote.", mention_author=False)
            return

        required = max(1, int(len(humans) * MUSIC_VOTE_REQUIRED_RATIO))
        vote_msg = await ctx.reply(
            f"📊 Vote to skip: {required}/{len(humans)} votes needed. "
            f"React with 👍 to vote, 👎 to cancel.",
            mention_author=False,
        )
        await asyncio.gather(
            vote_msg.add_reaction("👍"),
            vote_msg.add_reaction("👎"),
        )

        self.votes[vote_msg.id] = {
            "guild_id": ctx.guild.id,
            "action": "skip",
            "votes": set(),
            "required": required,
            "total": len(humans),
            "starter": ctx.author.id,
        }

    @commands.command(name="leave", aliases=["l", "stop", "disconnect", "dc"])
    async def stop(self, ctx):
        """Stop playback, clear the queue, and leave the voice channel."""
        voice = ctx.voice_client
        if voice is None or not voice.is_connected():
            await ctx.reply("I'm not in a voice channel.", mention_author=False)
            return

        if self._is_dj_or_admin(ctx):
            state = self.get_state(ctx.guild.id)
            state["queue"].clear()
            state["suppress_loop"] = True
            state["loop"] = "off"
            if voice.is_playing() or voice.is_paused():
                voice.stop()
            await voice.disconnect()
            self._reset_playback_state(state)
            state["last_played"] = None
            self._clear_resume_markers(state)
            self._persist()  # wipe the saved state now so a restart can't resurrect the cleared queue
            await ctx.reply("👋 Left the voice channel and cleared the queue.", mention_author=False)
            self._cancel_empty_timer(ctx.guild.id)
            return

        humans = self._get_human_voice_members(ctx)
        if not humans:
            await ctx.reply("You need to be in the voice channel to vote.", mention_author=False)
            return

        required = max(1, int(len(humans) * MUSIC_VOTE_REQUIRED_RATIO))
        vote_msg = await ctx.reply(
            f"📊 Vote to leave: {required}/{len(humans)} votes needed. "
            f"React with 👍 to vote, 👎 to cancel.",
            mention_author=False,
        )
        await asyncio.gather(
            vote_msg.add_reaction("👍"),
            vote_msg.add_reaction("👎"),
        )

        self.votes[vote_msg.id] = {
            "guild_id": ctx.guild.id,
            "action": "leave",
            "votes": set(),
            "required": required,
            "total": len(humans),
            "starter": ctx.author.id,
        }

    @commands.command(name="queue", aliases=["q"])
    async def queue(self, ctx, action: str = "", first: str = "", second: str = ""):
        """Show or manage the queue. Management actions require DJ access."""
        if ctx.guild is None:
            await ctx.reply("Music only works in a server.", mention_author=False)
            return
        state = self.get_state(ctx.guild.id)
        action = action.casefold()

        if action in ("clear", "empty"):
            if not await self._require_dj(ctx):
                return
            state["queue"].clear()
            await ctx.reply("🧹 Queue cleared. The current track is still playing.", mention_author=False)
            return

        if action in ("remove", "rm", "delete", "del"):
            if not await self._require_dj(ctx):
                return
            try:
                index = int(first) - 1
                track = state["queue"][index]
            except (ValueError, IndexError):
                await ctx.reply("Usage: `!queue remove <position>`.", mention_author=False)
                return
            state["queue"].pop(index)
            await ctx.reply(f"🗑️ Removed **{track['title']}** from the queue.", mention_author=False)
            return

        if action in ("move", "mv"):
            if not await self._require_dj(ctx):
                return
            try:
                old_index = int(first) - 1
                new_index = int(second) - 1
                if not 0 <= old_index < len(state["queue"]):
                    raise IndexError
                if not 0 <= new_index < len(state["queue"]):
                    raise IndexError
                track = state["queue"].pop(old_index)
                state["queue"].insert(new_index, track)
            except (ValueError, IndexError):
                await ctx.reply("Usage: `!queue move <from-position> <to-position>`.", mention_author=False)
                return
            await ctx.reply(f"↕️ Moved **{track['title']}** to queue position **{new_index + 1}**.", mention_author=False)
            return

        if action in ("shuffle", "mix"):
            if not await self._require_dj(ctx):
                return
            random.shuffle(state["queue"])
            await ctx.reply("🔀 Queue shuffled.", mention_author=False)
            return

        if action:
            await ctx.reply("Usage: `!queue`, `!queue remove <position>`, `!queue move <from> <to>`, `!queue clear`, or `!queue shuffle`.", mention_author=False)
            return

        embed = self._now_playing_embed(ctx.guild.id)
        if embed is not None:
            await ctx.reply(embed=embed, mention_author=False)
        if not state["queue"]:
            await ctx.send("Queue is empty.")
            return
        lines = [f"**Up next ({len(state['queue'])}):**"]
        for i, track in enumerate(state["queue"], start=1):
            duration = f" ({self._fmt_duration(track['duration'])})" if track.get("duration") else ""
            requester = f" • {track.get('requester', 'Unknown')}" if track.get("requester") else ""
            lines.append(f"**{i}.** {track['title']}{duration}{requester}")
        await ctx.send(embed=discord.Embed(description="\n".join(lines), color=0x00AAFF))

    @commands.command(name="pause", aliases=["ps"])
    async def pause(self, ctx):
        """Pause the current track."""
        if not await self._require_dj(ctx):
            return
        state = self.get_state(ctx.guild.id)
        voice = ctx.voice_client
        if voice and voice.is_playing():
            state["elapsed"] = self._elapsed_seconds(state)
            state["started_at"] = None
            state["paused"] = True
            voice.pause()
            await ctx.reply("⏸️ Paused.", mention_author=False)
        else:
            await ctx.reply("Nothing is playing.", mention_author=False)

    @commands.command(name="resume", aliases=["unpause"])
    async def resume(self, ctx):
        """Resume the paused track."""
        if not await self._require_dj(ctx):
            return
        state = self.get_state(ctx.guild.id)
        voice = ctx.voice_client
        if voice and voice.is_paused():
            state["started_at"] = time.monotonic()
            state["paused"] = False
            voice.resume()
            await ctx.reply("▶️ Resumed.", mention_author=False)
        else:
            await ctx.reply("Nothing is paused.", mention_author=False)
    @commands.command(name="nowplaying", aliases=["np"])
    async def nowplaying(self, ctx):
        """Show what's currently playing as a detailed embed."""
        if ctx.guild is None:
            await ctx.reply("Music only works in a server.", mention_author=False)
            return
        embed = self._now_playing_embed(ctx.guild.id)
        if embed is None:
            await ctx.reply("Nothing is playing right now.", mention_author=False)
            return
        await ctx.reply(embed=embed, mention_author=False)

    @commands.command(name="dj")
    @commands.has_permissions(manage_guild=True)
    async def set_dj(self, ctx, role: discord.Role):
        """Set the DJ role for this server. Usage: `!dj <role>`"""
        self.dj_roles[ctx.guild.id] = role.id
        _save_music_settings(self.dj_roles)
        await ctx.reply(f"🎧 DJ role set to **{role.name}**.", mention_author=False)

    @commands.command(name="help")
    async def music_help(self, ctx):
        """Show all music commands."""
        embed = discord.Embed(
            title="🎵 Music Bot Commands",
            description=f"Prefix: `!`\nUse `!help <command>` for details.",
            color=0x00ff00,
        )
        embed.add_field(
            name="Playback",
            value="`!play` / `!p` — Play a song\n"
                  "`!search` — Search YouTube\n"
                  "`!searchspotify` / `!ssearch` — Search Spotify\n"
                  "`!insert` / `!i` — Queue next\n"
                  "`!skip` / `!s` — Skip\n"
                  "`!pause` / `!ps` — Pause\n"
                  "`!resume` — Resume\n"
                  "`!seek` / `!goto` — Seek position\n"
                  "`!playagain` / `!rewind` — Replay last song",
            inline=False,
        )
        embed.add_field(
            name="Queue",
            value="`!queue` / `!q` — Show queue\n"
                  "`!shuffle` / `!mix` — Shuffle\n"
                  "`!removeduplicates` / `!rmdup` — Remove dupes",
            inline=False,
        )
        embed.add_field(
            name="Voice",
            value="`!join` / `!j` — Join voice\n"
                  "`!leave` / `!dc` — Leave voice\n"
                  "`!loop` / `!lp` — Loop track/queue\n"
                  "`!nowplaying` / `!np` — Now playing\n"
                  "`!volume` / `!v` — Set volume",
            inline=False,
        )
        embed.add_field(
            name="Admin",
            value="`!dj <role>` — Set DJ role",
            inline=False,
        )
        await ctx.reply(embed=embed, mention_author=False)

    def cog_unload(self):
        self._persist_loop.cancel()
        self._cancel_all_empty_timers()
        for task in self.kick_checks.values():
            if not task.done():
                task.cancel()
        self.kick_checks.clear()
        for vote in self.votes.values():
            vote["votes"].clear()
        self.votes.clear()
        self.search_sessions.clear()

    # ---------- events ----------

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        # Search pickers first (number emojis on search messages).
        if self.search_sessions:
            try:
                handled = await self._handle_search_pick(payload)
            except Exception as exc:
                print(f"[music] search pick error: {exc!r}")
                handled = True
            if handled:
                return

        vote = self.votes.get(payload.message_id)
        if vote is None:
            return
        if payload.user_id == self.bot.user.id:
            return
        if payload.emoji.name not in ("👍", "👎"):
            return

        channel = self.bot.get_channel(payload.channel_id)
        if channel is None:
            return
        try:
            msg = await channel.fetch_message(payload.message_id)
        except Exception:
            return

        if payload.emoji.name == "👍":
            vote["votes"].add(payload.user_id)
        elif payload.emoji.name == "👎":
            vote["votes"].discard(payload.user_id)

        yes = len(vote["votes"])
        needed = vote["required"]
        total = vote["total"]

        if yes >= needed:
            guild_id = vote["guild_id"]
            action = vote["action"]

            voice = None
            for vc in self.bot.voice_clients:
                if getattr(vc, "guild", None) and vc.guild.id == guild_id:
                    voice = vc
                    break

            state = self.get_state(guild_id)

            if action == "skip" and voice and (voice.is_playing() or voice.is_paused()):
                state["suppress_loop"] = True
                voice.stop()
                await msg.edit(content=f"✅ Skip passed with {yes}/{total} votes.")
            elif action == "leave" and voice and voice.is_connected():
                state["queue"].clear()
                state["current"] = None
                state["loop"] = "off"
                state["suppress_loop"] = True
                await voice.disconnect()
                await msg.edit(content=f"✅ Leave passed with {yes}/{total} votes.")
                self._cancel_empty_timer(guild_id)

            self.votes.pop(payload.message_id, None)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        # Clean up our state if the bot gets disconnected externally.
        if member.id == self.bot.user.id and after.channel is None:
            # Debounced: discord.py handles voice reconnects internally (region
            # moves, websocket drops) and during them the bot briefly reports
            # "no channel" (and even a not-connected client) while audio keeps
            # playing. Wiping immediately used to blank !np/!q mid-song, so
            # only treat it as a real kick if we're still gone 10s later.
            self._cancel_kick_check(member.guild.id)
            self.kick_checks[member.guild.id] = asyncio.create_task(
                self._confirm_external_kick(member.guild.id)
            )
            return

        voice = None
        for vc in self.bot.voice_clients:
            if getattr(vc, "guild", None) and vc.guild.id == member.guild.id:
                voice = vc
                break

        if voice is None or not voice.is_connected() or not getattr(voice, "channel", None):
            return

        humans = [m for m in voice.channel.members if not m.bot]
        if not humans:
            await self._start_empty_timer(member.guild.id)
        else:
            self._cancel_empty_timer(member.guild.id)


async def setup(bot):
    await bot.add_cog(Music(bot))