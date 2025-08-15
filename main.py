import discord
from discord.ext import commands, tasks
import asyncio
import aiohttp
from aiohttp import web
import os
import time
import logging
import signal
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, List, Any
from dataclasses import dataclass, asdict
from enum import Enum
from collections import defaultdict, deque
import json
import inspect

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger('BanBot3000-HA')

class BotRole(Enum):
    PRIMARY = "primary"
    SECONDARY = "secondary"

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
    duration: Optional[int] = None
    
    def to_dict(self):
        data = asdict(self)
        data['timestamp'] = self.timestamp.isoformat()
        data['action'] = self.action.value
        return data

@dataclass
class Warning:
    id: int
    user_id: int
    moderator_id: int
    reason: str
    timestamp: datetime
    
    def to_dict(self):
        data = asdict(self)
        data['timestamp'] = self.timestamp.isoformat()
        return data

@dataclass
class DeoppedUser:
    user_id: int
    deopped_by: int
    timestamp: datetime
    reason: str
    
    def to_dict(self):
        data = asdict(self)
        data['timestamp'] = self.timestamp.isoformat()
        return data

@dataclass
class CustomCommand:
    name: str
    description: str
    response: str
    created_by: int
    created_at: datetime
    usage_count: int = 0
    
    def to_dict(self):
        data = asdict(self)
        data['created_at'] = self.created_at.isoformat()
        return data

class HealthMonitor:
    def __init__(self, instance):
        self.instance = instance
        self.peer_url = None
        self.last_peer_heartbeat = datetime.now(timezone.utc)
        self.session = None
        
    async def initialize(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))
        
    async def cleanup(self):
        if self.session:
            await self.session.close()
    
    async def check_peer_health(self) -> bool:
        if not self.peer_url or not self.session:
            return False
        try:
            async with self.session.get(f"{self.peer_url}/health") as resp:
                if resp.status == 200:
                    health_data = await resp.json()
                    self.last_peer_heartbeat = datetime.fromisoformat(health_data['last_heartbeat'])
                    return health_data.get('is_active', False)
        except:
            pass
        return False
    
    async def sync_data(self):
        if not self.peer_url or not self.session:
            return
        try:
            sync_data = {
                'actions': [a.to_dict() for a in self.instance.moderation_actions[-100:]],
                'warnings': [w.to_dict() for w in self.instance.warnings[-50:]],
                'deopped': {str(k): v.to_dict() for k, v in self.instance.deopped_users.items()},
                'custom_commands': {k: v.to_dict() for k, v in self.instance.custom_commands.items()},
                'stats': self.instance.stats.copy(),
                'timestamp': datetime.now(timezone.utc).isoformat()
            }
            async with self.session.post(f"{self.peer_url}/sync", json=sync_data) as resp:
                if resp.status == 200:
                    logger.debug("Sync successful")
        except:
            pass
    
    def should_takeover(self) -> bool:
        if self.instance.role == BotRole.PRIMARY:
            return False
        return (datetime.now(timezone.utc) - self.last_peer_heartbeat).total_seconds() > 60

