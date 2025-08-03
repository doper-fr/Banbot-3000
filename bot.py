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
        return (user.name.lower() in [name.lower() for name in self.config["admin_users"]] or 
                (hasattr(user, 'guild') and user == user.guild.owner))

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
        return self.has_permission_for_action(ctx.author, action)

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

            if channel:
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
        member = ctx.author

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
            value=f"Can use bot{action} commands" if can_use else "Cannot use",
            inline=True
        )

    await ctx.send(embed=embed)

@bot.command()
async def uptime(ctx):
    """Show bot uptime"""
    now = datetime.now(timezone.utc)
    uptime = now - bot.stats["uptime_start"]

    days = uptime.days
    hours, remainder = divmod(uptime.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    embed = discord.Embed(
        title="⏰ Bot Uptime",
        description=f"**Uptime:** {days}d {hours}h {minutes}m {seconds}s",
        color=discord.Color.blue(),
        timestamp=now
    )
    await ctx.send(embed=embed)

@bot.command()
async def memory(ctx):
    """Show memory usage statistics"""
    embed = discord.Embed(
        title="🧠 Memory Usage Statistics",
        color=discord.Color.purple(),
        timestamp=datetime.now(timezone.utc)
    )

    embed.add_field(name="📋 Moderation Actions", value=len(bot.moderation_actions), inline=True)
    embed.add_field(name="⚠️ Warnings", value=len(bot.warnings), inline=True)
    embed.add_field(name="🚫 Deopped Users", value=len(bot.deopped_users), inline=True)
    embed.add_field(name="💬 Processed Messages", value=len(bot.processed_messages), inline=True)
    embed.add_field(name="👥 Tracked Users", value=len(bot.user_message_times), inline=True)
    embed.add_field(name="🆔 Next Warning ID", value=bot.next_warning_id - 1, inline=True)

    await ctx.send(embed=embed)

@bot.command()
async def stats(ctx):
    """Show bot statistics"""
    embed = discord.Embed(
        title="📊 BanBot 3000 Statistics",
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc)
    )

    embed.add_field(name="🔨 Bans", value=bot.stats.get("bans", 0), inline=True)
    embed.add_field(name="👢 Kicks", value=bot.stats.get("kicks", 0), inline=True)
    embed.add_field(name="⏰ Timeouts", value=bot.stats.get("timeouts", 0), inline=True)
    embed.add_field(name="⚠️ Warnings", value=bot.stats.get("warnings", 0), inline=True)
    embed.add_field(name="🚫 Deops", value=bot.stats.get("deops", 0), inline=True)
    embed.add_field(name="💬 Commands Used", value=bot.stats.get("commands_used", 0), inline=True)

    # Calculate uptime
    uptime = datetime.now(timezone.utc) - bot.stats["uptime_start"]
    uptime_str = f"{uptime.days}d {uptime.seconds//3600}h {(uptime.seconds//60)%60}m"
    embed.add_field(name="⏰ Uptime", value=uptime_str, inline=True)

    await ctx.send(embed=embed)

@bot.command()
async def ban(ctx, member: discord.Member, *, reason="No reason provided"):
    """Ban a user with enhanced logging"""
    if not bot.is_authorized(ctx, "ban"):
        return await ctx.send("❌ You don't have permission to ban members.")

    try:
        # Check bot permissions
        perms = await bot.check_permissions(ctx.guild)
        if not perms.get("ban_members", False):
            return await ctx.send("❌ I don't have permission to ban members!")

        # Log the action
        action = ModerationAction(
            user_id=member.id,
            moderator_id=ctx.author.id,
            action=ActionType.BAN,
            reason=reason,
            timestamp=datetime.now(timezone.utc)
        )
        bot.log_action(action)

        # Send DM to user before banning
        try:
            embed = discord.Embed(
                title="🔨 You have been banned",
                description=f"**Server:** {ctx.guild.name}\n**Reason:** {reason}\n**Moderator:** {ctx.author.mention}",
                color=discord.Color.red()
            )
            await member.send(embed=embed)
        except:
            pass  # User has DMs disabled

        await member.ban(reason=f"{reason} | Banned by {ctx.author}")

        embed = discord.Embed(
            title="🔨 User Banned",
            description=f"**User:** {member.mention} ({member.id})\n**Reason:** {reason}\n**Moderator:** {ctx.author.mention}",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc)
        )
        await ctx.send(embed=embed)

    except discord.Forbidden:
        await ctx.send("❌ I don't have permission to ban this user.")
    except Exception as e:
        await bot.report_error(ctx, traceback.format_exc())
        await ctx.send("❌ An error occurred while banning the user.")

