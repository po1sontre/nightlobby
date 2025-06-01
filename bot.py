import discord
from discord.ext import commands, tasks
import asyncio
from datetime import datetime, timedelta
import logging
import os
from dotenv import load_dotenv
import re
import json
import uuid

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Bot configuration
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
bot = commands.Bot(command_prefix='/', intents=intents, help_command=None)

# In-memory storage for active lobbies
active_lobbies = {}
user_sessions = {}  # Track which users are in active sessions
empty_lobby_timers = {}  # Track empty lobby timers
pending_requests = {}  # Store pending match requests
request_timeouts = {}  # Store request timeout tasks

# Steam friend code pattern (9-10 digits, can be within text)
STEAM_CODE_PATTERN = r'(?:^|\s|:)(\d{9,10})(?:\s|$|\.|,|!|\?)'

class CopyButton(discord.ui.Button):
    def __init__(self, label: str, command: str):
        super().__init__(
            label=label,
            style=discord.ButtonStyle.secondary,
            emoji="📋"
        )
        self.command = command

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            f"✅ Copied to clipboard: `{self.command}`",
            ephemeral=True
        )

class LobbyView(discord.ui.View):
    def __init__(self, owner_id, lobby_channel, lobby_hash):
        super().__init__(timeout=None)  # Persistent view
        self.owner_id = owner_id
        self.lobby_channel = lobby_channel
        self.lobby_hash = lobby_hash
        self.max_players = 3
        
    @discord.ui.button(label='Join Game', style=discord.ButtonStyle.green, emoji='🎮', custom_id='join_game_button')
    async def join_game(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "❌ This command is not available in this view.",
            ephemeral=True
        )

    async def _update_lobby_message(self, interaction):
        embed = discord.Embed(
            title="🕹️ NightReign Lobby",
            color=0x00ff00 if len(active_lobbies[self.lobby_channel.id]['players']) < self.max_players else 0xff0000,
            timestamp=datetime.now()
        )
        player_list = []
        for i, player_id in enumerate(active_lobbies[self.lobby_channel.id]['players']):
            user = bot.get_user(player_id)
            if user:
                crown = "👑" if i == 0 else "🎮"
                player_list.append(f"{crown} {user.display_name}")
        embed.add_field(
            name=f"Players ({len(active_lobbies[self.lobby_channel.id]['players'])}/{self.max_players})",
            value="\n".join(player_list) if player_list else "None",
            inline=False
        )
        embed.add_field(
            name="Lobby Channel",
            value=f"#{self.lobby_channel.name}",
            inline=True
        )
        if len(active_lobbies[self.lobby_channel.id]['players']) >= self.max_players:
            self.join_game.disabled = True
            self.join_game.style = discord.ButtonStyle.red
            self.join_game.label = "Lobby Full"
            embed.add_field(
                name="Status",
                value="🔴 **LOBBY FULL** - Ready to play!",
                inline=True
            )
        else:
            self.join_game.disabled = False
            self.join_game.style = discord.ButtonStyle.green
            self.join_game.label = "Join Game"
            embed.add_field(
                name="Status",
                value=f"🟢 **OPEN** - Need {self.max_players - len(active_lobbies[self.lobby_channel.id]['players'])} more player(s)",
                inline=True
            )
        # Edit the original Join Game message in the command channel
        lobby_data = active_lobbies[self.lobby_channel.id]
        join_msg_id = lobby_data.get('join_message_id')
        if join_msg_id:
            for guild in bot.guilds:
                for channel in guild.text_channels:
                    try:
                        msg = await channel.fetch_message(join_msg_id)
                        await msg.edit(embed=embed, view=self)
                        return
                    except Exception:
                        continue
        # Fallback: edit the interaction message if original not found
        await interaction.response.edit_message(embed=embed, view=self)

class LobbyChannelView(discord.ui.View):
    def __init__(self, lobby_data):
        super().__init__(timeout=None)
        self.lobby_data = lobby_data
        
    def get_live_players(self, channel_id):
        # Always get the latest player list from active_lobbies
        lobby = active_lobbies.get(channel_id)
        if lobby:
            return lobby['players']
        return []
        
    @discord.ui.button(label='Leave Lobby', style=discord.ButtonStyle.red, emoji='🚪')
    async def leave_lobby(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "❌ This command is not available in this view.",
            ephemeral=True
        )

    @discord.ui.button(label='Invite Player', style=discord.ButtonStyle.primary, emoji='📨')
    async def invite_player(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "❌ This command is not available in this view.",
            ephemeral=True
        )

    @discord.ui.button(label='End Session', style=discord.ButtonStyle.gray, emoji='🏁')
    async def end_session(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "❌ This command is not available in this view.",
            ephemeral=True
        )

class LobbyListButton(discord.ui.View):
    def __init__(self, lobby_channel):
        super().__init__(timeout=None)
        self.lobby_channel = lobby_channel
        
    @discord.ui.button(label='Join Lobby', style=discord.ButtonStyle.green, emoji='🎮')
    async def join_lobby(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "❌ This command is not available in this view.",
            ephemeral=True
        )