class HTTPServer:
    def __init__(self, bot_instance, port):
        self.bot = bot_instance
        self.port = port
        self.app = web.Application()
        self.runner = None
        self.site = None
        
        # Add CORS headers
        async def cors_handler(request, handler):
            response = await handler(request)
            response.headers['Access-Control-Allow-Origin'] = '*'
            response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
            response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
            return response
        
        self.app.middlewares.append(cors_handler)
        
        self.app.router.add_get('/', self.home)
        self.app.router.add_get('/health', self.health_check)
        self.app.router.add_get('/ping', self.ping_handler)
        self.app.router.add_post('/sync', self.sync_data)
        self.app.router.add_get('/stats', self.get_stats)
        self.app.router.add_get('/commands', self.get_custom_commands)
        
    async def home(self, request):
        return web.json_response({
            'message': 'BanBot 3000 HA is alive!',
            'role': self.bot.role.value,
            'active': self.bot.is_active_instance,
            'uptime': (datetime.now(timezone.utc) - self.bot.stats["start_time"]).total_seconds()
        })
        
    async def health_check(self, request):
        return web.json_response({
            'role': self.bot.role.value,
            'is_active': self.bot.is_active_instance,
            'discord_connected': self.bot.is_ready(),
            'last_heartbeat': datetime.now(timezone.utc).isoformat(),
            'uptime': (datetime.now(timezone.utc) - self.bot.stats["start_time"]).total_seconds(),
            'guilds': len(self.bot.guilds),
            'custom_commands': len(self.bot.custom_commands)
        })
    
    async def ping_handler(self, request):
        return web.json_response({
            'status': 'pong',
            'role': self.bot.role.value,
            'active': self.bot.is_active_instance,
            'latency': round(self.bot.latency * 1000) if self.bot.is_ready() else None
        })
    
    async def get_stats(self, request):
        return web.json_response({
            'stats': self.bot.stats,
            'custom_commands': len(self.bot.custom_commands),
            'total_actions': len(self.bot.moderation_actions),
            'role': self.bot.role.value
        })
    
    async def get_custom_commands(self, request):
        commands_data = {}
        for name, cmd in self.bot.custom_commands.items():
            commands_data[name] = {
                'description': cmd.description,
                'usage_count': cmd.usage_count,
                'created_by': cmd.created_by,
                'created_at': cmd.created_at.isoformat()
            }
        return web.json_response(commands_data)
    
    async def sync_data(self, request):
        if self.bot.is_active_instance:
            return web.json_response({'status': 'active_skip'})
        try:
            data = await request.json()
            # Sync custom commands
            if 'custom_commands' in data:
                for name, cmd_data in data['custom_commands'].items():
                    if name not in self.bot.custom_commands:
                        cmd = CustomCommand(
                            name=cmd_data['name'],
                            description=cmd_data['description'],
                            response=cmd_data['response'],
                            created_by=cmd_data['created_by'],
                            created_at=datetime.fromisoformat(cmd_data['created_at']),
                            usage_count=cmd_data.get('usage_count', 0)
                        )
                        self.bot.custom_commands[name] = cmd
                        self.bot.add_dynamic_command(cmd)
            
            self.bot.stats.update(data.get('stats', {}))
            return web.json_response({'status': 'success'})
        except Exception as e:
            logger.error(f"Sync error: {e}")
            return web.json_response({'status': 'error'}, status=500)
    
    async def start(self):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, '0.0.0.0', self.port)
        await self.site.start()
        logger.info(f"HTTP server running on port {self.port}")
    
    async def stop(self):
        if self.site:
            await self.site.stop()
        if self.runner:
            await self.runner.cleanup()

def get_port():
    return int(os.getenv('PORT', 5000))

def determine_role():
    # Railway deployment is always primary
    if os.getenv('RAILWAY_ENVIRONMENT'):
        return BotRole.PRIMARY, get_port(), None
    else:
        # Local development
        railway_url = os.getenv('RAILWAY_URL', 'https://your-app.railway.app')
        return BotRole.SECONDARY, 5001, railway_url

