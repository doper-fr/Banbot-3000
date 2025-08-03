import discord
from discord.ext import commands, tasks
import asyncio
import sys
import time
import traceback
import logging
import re
import os
from datetime import datetime, timedelta, timezone
from typing import Optional, Union, Dict, List
from dataclasses import dataclass
from enum import Enum
from collections import defaultdict, deque

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger('BanBot3000')

class ActionType(Enum):
    BAN = "ban"
    KICK = "kick"
    TIMEOUT = "timeout"
    WARN = "warn"
    DEOP = "deop"

@dataclass
class ModerationAction:
    user_id: int
    moderator_id: int
    action: ActionType
    reason: str
    timestamp: datetime
    duration: Optional[int] = None  # in minutes for timeouts
    active: bool = True

@dataclass
class Warning:
    id: int
    user_id: int
    moderator_id: int
    reason: str
    timestamp: datetime
    active: bool = True

@dataclass
class DeoppedUser:
    user_id: int
    deopped_by: int
    timestamp: datetime
    reason: str

class BanBot3000(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.guilds = True
        intents.moderation = True

        super().__init__(
            command_prefix="bot",
            intents=intents,
            help_command=None,
            case_insensitive=True
        )

        # In-memory configuration
        self.config = {
            "error_channel_id": None,
            "admin_users": ["doper_official"],  # Users who get all permissions
            "moderator_users": [],  # Users who get specific mod permissions (if needed)
            "max_timeout_days": 28,
            "auto_mod": {
                "enabled": False,
                "spam_threshold": 5,
                "spam_timeframe": 10
            }
        }

        # In-memory data storage
        self.moderation_actions: List[ModerationAction] = []
        self.deopped_users: Dict[int, DeoppedUser] = {}
        self.warnings: List[Warning] = []
        self.processed_messages: deque = deque(maxlen=1000)

        # Auto-incrementing IDs
        self.next_warning_id = 1
        self.next_action_id = 1

        # Stats tracking
        self.stats = {
            "bans": 0,
            "kicks": 0,
            "timeouts": 0,
            "warnings": 0,
            "deops": 0,
            "commands_used": 0,
            "uptime_start": datetime.now(timezone.utc)
        }

        # Spam detection
        self.user_message_times: Dict[int, deque] = defaultdict(lambda: deque(maxlen=10))

    def is_admin(self, user: Union[discord.Member, discord.User]) -> bool:
        """Check if user is admin"""
        is_named_admin = user.name.lower() in [name.lower() for name in self.config["admin_users"]]
        is_guild_owner = isinstance(user, discord.Member) and user == user.guild.owner
        return is_named_admin or is_guild_owner

    def is_deopped(self, user_id: int) -> bool:
        """Check if user is deopped"""
        return user_id in self.deopped_users

    def has_permission_for_action(self, member: discord.Member, action: str) -> bool:
        """Check if user has permission for specific moderation action"""
        # If user is explicitly mentioned in admin_users, give them all permissions
        if self.is_admin(member):
            return True

        # Check Discord role permissions for the specific action
        permissions = member.guild_permissions

        if action == "ban":
            return permissions.ban_members
        elif action == "kick":
            return permissions.kick_members
        elif action == "timeout":
            return permissions.moderate_members
        elif action == "warn":
            # Warnings can be issued by anyone with manage messages or kick/ban permissions
            return permissions.manage_messages or permissions.kick_members or permissions.ban_members
        elif action == "cleanup":
            return permissions.manage_messages
        else:
            return False

    def is_authorized(self, ctx: commands.Context, action: str = "general") -> bool:
        """Check if user is authorized to use moderation commands"""
        # First check if they're deopped
        if self.is_deopped(ctx.author.id):
            return False

        # Then check if they have permission for the specific action
        if isinstance(ctx.author, discord.Member):
            return self.has_permission_for_action(ctx.author, action)
        return False

    def log_action(self, action: ModerationAction):
        """Log moderation action to memory"""
        action.timestamp = datetime.now(timezone.utc)
        self.moderation_actions.append(action)

        # Update stats
        action_key = f"{action.action.value}s"
        if action_key in self.stats:
            self.stats[action_key] += 1

        # Keep only last 10000 actions to prevent memory issues
        if len(self.moderation_actions) > 10000:
            self.moderation_actions = self.moderation_actions[-5000:]

    def add_warning(self, user_id: int, moderator_id: int, reason: str) -> Warning:
        """Add a warning to memory"""
        warning = Warning(
            id=self.next_warning_id,
            user_id=user_id,
            moderator_id=moderator_id,
            reason=reason,
            timestamp=datetime.now(timezone.utc)
        )
        self.warnings.append(warning)
        self.next_warning_id += 1
        self.stats["warnings"] += 1
        return warning

    def get_user_warnings(self, user_id: int, limit: int = 10) -> List[Warning]:
        """Get warnings for a user"""
        user_warnings = [w for w in self.warnings if w.user_id == user_id and w.active]
        return sorted(user_warnings, key=lambda x: x.timestamp, reverse=True)[:limit]

    def get_user_actions(self, user_id: int, limit: int = 10) -> List[ModerationAction]:
        """Get moderation actions for a user"""
        user_actions = [a for a in self.moderation_actions if a.user_id == user_id]
        return sorted(user_actions, key=lambda x: x.timestamp, reverse=True)[:limit]

    async def report_error(self, ctx_or_channel, error_text: str):
        """Enhanced error reporting"""
        try:
            error_channel_id = self.config.get("error_channel_id")
            if error_channel_id:
                channel = self.get_channel(error_channel_id)
            else:
                if isinstance(ctx_or_channel, commands.Context):
                    channel = ctx_or_channel.channel
                else:
                    channel = ctx_or_channel

            if channel and hasattr(channel, 'send'):
                if len(error_text) > 1900:
                    error_text = error_text[:1900] + "\n[Truncated...]"

                embed = discord.Embed(
                    title="🚨 BanBot 3000 Error",
                    description=f"```python\n{error_text}\n```",
                    color=discord.Color.red(),
                    timestamp=datetime.now(timezone.utc)
                )
                await channel.send(embed=embed)
        except Exception as e:
            logger.error(f"Failed to send error report: {e}")

    async def check_permissions(self, guild: discord.Guild) -> Dict[str, bool]:
        """Check if bot has required permissions"""
        if not self.user:
            return {}
        bot_member = guild.get_member(self.user.id)
        if not bot_member:
            return {}

        permissions = bot_member.guild_permissions

        return {
            "ban_members": permissions.ban_members,
            "kick_members": permissions.kick_members,
            "moderate_members": permissions.moderate_members,  # For timeouts
            "manage_messages": permissions.manage_messages,
            "read_message_history": permissions.read_message_history,
            "send_messages": permissions.send_messages,
            "embed_links": permissions.embed_links
        }

    @tasks.loop(minutes=30)
    async def cleanup_task(self):
        """Periodic cleanup task"""
        try:
            # Clean up old message tracking
            now = datetime.now(timezone.utc)
            cutoff = now - timedelta(hours=1)

            for user_id in list(self.user_message_times.keys()):
                user_times = self.user_message_times[user_id]
                while user_times and user_times[0] < cutoff:
                    user_times.popleft()

                # Remove empty queues
                if not user_times:
                    del self.user_message_times[user_id]

            logger.info(f"Cleanup completed. Tracking {len(self.user_message_times)} users.")
        except Exception as e:
            logger.error(f"Cleanup task error: {e}")

    async def on_ready(self):
        """Bot ready event"""
        logger.info(f'{self.user} has connected to Discord!')
        logger.info(f'Bot is in {len(self.guilds)} guilds')
        
        # Start cleanup task
        if not self.cleanup_task.is_running():
            self.cleanup_task.start()

        # Set bot status
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="for rule violations | bothelp"
            ),
            status=discord.Status.online
        )

    async def on_command_error(self, ctx, error):
        """Global error handler"""
        self.stats["commands_used"] += 1

        if isinstance(error, commands.CommandNotFound):
            return  # Ignore command not found errors

        elif isinstance(error, commands.MissingPermissions):
            embed = discord.Embed(
                title="❌ Missing Permissions",
                description=f"You don't have permission to use this command.\nRequired: {', '.join(error.missing_permissions)}",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)

        elif isinstance(error, commands.BotMissingPermissions):
            embed = discord.Embed(
                title="❌ Bot Missing Permissions", 
                description=f"I don't have permission to do that.\nRequired: {', '.join(error.missing_permissions)}",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)

        elif isinstance(error, commands.MemberNotFound):
            embed = discord.Embed(
                title="❌ Member Not Found",
                description="Could not find that member. Make sure you mention them correctly.",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)

        elif isinstance(error, commands.BadArgument):
            embed = discord.Embed(
                title="❌ Invalid Argument",
                description=f"Invalid argument provided: {str(error)}",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)

        else:
            # Log unexpected errors
            error_text = f"Command: {ctx.command}\nError: {str(error)}\nTraceback: {traceback.format_exc()}"
            logger.error(error_text)
            await self.report_error(ctx, error_text)

            embed = discord.Embed(
                title="❌ Unexpected Error",
                description="An unexpected error occurred. The error has been logged.",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)

