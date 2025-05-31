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
bot = commands.Bot(command_prefix='/', intents=intents)

# In-memory storage for active lobbies
active_lobbies = {}
user_sessions = {}  # Track which users are in active sessions
empty_lobby_timers = {}  # Track empty lobby timers

# Steam friend code pattern (9-10 digits, can be within text)
STEAM_CODE_PATTERN = r'(?:^|\s|:)(\d{9,10})(?:\s|$|\.|,|!|\?)'

class LobbyView(discord.ui.View):
    def __init__(self, owner_id, lobby_channel, lobby_hash):
        super().__init__(timeout=None)  # Persistent view
        self.owner_id = owner_id
        self.lobby_channel = lobby_channel
        self.lobby_hash = lobby_hash
        self.max_players = 3
        
    @discord.ui.button(label='Join Game', style=discord.ButtonStyle.green, emoji='🎮', custom_id='join_game_button')
    async def join_game(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = interaction.user.id
        lobby = active_lobbies.get(self.lobby_channel.id)
        if not lobby:
            await interaction.response.send_message("❌ This lobby no longer exists.", ephemeral=True)
            return
        players = lobby['players']
        await interaction.response.defer(ephemeral=True)
        if user_id in players:
            await interaction.followup.send(
                "❌ You're already in this lobby!",
                ephemeral=True
            )
            return
        if len(players) >= self.max_players:
            await interaction.followup.send(
                "❌ This lobby is full! (3/3 players)",
                ephemeral=True
            )
            return
        players.append(user_id)
        lobby['players'] = players
        user_sessions[user_id] = self.lobby_channel.id
        await self.lobby_channel.set_permissions(
            interaction.user,
            read_messages=True,
            send_messages=True
        )
        await self._update_lobby_message(interaction)
        await self.lobby_channel.send(
            f"🎉 **{interaction.user.display_name}** joined the lobby! "
            f"({len(players)}/{self.max_players} players)"
        )
        await interaction.followup.send(
            f"🎮 You've joined the lobby! Click here to go to the channel: {self.lobby_channel.mention}",
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
        user_id = interaction.user.id
        channel_id = interaction.channel.id
        players = self.get_live_players(channel_id)
        
        # Always defer the interaction immediately
        await interaction.response.defer(ephemeral=True)
        
        # Remove player from all relevant places
        was_in_lobby = user_id in players
        if was_in_lobby:
            players.remove(user_id)
        if user_id in user_sessions:
            del user_sessions[user_id]
        
        # Update active_lobbies
        if channel_id in active_lobbies:
            active_lobbies[channel_id]['players'] = players
            if len(players) == 0:
                del active_lobbies[channel_id]
        
        # Always remove channel permissions
        try:
            overwrite = discord.PermissionOverwrite()
            overwrite.read_messages = False
            await interaction.channel.set_permissions(
                interaction.user,
                overwrite=overwrite
            )
        except discord.Forbidden:
            logger.error(f"Could not remove permissions for user {interaction.user}")
        
        # Update the lobby message in the original channel
        try:
            async for message in interaction.channel.history(limit=10):
                if message.author == bot.user and "NightReign Lobby" in message.embeds[0].title:
                    embed = message.embeds[0]
                    player_list = []
                    for i, player_id in enumerate(players):
                        user = bot.get_user(player_id)
                        if user:
                            crown = "👑" if i == 0 else "🎮"
                            player_list.append(f"{crown} {user.display_name}")
                    for i, field in enumerate(embed.fields):
                        if "Players" in field.name:
                            embed.set_field_at(
                                i,
                                name=f"Players ({len(players)}/3)",
                                value="\n".join(player_list) if player_list else "None",
                                inline=False
                            )
                            break
                    for i, field in enumerate(embed.fields):
                        if "Status" in field.name:
                            if len(players) >= 3:
                                status = "🔴 **LOBBY FULL** - Ready to play!"
                            else:
                                status = f"🟢 **OPEN** - Need {3 - len(players)} more player(s)"
                            embed.set_field_at(i, name="Status", value=status, inline=True)
                            break
                    await message.edit(embed=embed)
                    break
        except Exception as e:
            logger.error(f"Error updating lobby message: {str(e)}")
        
        # If this was the owner leaving, transfer ownership to the next player
        if was_in_lobby and user_id == self.lobby_data['owner'] and players:
            self.lobby_data['owner'] = players[0]
            if channel_id in active_lobbies:
                active_lobbies[channel_id]['owner'] = players[0]
            await interaction.channel.send(
                f"👑 **{bot.get_user(self.lobby_data['owner']).display_name}** is now the lobby owner!"
            )
        
        # Notify in the lobby channel
        if was_in_lobby:
            await interaction.channel.send(
                f"👋 **{interaction.user.display_name}** left the lobby. "
                f"({len(players)}/3 players remaining)"
            )
        else:
            await interaction.channel.send(
                f"👋 **{interaction.user.display_name}** left the channel.",
            )
        
        # If lobby is empty, start timer for deletion
        if len(players) == 0:
            if interaction.channel.id not in empty_lobby_timers:
                empty_lobby_timers[interaction.channel.id] = asyncio.create_task(
                    self._delete_empty_lobby(interaction.channel)
                )
        
        # Confirm to the user
        await interaction.followup.send(
            f"✅ You have left the lobby.",
            ephemeral=True
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
                
    @discord.ui.button(label='Invite Player', style=discord.ButtonStyle.primary, emoji='📨')
    async def invite_player(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Create a modal for entering the username
        class InviteModal(discord.ui.Modal, title='Invite Player'):
            username = discord.ui.TextInput(
                label='User to invite',
                placeholder='@mention, username, or nickname...',
                required=True,
                min_length=2,
                max_length=32
            )

            async def on_submit(self, interaction: discord.Interaction):
                input_str = str(self.username).strip()
                member = None
                # Try mention (e.g., <@1234567890>)
                if input_str.startswith('<@') and input_str.endswith('>'):
                    user_id = input_str.replace('<@', '').replace('!', '').replace('>', '')
                    try:
                        user_id = int(user_id)
                        member = interaction.guild.get_member(user_id)
                    except:
                        pass
                # Try by ID
                if not member and input_str.isdigit():
                    member = interaction.guild.get_member(int(input_str))
                # Try by username or nickname (case-insensitive)
                if not member:
                    for m in interaction.guild.members:
                        if (m.name.lower() == input_str.lower() or
                            (m.nick and m.nick.lower() == input_str.lower())):
                            member = m
                            break
                # Try partial match (username or nickname contains input)
                if not member:
                    for m in interaction.guild.members:
                        if (input_str.lower() in m.name.lower() or
                            (m.nick and input_str.lower() in m.nick.lower())):
                            member = m
                            break
                if not member:
                    await interaction.response.send_message(
                        f"❌ Could not find user '{input_str}' in this server.",
                        ephemeral=True
                    )
                    return

                # Check if user is already in a session
                if member.id in user_sessions:
                    await interaction.response.send_message(
                        f"❌ {member.mention} is already in an active lobby!",
                        ephemeral=True
                    )
                    return

                # Check if user is already in this lobby
                if member.id in self.lobby_data['players']:
                    await interaction.response.send_message(
                        f"❌ {member.mention} is already in this lobby!",
                        ephemeral=True
                    )
                    return

                # Check if lobby is full
                if len(self.lobby_data['players']) >= 3:
                    await interaction.response.send_message(
                        "❌ This lobby is full! (3/3 players)",
                        ephemeral=True
                    )
                    return

                # Add player to lobby
                self.lobby_data['players'].append(member.id)
                user_sessions[member.id] = interaction.channel.id

                # Add user permissions to the lobby channel
                await interaction.channel.set_permissions(
                    member,
                    read_messages=True,
                    send_messages=True
                )

                # Notify in the lobby channel
                await interaction.channel.send(
                    f"🎉 **{member.display_name}** was invited and joined the lobby! "
                    f"({len(self.lobby_data['players'])}/3 players)"
                )

                # Send DM to invited user
                try:
                    invite_embed = discord.Embed(
                        title="🎮 NightReign Lobby Invitation",
                        description=f"You've been invited to join a NightReign lobby by {interaction.user.display_name}!",
                        color=0x00ff00
                    )
                    invite_embed.add_field(
                        name="Lobby Channel",
                        value=f"#{interaction.channel.name}",
                        inline=True
                    )
                    invite_embed.add_field(
                        name="Players",
                        value=f"{len(self.lobby_data['players'])}/3",
                        inline=True
                    )
                    
                    view = discord.ui.View()
                    view.add_item(discord.ui.Button(
                        label="Join Lobby",
                        style=discord.ButtonStyle.green,
                        url=f"https://discord.com/channels/{interaction.guild.id}/{interaction.channel.id}"
                    ))
                    
                    await member.send(embed=invite_embed, view=view)
                except:
                    await interaction.channel.send(
                        f"⚠️ Could not send DM to {member.mention}. They may have DMs disabled."
                    )

                await interaction.response.send_message(
                    f"✅ Successfully invited {member.mention} to the lobby!",
                    ephemeral=True
                )

        # Show the invite modal
        modal = InviteModal()
        modal.lobby_data = self.lobby_data
        await interaction.response.send_modal(modal)

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
        view = LobbyView(lobby_data['owner'], self.lobby_channel, lobby_data['hash'])
        await view.join_game(interaction, button)

@bot.event
async def on_ready():
    print(f'{bot.user} has connected to Discord!')
    active_lobbies.clear()
    user_sessions.clear()
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
    cleanup_inactive_lobbies.start()

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
            # Send the Join Game button in the original channel (not persistent)
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
            join_embed.set_footer(text="Click 'Join Game' to join this lobby or use the Quick Join command!")
            view = LobbyView(lobby_data['owner'], lobby_channel, lobby_hash)
            msg = await (message.channel.send if hasattr(message, 'channel') else ctx.send)(embed=join_embed, view=view)
            bot.add_view(view, message_id=msg.id)
            lobby_data['join_message_id'] = msg.id
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
                f"Click here to go to your lobby: {lobby_channel.mention}"
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
        # Send the Join Game button in the original channel (not persistent)
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
        join_embed.set_footer(text="Click 'Join Game' to join this lobby or use the Quick Join command!")
        view = LobbyView(lobby_data['owner'], lobby_channel, lobby_hash)
        msg = await ctx.send(embed=join_embed, view=view)
        bot.add_view(view, message_id=msg.id)
        lobby_data['join_message_id'] = msg.id
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
    """List all active lobbies with accurate player stats and join buttons"""
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
        if not channel:
            continue
        owner = bot.get_user(lobby_data['owner'])
        owner_name = owner.display_name if owner else "Unknown"
        # Get player list, excluding owner, mods, and bots
        player_list = []
        real_player_count = 0
        for player_id in lobby_data['players']:
            member = channel.guild.get_member(player_id)
            if member and not member.bot and not member.guild_permissions.administrator and not member.guild_permissions.manage_channels:
                player_list.append(member.display_name)
                real_player_count += 1
        # Create player count string
        player_count = f"{real_player_count}/3"
        status = "🔴 FULL" if real_player_count >= 3 else "🟢 OPEN"
        embed.add_field(
            name=f"#{channel.name}",
            value=(
                f"👑 Owner: {owner_name}\n"
                f"👥 Players: {player_count} {status}\n"
                f"🎮 Players: {', '.join(player_list) if player_list else 'None'}"
            ),
            inline=False
        )
        # Add a fresh join button for this lobby
        view = discord.ui.View()
        view.add_item(discord.ui.Button(
            label="Join Lobby",
            style=discord.ButtonStyle.green,
            url=f"https://discord.com/channels/{ctx.guild.id}/{channel.id}"
        ))
        await ctx.send(embed=embed, view=view)
        embed = discord.Embed()  # Reset for next lobby

@tasks.loop(minutes=5)
async def cleanup_inactive_lobbies():
    now = datetime.utcnow()
    to_delete = []
    for channel_id, lobby_data in list(active_lobbies.items()):
        channel = bot.get_channel(channel_id)
        if not channel:
            continue
        try:
            last_message = None
            async for msg in channel.history(limit=1, oldest_first=False):
                last_message = msg
                break
            if last_message:
                last_time = last_message.created_at.replace(tzinfo=None)
                if (now - last_time) > timedelta(hours=3):
                    to_delete.append(channel_id)
            else:
                # No messages at all, use creation time
                if (now - lobby_data['created_at'].replace(tzinfo=None)) > timedelta(hours=3):
                    to_delete.append(channel_id)
        except Exception as e:
            logger.error(f"Error checking inactivity for channel {channel_id}: {e}")
    for channel_id in to_delete:
        channel = bot.get_channel(channel_id)
        if channel:
            try:
                await channel.delete(reason="Inactive lobby (3h no messages)")
            except Exception as e:
                logger.error(f"Error deleting inactive lobby channel {channel_id}: {e}")
        if channel_id in active_lobbies:
            del active_lobbies[channel_id]
        # Remove all user_sessions for this channel
        for uid in list(user_sessions):
            if user_sessions[uid] == channel_id:
                del user_sessions[uid]

@bot.command(name='invite')
async def invite_player(ctx, member: discord.Member):
    """Invite a player to your current lobby"""
    user_id = ctx.author.id
    
    # Check if the inviter is in a lobby
    if user_id not in user_sessions:
        await ctx.send("❌ You're not in any lobby! Create or join a lobby first.", ephemeral=True)
        return
        
    channel_id = user_sessions[user_id]
    lobby_channel = bot.get_channel(channel_id)
    
    if not lobby_channel:
        del user_sessions[user_id]
        await ctx.send("❌ Your lobby channel no longer exists.", ephemeral=True)
        return
        
    # Get the lobby data
    lobby_data = active_lobbies.get(channel_id)
    if not lobby_data:
        await ctx.send("❌ Lobby data not found.", ephemeral=True)
        return
        
    # Check if user is already in a session
    if member.id in user_sessions:
        await ctx.send(
            f"❌ {member.mention} is already in an active lobby!",
            ephemeral=True
        )
        return

    # Check if user is already in this lobby
    if member.id in lobby_data['players']:
        await ctx.send(
            f"❌ {member.mention} is already in this lobby!",
            ephemeral=True
        )
        return

    # Check if lobby is full
    if len(lobby_data['players']) >= 3:
        await ctx.send(
            "❌ This lobby is full! (3/3 players)",
            ephemeral=True
        )
        return

    # Add player to lobby
    lobby_data['players'].append(member.id)
    user_sessions[member.id] = channel_id

    # Add user permissions to the lobby channel
    await lobby_channel.set_permissions(
        member,
        read_messages=True,
        send_messages=True
    )

    # Notify in the lobby channel
    await lobby_channel.send(
        f"🎉 **{member.display_name}** was invited and joined the lobby! "
        f"({len(lobby_data['players'])}/3 players)"
    )

    # Send DM to invited user
    try:
        invite_embed = discord.Embed(
            title="🎮 NightReign Lobby Invitation",
            description=f"You've been invited to join a NightReign lobby by {ctx.author.display_name}!",
            color=0x00ff00
        )
        invite_embed.add_field(
            name="Lobby Channel",
            value=f"#{lobby_channel.name}",
            inline=True
        )
        invite_embed.add_field(
            name="Players",
            value=f"{len(lobby_data['players'])}/3",
            inline=True
        )
        
        view = discord.ui.View()
        view.add_item(discord.ui.Button(
            label="Join Lobby",
            style=discord.ButtonStyle.green,
            url=f"https://discord.com/channels/{ctx.guild.id}/{lobby_channel.id}"
        ))
        
        await member.send(embed=invite_embed, view=view)
    except:
        await lobby_channel.send(
            f"⚠️ Could not send DM to {member.mention}. They may have DMs disabled."
        )

    await ctx.send(
        f"✅ Successfully invited {member.mention} to the lobby!",
        ephemeral=True
    )

@bot.command(name='lobbyhelp')
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
            "`/invite @user` - Invite a player to your current lobby"
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
            "• Use `/invite @user` to invite friends directly\n"
            "• Lobbies auto-delete after 5 minutes of being empty"
        ),
        inline=False
    )
    
    embed.set_footer(text="Need more help? Contact a moderator!")
    
    await ctx.send(embed=embed)

@bot.command(name='join_lobby')
async def join_lobby(ctx, lobby_hash: str):
    """Join a lobby by its hash."""
    for lobby in active_lobbies.values():
        if lobby['hash'] == lobby_hash:
            channel = bot.get_channel(lobby['channel'])
            if not channel:
                await ctx.send("❌ That lobby no longer exists.")
                return
            if ctx.author.id in lobby['players']:
                await ctx.send("❌ You are already in this lobby.")
                return
            if len(lobby['players']) >= 3:
                await ctx.send("❌ This lobby is full! (3/3 players)")
                return
            lobby['players'].append(ctx.author.id)
            user_sessions[ctx.author.id] = channel.id
            await channel.set_permissions(ctx.author, read_messages=True, send_messages=True)
            await channel.send(f"🎉 **{ctx.author.display_name}** joined the lobby! ({len(lobby['players'])}/3 players)")
            await ctx.send(f"🎮 You've joined the lobby! Click here to go to the channel: {channel.mention}")
            # Update the original Join Game embed
            join_msg_id = lobby.get('join_message_id')
            if join_msg_id:
                for guild in bot.guilds:
                    for ch in guild.text_channels:
                        try:
                            msg = await ch.fetch_message(join_msg_id)
                            # Build updated embed
                            embed = discord.Embed(
                                title="🕹️ NightReign Lobby",
                                color=0x00ff00 if len(lobby['players']) < 3 else 0xff0000,
                                timestamp=datetime.now()
                            )
                            player_list = []
                            for i, player_id in enumerate(lobby['players']):
                                user = bot.get_user(player_id)
                                if user:
                                    crown = "👑" if i == 0 else "🎮"
                                    player_list.append(f"{crown} {user.display_name}")
                            embed.add_field(
                                name=f"Players ({len(lobby['players'])}/3)",
                                value="\n".join(player_list) if player_list else "None",
                                inline=False
                            )
                            embed.add_field(
                                name="Lobby Channel",
                                value=f"{channel.mention}",
                                inline=True
                            )
                            if len(lobby['players']) >= 3:
                                status = "🔴 **LOBBY FULL** - Ready to play!"
                            else:
                                status = f"🟢 **OPEN** - Need {3 - len(lobby['players'])} more player(s)"
                            embed.add_field(
                                name="Status",
                                value=status,
                                inline=True
                            )
                            embed.add_field(
                                name="How to Join",
                                value=f"**To join this lobby, copy and paste the command below:**\n```/join_lobby {lobby['hash']}```",
                                inline=False
                            )
                            embed.set_footer(text="Click 'Join Game' to join this lobby or use the Quick Join command!")
                            # Update the view/button state
                            view = LobbyView(lobby['owner'], channel, lobby['hash'])
                            await msg.edit(embed=embed, view=view)
                            return
                        except Exception:
                            continue
            return
    await ctx.send("❌ No lobby found with that hash.")

# Run the bot
bot.run(os.getenv('DISCORD_TOKEN'))