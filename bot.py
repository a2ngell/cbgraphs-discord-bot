from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands


API_BASE_URL = os.getenv("CBGRAPHS_API_BASE_URL", "https://www.cbgraphs.info").rstrip("/")
API_KEY = os.getenv("CBGRAPHS_API_KEY", "").strip()
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
GUILD_ID = int(os.getenv("DISCORD_GUILD_ID", "0") or 0)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cbgraphs-discord")


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class CBGraphsApi:
    def __init__(self) -> None:
        self.session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        self.session = aiohttp.ClientSession(
            base_url=API_BASE_URL,
            headers={"Accept": "application/json", "X-API-Key": API_KEY},
            timeout=aiohttp.ClientTimeout(total=10),
        )

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()

    async def get(self, path: str, **params: Any) -> dict[str, Any]:
        if self.session is None:
            raise RuntimeError("API client is not ready")
        async with self.session.get(path, params={key: value for key, value in params.items() if value is not None}) as response:
            data = await response.json(content_type=None)
            if response.status >= 400:
                detail = data.get("detail") or data.get("message") or "CBGraphs API request failed."
                raise ApiError(response.status, str(detail))
            return data


def make_embed(raw: dict[str, Any]) -> discord.Embed:
    embed = discord.Embed(
        title=str(raw.get("title") or "CBGraphs"),
        description=str(raw.get("description") or ""),
        url=raw.get("url"),
        color=int(raw.get("color") or 0xF5B544),
    )
    for field in raw.get("fields") or []:
        embed.add_field(
            name=str(field.get("name") or "—"),
            value=str(field.get("value") or "—"),
            inline=bool(field.get("inline", False)),
        )
    thumbnail = raw.get("thumbnail") or {}
    if thumbnail.get("url"):
        embed.set_thumbnail(url=str(thumbnail["url"]))
    footer = raw.get("footer") or {}
    if footer.get("text"):
        embed.set_footer(text=str(footer["text"]))
    return embed


async def send_error(interaction: discord.Interaction, error: Exception) -> None:
    if isinstance(error, ApiError) and error.status == 429:
        message = "CBGraphs API rate limitine ulaşıldı. Biraz sonra tekrar deneyin."
    elif isinstance(error, ApiError) and error.status == 404:
        message = "İstenen oyuncu veya grup indekslenmiş verilerde bulunamadı."
    elif isinstance(error, ApiError) and error.status in {401, 403}:
        message = "Bot API anahtarı geçersiz veya gerekli scope'a sahip değil."
    else:
        log.warning("Discord command failed: %s", error)
        message = "CBGraphs şu anda yanıt veremedi. Birkaç saniye sonra tekrar deneyin."
    await interaction.followup.send(message, ephemeral=True)


class CBGraphsBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=discord.Intents.none())
        self.api = CBGraphsApi()

    async def setup_hook(self) -> None:
        await self.api.start()
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Slash commands synced to guild %s", GUILD_ID)
        else:
            await self.tree.sync()
            log.info("Global slash commands synced")

    async def close(self) -> None:
        await self.api.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Logged in as %s", self.user)


bot = CBGraphsBot()


@bot.tree.command(name="player", description="Show an indexed CBGraphs player profile")
@app_commands.describe(server="Internal server ID, for example 3006", character_id="Conqueror's Blade character ID")
async def player(interaction: discord.Interaction, server: str, character_id: str) -> None:
    await interaction.response.defer()
    try:
        payload = await bot.api.get(f"/api/v1/bot/players/{server.strip()}/{character_id.strip()}")
        await interaction.followup.send(embed=make_embed(payload.get("embed") or {}))
    except Exception as error:
        await send_error(interaction, error)