# Create bot instance
bot = BanBot3000()

# --- COMMANDS ---

@bot.command(name="help", aliases=["h"])
async def help_command(ctx):
    """Display help information"""
    embed = discord.Embed(
        title="🤖 BanBot 3000 Commands",
        description="Advanced Discord Moderation Bot (RAM Edition)",
        color=discord.Color.blue()
    )

    embed.add_field(
        name="👑 Moderation Commands",
        value="""
`botban @user [reason]` - Ban a user (Requires Ban Members permission)
`botkick @user [reason]` - Kick a user (Requires Kick Members permission)
`bottimeout @user <duration> [reason]` - Timeout a user (Requires Timeout Members permission)
`bottimeout(Xm/h/d) @user` - Legacy timeout format
`botwarn @user [reason]` - Warn a user (Requires Manage Messages/Kick/Ban permission)
`botdeop @user [reason]` - Remove admin privileges (Admin only)
`botreop @user` - Restore admin privileges (Admin only)
""",
        inline=False
    )

    embed.add_field(
        name="📊 Info Commands",
        value="""
`botstats` - Show bot statistics
`botwarnings @user` - Show user warnings
`bothistory @user` - Show moderation history
`botdeopped` - Show deopped users
`botmemory` - Show memory usage stats
`botperms` - Check bot permissions
`botuserperms @user` - Check user permissions
""",
        inline=False
    )

    embed.add_field(
        name="🔧 Utility",
        value="`botping` - Test bot response\n`botcleanup [amount]` - Clean messages (Requires Manage Messages)\n`botuptime` - Show bot uptime",
        inline=False
    )

    await ctx.send(embed=embed)

