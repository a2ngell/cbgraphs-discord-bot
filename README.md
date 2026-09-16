# CBGraphs Discord test bot

This bot uses only the existing CBGraphs API. It does not call the official game API and it does not access the CBGraphs database directly.

## Required API key

Create a key in the CBGraphs admin panel with these read-only scopes:

- `read:players` for `/player` and `/search`
- `read:leaderboards` for `/leaderboard`
- `read:combat` is not required by the current commands
- `read:status` for `/status`
- `read:players` for `/house`

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

Commands:

- `/player server:3006 character_id:11969813004`
- `/leaderboard category:Rank server:all limit:10`
- `/search query:FrostboundEcho`
- `/house name:WildBlood group_type:House server:3006`
- `/status`

For instant command registration during testing, put the test Discord server ID in `DISCORD_GUILD_ID`. Without it, Discord global command propagation can take time.
