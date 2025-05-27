import discord
from discord.ext import commands
from datetime import timedelta
from discord.utils import utcnow
from discord import Forbidden

# 🔓 Hardcoded token (REPLACE THIS with your actual bot token)
DISCORD_TOKEN = "Your Token here"

# Create the bot with the necessary intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)

# Custom list of usernames allowed to use all commands (lowercase for case-insensitive match)
authorized_users = ["doper_official", "h4shatter", "poopiediaper1_22550"]

# Helper function to check if the user is authorized (compares usernames without discriminator)
def is_authorized(ctx):
    username = str(ctx.author).split("#")[0].lower()
    return username in authorized_users

@bot.event
async def on_ready():
    print(f'✅ Logged in as {bot.user.name}')

@bot.command()
async def ping(ctx):
    """Responds with Pong!"""
    await ctx.send("Pong! 🏓")

@bot.command()
async def ban(ctx, member: discord.Member, *, reason=None):
    """Ban a user from the server."""
    if ctx.author.guild_permissions.ban_members or is_authorized(ctx):
        await member.ban(reason=reason)
        await ctx.send(f'🚨 User {member.name} has been **banned** for: {reason}')
    else:
        print(f"❌ Unauthorized ban attempt by {ctx.author}")
        await ctx.send('🛑 HOLD IT RIGHT THERE, CRIMINAL SCUM. ❌ You don’t have permission to ban members.')

@bot.command()
async def kick(ctx, member: discord.Member, *, reason=None):
    """Kick a user from the server."""
    if ctx.author.guild_permissions.kick_members or is_authorized(ctx):
        await member.kick(reason=reason)
        await ctx.send(f'👢 User {member.name} has been **kicked** for: {reason}')
    else:
        print(f"❌ Unauthorized kick attempt by {ctx.author}")
        await ctx.send('🛑 HOLD IT RIGHT THERE, CRIMINAL SCUM. ❌ You don’t have permission to kick members.')

# Dynamically create and register timeout commands from 1 to 60 minutes
def create_timeout_command(minutes):
    async def timeout(ctx, member: discord.Member, *, reason=None):
        if ctx.author.guild_permissions.moderate_members or is_authorized(ctx):
            try:
                duration = utcnow() + timedelta(minutes=minutes)
                await member.timeout(duration, reason=reason)
                await ctx.send(f'⏰ User {member.name} has been **timed out** for {minutes} minute(s). Reason: {reason}')
            except Forbidden:
                await ctx.send("🛑 HOLD IT RIGHT THERE, CRIMINAL SCUM! 🛑 I don’t have enough power to timeout this user. Check bot role hierarchy and permissions.")
            except Exception as e:
                await ctx.send(f'❌ Failed to timeout {member.name}. Unexpected error: {e}')
        else:
            print(f"❌ Unauthorized timeout_{minutes} attempt by {ctx.author}")
            await ctx.send('🛑 HOLD IT RIGHT THERE, CRIMINAL SCUM. ❌ You don’t have permission to timeout members.')
    return timeout

# Register all timeout commands dynamically for 1 to 60 minutes
for i in range(1, 61):
    bot.command(name=f'timeout_{i}')(create_timeout_command(i))

# Run the bot
bot.run(DISCORD_TOKEN)