@bot.command()
async def kick(ctx, member: discord.Member, *, reason="No reason provided"):
    """Kick a user with enhanced logging"""
    if not bot.is_authorized(ctx, "kick"):
        return await ctx.send("❌ You don't have permission to kick members.")

    try:
        # Check bot permissions
        perms = await bot.check_permissions(ctx.guild)
        if not perms.get("kick_members", False):
            return await ctx.send("❌ I don't have permission to kick members!")

        action = ModerationAction(
            user_id=member.id,
            moderator_id=ctx.author.id,
            action=ActionType.KICK,
            reason=reason,
            timestamp=datetime.now(timezone.utc)
        )
        bot.log_action(action)

        await member.kick(reason=f"{reason} | Kicked by {ctx.author}")

        embed = discord.Embed(
            title="👢 User Kicked",
            description=f"**User:** {member.mention} ({member.id})\n**Reason:** {reason}\n**Moderator:** {ctx.author.mention}",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc)
        )
        await ctx.send(embed=embed)

    except discord.Forbidden:
        await ctx.send("❌ I don't have permission to kick this user.")
    except Exception as e:
        await bot.report_error(ctx, traceback.format_exc())

@bot.command()
async def timeout(ctx, member: discord.Member, duration: str, *, reason="No reason provided"):
    """Timeout a user with proper command structure"""
    if not bot.is_authorized(ctx, "timeout"):
        return await ctx.send("❌ You don't have permission to timeout members.")

    try:
        # Check bot permissions
        perms = await bot.check_permissions(ctx.guild)
        if not perms.get("moderate_members", False):
            return await ctx.send("❌ I don't have permission to timeout members! Missing 'Timeout Members' permission.")

        # Parse duration (e.g., "10m", "2h", "1d")
        match = re.match(r"(\d+)([mhd])", duration.lower())
        if not match:
            return await ctx.send("❌ Invalid duration format! Use format like: 10m, 2h, 1d")

        value = int(match.group(1))
        unit = match.group(2)

        if value <= 0:
            return await ctx.send("⚠️ Timeout duration must be positive.")

        now = datetime.now(timezone.utc)
        if unit == "m":
            until = now + timedelta(minutes=value)
            duration_minutes = value
        elif unit == "h":
            until = now + timedelta(hours=value)
            duration_minutes = value * 60
        elif unit == "d":
            if value > bot.config["max_timeout_days"]:
                return await ctx.send(f"❌ Max timeout is {bot.config['max_timeout_days']} days.")
            until = now + timedelta(days=value)
            duration_minutes = value * 24 * 60

        # Log action
        action = ModerationAction(
            user_id=member.id,
            moderator_id=ctx.author.id,
            action=ActionType.TIMEOUT,
            reason=reason,
            timestamp=now,
            duration=duration_minutes
        )
        bot.log_action(action)

        await member.timeout(until, reason=f"{reason} | Timed out by {ctx.author.name}")

        embed = discord.Embed(
            title="⏰ User Timed Out",
            description=f"**User:** {member.mention}\n**Duration:** {value}{unit}\n**Until:** <t:{int(until.timestamp())}:F>\n**Reason:** {reason}\n**Moderator:** {ctx.author.mention}",
            color=discord.Color.orange(),
            timestamp=now
        )
        await ctx.send(embed=embed)

    except discord.Forbidden:
        await ctx.send("❌ I don't have permission to timeout this user. Check bot permissions!")
    except Exception as e:
        await ctx.send("❌ Failed to timeout user.")
        await bot.report_error(ctx, traceback.format_exc())