class BanBot3000HA(commands.Bot):
    def __init__(self, role: BotRole, port: int, peer_url: Optional[str] = None):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.moderation = True

        super().__init__(command_prefix="bot ", intents=intents, help_command=None)
        
        self.role = role
        self.port = port
        self.peer_url = peer_url
        self.is_active_instance = (role == BotRole.PRIMARY)
        
        self.health_monitor = HealthMonitor(self)
        self.http_server = HTTPServer(self, port)
        
        if peer_url:
            self.health_monitor.peer_url = peer_url

        self.config = {
            "admin_users": ["doper_official"],  # Add your Discord username here
            "max_timeout_days": 28
        }

        # Data storage
        self.moderation_actions: List[ModerationAction] = []
        self.deopped_users: Dict[int, DeoppedUser] = {}
        self.warnings: List[Warning] = []
        self.custom_commands: Dict[str, CustomCommand] = {}
        self.command_history: deque = deque(maxlen=100)

        self.next_warning_id = 1
        self.stats = {
            "bans": 0, "kicks": 0, "timeouts": 0, "warnings": 0, "deops": 0,
            "commands_used": 0, "custom_commands_used": 0,
            "start_time": datetime.now(timezone.utc)
        }

    def is_admin(self, user) -> bool:
        return (user.name.lower() in [n.lower() for n in self.config["admin_users"]] or
                (isinstance(user, discord.Member) and user == user.guild.owner))

    def is_deopped(self, user_id: int) -> bool:
        return user_id in self.deopped_users

    def has_permission(self, member: discord.Member, action: str) -> bool:
        if self.is_admin(member):
            return True
        perms = member.guild_permissions
        return {
            "ban": perms.ban_members,
            "kick": perms.kick_members, 
            "timeout": perms.moderate_members,
            "warn": True,
            "cleanup": perms.manage_messages
        }.get(action, False)

    def is_authorized(self, ctx, action="general") -> bool:
        return (self.is_active_instance and not self.is_deopped(ctx.author.id) and
                isinstance(ctx.author, discord.Member) and 
                self.has_permission(ctx.author, action))

    def log_action(self, action: ModerationAction):
        action.timestamp = datetime.now(timezone.utc)
        self.moderation_actions.append(action)
        self.stats[f"{action.action.value}s"] = self.stats.get(f"{action.action.value}s", 0) + 1
        
        # Keep last 500 actions
        if len(self.moderation_actions) > 500:
            self.moderation_actions = self.moderation_actions[-250:]

    def add_warning(self, user_id: int, moderator_id: int, reason: str) -> Warning:
        warning = Warning(self.next_warning_id, user_id, moderator_id, reason, datetime.now(timezone.utc))
        self.warnings.append(warning)
        self.next_warning_id += 1
        self.stats["warnings"] += 1
        return warning

    def get_user_warnings(self, user_id: int) -> List[Warning]:
        return [w for w in self.warnings if w.user_id == user_id][-10:]

    def add_dynamic_command(self, custom_cmd: CustomCommand):
        """Add a custom command dynamically"""
        async def dynamic_command_func(ctx, *args):
            if not self.is_active_instance:
                return
            
            # Update usage count
            custom_cmd.usage_count += 1
            self.stats["custom_commands_used"] += 1
            
            # Replace placeholders in response
            response = custom_cmd.response
            response = response.replace("{user}", ctx.author.display_name)
            response = response.replace("{guild}", ctx.guild.name if ctx.guild else "DM")
            response = response.replace("{args}", " ".join(args) if args else "")
            
            embed = discord.Embed(
                title=f"📝 {custom_cmd.name.title()}",
                description=response,
                color=discord.Color.blue()
            )
            embed.set_footer(text=f"Custom command • Used {custom_cmd.usage_count} times")
            await ctx.send(embed=embed)

        # Create the command
        command = commands.Command(
            dynamic_command_func,
            name=custom_cmd.name,
            help=custom_cmd.description
        )
        
        # Add to bot
        self.add_command(command)
        logger.info(f"Added dynamic command: {custom_cmd.name}")

    async def become_active(self):
        if self.is_active_instance:
            return
        logger.info(f"🔄 {self.role.value.upper()} becoming ACTIVE")
        self.is_active_instance = True
        await self.change_presence(
            activity=discord.Activity(type=discord.ActivityType.watching, 
                                    name=f"for violations | {self.role.value} ACTIVE"),
            status=discord.Status.online
        )

    async def become_standby(self):
        if not self.is_active_instance:
            return
        logger.info(f"⏸️ {self.role.value.upper()} entering STANDBY")
        self.is_active_instance = False
        await self.change_presence(
            activity=discord.Activity(type=discord.ActivityType.watching, 
                                    name=f"in standby | {self.role.value} BACKUP"),
            status=discord.Status.idle
        )

    @tasks.loop(seconds=30)
    async def health_loop(self):
        try:
            if self.role == BotRole.SECONDARY and self.health_monitor.should_takeover():
                await self.become_active()
            await self.health_monitor.sync_data()
            await self.health_monitor.check_peer_health()
        except Exception as e:
            logger.error(f"Health check error: {e}")

    async def on_ready(self):
        logger.info(f'🚀 {self.user} ready! Role: {self.role.value.upper()}, Active: {self.is_active_instance}')
        
        await self.health_monitor.initialize()
        
        if not self.health_loop.is_running():
            self.health_loop.start()

        status_text = f"for violations | {self.role.value} {'ACTIVE' if self.is_active_instance else 'BACKUP'}"
        await self.change_presence(
            activity=discord.Activity(type=discord.ActivityType.watching, name=status_text),
            status=discord.Status.online if self.is_active_instance else discord.Status.idle
        )

    async def on_command_error(self, ctx, error):
        if not self.is_active_instance:
            return
        
        self.stats["commands_used"] += 1
        
        if isinstance(error, commands.CommandNotFound):
            return
        elif isinstance(error, (commands.MissingPermissions, commands.BotMissingPermissions)):
            await ctx.send(embed=discord.Embed(title="❌ Missing Permissions", 
                                             description=str(error), color=discord.Color.red()))
        elif isinstance(error, commands.MemberNotFound):
            await ctx.send(embed=discord.Embed(title="❌ Member Not Found", 
                                             description="Could not find that member.", color=discord.Color.red()))
        else:
            logger.error(f"Command error: {error}")

    async def process_commands(self, message):
        if self.is_active_instance:
            await super().process_commands(message)

    async def close(self):
        logger.info(f"🔄 Shutting down {self.role.value}")
        if self.health_loop.is_running():
            self.health_loop.stop()
        await self.health_monitor.cleanup()
        await self.http_server.stop()
        await super().close()