class LobbyPaginator(discord.ui.View):
    def __init__(self, lobbies_data, timeout=180):
        super().__init__(timeout=timeout)
        self.lobbies_data = lobbies_data
        self.current_page = 0
        self.lobbies_per_page = 5
        self.total_pages = (len(lobbies_data) + self.lobbies_per_page - 1) // self.lobbies_per_page
        
        # Update button states
        self.update_buttons()
    
    def update_buttons(self):
        self.previous_page.disabled = self.current_page == 0
        self.next_page.disabled = self.current_page >= self.total_pages - 1
    
    def get_page_embed(self):
        start_idx = self.current_page * self.lobbies_per_page
        end_idx = min(start_idx + self.lobbies_per_page, len(self.lobbies_data))
        current_lobbies = self.lobbies_data[start_idx:end_idx]
        
        embed = discord.Embed(
            title="🕹️ Active NightReign Lobbies",
            description="Use the commands below to join a lobby",
            color=0x00ff00,
            timestamp=datetime.now()
        )
        
        # Add lobbies for current page
        for lobby_data in current_lobbies:
            channel = bot.get_channel(lobby_data['channel_id'])
            if not channel:
                continue
                
            owner = bot.get_user(lobby_data['owner'])
            owner_name = owner.display_name if owner else "Unknown"
            
            embed.add_field(
                name=f"#{channel.name}",
                value=(
                    f"👑 Owner: {owner_name}\n"
                    f"👥 Players: {lobby_data['member_count']}/3\n"
                    f"🎮 Players: {', '.join(lobby_data['player_list']) if lobby_data['player_list'] else 'None'}\n"
                    f"🔑 Join Command: `/join_lobby {lobby_data['hash']}`"
                ),
                inline=True
            )
        
        # Add summary field
        embed.add_field(
            name="📊 Summary",
            value=(
                f"Total Lobbies: {len(self.lobbies_data)}\n"
                f"Available Spots: {sum(3 - data['member_count'] for data in self.lobbies_data)}\n"
                f"Page {self.current_page + 1}/{self.total_pages}"
            ),
            inline=False
        )
        
        embed.set_footer(text="Copy and paste the join command to join a lobby")
        return embed
    
    @discord.ui.button(label="◀️ Previous", style=discord.ButtonStyle.gray, custom_id="previous_page")
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page > 0:
            self.current_page -= 1
            self.update_buttons()
            await interaction.response.edit_message(embed=self.get_page_embed(), view=self)
    
    @discord.ui.button(label="Next ▶️", style=discord.ButtonStyle.gray, custom_id="next_page")
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page < self.total_pages - 1:
            self.current_page += 1
            self.update_buttons()
            await interaction.response.edit_message(embed=self.get_page_embed(), view=self)

@bot.event
async def on_ready():
    print(f'{bot.user} has connected to Discord!')
    
    # Register slash commands
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"Failed to sync commands: {e}")
    
    # Store existing lobby channels before clearing
    existing_lobbies = []
    for guild in bot.guilds:
        for channel in guild.text_channels:
            if channel.name.startswith('lobby-'):
                existing_lobbies.append(channel)
    
    # Clear active lobbies and sessions
    active_lobbies.clear()
    user_sessions.clear()
    
    # Send restart notification to existing lobbies
    for channel in existing_lobbies:
        try:
            embed = discord.Embed(
                title="🔄 Bot Restarted",
                description=(
                    "The bot was restarted for maintenance or updates.\n"
                    "Most features should work as normal, but some features may temporarily behave differently.\n"
                    "If you notice any issues, please ping @po1sontre.\n\n"
                    "**New Features:**\n"
                    "• `/kick_lobby` - Anyone in a lobby can kick another member.\n"
                    "• `/help` - See all available commands.\n"
                    "• Slash commands and auto-suggestions are now supported!\n"
                ),
                color=0x00ff00
            )
            embed.set_footer(text="Thank you for your patience!")
            await channel.send(embed=embed)
        except Exception as e:
            logger.error(f"Error sending restart message to {channel.name}: {e}")
    
    # Continue with normal lobby restoration
    for guild in bot.guilds:
        for channel in guild.text_channels:
            if channel.name.startswith('lobby-'):
                players = []
                owner_id = None
                hash_message_id = None
                lobby_hash = None
                async for message in channel.history(limit=20):
                    if message.author == bot.user and message.content and message.content.startswith('Lobby Hash:'):
                        lobby_hash = message.content.split('`')[1]
                        hash_message_id = message.id
                        break
                for member in channel.members:
                    perms = channel.permissions_for(member)
                    if perms.read_messages and perms.send_messages and not member.bot:
                        players.append(member.id)
                        if owner_id is None:
                            owner_id = member.id
                if owner_id is None and players:
                    owner_id = players[0]
                if owner_id and lobby_hash and hash_message_id:
                    lobby_data = {
                        'owner': owner_id,
                        'players': players,
                        'channel': channel.id,
                        'created_at': datetime.utcnow(),
                        'hash': lobby_hash,
                        'hash_message_id': hash_message_id
                    }
                    active_lobbies[channel.id] = lobby_data
                    for pid in players:
                        user_sessions[pid] = channel.id
    
    # Start the cleanup task
    if not cleanup_inactive_lobbies.is_running():
        cleanup_inactive_lobbies.start()
        print("Started lobby cleanup task - running every 5 minutes")
    
    # Start the periodic announcement task
    if not periodic_announcement.is_running():
        periodic_announcement.start()
        print("Started periodic announcement task - running every 4 hours")

