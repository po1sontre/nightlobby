# Discord NightReign Lobby Bot

A Discord bot for managing game lobbies for NightReign.

## Setup

1. Install the required dependencies:
```bash
pip install -r requirements.txt
```

2. Create a `.env` file in the root directory with your Discord bot token:
```
DISCORD_TOKEN=your_bot_token_here
```

3. Run the bot:
```bash
python bot.py
```

## Commands

- `/create_game` - Create a new game lobby
- `/my_lobby` - Check your current lobby status
- `/lobbies` - List all active lobbies

## Features

- Create private lobby channels
- Join/leave game sessions
- Automatic cleanup of stale sessions
- Support for up to 3 players per lobby
- Lobby owner controls 