# Create bot instance
role, port, peer_url = determine_role()
bot = BanBot3000HA(role, port, peer_url)

# --- COMMANDS ---

@bot.command(name="help", aliases=["h"])
async def bothelp(ctx):
    """Display help information"""
    if not bot.is_active_instance:
        return
    
    embed = discord.Embed(
        title="🤖 BanBot 3000 HA Commands",
        description=f"High Availability Discord Moderation Bot\n**Instance: {bot.role.value.upper()} {'ACTIVE' if bot.is_active_instance else 'STANDBY'}**",
        color=discord.Color.blue()
    )

    embed.add_field(
        name="👑 Moderation",
        value="`ban @user [reason]` - Ban user\n`kick @user [reason]` - Kick user\n`timeout @user <time> [reason]` - Timeout\n`warn @user [reason]` - Warn user\n`deop @user [reason]` - Remove admin\n`reop @user` - Restore admin",
        inline=False
    )

    embed.add_field(
        name="📊 Info & Utility",
        value="`stats` - Bot statistics\n`warnings @user` - User warnings\n`hastatus` - HA status\n`ping` - Response test\n`cleanup [amount]` - Delete messages",
        inline=False
    )

    embed.add_field(
        name="🔧 Custom Commands",
        value="`addcmd <name> <description> <response>` - Add custom command\n`delcmd <name>` - Delete custom command\n`listcmds` - List custom commands",
        inline=False
    )

    embed.add_field(
        name="🏥 High Availability",
        value="• Dual-instance failover\n• Auto-sync between instances\n• Railway.com optimized\n• HTTP health endpoints",
        inline=False
    )

    await ctx.send(embed=embed)

def parse_duration(duration_str: str) -> int:
    """Parse duration string and return minutes"""
    duration_str = duration_str.replace(" ", "").lower()
    if duration_str.endswith('m'):
        return int(duration_str[:-1])
    elif duration_str.endswith('h'):
        return int(duration_str[:-1]) * 60
    elif duration_str.endswith('d'):
        return int(duration_str[:-1]) * 1440
    else:
        raise ValueError("Use format: 1m, 1h, 1d")

