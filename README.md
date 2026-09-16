# CBGraphs Discord test bot

This bot uses only the existing CBGraphs API. It does not call the official game API and it does not access the CBGraphs database directly.

Player registrations are isolated per Discord server. The local bot database uses `(discord_server_id, discord_user_id)` as its primary key. The same Discord user can therefore map to a different CB account in another server, and the same CB account can be registered to multiple Discord users.

## Required API key

Create a key in the CBGraphs admin panel with these read-only scopes:

- `read:players` for `/player` and `/search`
- `read:leaderboards` for `/leaderboard`
- `read:combat` is not required by the current commands
- `read:status` for `/status`
- `read:players` for `/house`

The bot itself needs these Discord permissions to apply registrations:

- `Manage Nicknames`
- `Manage Roles`

Its highest role must be above the rank roles it creates. It only changes nicknames and roles inside the Discord server where a manager registered the member.

Never commit `.env` or paste either secret into chat. The Discord bot token belongs in `DISCORD_BOT_TOKEN`; the CBGraphs key belongs in `CBGRAPHS_API_KEY`.

## Run locally

```powershell
cd apps/discord_bot
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
# Fill .env, then:
python bot.py
```

Public commands:

- `/player server:3006 character_id:11969813004`
- `/leaderboard category:Rank server:all limit:10`
- `/search query:FrostboundEcho`
- `/house name:WildBlood group_type:House server:3006`
- `/status`

Manager-only registration commands:

- `/register user:@member server:3006 character_id:11969813004`
- `/unregister user:@member`
- `/registrations`
- `/sync user:@member`
- `/sync-all`

`/register` first verifies the player through the CBGraphs API, then stores the mapping only for the current Discord server. The background synchronizer checks registered players every `CBGRAPHS_SYNC_INTERVAL_SECONDS` (minimum five minutes), updates the member nickname, and assigns a `CB • <rank>` role. No mapping is created by a normal `/player` lookup.

For instant command registration during testing, put the test Discord server ID in `DISCORD_GUILD_ID`. Without it, Discord global command propagation can take time.
