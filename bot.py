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