# Fixed timeout command using on_message event (legacy support)
@bot.event
async def on_message(message):
    if message.author.bot:
        return

    # Handle timeout command with regex (legacy support)
    raw = message.content.strip()
    match = re.match(r"bottimeout\((\d+)([mhd])\)", raw)

    if match:
        ctx = await bot.get_context(message)
        if not bot.is_authorized(ctx, "timeout"):
            return await message.channel.send("❌ You don't have permission to timeout members.")

        try:
            # Check bot permissions first
            perms = await bot.check_permissions(ctx.guild)
            if not perms.get("moderate_members", False):
                return await message.channel.send("❌ I don't have permission to timeout members! Missing 'Timeout Members' permission.")

            value = int(match.group(1))
            unit = match.group(2)

            if value <= 0:
                return await message.channel.send("⚠️ Timeout duration must be positive.")
            if not message.mentions:
                return await message.channel.send("❌ You must mention a user to timeout.")

            member = message.mentions[0]

            now = datetime.now(timezone.utc)
            if unit == "m":
                until = now + timedelta(minutes=value)
                duration_minutes = value
            elif unit == "h":
                until = now + timedelta(hours=value)
                duration_minutes = value * 60
            elif unit == "d":
                if value > bot.config["max_timeout_days"]:
                    return await message.channel.send(f"❌ Max timeout is {bot.config['max_timeout_days']} days.")
                until = now + timedelta(days=value)
                duration_minutes = value * 24 * 60

            # Log action
            action = ModerationAction(
                user_id=member.id,
                moderator_id=message.author.id,
                action=ActionType.TIMEOUT,
                reason=f"Timed out for {value}{unit}",
                timestamp=now,
                duration=duration_minutes
            )
            bot.log_action(action)

            await member.timeout(until, reason=f"Timed out by {message.author.name}")

            embed = discord.Embed(
                title="⏰ User Timed Out",
                description=f"**User:** {member.mention}\n**Duration:** {value}{unit}\n**Until:** <t:{int(until.timestamp())}:F>\n**Moderator:** {message.author.mention}",
                color=discord.Color.orange(),
                timestamp=now
            )
            await message.channel.send(embed=embed)

        except discord.Forbidden:
            await message.channel.send("❌ I don't have permission to timeout this user. Check bot permissions!")
        except Exception as e:
            await message.channel.send("❌ Failed to timeout user.")
            logger.error(f"Timeout error: {traceback.format_exc()}")

    # Process other commands
    await bot.process_commands(message)

@bot.command()
async def warn(ctx, member: discord.Member, *, reason="No reason provided"):
    """Issue a warning to a user"""
    if not bot.is_authorized(ctx, "warn"):
        return await ctx.send("❌ You don't have permission to warn members.")

    try:
        # Add warning to memory
        warning = bot.add_warning(member.id, ctx.author.id, reason)

        # Log as moderation action
        action = ModerationAction(
            user_id=member.id,
            moderator_id=ctx.author.id,
            action=ActionType.WARN,
            reason=reason,
            timestamp=datetime.now(timezone.utc)
        )
        bot.log_action(action)

        embed = discord.Embed(
            title="⚠️ User Warned",
            description=f"**User:** {member.mention}\n**Warning ID:** #{warning.id}\n**Reason:** {reason}\n**Moderator:** {ctx.author.mention}",
            color=discord.Color.yellow(),
            timestamp=datetime.now(timezone.utc)
        )
        await ctx.send(embed=embed)

        # Send DM to user
        try:
            dm_embed = discord.Embed(
                title="⚠️ Warning Received",
                description=f"**Server:** {ctx.guild.name}\n**Warning ID:** #{warning.id}\n**Reason:** {reason}\n**Moderator:** {ctx.author.mention}",
                color=discord.Color.yellow()
            )
            await member.send(embed=dm_embed)
        except:
            pass

    except Exception as e:
        await bot.report_error(ctx, traceback.format_exc())

@bot.command()
async def warnings(ctx, member: discord.Member):
    """Show warnings for a user"""
    warnings = bot.get_user_warnings(member.id, 10)

    if not warnings:
        return await ctx.send(f"😇 {member.mention} has no warnings.")

    embed = discord.Embed(
        title=f"⚠️ Warnings for {member.display_name}",
        description=f"Total active warnings: {len(warnings)}",
        color=discord.Color.yellow()
    )

    for warning in warnings[:5]:  # Show first 5
        moderator = bot.get_user(warning.moderator_id)
        mod_name = moderator.name if moderator else f"ID: {warning.moderator_id}"
        embed.add_field(
            name=f"Warning #{warning.id}",
            value=f"**Reason:** {warning.reason}\n**By:** {mod_name}\n**Date:** {warning.timestamp.strftime('%Y-%m-%d %H:%M')}",
            inline=False
        )

    if len(warnings) > 5:
        embed.set_footer(text=f"Showing 5 of {len(warnings)} warnings")

    await ctx.send(embed=embed)