@bot.command()
async def addcmd(ctx, name: str, description: str, *, response: str):
    """Add a custom command (Admin only)"""
    if not bot.is_active_instance or not bot.is_admin(ctx.author):
        return await ctx.send(embed=discord.Embed(title="❌ Admin Only", color=discord.Color.red()))
    
    name = name.lower()
    
    # Check if command already exists
    if name in [cmd.name for cmd in bot.commands] or name in bot.custom_commands:
        return await ctx.send(embed=discord.Embed(title="❌ Command already exists", color=discord.Color.red()))
    
    # Create custom command
    custom_cmd = CustomCommand(
        name=name,
        description=description,
        response=response,
        created_by=ctx.author.id,
        created_at=datetime.now(timezone.utc)
    )
    
    # Add to storage and bot
    bot.custom_commands[name] = custom_cmd
    bot.add_dynamic_command(custom_cmd)
    
    embed = discord.Embed(title="✅ Custom Command Added", color=discord.Color.green())
    embed.add_field(name="Name", value=name, inline=True)
    embed.add_field(name="Description", value=description, inline=True)
    embed.add_field(name="Response Preview", value=response[:100] + "..." if len(response) > 100 else response, inline=False)
    embed.set_footer(text="Use placeholders: {user}, {guild}, {args}")
    
    await ctx.send(embed=embed)

@bot.command()
async def delcmd(ctx, name: str):
    """Delete a custom command (Admin only)"""
    if not bot.is_active_instance or not bot.is_admin(ctx.author):
        return await ctx.send(embed=discord.Embed(title="❌ Admin Only", color=discord.Color.red()))
    
    name = name.lower()
    
    if name not in bot.custom_commands:
        return await ctx.send(embed=discord.Embed(title="❌ Command not found", color=discord.Color.red()))
    
    # Remove from storage
    cmd = bot.custom_commands.pop(name)
    
    # Remove from bot
    bot.remove_command(name)
    
    embed = discord.Embed(title="🗑️ Custom Command Deleted", color=discord.Color.orange())
    embed.add_field(name="Name", value=name, inline=True)
    embed.add_field(name="Usage Count", value=cmd.usage_count, inline=True)
    
    await ctx.send(embed=embed)

@bot.command()
async def listcmds(ctx):
    """List all custom commands"""
    if not bot.is_active_instance:
        return
    
    if not bot.custom_commands:
        return await ctx.send(embed=discord.Embed(title="📝 No Custom Commands", description="No custom commands have been added yet.", color=discord.Color.blue()))
    
    embed = discord.Embed(title="📝 Custom Commands", color=discord.Color.blue())
    embed.description = f"Total: {len(bot.custom_commands)} commands"
    
    for name, cmd in list(bot.custom_commands.items())[:10]:  # Show max 10
        creator = bot.get_user(cmd.created_by)
        creator_name = creator.display_name if creator else "Unknown"
        
        embed.add_field(
            name=f"`bot {name}`",
            value=f"**Description:** {cmd.description}\n**Creator:** {creator_name}\n**Uses:** {cmd.usage_count}",
            inline=False
        )
    
    if len(bot.custom_commands) > 10:
        embed.set_footer(text=f"Showing 10/{len(bot.custom_commands)} commands")
    
    await ctx.send(embed=embed)

# --- EXISTING MODERATION COMMANDS ---

@bot.command()
async def ping(ctx):
    """Test bot response"""
    if not bot.is_active_instance:
        return
    
    embed = discord.Embed(
        title="🏓 Pong!",
        description=f"**Instance:** {bot.role.value.upper()} {'ACTIVE' if bot.is_active_instance else 'STANDBY'}\n**Latency:** {round(bot.latency * 1000)}ms",
        color=discord.Color.green()
    )
    await ctx.send(embed=embed)