@bot.event
async def on_message(message):
    # Don't respond to our own messages
    if message.author == bot.user:
        return

    # Check if the message contains a Steam friend code
    steam_codes = re.findall(STEAM_CODE_PATTERN, message.content)
    
    if steam_codes:
        logger.info(f"Detected Steam code(s) in message from {message.author}: {steam_codes}")
        
        # Skip Steam code detection in private lobby channels
        if message.channel.name.startswith('lobby-'):
            await bot.process_commands(message)
            return
            
        # Check if user is already in a session
        if message.author.id in user_sessions:
            channel_id = user_sessions[message.author.id]
            existing_channel = bot.get_channel(channel_id)
            if existing_channel:
                await message.channel.send(
                    f"❌ {message.author.mention} You're already in an active lobby! "
                    f"Please leave your current session first: {existing_channel.mention}"
                )
            else:
                # Clean up stale session
                del user_sessions[message.author.id]
            return

        # Create a new lobby for the user
        try:
            # Create private lobby channel OUTSIDE the Bot Data category
            overwrites = {
                message.guild.default_role: discord.PermissionOverwrite(read_messages=False),
                message.author: discord.PermissionOverwrite(read_messages=True, send_messages=True),
                bot.user: discord.PermissionOverwrite(read_messages=True, send_messages=True)
            }
            # Find the 'Bot Data' category to avoid it
            bot_data_category = discord.utils.get(message.guild.categories, name='Bot Data')
            channel_name = f"lobby-{message.author.display_name.lower()}-{datetime.now().strftime('%H%M')}"
            lobby_channel = await message.guild.create_text_channel(
                channel_name,
                overwrites=overwrites,
                category=None if not bot_data_category else None,  # Explicitly not in Bot Data
                reason=f"NightReign lobby created by {message.author}"
            )
            logger.info(f"Created new lobby channel {channel_name} for user {message.author}")
            # Store lobby data
            lobby_hash = str(uuid.uuid4())
            lobby_data = {
                'owner': message.author.id,
                'players': [message.author.id],
                'channel': lobby_channel.id,
                'created_at': datetime.now(),
                'hash': lobby_hash,
                'hash_message_id': None
            }
            active_lobbies[lobby_channel.id] = lobby_data
            user_sessions[message.author.id] = lobby_channel.id
            # Send the hash message in the lobby channel
            hash_msg = await lobby_channel.send(f"Lobby Hash: `{lobby_hash}`\nQuick Join: `/join_lobby {lobby_hash}`")
            lobby_data['hash_message_id'] = hash_msg.id
            # Send welcome message in lobby channel
            welcome_embed = discord.Embed(
                title="🎉 Welcome to your NightReign Lobby!",
                description=f"Lobby Hash: `{lobby_hash}`\n\nUse the commands below to manage your lobby:",
                color=0x00ff00
            )
            welcome_embed.add_field(
                name="📋 Lobby Commands",
                value=(
                    f"• `/join_lobby {lobby_hash}` — Join this lobby\n"
                    f"• `/leave_lobby` — Leave this lobby\n"
                    f"• `/invite_lobby @user` — Invite a user to this lobby\n"
                    f"• `/end_lobby` — End the lobby (owner/mod only)"
                ),
                inline=False
            )
            welcome_embed.add_field(
                name="Instructions",
                value="Share your Steam friend codes, coordinate your game time, and use the commands above to manage your session.",
                inline=False
            )
            await lobby_channel.send(embed=welcome_embed)
            # Notify the user
            await message.channel.send(
                f"🎮 {message.author.mention} I've created a lobby for you! "
                f"Click here to go to your lobby: {lobby_channel.mention}"
            )
            # In both /create_game and Steam friend code detection, after creating the lobby and hash, send the join_embed in the original channel
            join_embed = discord.Embed(
                title="🕹️ NightReign Lobby",
                color=0x00ff00,
                timestamp=datetime.now()
            )
            join_embed.add_field(
                name="Players (1/3)",
                value=f"👑 {message.author.display_name if hasattr(message, 'author') else ctx.author.display_name}",
                inline=False
            )
            join_embed.add_field(
                name="Lobby Channel",
                value=f"{lobby_channel.mention}",
                inline=True
            )
            join_embed.add_field(
                name="Status",
                value="🟢 **OPEN** - Need 2 more players",
                inline=True
            )
            join_embed.add_field(
                name="How to Join",
                value=f"**To join this lobby, copy and paste the command below:**\n```/join_lobby {lobby_hash}```",
                inline=False
            )
            join_embed.set_footer(text="Use the Quick Join command below to join this lobby!")
            msg = await (message.channel.send if hasattr(message, 'channel') else ctx.send)(embed=join_embed)
            lobby_data['join_message_id'] = msg.id
        except discord.Forbidden:
            logger.error(f"Permission error creating channel for user {message.author}")
            await message.channel.send("❌ I don't have permission to create channels!")
        except Exception as e:
            logger.error(f"Error creating lobby for user {message.author}: {str(e)}")
            await message.channel.send(f"❌ Error creating lobby: {str(e)}")
    # Process commands after checking for Steam codes
    await bot.process_commands(message)