@bot.command()
async def history(ctx, member: discord.Member):
    """Show moderation history for a user"""
    actions = bot.get_user_actions(member.id, 10)

    if not actions:
        return await ctx.send(f"😇 {member.mention} has no moderation history.")

    embed = discord.Embed(
        title=f"📋 Moderation History for {member.display_name}",
        description=f"Total actions: {len(actions)}",
        color=discord.Color.blue()
    )

    for action in actions[:5]:  # Show first 5
        moderator = bot.get_user(action.moderator_id)
        mod_name = moderator.name if moderator else f"ID: {action.moderator_id}"

        duration_text = ""
        if action.duration:
            duration_text = f"\n**Duration:** {action.duration} minutes"

        embed.add_field(
            name=f"{action.action.value.title()} - {action.timestamp.strftime('%Y-%m-%d %H:%M')}",
            value=f"**Reason:** {action.reason}\n**By:** {mod_name}{duration_text}",
            inline=False
        )

    if len(actions) > 5:
        embed.set_footer(text=f"Showing 5 of {len(actions)} actions")

    await ctx.send(embed=embed)

@bot.command()
async def deop(ctx, user: discord.Member, *, reason="No reason provided"):
    """Deop a user with reason (Admin only)"""
    if not bot.is_admin(ctx.author):
        return await ctx.send("🚫 Only admins can deop users.")

    if bot.is_deopped(user.id):
        return await ctx.send(f"⚠️ {user.mention} is already deopped.")

    # Add to deopped users
    deop_data = DeoppedUser(
        user_id=user.id,
        deopped_by=ctx.author.id,
        timestamp=datetime.now(timezone.utc),
        reason=reason
    )
    bot.deopped_users[user.id] = deop_data
    bot.stats["deops"] += 1

    embed = discord.Embed(
        title="👎 User Deopped",
        description=f"**User:** {user.mention}\n**Reason:** {reason}\n**By:** {ctx.author.mention}",
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc)
    )
    await ctx.send(embed=embed)

@bot.command()
async def reop(ctx, user: discord.Member):
    """Reop a user (Admin only)"""
    if not bot.is_admin(ctx.author):
        return await ctx.send("🚫 Only admins can reop users.")

    if not bot.is_deopped(user.id):
        return await ctx.send(f"🤷 {user.mention} wasn't deopped.")

    # Remove from deopped users
    del bot.deopped_users[user.id]

    embed = discord.Embed(
        title="👍 User Reopped",
        description=f"**User:** {user.mention} has been forgiven and reopped.\n**By:** {ctx.author.mention}",
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc)
    )
    await ctx.send(embed=embed)

@bot.command(aliases=["deopped"])
async def show_deopped(ctx):
    """Show all deopped users"""
    if not bot.deopped_users:
        return await ctx.send("😇 Nobody is deopped. Peace reigns.")

    embed = discord.Embed(
        title="🚫 Deopped Users",
        description=f"Total deopped users: {len(bot.deopped_users)}",
        color=discord.Color.red()
    )

    count = 0
    for user_id, deop_data in bot.deopped_users.items():
        if count >= 10:  # Limit to 10
            break

        user = bot.get_user(user_id)
        username = user.name if user else f"Unknown (ID: {user_id})"
        embed.add_field(
            name=username,
            value=f"**Reason:** {deop_data.reason}\n**Date:** {deop_data.timestamp.strftime('%Y-%m-%d')}",
            inline=True
        )
        count += 1

    if len(bot.deopped_users) > 10:
        embed.set_footer(text=f"Showing 10 of {len(bot.deopped_users)} deopped users")

    await ctx.send(embed=embed)

@bot.command()
async def cleanup(ctx, amount: int = 10):
    """Clean up messages"""
    if not bot.is_authorized(ctx, "cleanup"):
        return await ctx.send("❌ You don't have permission to manage messages.")

    if amount > 100:
        return await ctx.send("❌ Cannot delete more than 100 messages at once.")

    try:
        # Check bot permissions
        perms = await bot.check_permissions(ctx.guild)
        if not perms.get("manage_messages", False):
            return await ctx.send("❌ I don't have permission to manage messages!")

        deleted = await ctx.channel.purge(limit=amount + 1)  # +1 for the command message
        embed = discord.Embed(
            title="🧹 Messages Cleaned",
            description=f"Deleted {len(deleted) - 1} messages.",
            color=discord.Color.green()
        )
        msg = await ctx.send(embed=embed)
        await asyncio.sleep(3)
        await msg.delete()
    except discord.Forbidden:
        await ctx.send("❌ I don't have permission to delete messages.")
    except Exception as e:
        await bot.report_error(ctx, traceback.format_exc())