@bot.command()
async def stats(ctx):
    """Show bot statistics"""
    if not bot.is_active_instance:
        return
    
    uptime = datetime.now(timezone.utc) - bot.stats["start_time"]
    
    embed = discord.Embed(
        title="📊 BanBot 3000 HA Stats",
        description=f"**Instance:** {bot.role.value.upper()} {'ACTIVE' if bot.is_active_instance else 'STANDBY'}",
        color=discord.Color.blue()
    )
    
    embed.add_field(name="Uptime", value=f"{uptime.days}d {uptime.seconds//3600}h", inline=True)
    embed.add_field(name="Guilds", value=len(bot.guilds), inline=True) 
    embed.add_field(name="Commands", value=bot.stats["commands_used"], inline=True)
    
    embed.add_field(name="Bans", value=bot.stats["bans"], inline=True)
    embed.add_field(name="Kicks", value=bot.stats["kicks"], inline=True)
    embed.add_field(name="Timeouts", value=bot.stats["timeouts"], inline=True)
    
    embed.add_field(name="Warnings", value=bot.stats["warnings"], inline=True)
    embed.add_field(name="Custom Commands", value=len(bot.custom_commands), inline=True)
    embed.add_field(name="Custom Uses", value=bot.stats["custom_commands_used"], inline=True)

    await ctx.send(embed=embed)

@bot.command()
async def hastatus(ctx):
    """High Availability status"""
    if not bot.is_active_instance:
        return
    
    embed = discord.Embed(title="🏥 HA Status", color=discord.Color.blue())
    
    embed.add_field(
        name=f"This Instance ({bot.role.value.upper()})",
        value=f"**Status:** {'ACTIVE' if bot.is_active_instance else 'STANDBY'}\n**Port:** {bot.port}\n**Ready:** {'✅' if bot.is_ready() else '❌'}",
        inline=True
    )
    
    # Health endpoints info
    base_url = os.getenv('RAILWAY_STATIC_URL', f'http://localhost:{bot.port}')
    embed.add_field(
        name="Health Endpoints", 
        value=f"[Health Check]({base_url}/health)\n[Ping]({base_url}/ping)\n[Stats]({base_url}/stats)",
        inline=True
    )
    
    uptime = datetime.now(timezone.utc) - bot.stats["start_time"]
    embed.add_field(
        name="System Info", 
        value=f"**Uptime:** {uptime.days}d {uptime.seconds//3600}h\n**Platform:** {'Railway' if os.getenv('RAILWAY_ENVIRONMENT') else 'Local'}\n**Custom Commands:** {len(bot.custom_commands)}", 
        inline=False
    )
    
    await ctx.send(embed=embed)

# Add all your existing moderation commands here (ban, kick, timeout, etc.)
# I'll include a few key ones:

@bot.command()
async def ban(ctx, member: discord.Member, *, reason="No reason provided"):
    """Ban a user"""
    if not bot.is_authorized(ctx, "ban"):
        return await ctx.send(embed=discord.Embed(title="❌ Access Denied", color=discord.Color.red()))
    
    if member == ctx.author or (member.top_role >= ctx.author.top_role and not bot.is_admin(ctx.author)):
        return await ctx.send(embed=discord.Embed(title="❌ Invalid Target", color=discord.Color.red()))

    try:
        action = ModerationAction(member.id, ctx.author.id, ActionType.BAN, reason, datetime.now(timezone.utc))
        bot.log_action(action)
        
        await member.ban(reason=f"By {ctx.author}: {reason}")
        
        embed = discord.Embed(title="🔨 User Banned", color=discord.Color.red())
        embed.add_field(name="User", value=f"{member.display_name}", inline=True)
        embed.add_field(name="By", value=f"{ctx.author.display_name}", inline=True)
        embed.add_field(name="Reason", value=reason, inline=False)
        
        await ctx.send(embed=embed)
        
    except discord.Forbidden:
        await ctx.send(embed=discord.Embed(title="❌ No Permission", color=discord.Color.red()))