@bot.command()
async def ping(ctx):
    """Test bot responsiveness"""
    latency = round(bot.latency * 1000)
    embed = discord.Embed(
        title="🏓 Pong!",
        description=f"BanBot 3000 (RAM Edition) is online!\nLatency: {latency}ms",
        color=discord.Color.green()
    )
    await ctx.send(embed=embed)

@bot.command()
async def perms(ctx):
    """Check bot permissions"""
    perms = await bot.check_permissions(ctx.guild)
    embed = discord.Embed(
        title="🔐 Bot Permissions",
        color=discord.Color.blue()
    )

    for perm, has_perm in perms.items():
        status = "✅" if has_perm else "❌"
        embed.add_field(
            name=f"{status} {perm.replace('_', ' ').title()}",
            value=f"Required for moderation" if perm in ["ban_members", "kick_members", "moderate_members"] else "Utility",
            inline=True
        )

    if not perms.get("moderate_members", False):
        embed.add_field(
            name="⚠️ Missing Permissions",
            value="Bot needs 'Timeout Members' permission for timeout commands!",
            inline=False
        )

    await ctx.send(embed=embed)

@bot.command()
async def userperms(ctx, member: discord.Member = None):
    """Check user permissions for moderation commands"""
    if member is None:
        if isinstance(ctx.author, discord.Member):
            member = ctx.author
        else:
            await ctx.send("This command can only be used in a server.")
            return

    embed = discord.Embed(
        title=f"🔐 {member.display_name}'s Moderation Permissions",
        color=discord.Color.blue()
    )

    permissions_check = {
        "Ban Members": ("ban", member.guild_permissions.ban_members),
        "Kick Members": ("kick", member.guild_permissions.kick_members),
        "Timeout Members": ("timeout", member.guild_permissions.moderate_members),
        "Manage Messages": ("cleanup", member.guild_permissions.manage_messages),
    }

    # Check if user is admin
    is_admin = bot.is_admin(member)
    is_deopped = bot.is_deopped(member.id)

    embed.add_field(
        name="👑 Admin Status",
        value="✅ Admin User" if is_admin else "❌ Not Admin",
        inline=True
    )

    embed.add_field(
        name="🚫 Deop Status",
        value="❌ Deopped" if is_deopped else "✅ Active",
        inline=True
    )

    embed.add_field(name="\u200b", value="\u200b", inline=True)  # Empty field for formatting

    for perm_name, (action, has_perm) in permissions_check.items():
        can_use = bot.has_permission_for_action(member, action) and not is_deopped
        status = "✅" if can_use else "❌"
        embed.add_field(
            name=f"{status} {perm_name}",
            value=f"Can use bot{action} commands" if can_use else "Cannot use command",
            inline=True
        )

    await ctx.send(embed=embed)

