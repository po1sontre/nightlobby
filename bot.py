import discord
from discord.ext import commands, tasks
import asyncio
from datetime import datetime, timedelta
import logging
import os
from dotenv import load_dotenv
import re

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
bot = commands.Bot(command_prefix='/', intents=intents)

# In-memory storage for active lobbies
active_lobbies = {}
user_sessions = {}  # Track which users are in active sessions
empty_lobby_timers = {}  # Track empty lobby timers

# Steam friend code pattern (9-10 digits, can be within text)
STEAM_CODE_PATTERN = r'(?:^|\s|:)(\d{9,10})(?:\s|$|\.|,|!|\?)'

class LobbyView(discord.ui.View):
    def __init__(self, owner_id, lobby_channel):
        super().__init__(timeout=1800)  # 30 minute timeout
        self.owner_id = owner_id
        self.lobby_channel = lobby_channel
        self.players = [owner_id]
        self.max_players = 3
        
    @discord.ui.button(label='Join Game', style=discord.ButtonStyle.green, emoji='🎮')
    async def join_game(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = interaction.user.id
        
        # Check if user is already in a session
        if user_id in user_sessions:
            # Verify if the session still exists
            channel_id = user_sessions[user_id]
            channel = bot.get_channel(channel_id)
            if not channel:
                # Clean up stale session
                del user_sessions[user_id]
            else:
                await interaction.response.send_message(
                    "❌ You're already in an active lobby! Leave your current session first.",
                    ephemeral=True
                )
                return
            
        # Check if user is already in this lobby
        if user_id in self.players:
            await interaction.response.send_message(
                "❌ You're already in this lobby!",
                ephemeral=True
            )
            return
            
        # Check if lobby is full
        if len(self.players) >= self.max_players:
            await interaction.response.send_message(
                "❌ This lobby is full! (3/3 players)",
                ephemeral=True
            )
            return
            
        # Add player to lobby
        self.players.append(user_id)
        user_sessions[user_id] = self.lobby_channel.id
        
        # Add user permissions to the lobby channel
        await self.lobby_channel.set_permissions(
            interaction.user,
            read_messages=True,
            send_messages=True
        )
        
        # Update the embed and button state
        await self._update_lobby_message(interaction)
        
        # Notify in the lobby channel
        await self.lobby_channel.send(
            f"🎉 **{interaction.user.display_name}** joined the lobby! "
            f"({len(self.players)}/{self.max_players} players)"
        )
        
        # Send message to user with channel link
        await interaction.response.send_message(
            f"🎮 You've joined the lobby! Click here to go to the channel: {self.lobby_channel.mention}",
            ephemeral=True
        )

    async def _update_lobby_message(self, interaction):
        # Create updated embed
        embed = discord.Embed(
            title="🕹️ NightReign Lobby",
            color=0x00ff00 if len(self.players) < self.max_players else 0xff0000,
            timestamp=datetime.now()
        )
        
        # Add players field
        player_list = []
        for i, player_id in enumerate(self.players):
            user = bot.get_user(player_id)
            if user:
                crown = "👑" if i == 0 else "🎮"
                player_list.append(f"{crown} {user.display_name}")
        
        embed.add_field(
            name=f"Players ({len(self.players)}/{self.max_players})",
            value="\n".join(player_list) if player_list else "None",
            inline=False
        )
        
        embed.add_field(
            name="Lobby Channel",
            value=f"#{self.lobby_channel.name}",
            inline=True
        )
        
        # Update button state if lobby is full
        if len(self.players) >= self.max_players:
            self.join_game.disabled = True
            self.join_game.style = discord.ButtonStyle.red
            self.join_game.label = "Lobby Full"
            embed.add_field(
                name="Status",
                value="🔴 **LOBBY FULL** - Ready to play!",
                inline=True
            )
        else:
            embed.add_field(
                name="Status",
                value=f"🟢 **OPEN** - Need {self.max_players - len(self.players)} more player(s)",
                inline=True
            )
        
        await interaction.response.edit_message(embed=embed, view=self)

class LobbyChannelView(discord.ui.View):
    def __init__(self, lobby_data):
        super().__init__(timeout=None)
        self.lobby_data = lobby_data
        
    @discord.ui.button(label='Leave Lobby', style=discord.ButtonStyle.red, emoji='🚪')
    async def leave_lobby(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = interaction.user.id
        
        if user_id not in self.lobby_data['players']:
            await interaction.response.send_message(
                "❌ You're not in this lobby!",
                ephemeral=True
            )
            return
            
        # Remove player from lobby
        self.lobby_data['players'].remove(user_id)
        if user_id in user_sessions:
            del user_sessions[user_id]
            
        # Remove channel permissions
        await interaction.channel.set_permissions(
            interaction.user,
            overwrite=None
        )
        
        await interaction.response.send_message(
            f"👋 **{interaction.user.display_name}** left the lobby. "
            f"({len(self.lobby_data['players'])}/3 players remaining)"
        )
        
        # If lobby is empty, start timer for deletion
        if len(self.lobby_data['players']) == 0:
            if interaction.channel.id not in empty_lobby_timers:
                empty_lobby_timers[interaction.channel.id] = asyncio.create_task(
                    self._delete_empty_lobby(interaction.channel)
                )
                
    async def _delete_empty_lobby(self, channel):
        await asyncio.sleep(300)  # 5 minutes
        if channel.id in active_lobbies and len(active_lobbies[channel.id]['players']) == 0:
            try:
                await channel.delete(reason="Empty lobby timeout")
            except:
                pass
            if channel.id in active_lobbies:
                del active_lobbies[channel.id]
            if channel.id in empty_lobby_timers:
                del empty_lobby_timers[channel.id]
                
    @discord.ui.button(label='End Session', style=discord.ButtonStyle.gray, emoji='🏁')
    async def end_session(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Check if user is owner or has admin/mod permissions
        if interaction.user.id != self.lobby_data['owner']:
            # Check for admin/mod permissions
            if not interaction.user.guild_permissions.administrator and not interaction.user.guild_permissions.manage_channels:
                await interaction.response.send_message(
                    "❌ Only the lobby owner or moderators can end the session!",
                    ephemeral=True
                )
                return
            
        await interaction.response.send_message(
            "🏁 **Session ended by lobby owner.** Channel will be deleted in 10 seconds...\n"
            "GG everyone! 🎮"
        )
        
        # Clean up user sessions
        for player_id in self.lobby_data['players']:
            if player_id in user_sessions:
                del user_sessions[player_id]
                
        # Clean up lobby data
        if interaction.channel.id in active_lobbies:
            del active_lobbies[interaction.channel.id]
            
        await asyncio.sleep(10)
        await interaction.channel.delete(reason="Session ended by owner")

class LobbyListButton(discord.ui.View):
    def __init__(self, lobby_channel):
        super().__init__(timeout=None)
        self.lobby_channel = lobby_channel
        
    @discord.ui.button(label='Join Lobby', style=discord.ButtonStyle.green, emoji='🎮')
    async def join_lobby(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Get the lobby data
        lobby_data = active_lobbies.get(self.lobby_channel.id)
        if not lobby_data:
            await interaction.response.send_message("❌ This lobby no longer exists.", ephemeral=True)
            return
            
        # Create a temporary view to handle the join
        view = LobbyView(lobby_data['owner'], self.lobby_channel)
        await view.join_game(interaction, button)

@bot.event
async def on_ready():
    print(f'{bot.user} has connected to Discord!')
    cleanup_stale_sessions.start()

@bot.event
async def on_message(message):
    # Don't respond to our own messages
    if message.author == bot.user:
        return

    # Check if the message contains a Steam friend code
    steam_codes = re.findall(STEAM_CODE_PATTERN, message.content)
    
    if steam_codes:
        logger.info(f"Detected Steam code(s) in message from {message.author}: {steam_codes}")
        
        # Check if user is already in a session
        if message.author.id in user_sessions:
            await message.channel.send(
                f"❌ {message.author.mention} You're already in an active lobby! "
                f"Please leave your current session first before creating a new one."
            )
            return

        # Create a new lobby for the user
        try:
            # Create private lobby channel
            overwrites = {
                message.guild.default_role: discord.PermissionOverwrite(read_messages=False),
                message.author: discord.PermissionOverwrite(read_messages=True, send_messages=True),
                bot.user: discord.PermissionOverwrite(read_messages=True, send_messages=True)
            }
            
            channel_name = f"lobby-{message.author.display_name.lower()}-{datetime.now().strftime('%H%M')}"
            lobby_channel = await message.guild.create_text_channel(
                channel_name,
                overwrites=overwrites,
                category=None,
                reason=f"NightReign lobby created by {message.author}"
            )
            
            logger.info(f"Created new lobby channel {channel_name} for user {message.author}")
            
            # Store lobby data
            lobby_data = {
                'owner': message.author.id,
                'players': [message.author.id],
                'channel': lobby_channel.id,
                'created_at': datetime.now()
            }
            active_lobbies[lobby_channel.id] = lobby_data
            user_sessions[message.author.id] = lobby_channel.id
            
            # Create and send lobby embed
            embed = discord.Embed(
                title="🕹️ NightReign Lobby",
                color=0x00ff00,
                timestamp=datetime.now()
            )
            
            embed.add_field(
                name="Players (1/3)",
                value=f"👑 {message.author.display_name}",
                inline=False
            )
            
            embed.add_field(
                name="Lobby Channel",
                value=f"#{lobby_channel.name}",
                inline=True
            )
            
            embed.add_field(
                name="Status",
                value="🟢 **OPEN** - Need 2 more players",
                inline=True
            )
            
            embed.set_footer(text="Click 'Join Game' to join this lobby!")
            
            view = LobbyView(message.author.id, lobby_channel)
            await message.channel.send(embed=embed, view=view)
            
            # Send welcome message in lobby channel
            lobby_view = LobbyChannelView(lobby_data)
            welcome_embed = discord.Embed(
                title="🎉 Welcome to your NightReign Lobby!",
                description=f"Your Steam friend code: {steam_codes[0]}\n\n"
                           f"Drop your Steam friend codes here and plan your game.\n\n"
                           f"📢 Check #nightreign-online to get everything working!",
                color=0x00ff00
            )
            welcome_embed.add_field(
                name="📋 Instructions",
                value="• Share your Steam friend codes\n• Coordinate your game time\n• Use 'Leave Lobby' to exit\n• Owner can 'End Session' to close the lobby",
                inline=False
            )
            
            await lobby_channel.send(embed=welcome_embed, view=lobby_view)
            
            # Notify the user
            await message.channel.send(
                f"🎮 {message.author.mention} I've created a lobby for you! "
                f"Click the 'Join Game' button above to join the lobby, or use this link: {lobby_channel.mention}"
            )
            
        except discord.Forbidden:
            logger.error(f"Permission error creating channel for user {message.author}")
            await message.channel.send("❌ I don't have permission to create channels!")
        except Exception as e:
            logger.error(f"Error creating lobby for user {message.author}: {str(e)}")
            await message.channel.send(f"❌ Error creating lobby: {str(e)}")

    # Process commands after checking for Steam codes
    await bot.process_commands(message)

@bot.command(name='create_game')
async def create_game(ctx):
    """Create a new NightReign lobby"""
    user_id = ctx.author.id
    
    # Check if user already has an active session
    if user_id in user_sessions:
        await ctx.send("❌ You already have an active lobby! Leave it first before creating a new one.", ephemeral=True)
        return
        
    try:
        # Create private lobby channel
        overwrites = {
            ctx.guild.default_role: discord.PermissionOverwrite(read_messages=False),
            ctx.author: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            bot.user: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }
        
        channel_name = f"lobby-{ctx.author.display_name.lower()}-{datetime.now().strftime('%H%M')}"
        lobby_channel = await ctx.guild.create_text_channel(
            channel_name,
            overwrites=overwrites,
            category=None,  # You can set a specific category if needed
            reason=f"NightReign lobby created by {ctx.author}"
        )
        
        # Store lobby data
        lobby_data = {
            'owner': user_id,
            'players': [user_id],
            'channel': lobby_channel.id,
            'created_at': datetime.now()
        }
        active_lobbies[lobby_channel.id] = lobby_data
        user_sessions[user_id] = lobby_channel.id
        
        # Create and send lobby embed in the original channel
        embed = discord.Embed(
            title="🕹️ NightReign Lobby",
            color=0x00ff00,
            timestamp=datetime.now()
        )
        
        embed.add_field(
            name="Players (1/3)",
            value=f"👑 {ctx.author.display_name}",
            inline=False
        )
        
        embed.add_field(
            name="Lobby Channel",
            value=f"#{lobby_channel.name}",
            inline=True
        )
        
        embed.add_field(
            name="Status",
            value="🟢 **OPEN** - Need 2 more players",
            inline=True
        )
        
        embed.set_footer(text="Click 'Join Game' to join this lobby!")
        
        view = LobbyView(user_id, lobby_channel)
        await ctx.send(embed=embed, view=view)
        
        # Send welcome message in lobby channel
        lobby_view = LobbyChannelView(lobby_data)
        welcome_embed = discord.Embed(
            title="🎉 Welcome to your NightReign Lobby!",
            description="Drop your Steam friend codes here and plan your game.",
            color=0x00ff00
        )
        welcome_embed.add_field(
            name="📋 Instructions",
            value="• Share your Steam friend codes\n• Coordinate your game time\n• Use 'Leave Lobby' to exit\n• Owner can 'End Session' to close the lobby",
            inline=False
        )
        
        await lobby_channel.send(embed=welcome_embed, view=lobby_view)
        
    except discord.Forbidden:
        await ctx.send("❌ I don't have permission to create channels!")
    except Exception as e:
        await ctx.send(f"❌ Error creating lobby: {str(e)}")

@bot.command(name='my_lobby')
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
        
    await ctx.send(f"🎮 Your active lobby: {lobby_channel.mention}")

@bot.command(name='lobbies')
async def list_lobbies(ctx):
    """List all active lobbies"""
    if not active_lobbies:
        await ctx.send("🔍 No active lobbies found.")
        return
        
    embed = discord.Embed(
        title="🕹️ Active NightReign Lobbies",
        color=0x00ff00,
        timestamp=datetime.now()
    )
    
    for channel_id, lobby_data in active_lobbies.items():
        channel = bot.get_channel(channel_id)
        if channel:
            owner = bot.get_user(lobby_data['owner'])
            owner_name = owner.display_name if owner else "Unknown"
            
            embed.add_field(
                name=f"#{channel.name}",
                value=f"👑 Owner: {owner_name}\n👥 Players: {len(lobby_data['players'])}/3",
                inline=True
            )
            
            # Add join button for each lobby
            view = LobbyListButton(channel)
            await ctx.send(embed=embed, view=view)
            return  # Send only the first lobby for now (we can modify this to show all)
    
    await ctx.send(embed=embed)

@tasks.loop(minutes=5)
async def cleanup_stale_sessions():
    """Clean up stale sessions every 5 minutes"""
    current_time = datetime.now()
    stale_lobbies = []
    
    for channel_id, lobby_data in active_lobbies.items():
        # Remove lobbies older than 2 hours
        if current_time - lobby_data['created_at'] > timedelta(hours=2):
            stale_lobbies.append(channel_id)
            
    for channel_id in stale_lobbies:
        channel = bot.get_channel(channel_id)
        if channel:
            try:
                await channel.delete(reason="Stale lobby cleanup")
            except:
                pass
                
        # Clean up data
        lobby_data = active_lobbies.get(channel_id, {})
        for player_id in lobby_data.get('players', []):
            if player_id in user_sessions:
                del user_sessions[player_id]
                
        if channel_id in active_lobbies:
            del active_lobbies[channel_id]

# Run the bot
bot.run(os.getenv('DISCORD_TOKEN'))