@bot.command()
async def warn(ctx, member: discord.Member, *, reason="No reason provided"):
    """Warn a user"""
    if not bot.is_authorized(ctx, "warn"):
        return await ctx.send(embed=discord.Embed(title="❌ You're deopped", color=discord.Color.red()))
    
    if member == ctx.author:
        return await ctx.send(embed=discord.Embed(title="❌ Can't warn yourself", color=discord.Color.red()))

    warning = bot.add_warning(member.id, ctx.author.id, reason)
    warnings_count = len(bot.get_user_warnings(member.id))
    
    embed = discord.Embed(title="⚠️ User Warned", color=discord.Color.yellow())
    embed.add_field(name="User", value=f"{member.display_name}", inline=True)
    embed.add_field(name="Warning #", value=f"{warning.id} (Total: {warnings_count})", inline=True)
    embed.add_field(name="Reason", value=reason, inline=False)
    
    await ctx.send(embed=embed)

@bot.command()
async def kick(ctx, member: discord.Member, *, reason="No reason provided"):
    """Kick a user"""
    if not bot.is_authorized(ctx, "kick"):
        return await ctx.send(embed=discord.Embed(title="❌ Access Denied", color=discord.Color.red()))
    
    if member == ctx.author or (member.top_role >= ctx.author.top_role and not bot.is_admin(ctx.author)):
        return await ctx.send(embed=discord.Embed(title="❌ Invalid Target", color=discord.Color.red()))

    try:
        action = ModerationAction(member.id, ctx.author.id, ActionType.KICK, reason, datetime.now(timezone.utc))
        bot.log_action(action)
        
        await member.kick(reason=f"By {ctx.author}: {reason}")
        
        embed = discord.Embed(title="👢 User Kicked", color=discord.Color.orange())
        embed.add_field(name="User", value=f"{member.display_name}", inline=True)
        embed.add_field(name="By", value=f"{ctx.author.display_name}", inline=True) 
        embed.add_field(name="Reason", value=reason, inline=False)
        
        await ctx.send(embed=embed)
        
    except discord.Forbidden:
        await ctx.send(embed=discord.Embed(title="❌ No Permission", color=discord.Color.red()))

@bot.command()
async def timeout(ctx, member: discord.Member, duration: str, *, reason="No reason provided"):
    """Timeout a user (1m, 1h, 1d format)"""
    if not bot.is_authorized(ctx, "timeout"):
        return await ctx.send(embed=discord.Embed(title="❌ Access Denied", color=discord.Color.red()))
    
    try:
        minutes = parse_duration(duration)
        if minutes > 28 * 1440:  # 28 days max
            return await ctx.send(embed=discord.Embed(title="❌ Max 28 days", color=discord.Color.red()))
    except ValueError as e:
        return await ctx.send(embed=discord.Embed(title="❌ Invalid Duration", description=str(e), color=discord.Color.red()))

    try:
        until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        action = ModerationAction(member.id, ctx.author.id, ActionType.TIMEOUT, reason, datetime.now(timezone.utc), minutes)
        bot.log_action(action)
        
        await member.timeout(until, reason=f"By {ctx.author}: {reason}")
        
        embed = discord.Embed(title="🤐 User Timed Out", color=discord.Color.orange())
        embed.add_field(name="User", value=f"{member.display_name}", inline=True)
        embed.add_field(name="Duration", value=duration, inline=True)
        embed.add_field(name="Reason", value=reason, inline=False)
        
        await ctx.send(embed=embed)
        
    except discord.Forbidden:
        await ctx.send(embed=discord.Embed(title="❌ No Permission", color=discord.Color.red()))

@bot.command()
async def warnings(ctx, member: discord.Member = None):
    """Show user warnings"""
    if not bot.is_active_instance:
        return
    
    if not member:
        member = ctx.author
    
    warnings = bot.get_user_warnings(member.id)
    
    embed = discord.Embed(title=f"⚠️ Warnings for {member.display_name}", color=discord.Color.yellow())
    
    if not warnings:
        embed.description = "No warnings found"
    else:
        embed.description = f"Total: {len(warnings)}"
        for w in warnings[:5]:
            mod = ctx.guild.get_member(w.moderator_id)
            mod_name = mod.display_name if mod else "Unknown"
            embed.add_field(
                name=f"Warning #{w.id}",
                value=f"**By:** {mod_name}\n**Reason:** {w.reason}\n**Date:** {w.timestamp.strftime('%Y-%m-%d')}",
                inline=False
            )
    
    await ctx.send(embed=embed)

