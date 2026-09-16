# CBGraphs Discord bot

CBGraphs Discord bot displays indexed player, leaderboard, clan, and service-status data through Discord commands. It uses the existing CBGraphs API; it does not call the official game API or access the CBGraphs database directly.

Player registrations are scoped to the Discord server where they are created. The same Discord user can therefore be linked to different CB accounts in different servers.

## Requirements

- Python 3.11 or newer
- A CBGraphs API key with read access to the endpoints used by the bot
- A Discord bot token
- `Manage Nicknames` and `Manage Roles` permissions for the bot in the target server

## Configuration

Create the local configuration file from the template:

```bash
cp .env.example .env
```

On Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

Set these values in `.env`:

- `DISCORD_BOT_TOKEN`: the bot token from the Discord Developer Portal.
- `CBGRAPHS_API_BASE_URL`: normally `https://www.cbgraphs.info`.
- `CBGRAPHS_API_KEY`: a bot API key issued by the CBGraphs administrator.
- `CBGRAPHS_BOT_DB`: the local SQLite file used for server-scoped registrations.
- `CBGRAPHS_SYNC_INTERVAL_SECONDS`: registration refresh interval; values below 300 seconds are clamped.
- `CBGRAPHS_SYNC_NICKNAMES`: set to `true` or `false`.
- `CBGRAPHS_SYNC_ROLES`: set to `true` or `false`.

## Run locally

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python bot.py
```

Linux:

```bash
sudo apt-get install -y python3-venv libcairo2
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python bot.py
```

## Commands

Public commands:

- `/player`: show a profile by player name or character ID. Server input accepts `EU1`, `AM1`, `EU4`, or a numeric server ID.
- `/search`: search indexed players by name or ID.
- `/leaderboard`: show Rank, Level, KDA, Win rate, Damage, Hero kills, MVP, or Sovereigns rankings.
- `/clan`: show House or Alliance information.
- `/status`: show CBGraphs API and bot connection status.

For `/player`, `/search`, and `/leaderboard`, choose `Detailed` or `Card` from the Discord `format` menu. No manual format text is required.

Manager commands:

- `/register`: link a Discord member to a CBGraphs player for the current server.
- `/unregister`: remove the member's registration and bot-managed synchronization.
- `/registrations`: list registrations belonging to the current server.
- `/sync`: refresh one registration.
- `/sync-all`: refresh all registrations in the current server.

`/register` verifies the player through CBGraphs before storing the mapping. The background synchronizer periodically refreshes registered players, updates nicknames, and assigns rank roles when enabled.

## Security

- `.env` and the local SQLite database are excluded from version control.
- Never put tokens or API keys in source code, README files, screenshots, or Discord messages.
- Rotate a token immediately through its provider if it may have been exposed.
- Run the bot as a restricted service user instead of root when deploying to Linux.