@bot.command()
async def ban(ctx, member: discord.Member, *, reason: str = "No reason provided"):
    """Ban a user from the server"""
    if not bot.is_authorized(ctx, "ban"):
        embed = discord.Embed(
            title="❌ Access Denied",
            description="You don't have permission to ban members or you've been deopped.",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)
        return

    if member == ctx.author:
        embed = discord.Embed(
            title="❌ Invalid Target",
            description="You cannot ban yourself!",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)
        return

    if member.top_role >= ctx.author.top_role and not bot.is_admin(ctx.author):
        embed = discord.Embed(
            title="❌ Insufficient Hierarchy",
            description="You cannot ban someone with a higher or equal role!",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)
        return

    try:
        # Log the action
        action = ModerationAction(
            user_id=member.id,
            moderator_id=ctx.author.id,
            action=ActionType.BAN,
            reason=reason,
            timestamp=datetime.now(timezone.utc)
        )
        bot.log_action(action)

        # Ban the member
        await member.ban(reason=f"Banned by {ctx.author}: {reason}")

        embed = discord.Embed(
            title="🔨 User Banned",
            description=f"{member.mention} has been banned.",
            color=discord.Color.red()
        )
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        embed.timestamp = datetime.now(timezone.utc)

        await ctx.send(embed=embed)
        logger.info(f"User {member} banned by {ctx.author} for: {reason}")

    except discord.Forbidden:
        embed = discord.Embed(
            title="❌ Ban Failed",
            description="I don't have permission to ban this user.",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)
    except Exception as e:
        await bot.report_error(ctx, f"Ban command error: {str(e)}")

@bot.command()
async def kick(ctx, member: discord.Member, *, reason: str = "No reason provided"):
    """Kick a user from the server"""
    if not bot.is_authorized(ctx, "kick"):
        embed = discord.Embed(
            title="❌ Access Denied",
            description="You don't have permission to kick members or you've been deopped.",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)
        return

    if member == ctx.author:
        embed = discord.Embed(
            title="❌ Invalid Target",
            description="You cannot kick yourself!",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)
        return

    if member.top_role >= ctx.author.top_role and not bot.is_admin(ctx.author):
        embed = discord.Embed(
            title="❌ Insufficient Hierarchy",
            description="You cannot kick someone with a higher or equal role!",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)
        return

    try:
        # Log the action
        action = ModerationAction(
            user_id=member.id,
            moderator_id=ctx.author.id,
            action=ActionType.KICK,
            reason=reason,
            timestamp=datetime.now(timezone.utc)
        )
        bot.log_action(action)

        # Kick the member
        await member.kick(reason=f"Kicked by {ctx.author}: {reason}")

        embed = discord.Embed(
            title="👢 User Kicked",
            description=f"{member.mention} has been kicked.",
            color=discord.Color.orange()
        )
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        embed.timestamp = datetime.now(timezone.utc)

        await ctx.send(embed=embed)
        logger.info(f"User {member} kicked by {ctx.author} for: {reason}")

    except discord.Forbidden:
        embed = discord.Embed(
            title="❌ Kick Failed",
            description="I don't have permission to kick this user.",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)
    except Exception as e:
        await bot.report_error(ctx, f"Kick command error: {str(e)}")