@bot.command()
async def deop(ctx, member: discord.Member, *, reason="No reason provided"):
    """Remove admin privileges (Admin only)"""
    if not bot.is_active_instance or not bot.is_admin(ctx.author):
        return await ctx.send(embed=discord.Embed(title="❌ Admin Only", color=discord.Color.red()))
    
    if member == ctx.author:
        return await ctx.send(embed=discord.Embed(title="❌ Can't deop yourself", color=discord.Color.red()))

    deopped = DeoppedUser(member.id, ctx.author.id, datetime.now(timezone.utc), reason)
    bot.deopped_users[member.id] = deopped
    bot.stats["deops"] += 1
    
    embed = discord.Embed(title="🚫 User Deopped", color=discord.Color.red())
    embed.add_field(name="User", value=f"{member.display_name}", inline=True)
    embed.add_field(name="By", value=f"{ctx.author.display_name}", inline=True)
    embed.add_field(name="Reason", value=reason, inline=False)
    
    await ctx.send(embed=embed)

@bot.command()
async def reop(ctx, member: discord.Member):
    """Restore admin privileges (Admin only)"""
    if not bot.is_active_instance or not bot.is_admin(ctx.author):
        return await ctx.send(embed=discord.Embed(title="❌ Admin Only", color=discord.Color.red()))
    
    if member.id not in bot.deopped_users:
        return await ctx.send(embed=discord.Embed(title="❌ User not deopped", color=discord.Color.red()))

    del bot.deopped_users[member.id]
    
    embed = discord.Embed(title="✅ User Reopped", color=discord.Color.green())
    embed.add_field(name="User", value=f"{member.display_name}", inline=True)
    embed.add_field(name="By", value=f"{ctx.author.display_name}", inline=True)
    
    await ctx.send(embed=embed)

@bot.command()
async def cleanup(ctx, amount: int = 10):
    """Delete messages (1-100)"""
    if not bot.is_authorized(ctx, "cleanup"):
        return await ctx.send(embed=discord.Embed(title="❌ Access Denied", color=discord.Color.red()))
    
    if not 1 <= amount <= 100:
        return await ctx.send(embed=discord.Embed(title="❌ Amount 1-100 only", color=discord.Color.red()))

    try:
        deleted = await ctx.channel.purge(limit=amount + 1)
        embed = discord.Embed(title="🧹 Cleanup", description=f"Deleted {len(deleted)-1} messages", color=discord.Color.green())
        msg = await ctx.send(embed=embed)
        await asyncio.sleep(3)
        await msg.delete()
    except discord.Forbidden:
        await ctx.send(embed=discord.Embed(title="❌ No Permission", color=discord.Color.red()))

# Graceful shutdown handling
def signal_handler(signum, frame):
    logger.info(f"Received signal {signum}, shutting down...")
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.create_task(bot.close())
        else:
            loop.run_until_complete(bot.close())
    except:
        pass

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

async def main():
    """Main function"""
    try:
        # Start HTTP server
        await bot.http_server.start()
        logger.info(f"🌐 HTTP server running on port {bot.port}")
        
        # Get token
        token = os.getenv('DISCORD_TOKEN')
        if not token:
            logger.error("❌ No DISCORD_TOKEN environment variable!")
            return
        
        # Start bot
        logger.info(f"🚀 Starting BanBot HA - {bot.role.value.upper()}")
        await bot.start(token)
        
    except Exception as e:
        logger.error(f"❌ Error: {e}")
    finally:
        await bot.http_server.stop()

def run():
    """Entry point"""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Stopped by user")
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}")

if __name__ == "__main__":
    run()