@bot.tree.command(name="leaderboard", description="Show a CBGraphs leaderboard")
@app_commands.describe(category="Leaderboard metric", server="Internal server ID or all", limit="Number of entries, 1–10")
@app_commands.choices(category=[
    app_commands.Choice(name="Rank", value="rank"),
    app_commands.Choice(name="Level", value="level"),
    app_commands.Choice(name="KDA", value="kda"),
    app_commands.Choice(name="Win rate", value="win_rate"),
    app_commands.Choice(name="Damage", value="damage"),
    app_commands.Choice(name="Hero kills", value="hero_kills"),
    app_commands.Choice(name="MVP", value="mvp"),
    app_commands.Choice(name="Sovereigns", value="sovereigns"),
])
async def leaderboard(interaction: discord.Interaction, category: app_commands.Choice[str], server: str = "all", limit: int = 10) -> None:
    await interaction.response.defer()
    try:
        payload = await bot.api.get("/api/v1/bot/leaderboards", category=category.value, server=server.strip() or "all", limit=max(1, min(limit, 10)))
        await interaction.followup.send(embed=make_embed(payload.get("embed") or {}))
    except Exception as error:
        await send_error(interaction, error)


@bot.tree.command(name="search", description="Search indexed CBGraphs players by name or ID")
@app_commands.describe(query="Player name, ID, or ID#EU1")
async def search(interaction: discord.Interaction, query: str) -> None:
    await interaction.response.defer()
    try:
        payload = await bot.api.get("/api/v1/search/players", q=query.strip(), limit=8)
        items = payload.get("items") or []
        if not items:
            await interaction.followup.send("Oyuncu bulunamadı.", ephemeral=True)
            return
        lines = []
        for item in items:
            server = (item.get("server") or {}).get("short_name") or item.get("server_group_id") or "unverified"
            lines.append(f"**{item.get('name') or item.get('character_id')}** · {server} · `{item.get('character_id')}`")
        embed = discord.Embed(title="CBGraphs · Player search", description="\n".join(lines), color=0xF5B544)
        embed.set_footer(text="Open a result on cbgraphs.info for the full profile.")
        await interaction.followup.send(embed=embed)
    except Exception as error:
        await send_error(interaction, error)


@bot.tree.command(name="house", description="Show an indexed House or Alliance")
@app_commands.describe(name="Exact House or Alliance name", group_type="house or alliance", server="Internal server ID or all")
@app_commands.choices(group_type=[app_commands.Choice(name="House", value="house"), app_commands.Choice(name="Alliance", value="alliance")])
async def house(interaction: discord.Interaction, name: str, group_type: app_commands.Choice[str], server: str = "all") -> None:
    await interaction.response.defer()
    try:
        payload = await bot.api.get("/api/v1/groups/members", type=group_type.value, name=name.strip(), server=server.strip() or "all", sort="rank", page=1, page_size=10)
        summary = payload.get("summary") or {}
        members = payload.get("items") or []
        lines = [f"**{item.get('name') or item.get('character_id')}** · {item.get('server', {}).get('short_name') or item.get('server_group_id')}" for item in members[:10]]
        embed = discord.Embed(title=f"CBGraphs · {name}", description="\n".join(lines) or "No indexed members.", color=0xF5B544)
        embed.add_field(name="Known members", value=str(summary.get("member_count") or len(members)), inline=True)
        embed.add_field(name="Type", value=group_type.name, inline=True)
        await interaction.followup.send(embed=embed)
    except Exception as error:
        await send_error(interaction, error)


@bot.tree.command(name="status", description="Check CBGraphs API health")
async def status(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    try:
        payload = await bot.api.get("/api/v1/status")
        embed = discord.Embed(title="CBGraphs API status", description="API is responding.", color=0x75E0B1)
        embed.add_field(name="Indexed players", value=str(payload.get("directory_players") or "—"), inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as error:
        await send_error(interaction, error)


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("DISCORD_BOT_TOKEN is required")
    if not API_KEY:
        raise SystemExit("CBGRAPHS_API_KEY is required")
    bot.run(DISCORD_TOKEN, log_handler=None)