@bot.command()
async def timeout(ctx, member: discord.Member, duration: str, *, reason: str = "No reason provided"):
    """Timeout a user for a specified duration"""
    if not bot.is_authorized(ctx, "timeout"):
        embed = discord.Embed(
            title="❌ Access Denied",
            description="You don't have permission to timeout members or you've been deopped.",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)
        return

    if member == ctx.author:
        embed = discord.Embed(
            title="❌ Invalid Target",
            description="You cannot timeout yourself!",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)
        return

    if member.top_role >= ctx.author.top_role and not bot.is_admin(ctx.author):
        embed = discord.Embed(
            title="❌ Insufficient Hierarchy",
            description="You cannot timeout someone with a higher or equal role!",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)
        return

    # Parse duration
    try:
        duration_minutes = parse_duration(duration)
        if duration_minutes > (28 * 24 * 60):  # 28 days max
            embed = discord.Embed(
                title="❌ Invalid Duration",
                description="Maximum timeout duration is 28 days.",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed)
            return
    except ValueError as e:
        embed = discord.Embed(
            title="❌ Invalid Duration",
            description=str(e),
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)
        return

    try:
        # Calculate timeout end time
        timeout_until = datetime.now(timezone.utc) + timedelta(minutes=duration_minutes)
        
        # Log the action
        action = ModerationAction(
            user_id=member.id,
            moderator_id=ctx.author.id,
            action=ActionType.TIMEOUT,
            reason=reason,
            duration=duration_minutes,
            timestamp=datetime.now(timezone.utc)
        )
        bot.log_action(action)

        # Timeout the member
        await member.timeout(timeout_until, reason=f"Timeout by {ctx.author}: {reason}")

        embed = discord.Embed(
            title="🤐 User Timed Out",
            description=f"{member.mention} has been timed out for {duration}.",
            color=discord.Color.orange()
        )
        embed.add_field(name="Duration", value=duration, inline=True)
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        embed.timestamp = datetime.now(timezone.utc)

        await ctx.send(embed=embed)
        logger.info(f"User {member} timed out by {ctx.author} for {duration}: {reason}")

    except discord.Forbidden:
        embed = discord.Embed(
            title="❌ Timeout Failed",
            description="I don't have permission to timeout this user.",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)
    except Exception as e:
        await bot.report_error(ctx, f"Timeout command error: {str(e)}")

def parse_duration(duration_str: str) -> int:
    """Parse duration string and return minutes"""
    # Remove spaces and make lowercase
    duration_str = duration_str.replace(" ", "").lower()
    
    # Extract number and unit
    if duration_str[-1] == 'm':
        return int(duration_str[:-1])
    elif duration_str[-1] == 'h':
        return int(duration_str[:-1]) * 60
    elif duration_str[-1] == 'd':
        return int(duration_str[:-1]) * 60 * 24
    else:
        raise ValueError("Duration must end with 'm' (minutes), 'h' (hours), or 'd' (days)")