@bot.command(name='create_game', description='Create a new NightReign lobby')
async def create_game(ctx):
    """Create a new NightReign lobby"""
    user_id = ctx.author.id
    # Check if user already has an active session
    if user_id in user_sessions:
        channel_id = user_sessions[user_id]
        existing_channel = bot.get_channel(channel_id)
        if existing_channel:
            await ctx.send(
                f"❌ You're already in an active lobby! Leave your current session first: {existing_channel.mention}",
                ephemeral=True
            )
        else:
            # Clean up stale session
            del user_sessions[user_id]
        return
    try:
        # Create private lobby channel OUTSIDE the Bot Data category
        overwrites = {
            ctx.guild.default_role: discord.PermissionOverwrite(read_messages=False),
            ctx.author: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            bot.user: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }
        # Find the 'Bot Data' category to avoid it
        bot_data_category = discord.utils.get(ctx.guild.categories, name='Bot Data')
        channel_name = f"lobby-{ctx.author.display_name.lower()}-{datetime.now().strftime('%H%M')}"
        lobby_channel = await ctx.guild.create_text_channel(
            channel_name,
            overwrites=overwrites,
            category=None if not bot_data_category else None,  # Explicitly not in Bot Data
            reason=f"NightReign lobby created by {ctx.author}"
        )
        # Store lobby data
        lobby_hash = str(uuid.uuid4())
        lobby_data = {
            'owner': user_id,
            'players': [user_id],
            'channel': lobby_channel.id,
            'created_at': datetime.now(),
            'hash': lobby_hash,
            'hash_message_id': None
        }
        active_lobbies[lobby_channel.id] = lobby_data
        user_sessions[user_id] = lobby_channel.id
        # Send the hash message in the lobby channel
        hash_msg = await lobby_channel.send(f"Lobby Hash: `{lobby_hash}`\nQuick Join: `/join_lobby {lobby_hash}`")
        lobby_data['hash_message_id'] = hash_msg.id
        # Send welcome message in lobby channel
        welcome_embed = discord.Embed(
            title="🎉 Welcome to your NightReign Lobby!",
            description=f"Lobby Hash: `{lobby_hash}`\n\nUse the commands below to manage your lobby:",
            color=0x00ff00
        )
        welcome_embed.add_field(
            name="📋 Lobby Commands",
            value=(
                f"• `/join_lobby {lobby_hash}` — Join this lobby\n"
                f"• `/leave_lobby` — Leave this lobby\n"
                f"• `/invite_lobby @user` — Invite a user to this lobby\n"
                f"• `/end_lobby` — End the lobby (owner/mod only)"
            ),
            inline=False
        )
        welcome_embed.add_field(
            name="Instructions",
            value="Share your Steam friend codes, coordinate your game time, and use the commands above to manage your session.",
            inline=False
        )
        await lobby_channel.send(embed=welcome_embed)
        # In both /create_game and Steam friend code detection, after creating the lobby and hash, send the join_embed in the original channel
        join_embed = discord.Embed(
            title="🕹️ NightReign Lobby",
            color=0x00ff00,
            timestamp=datetime.now()
        )
        join_embed.add_field(
            name="Players (1/3)",
            value=f"👑 {ctx.author.display_name}",
            inline=False
        )
        join_embed.add_field(
            name="Lobby Channel",
            value=f"{lobby_channel.mention}",
            inline=True
        )
        join_embed.add_field(
            name="Status",
            value="🟢 **OPEN** - Need 2 more players",
            inline=True
        )
        join_embed.add_field(
            name="How to Join",
            value=f"**To join this lobby, copy and paste the command below:**\n```/join_lobby {lobby_hash}```",
            inline=False
        )
        join_embed.set_footer(text="Use the Quick Join command below to join this lobby!")
        msg = await ctx.send(embed=join_embed)
        lobby_data['join_message_id'] = msg.id
    except discord.Forbidden:
        await ctx.send("❌ I don't have permission to create channels!")
    except Exception as e:
        await ctx.send(f"❌ Error creating lobby: {str(e)}")

@bot.command(name='my_lobby', description='Check your current lobby status')
async def my_lobby(ctx):
    """Check your current lobby status"""
    user_id = ctx.author.id
    
    if user_id not in user_sessions:
        await ctx.send("❌ You don't have an active lobby.")
        return
        
    channel_id = user_sessions[user_id]
    lobby_channel = bot.get_channel(channel_id)
    
    if not lobby_channel:
        # Clean up stale session
        del user_sessions[user_id]
        await ctx.send("❌ Your lobby channel no longer exists.")
        return
    
    # Get the lobby hash from the channel history
    lobby_hash = None
    async for message in lobby_channel.history(limit=20):
        if message.author == bot.user and message.content and message.content.startswith('Lobby Hash:'):
            lobby_hash = message.content.split('`')[1]
            break
    
    if lobby_hash:
        await ctx.send(
            f"🎮 Your active lobby: {lobby_channel.mention}\n"
            f"To join this lobby, use: `/join_lobby {lobby_hash}`"
        )
    else:
        await ctx.send(f"🎮 Your active lobby: {lobby_channel.mention}")

@bot.command(name='lobbies', description='List all active lobbies')
async def list_lobbies(ctx):
    """List all active lobbies with accurate player stats and join commands"""
    if not active_lobbies:
        await ctx.send("🔍 No active lobbies found.")
        return
    
    # Collect all available lobby data
    available_lobbies = []
    for channel_id, lobby_data in active_lobbies.items():
        channel = bot.get_channel(channel_id)
        if not channel:
            continue
            
        # Get member count by checking for the specific role
        member_count = 0
        player_list = []
        for member in channel.members:
            if any(role.id == 1242067709433217088 for role in member.roles):
                member_count += 1
                player_list.append(member.display_name)
        
        # Skip full lobbies
        if member_count >= 3:
            continue
            
        # Get the lobby hash
        lobby_hash = lobby_data.get('hash', '')
        if not lobby_hash:
            continue
            
        available_lobbies.append({
            'channel_id': channel_id,
            'owner': lobby_data['owner'],
            'member_count': member_count,
            'player_list': player_list,
            'hash': lobby_hash
        })
    
    if not available_lobbies:
        await ctx.send("🔍 No available lobbies found.")
        return
    
    # Create and send the paginated view
    view = LobbyPaginator(available_lobbies)
    await ctx.send(embed=view.get_page_embed(), view=view)

# Add button callback for join buttons
@bot.event
async def on_interaction(interaction: discord.Interaction):
    if not interaction.data or 'custom_id' not in interaction.data:
        return
        
    if interaction.data['custom_id'].startswith('join_'):
        channel_id = int(interaction.data['custom_id'].split('_')[1])
        channel = bot.get_channel(channel_id)
        
        if not channel:
            await interaction.response.send_message("❌ This lobby no longer exists.", ephemeral=True)
            return
            
        # Get the lobby hash
        lobby_hash = None
        async for message in channel.history(limit=20):
            if message.author == bot.user and message.content and message.content.startswith('Lobby Hash:'):
                lobby_hash = message.content.split('`')[1]
                break
        
        if not lobby_hash:
            await interaction.response.send_message("❌ Could not find lobby information.", ephemeral=True)
            return
            
        # Create a temporary view to handle the join
        view = LobbyView(active_lobbies[channel_id]['owner'], channel, lobby_hash)
        await view.join_game(interaction, None)

