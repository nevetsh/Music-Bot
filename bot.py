import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

from config import SUPER_ADMIN_ID, BOT_PREFIX

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True


class MusicBot(commands.Bot):
    async def setup_hook(self):
        await self.load_extension("music")

        # Sync globally by default. Set DISCORD_GUILD_ID for immediate updates
        # while developing; global Discord commands can take up to an hour to appear.
        guild_id = os.getenv("DISCORD_GUILD_ID")
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            print(f"[OK] Synced {len(synced)} slash commands to guild {guild_id}")
        else:
            synced = await self.tree.sync()
            print(f"[OK] Synced {len(synced)} global slash commands")


bot = MusicBot(command_prefix=BOT_PREFIX, intents=intents)
bot.help_command = None


@bot.event
async def on_ready():
    print(f"[OK] Music bot logged in: {bot.user.name}")


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    try:
        await ctx.reply(f"Error: {error}", mention_author=False)
    except Exception as reply_exc:
        print(f"[error] Command error in #{ctx.channel}: {error!r} (reply also failed: {reply_exc!r})")


if __name__ == "__main__":
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN is not configured.")
    bot.run(token)