@bot.command()
async def warn(ctx, member: discord.Member, *, reason: str = "No reason provided"):
    """Warn a user"""
    if not bot.is_authorized(ctx, "warn"):
        embed = discord.Embed(
            title="❌ Access Denied",
            description="You don't have permission to warn members or you've been deopped.",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)
        return

    if member == ctx.author:
        embed = discord.Embed(
            title="❌ Invalid Target",
            description="You cannot warn yourself!",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)
        return

    try:
        # Add warning
        warning = bot.add_warning(member.id, ctx.author.id, reason)
        
        # Get user's warning count
        user_warnings = bot.get_user_warnings(member.id)
        warning_count = len(user_warnings)

        embed = discord.Embed(
            title="⚠️ User Warned",
            description=f"{member.mention} has been warned.",
            color=discord.Color.yellow()
        )
        embed.add_field(name="Warning ID", value=f"#{warning.id}", inline=True)
        embed.add_field(name="Total Warnings", value=warning_count, inline=True)
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        embed.timestamp = datetime.now(timezone.utc)

        await ctx.send(embed=embed)
        logger.info(f"User {member} warned by {ctx.author}: {reason}")

    except Exception as e:
        await bot.report_error(ctx, f"Warn command error: {str(e)}")

@bot.command()
async def warnings(ctx, member: discord.Member = None):
    """Show warnings for a user"""
    if member is None:
        if isinstance(ctx.author, discord.Member):
            member = ctx.author
        else:
            await ctx.send("This command can only be used in a server.")
            return

    user_warnings = bot.get_user_warnings(member.id)
    
    embed = discord.Embed(
        title=f"⚠️ Warnings for {member.display_name}",
        color=discord.Color.yellow()
    )

    if not user_warnings:
        embed.description = "No warnings found."
    else:
        embed.description = f"Total active warnings: {len(user_warnings)}"
        
        for warning in user_warnings[:5]:  # Show last 5 warnings
            moderator = ctx.guild.get_member(warning.moderator_id)
            mod_name = moderator.display_name if moderator else "Unknown"
            
            embed.add_field(
                name=f"Warning #{warning.id}",
                value=f"**Reason:** {warning.reason}\n**Moderator:** {mod_name}\n**Date:** {warning.timestamp.strftime('%Y-%m-%d %H:%M UTC')}",
                inline=False
            )

    await ctx.send(embed=embed)

@bot.command()
async def stats(ctx):
    """Show bot statistics"""
    uptime = datetime.now(timezone.utc) - bot.stats["uptime_start"]
    
    embed = discord.Embed(
        title="📊 BanBot 3000 Statistics",
        color=discord.Color.blue()
    )
    
    embed.add_field(name="Uptime", value=f"{uptime.days}d {uptime.seconds//3600}h {(uptime.seconds//60)%60}m", inline=True)
    embed.add_field(name="Guilds", value=len(bot.guilds), inline=True)
    embed.add_field(name="Commands Used", value=bot.stats["commands_used"], inline=True)
    
    embed.add_field(name="Bans", value=bot.stats["bans"], inline=True)
    embed.add_field(name="Kicks", value=bot.stats["kicks"], inline=True)
    embed.add_field(name="Timeouts", value=bot.stats["timeouts"], inline=True)
    
    embed.add_field(name="Warnings", value=bot.stats["warnings"], inline=True)
    embed.add_field(name="Deops", value=bot.stats["deops"], inline=True)
    embed.add_field(name="Total Actions", value=len(bot.moderation_actions), inline=True)

    await ctx.send(embed=embed)

@bot.command()
async def deop(ctx, member: discord.Member, *, reason: str = "No reason provided"):
    """Remove admin privileges from a user (Admin only)"""
    if not bot.is_admin(ctx.author):
        embed = discord.Embed(
            title="❌ Access Denied",
            description="Only administrators can use this command.",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)
        return

    if member == ctx.author:
        embed = discord.Embed(
            title="❌ Invalid Target",
            description="You cannot deop yourself!",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)
        return

    # Add to deopped users
    deopped_user = DeoppedUser(
        user_id=member.id,
        deopped_by=ctx.author.id,
        timestamp=datetime.now(timezone.utc),
        reason=reason
    )
    bot.deopped_users[member.id] = deopped_user
    bot.stats["deops"] += 1

    embed = discord.Embed(
        title="🚫 User Deopped",
        description=f"{member.mention} has been removed from admin privileges.",
        color=discord.Color.red()
    )
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(name="Admin", value=ctx.author.mention, inline=True)
    embed.timestamp = datetime.now(timezone.utc)

    await ctx.send(embed=embed)
    logger.info(f"User {member} deopped by {ctx.author}: {reason}")

