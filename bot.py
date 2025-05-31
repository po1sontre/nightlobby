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
            # Send welcome message in lobby channel
            welcome_embed = discord.Embed(
                title="🎉 Welcome to your NightReign Lobby!",
                description=f"Your Steam friend code: {steam_codes[0] if 'steam_codes' in locals() and steam_codes else ''}\n\nDrop your Steam friend codes here and plan your game.\n\n📢 Check #nightreign-online to get everything working!",
                color=0x00ff00
            )
            welcome_embed.add_field(
                name="📋 Instructions",
                value=(
                    "• Share your Steam friend codes\n"
                    "• Coordinate your game time\n"
                    "• To leave, type `/leave_lobby`\n"
                    "• To invite, type `/invite_lobby @user`\n"
                    "• To end the session, type `/end_lobby` (owner only)"
                ),
                inline=False
            )
            await lobby_channel.send(embed=welcome_embed)
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
        # Send welcome message in lobby channel
        welcome_embed = discord.Embed(
            title="🎉 Welcome to your NightReign Lobby!",
            description=f"Your Steam friend code: {steam_codes[0] if 'steam_codes' in locals() and steam_codes else ''}\n\nDrop your Steam friend codes here and plan your game.\n\n📢 Check #nightreign-online to get everything working!",
            color=0x00ff00
        )
        welcome_embed.add_field(
            name="📋 Instructions",
            value=(
                "• Share your Steam friend codes\n"
                "• Coordinate your game time\n"
                "• To leave, type `/leave_lobby`\n"
                "• To invite, type `/invite_lobby @user`\n"
                "• To end the session, type `/end_lobby` (owner only)"
            ),
            inline=False
        )
        await lobby_channel.send(embed=welcome_embed)
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
    """Join a lobby by its hash. 3-person limit for new lobbies, unlimited for old/untracked."""
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
                await ctx.send("❌ This lobby is full! (3/3 players)")
                return
            lobby['players'].append(ctx.author.id)
            user_sessions[ctx.author.id] = channel.id
            await channel.set_permissions(ctx.author, read_messages=True, send_messages=True)
            await channel.send(f"🎉 **{ctx.author.display_name}** joined the lobby! ({len(lobby['players'])}/3 players)")
            await ctx.send(f"🎮 You've joined the lobby! Click here to go to the channel: {channel.mention}")
            # (embed update code omitted for brevity)
            return
    # If not found in active_lobbies, search all text channels for the hash
    for guild in bot.guilds:
        for channel in guild.text_channels:
            try:
                async for message in channel.history(limit=20):
                    if message.author == bot.user and message.content and message.content.lower().startswith('lobby hash:'):
                        if input_hash in message.content.lower():
                            # Found the hash in this channel, allow unlimited joins
                            await channel.set_permissions(ctx.author, read_messages=True, send_messages=True)
                            await channel.send(f"🎉 **{ctx.author.display_name}** joined the lobby! (player count unknown)")
                            await ctx.send(f"🎮 You've joined the lobby! Click here to go to the channel: {channel.mention}")
                            return
            except Exception:
                continue
    print(f"[DEBUG] No lobby found for hash: '{input_hash}'.")
    await ctx.send("❌ No lobby found with that hash.")

# Run the bot
bot.run(os.getenv('DISCORD_TOKEN'))