@tasks.loop(minutes=5)
async def cleanup_inactive_lobbies():
    """Clean up inactive lobbies that haven't had messages in 2 hours"""
    now = datetime.utcnow()
    to_delete = []
    
    # First check all channels that start with 'lobby-'
    for guild in bot.guilds:
        for channel in guild.text_channels:
            if not channel.name.startswith('lobby-'):
                continue
                
            try:
                # Get the last message in the channel
                last_message = None
                async for msg in channel.history(limit=1, oldest_first=False):
                    last_message = msg
                    break
                
                # If no messages found or last message is older than 2 hours
                if not last_message or (now - last_message.created_at.replace(tzinfo=None)) > timedelta(hours=2):
                    to_delete.append(channel.id)
                    logger.info(f"Marking channel {channel.name} for deletion - inactive for 2+ hours")
                    
            except Exception as e:
                logger.error(f"Error checking inactivity for channel {channel.name}: {e}")
    
    # Delete marked channels and clean up data
    for channel_id in to_delete:
        channel = bot.get_channel(channel_id)
        if channel:
            try:
                await channel.delete(reason="Inactive lobby (2h no messages)")
                logger.info(f"Deleted inactive lobby channel: {channel.name}")
            except Exception as e:
                logger.error(f"Error deleting inactive lobby channel {channel_id}: {e}")
        
        # Clean up data structures
        if channel_id in active_lobbies:
            del active_lobbies[channel_id]
        
        # Remove all user_sessions for this channel
        for uid in list(user_sessions):
            if user_sessions[uid] == channel_id:
                del user_sessions[uid]

@bot.event
async def on_member_join(member):
    """Send welcome message to new members"""
    # Wait a bit to ensure the member is fully joined
    await asyncio.sleep(1)
    
    try:
        embed = discord.Embed(
            title="🎮 Welcome to NightReign!",
            description=(
                "I'm your friendly NightReign Lobby Bot! Here's how to get started:\n\n"
                "**Quick Start:**\n"
                "1. Use `/create_game` to create a lobby\n"
                "2. Share your Steam friend code in the lobby\n"
                "3. Use `/find_match` to find other players\n"
                "4. Use `/lobbies` to see all active games\n\n"
                "**Need Help?**\n"
                "• Use `/lobbyhelp` for all commands\n"
                "• Use `/find_match` to find players\n"
                "• Use `/my_lobby` to check your status"
            ),
            color=0x00ff00
        )
        
        # Try to send DM first
        try:
            await member.send(embed=embed)
        except:
            # If DM fails, try to find a suitable channel
            for channel in member.guild.text_channels:
                if channel.permissions_for(member).send_messages:
                    await channel.send(f"{member.mention}", embed=embed)
                    break
    except Exception as e:
        logger.error(f"Error sending welcome message to {member}: {e}")

@tasks.loop(hours=4)
async def periodic_announcement():
    """Send periodic quick start reminders about the bot's features"""
    for guild in bot.guilds:
        try:
            # Get the specific announcement channel
            announcement_channel = guild.get_channel(1242067710385590293)
            
            if announcement_channel:
                embed = discord.Embed(
                    title="🎮 NightReign Lobby Bot Quick Start",
                    description=(
                        "**Quick Commands:**\n"
                        "• `/create_game` — Create a new lobby\n"
                        "• `/join_lobby <hash>` — Join a lobby using its hash\n"
                        "• `/find_match` — Find players to join\n"
                        "• `/lobbies` — View all active games\n"
                        "• `/help` or `/lobbyhelp` — See all commands"
                    ),
                    color=0x00ff00
                )
                await announcement_channel.send(embed=embed)
            else:
                logger.error(f"Could not find announcement channel in {guild}")
        except Exception as e:
            logger.error(f"Error sending periodic announcement to {guild}: {e}")

@bot.command(name='leave_lobby', description='Leave your current lobby')
async def leave_lobby(ctx):
    """Leave the current lobby"""
    user_id = ctx.author.id
    
    # First check if they're in the channel they're trying to leave from
    if ctx.channel.name.startswith('lobby-'):
        # They're in a lobby channel, check if they have permissions
        if not ctx.channel.permissions_for(ctx.author).read_messages:
            await ctx.send("❌ You don't have access to this lobby.")
            return
            
        # Remove their permissions from this channel
        try:
            await ctx.channel.set_permissions(ctx.author, overwrite=None)
            await ctx.channel.send(f"👋 **{ctx.author.display_name}** left the lobby.")
            
            # Clean up user_sessions
            if user_id in user_sessions:
                del user_sessions[user_id]
            
            # Clean up active_lobbies
            if ctx.channel.id in active_lobbies:
                lobby = active_lobbies[ctx.channel.id]
                if user_id in lobby['players']:
                    lobby['players'].remove(user_id)
                    if len(lobby['players']) == 0:
                        del active_lobbies[ctx.channel.id]
            
            await ctx.send("✅ You have left the lobby.", ephemeral=True)
        except Exception as e:
            logger.error(f"Error removing permissions for user {ctx.author}: {e}")
            await ctx.send("❌ Error removing you from the lobby.")
        return
    
    # If they're not in a lobby channel, check user_sessions
    if user_id not in user_sessions:
        await ctx.send("❌ You are not in any lobby.")
        return
        
    channel_id = user_sessions[user_id]
    channel = bot.get_channel(channel_id)
    
    # Clean up user_sessions first
    del user_sessions[user_id]
    
    if not channel:
        await ctx.send("✅ You have been removed from the lobby.", ephemeral=True)
        return
        
    # Remove user from active_lobbies if tracked
    lobby = active_lobbies.get(channel_id)
    if lobby and user_id in lobby['players']:
        lobby['players'].remove(user_id)
        if len(lobby['players']) == 0:
            del active_lobbies[channel_id]
    
    try:
        await channel.set_permissions(ctx.author, overwrite=None)
        await channel.send(f"👋 **{ctx.author.display_name}** left the lobby.")
        await ctx.send("✅ You have left the lobby.", ephemeral=True)
    except Exception as e:
        logger.error(f"Error removing permissions for user {ctx.author}: {e}")
        await ctx.send("❌ Error removing you from the lobby.")