@bot.command()
async def reop(ctx, member: discord.Member):
    """Restore admin privileges to a user (Admin only)"""
    if not bot.is_admin(ctx.author):
        embed = discord.Embed(
            title="❌ Access Denied",
            description="Only administrators can use this command.",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)
        return

    if member.id not in bot.deopped_users:
        embed = discord.Embed(
            title="❌ User Not Deopped",
            description=f"{member.mention} is not currently deopped.",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)
        return

    # Remove from deopped users
    del bot.deopped_users[member.id]

    embed = discord.Embed(
        title="✅ User Reopped",
        description=f"{member.mention} has had their admin privileges restored.",
        color=discord.Color.green()
    )
    embed.add_field(name="Admin", value=ctx.author.mention, inline=True)
    embed.timestamp = datetime.now(timezone.utc)

    await ctx.send(embed=embed)
    logger.info(f"User {member} reopped by {ctx.author}")

@bot.command()
async def deopped(ctx):
    """Show currently deopped users"""
    if not bot.deopped_users:
        embed = discord.Embed(
            title="🚫 Deopped Users",
            description="No users are currently deopped.",
            color=discord.Color.green()
        )
        await ctx.send(embed=embed)
        return

    embed = discord.Embed(
        title="🚫 Deopped Users",
        color=discord.Color.red()
    )

    for user_id, deopped_user in bot.deopped_users.items():
        member = ctx.guild.get_member(user_id)
        member_name = member.display_name if member else f"User ID: {user_id}"
        
        deopped_by = ctx.guild.get_member(deopped_user.deopped_by)
        deopped_by_name = deopped_by.display_name if deopped_by else "Unknown"
        
        embed.add_field(
            name=member_name,
            value=f"**Reason:** {deopped_user.reason}\n**Deopped by:** {deopped_by_name}\n**Date:** {deopped_user.timestamp.strftime('%Y-%m-%d %H:%M UTC')}",
            inline=False
        )

    await ctx.send(embed=embed)

@bot.command()
async def cleanup(ctx, amount: int = 10):
    """Delete messages from the channel"""
    if not bot.is_authorized(ctx, "cleanup"):
        embed = discord.Embed(
            title="❌ Access Denied",
            description="You don't have permission to manage messages or you've been deopped.",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)
        return

    if amount < 1 or amount > 100:
        embed = discord.Embed(
            title="❌ Invalid Amount",
            description="Amount must be between 1 and 100.",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)
        return

    try:
        deleted = await ctx.channel.purge(limit=amount + 1)  # +1 to include the command message
        
        embed = discord.Embed(
            title="🧹 Messages Cleaned",
            description=f"Deleted {len(deleted) - 1} messages.",
            color=discord.Color.green()
        )
        
        # Send and auto-delete the confirmation
        msg = await ctx.send(embed=embed)
        await asyncio.sleep(5)
        await msg.delete()

    except discord.Forbidden:
        embed = discord.Embed(
            title="❌ Cleanup Failed",
            description="I don't have permission to delete messages.",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)
    except Exception as e:
        await bot.report_error(ctx, f"Cleanup command error: {str(e)}")

@bot.command()
async def uptime(ctx):
    """Show bot uptime"""
    uptime = datetime.now(timezone.utc) - bot.stats["uptime_start"]
    
    embed = discord.Embed(
        title="⏰ Bot Uptime",
        description=f"BanBot 3000 has been running for:\n{uptime.days} days, {uptime.seconds//3600} hours, {(uptime.seconds//60)%60} minutes",
        color=discord.Color.blue()
    )
    embed.timestamp = datetime.now(timezone.utc)
    
    await ctx.send(embed=embed)

def run_bot():
    """Run the Discord bot"""
    token = os.getenv('DISCORD_TOKEN')
    if not token:
        logger.error("No Discord token provided! Set the DISCORD_TOKEN environment variable.")
        return
    
    try:
        bot.run(token)
    except discord.LoginFailure:
        logger.error("Invalid Discord token provided!")
    except Exception as e:
        logger.error(f"Bot error: {e}")
        raise