@bot.command()
async def clear_memory(ctx):
    """Clear all stored data (Admin only)"""
    if not bot.is_admin(ctx.author):
        return await ctx.send("❌ Only admins can clear memory.")

    # Clear all data
    bot.moderation_actions.clear()
    bot.warnings.clear()
    bot.deopped_users.clear()
    bot.processed_messages.clear()
    bot.user_message_times.clear()

    # Reset counters
    bot.next_warning_id = 1
    bot.next_action_id = 1

    # Reset stats (keep uptime_start)
    uptime_start = bot.stats["uptime_start"]
    bot.stats = {
        "bans": 0,
        "kicks": 0,
        "timeouts": 0,
        "warnings": 0,
        "deops": 0,
        "commands_used": 0,
        "uptime_start": uptime_start
    }

    embed = discord.Embed(
        title="🧠 Memory Cleared",
        description="All stored data has been cleared from memory.",
        color=discord.Color.green()
    )
    await ctx.send(embed=embed)

# --- EVENT HANDLERS ---

@bot.event
async def on_ready():
    logger.info(f"✅ BanBot 3000 (RAM Edition) is online as {bot.user}")
    logger.info(f"Connected to {len(bot.guilds)} servers")

    # Check permissions on startup
    for guild in bot.guilds:
        perms = await bot.check_permissions(guild)
        missing_perms = [perm for perm, has_perm in perms.items() if not has_perm and perm in ["ban_members", "kick_members", "moderate_members"]]
        if missing_perms:
            logger.warning(f"Missing critical permissions in {guild.name}: {', '.join(missing_perms)}")

    # Start cleanup task after bot is ready
    if not bot.cleanup_task.is_running():
        bot.cleanup_task.start()

    # Set status
    activity = discord.Activity(type=discord.ActivityType.watching, name="bothelp")
    await bot.change_presence(status=discord.Status.online, activity=activity)

@bot.event
async def on_command_error(ctx, error):
    """Handle command errors gracefully"""
    if isinstance(error, commands.CommandNotFound):
        # Ignore command not found errors to reduce spam
        return
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing required argument: {error.param.name}")
    elif isinstance(error, commands.BadArgument):
        await ctx.send(f"❌ Invalid argument: {error}")
    elif isinstance(error, commands.MemberNotFound):
        await ctx.send("❌ Member not found. Please mention a valid member.")
    elif isinstance(error, discord.Forbidden):
        await ctx.send("❌ I don't have permission to perform this action.")
    else:
        # Log unexpected errors
        logger.error(f"Unexpected error in command {ctx.command}: {error}")
        await bot.report_error(ctx, traceback.format_exc())
        await ctx.send("❌ An unexpected error occurred.")

def run_bot():
    """Run the Discord bot with error handling and auto-restart."""
    max_retries = 5
    retry_count = 0
    
    # Get Discord token from environment
    discord_token = os.getenv('DISCORD_TOKEN', 'your_discord_token_here')
    
    while retry_count < max_retries:
        try:
            logger.info(f"Starting BanBot 3000 (attempt {retry_count + 1}/{max_retries})")
            
            if not discord_token or discord_token == 'your_discord_token_here':
                logger.error("DISCORD_TOKEN not found in environment variables!")
                logger.error("Please set your Discord bot token in the Replit secrets tab.")
                break
            
            bot.run(discord_token, log_handler=None)
            
        except discord.LoginFailure:
            logger.error("Invalid Discord token! Please check your DISCORD_TOKEN environment variable.")
            break
            
        except discord.ConnectionClosed:
            logger.warning("Connection closed by Discord. Attempting to reconnect...")
            retry_count += 1
            if retry_count < max_retries:
                logger.info(f"Retrying in 10 seconds... ({retry_count}/{max_retries})")
                time.sleep(10)
            
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            retry_count += 1
            if retry_count < max_retries:
                logger.info(f"Retrying in 30 seconds... ({retry_count}/{max_retries})")
                time.sleep(30)
    
    logger.error("Max retries reached. Bot shutting down.")