@bot.command(name='end_lobby', description='End the current lobby (owner/mod/role only)')
async def end_lobby(ctx):
    """End the current lobby"""
    # First check if this is a lobby channel
    if not ctx.channel.name.startswith('lobby-'):
        await ctx.send("❌ This command can only be used in lobby channels.")
        return
        
    # Check if user has access to the channel
    if not ctx.channel.permissions_for(ctx.author).read_messages:
        await ctx.send("❌ You don't have access to this lobby.")
        return
        
    # Check if user has the required role
    has_role = any(role.id == 1242067709433217088 for role in ctx.author.roles)
    
    # Check if user is owner, mod, or has the specific role
    is_owner = False
    is_mod = ctx.author.guild_permissions.administrator or ctx.author.guild_permissions.manage_channels
    
    # Try to get lobby data
    lobby = active_lobbies.get(ctx.channel.id)
    if lobby:
        is_owner = lobby['owner'] == ctx.author.id
    
    if not (is_owner or is_mod or has_role):
        await ctx.send("❌ Only the lobby owner, moderators, or users with the NightReign role can end the session!")
        return
        
    # Remove all users from user_sessions
    if lobby:
        for pid in lobby['players']:
            if pid in user_sessions:
                del user_sessions[pid]
        del active_lobbies[ctx.channel.id]
    
    await ctx.send("🏁 **Session ended.** Channel will be deleted in 10 seconds...")
    await asyncio.sleep(10)
    try:
        await ctx.channel.delete(reason="Session ended by owner/mod/role")
    except Exception:
        pass

@bot.command(name='invite_lobby', description='Invite a player to your lobby')
async def invite_lobby(ctx, member: discord.Member):
    """Invite a player to your lobby"""
    user_id = ctx.author.id
    if user_id not in user_sessions:
        await ctx.send("❌ You are not in any lobby.")
        return
    channel_id = user_sessions[user_id]
    channel = bot.get_channel(channel_id)
    if not channel:
        del user_sessions[user_id]
        await ctx.send("❌ Your lobby channel no longer exists.")
        return
    lobby = active_lobbies.get(channel_id)
    if lobby and member.id in lobby['players']:
        await ctx.send(f"❌ {member.mention} is already in this lobby.")
        return
    if lobby and len(lobby['players']) >= 3:
        await ctx.send("❌ This lobby is full! (3/3 players)")
        return
    if lobby:
        lobby['players'].append(member.id)
    user_sessions[member.id] = channel_id
    await channel.set_permissions(member, read_messages=True, send_messages=True)
    await channel.send(f"🎉 **{member.display_name}** was invited and joined the lobby! ({len(lobby['players']) if lobby else 'unknown'} players)")
    await ctx.send(f"✅ Successfully invited {member.mention} to the lobby!")

@bot.command(name='lobbyhelp', description='Show all available lobby commands')
async def lobby_help(ctx):
    """Show all available lobby commands and their usage"""
    embed = discord.Embed(
        title="🎮 NightReign Lobby Bot Commands",
        description="Here are all the available commands for the NightReign Lobby Bot:",
        color=0x00ff00
    )
    
    # Basic Commands
    embed.add_field(
        name="📋 Commands",
        value=(
            "`/create_game` - Create a new game lobby\n"
            "`/my_lobby` - Check your current lobby status\n"
            "`/lobbies` - List all active lobbies\n"
            "`/invite_lobby @user` - Invite a player to your current lobby"
        ),
        inline=False
    )
    
    # Quick Start
    embed.add_field(
        name="🚀 Quick Start",
        value=(
            "1. Type `/create_game` to create a lobby\n"
            "2. Share your Steam friend code in the lobby\n"
            "3. Use the 'Join Game' button to join others' lobbies\n"
            "4. Use 'Leave Lobby' when you're done"
        ),
        inline=False
    )
    
    # Tips
    embed.add_field(
        name="💡 Tips",
        value=(
            "• Share your Steam code to auto-create a lobby\n"
            "• Check #nightreign-online for game setup\n"
            "• Use `/invite_lobby @user` to invite friends directly\n"
            "• Lobbies auto-delete after 5 minutes of being empty"
        ),
        inline=False
    )
    
    embed.set_footer(text="Need more help? Contact a moderator!")
    
    await ctx.send(embed=embed)

@bot.command(name='join_lobby', description='Join a lobby using its hash')
async def join_lobby(ctx, lobby_hash: str):
    """Join a lobby by its hash"""
    input_hash = lobby_hash.strip().lower()
    # First, try active_lobbies as before
    for lobby in active_lobbies.values():
        stored_hash = str(lobby['hash']).strip().lower()
        if stored_hash == input_hash:
            channel = bot.get_channel(lobby['channel'])
            if not channel:
                await ctx.send("❌ That lobby no longer exists.")
                return
            if ctx.author.id in lobby['players']:
                await ctx.send("❌ You are already in this lobby.")
                return
            # Enforce the 3-player limit for tracked lobbies
            if len(lobby['players']) >= 3:
                await ctx.send(f"❌ This lobby is full! ({len(lobby['players'])}/3 players)\nPlayers in lobby: {', '.join([bot.get_user(pid).display_name for pid in lobby['players']])}")
                return
            lobby['players'].append(ctx.author.id)
            user_sessions[ctx.author.id] = channel.id
            await channel.set_permissions(ctx.author, read_messages=True, send_messages=True)
            await channel.send(f"🎉 **{ctx.author.display_name}** joined the lobby! ({len(lobby['players'])}/3 players)")
            await ctx.send(f"🎮 You've joined the lobby! Click here to go to the channel: {channel.mention}")
            return

    # If not found in active_lobbies, search all text channels for the hash
    for guild in bot.guilds:
        for channel in guild.text_channels:
            try:
                async for message in channel.history(limit=20):
                    if message.author == bot.user and message.content and message.content.lower().startswith('lobby hash:'):
                        if input_hash in message.content.lower():
                            # Check if channel is full by counting members with read permissions
                            member_count = 0
                            for member in channel.members:
                                if channel.permissions_for(member).read_messages and not member.bot:
                                    member_count += 1
                            
                            if member_count >= 3:
                                await ctx.send(f"❌ This lobby is full! ({member_count}/3 players)\nPlayers in lobby: {', '.join([m.display_name for m in channel.members if channel.permissions_for(m).read_messages and not m.bot])}")
                                return
                                
                            # Found the hash in this channel, allow unlimited joins
                            await channel.set_permissions(ctx.author, read_messages=True, send_messages=True)
                            await channel.send(f"🎉 **{ctx.author.display_name}** joined the lobby! ({member_count + 1}/3 players)")
                            await ctx.send(f"🎮 You've joined the lobby! Click here to go to the channel: {channel.mention}")
                            return
            except Exception:
                continue
                
    await ctx.send("❌ No lobby found with that hash.")

@bot.command(name='find_match', description='Find players to join your game')
async def find_match(ctx):
    """Broadcast a request to join any available lobby"""
    user_id = ctx.author.id
    
    # Check if user is already in a session
    if user_id in user_sessions:
        channel_id = user_sessions[user_id]
        existing_channel = bot.get_channel(channel_id)
        if existing_channel:
            await ctx.send(
                f"❌ You're already in an active lobby! Leave your current session first: {existing_channel.mention}",
                ephemeral=True
            )
        else:
            # Clean up stale session
            del user_sessions[user_id]
        return
    
    # Check if user already has a pending request
    if user_id in pending_requests:
        await ctx.send("❌ You already have a pending match request. Please wait for responses or use `/cancel_request` to cancel.", ephemeral=True)
        return
    
    # Create a unique request ID
    request_id = str(uuid.uuid4())
    
    # Store the request
    pending_requests[user_id] = {
        'request_id': request_id,
        'user_id': user_id,
        'username': ctx.author.display_name,
        'timestamp': datetime.now(),
        'responses': set()
    }
    
    # Create the request embed
    embed = discord.Embed(
        title="🎮 Match Request",
        description=f"**{ctx.author.display_name}** is looking for a game!",
        color=0x00ff00,
        timestamp=datetime.now()
    )
    embed.add_field(
        name="How to Respond",
        value="Use `/allow` to accept this player\nUse `/deny` to decline",
        inline=False
    )
    embed.set_footer(text=f"Request ID: {request_id}")
    
    # Send the request to all active lobbies
    sent_count = 0
    for channel_id, lobby_data in active_lobbies.items():
        channel = bot.get_channel(channel_id)
        if channel and len(lobby_data['players']) < 3:  # Only send to non-full lobbies
            try:
                await channel.send(embed=embed)
                sent_count += 1
            except Exception as e:
                logger.error(f"Error sending match request to {channel.name}: {e}")
    
    if sent_count == 0:
        await ctx.send("❌ No available lobbies found to send your request to.", ephemeral=True)
        del pending_requests[user_id]
        return
    
    # Create timeout task
    async def request_timeout():
        await asyncio.sleep(300)  # 5 minute timeout
        if user_id in pending_requests and pending_requests[user_id]['request_id'] == request_id:
            await ctx.send("⏰ Your match request has expired. No lobbies responded in time.", ephemeral=True)
            del pending_requests[user_id]
    
    request_timeouts[request_id] = asyncio.create_task(request_timeout())
    
    await ctx.send(
        f"✅ Your match request has been sent to {sent_count} available lobbies!\n"
        "Waiting for responses... (5 minute timeout)",
        ephemeral=True
    )

@bot.command(name='allow', description='Allow a player to join your lobby')
async def allow_player(ctx):
    """Allow a player to join your lobby"""
    if not ctx.channel.name.startswith('lobby-'):
        await ctx.send("❌ This command can only be used in lobby channels.", ephemeral=True)
        return
    
    # Find the most recent match request by checking message history
    request = None
    request_id = None
    
    # Look for the most recent match request message in the channel
    async for message in ctx.channel.history(limit=20):
        if (message.author == bot.user and 
            message.embeds and 
            message.embeds[0].title == "🎮 Match Request"):
            # Extract request ID from footer
            if message.embeds[0].footer and message.embeds[0].footer.text:
                request_id = message.embeds[0].footer.text.split("Request ID: ")[-1]
                # Find the request in pending_requests
                for user_id, req in pending_requests.items():
                    if req['request_id'] == request_id:
                        request = req
                        break
                if request:
                    break
    
    if not request:
        await ctx.send("❌ No active match requests found in this channel.", ephemeral=True)
        return
    
    # Check if lobby is full
    lobby = active_lobbies.get(ctx.channel.id)
    if not lobby:
        await ctx.send("❌ This lobby is no longer active.", ephemeral=True)
        return
    
    # Count actual members in channel
    member_count = 0
    for member in ctx.channel.members:
        if (ctx.channel.permissions_for(member).read_messages and 
            not member.bot and 
            not member.guild_permissions.administrator and 
            not member.guild_permissions.manage_channels):
            member_count += 1
    
    if member_count >= 3:
        await ctx.send("❌ This lobby is full! (3/3 players)", ephemeral=True)
        return
    
    # Add player to lobby
    user_id = request['user_id']
    
    # Try to fetch the member using the guild
    try:
        user = await ctx.guild.fetch_member(user_id)
    except:
        # If fetch fails, try to get from cache
        user = ctx.guild.get_member(user_id)
    
    if not user:
        await ctx.send("❌ Could not find the requesting user. They may have left the server.", ephemeral=True)
        return
    
    # Check if user is already in a session
    if user_id in user_sessions:
        await ctx.send(f"❌ {user.display_name} is already in another lobby.", ephemeral=True)
        return
    
    # Add to lobby data
    if user_id not in lobby['players']:
        lobby['players'].append(user_id)
    user_sessions[user_id] = ctx.channel.id
    
    # Add permissions
    try:
        await ctx.channel.set_permissions(user, read_messages=True, send_messages=True)
        await ctx.channel.send(f"🎉 **{user.display_name}** was accepted and joined the lobby! ({member_count + 1}/3 players)")
        
        # Notify the user
        try:
            await user.send(f"✅ Your match request was accepted! Click here to join: {ctx.channel.mention}")
        except:
            pass
        
        # Clean up the request
        if user_id in pending_requests:
            del pending_requests[user_id]
        if request_id in request_timeouts:
            request_timeouts[request_id].cancel()
            del request_timeouts[request_id]
        
        await ctx.send("✅ Player has been added to the lobby!", ephemeral=True)
    except discord.Forbidden:
        await ctx.send("❌ I don't have permission to add the player to this channel.", ephemeral=True)
    except Exception as e:
        await ctx.send(f"❌ Error adding player to the lobby: {str(e)}", ephemeral=True)
        logger.error(f"Error in allow command: {str(e)}")

@bot.command(name='deny', description='Deny a player\'s request to join')
async def deny_player(ctx):
    """Deny a player's request to join your lobby"""
    if not ctx.channel.name.startswith('lobby-'):
        await ctx.send("❌ This command can only be used in lobby channels.", ephemeral=True)
        return
    
    # Find the most recent match request
    request = None
    for user_id, req in pending_requests.items():
        if req['timestamp'] > datetime.now() - timedelta(minutes=5):  # Only consider recent requests
            request = req
            break
    
    if not request:
        await ctx.send("❌ No active match requests found.", ephemeral=True)
        return
    
    # Notify the user
    user = ctx.guild.get_member(request['user_id'])
    if user:
        try:
            await user.send(f"❌ Your match request was denied by {ctx.channel.name}")
        except:
            pass
    
    await ctx.send("✅ Match request denied.", ephemeral=True)

@bot.command(name='cancel_request', description='Cancel your pending match request')
async def cancel_request(ctx):
    """Cancel your pending match request"""
    user_id = ctx.author.id
    
    if user_id not in pending_requests:
        await ctx.send("❌ You don't have any pending match requests.", ephemeral=True)
        return
    
    request = pending_requests[user_id]
    if request['request_id'] in request_timeouts:
        request_timeouts[request['request_id']].cancel()
        del request_timeouts[request['request_id']]
    
    del pending_requests[user_id]
    await ctx.send("✅ Your match request has been cancelled.", ephemeral=True)

@bot.command(name='kick_lobby', description='Kick a player from your current lobby')
async def kick_lobby(ctx, member: discord.Member):
    """Kick a player from your current lobby (anyone in the lobby can kick anyone)"""
    # Must be used in a lobby channel
    if not ctx.channel.name.startswith('lobby-'):
        await ctx.send("❌ This command can only be used in lobby channels.", ephemeral=True)
        return
    # Both must be in the channel
    if not ctx.channel.permissions_for(ctx.author).read_messages or not ctx.channel.permissions_for(member).read_messages:
        await ctx.send("❌ Both you and the target must be in this lobby.", ephemeral=True)
        return
    # Don't allow kicking yourself
    if ctx.author.id == member.id:
        await ctx.send("❌ You cannot kick yourself.", ephemeral=True)
        return
    # Remove from lobby data
    lobby = active_lobbies.get(ctx.channel.id)
    if lobby and member.id in lobby['players']:
        lobby['players'].remove(member.id)
    if member.id in user_sessions:
        del user_sessions[member.id]
    try:
        await ctx.channel.set_permissions(member, overwrite=None)
        await ctx.channel.send(f"👢 **{member.display_name}** was kicked from the lobby by **{ctx.author.display_name}**.")
        try:
            await member.send(f"❌ You were kicked from the lobby {ctx.channel.mention} by {ctx.author.display_name}.")
        except:
            pass
        await ctx.send(f"✅ {member.display_name} has been kicked from the lobby.", ephemeral=True)
    except Exception as e:
        await ctx.send(f"❌ Error kicking {member.display_name}: {str(e)}", ephemeral=True)

@bot.command(name='help', description='Show all available commands')
async def help_command(ctx):
    """Show all available commands and their descriptions"""
    embed = discord.Embed(
        title="🤖 NightReign Bot Help",
        description="Here are all available commands:",
        color=0x00ff00
    )
    embed.add_field(
        name="Lobby Commands",
        value=(
            "`/create_game` - Create a new NightReign lobby\n"
            "`/my_lobby` - Check your current lobby status\n"
            "`/lobbies` - List all active lobbies\n"
            "`/leave_lobby` - Leave your current lobby\n"
            "`/end_lobby` - End the current lobby (owner/mod/role only)\n"
            "`/invite_lobby @user` - Invite a player to your lobby\n"
            "`/join_lobby <hash>` - Join a lobby using its hash\n"
            "`/find_match` - Find players to join your game\n"
            "`/allow` - Allow a player to join your lobby\n"
            "`/deny` - Deny a player's request to join\n"
            "`/cancel_request` - Cancel your pending match request\n"
            "`/kick_lobby @user` - Kick a player from your current lobby\n"
            "`/lobbyhelp` - Show all available lobby commands\n"
            "`/help` - Show this help message"
        ),
        inline=False
    )
    embed.set_footer(text="Use /<command> for slash commands or type them as text commands.")
    await ctx.send(embed=embed)

@bot.tree.command(name="help", description="Show all available commands")
async def help_slash(interaction: discord.Interaction):
    ctx = await bot.get_context(interaction)
    await help_command(ctx)

# Run the bot
bot.run(os.getenv('DISCORD_TOKEN'))