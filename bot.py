from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks


API_BASE_URL = os.getenv("CBGRAPHS_API_BASE_URL", "https://www.cbgraphs.info").rstrip("/")
API_KEY = os.getenv("CBGRAPHS_API_KEY", "").strip()
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
COMMAND_GUILD_ID = int(os.getenv("DISCORD_GUILD_ID", "0") or 0)
BOT_DB_PATH = os.getenv("CBGRAPHS_BOT_DB", "cbgraphs-bot.sqlite3")
SYNC_INTERVAL_SECONDS = max(300, int(os.getenv("CBGRAPHS_SYNC_INTERVAL_SECONDS", "900")))
SYNC_NICKNAMES = os.getenv("CBGRAPHS_SYNC_NICKNAMES", "true").lower() in {"1", "true", "yes"}
SYNC_ROLES = os.getenv("CBGRAPHS_SYNC_ROLES", "true").lower() in {"1", "true", "yes"}

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cbgraphs-discord")


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class LinkStore:
    """Local bot-only mapping; Discord server ID is intentionally part of the key."""

    def __init__(self, path: str) -> None:
        self.path = path
        with self._connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS player_links (
                    discord_server_id TEXT NOT NULL,
                    discord_user_id TEXT NOT NULL,
                    cb_server_id TEXT NOT NULL,
                    character_id TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (discord_server_id, discord_user_id)
                )"""
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def upsert(self, server_id: int, user_id: int, cb_server: str, character_id: str, created_by: int) -> None:
        with self._connect() as db:
            db.execute(
                """INSERT INTO player_links
                   (discord_server_id, discord_user_id, cb_server_id, character_id, created_by, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(discord_server_id, discord_user_id) DO UPDATE SET
                   cb_server_id=excluded.cb_server_id, character_id=excluded.character_id,
                   created_by=excluded.created_by, created_at=excluded.created_at""",
                (str(server_id), str(user_id), cb_server, character_id, str(created_by), datetime.now(timezone.utc).isoformat()),
            )

    def remove(self, server_id: int, user_id: int) -> bool:
        with self._connect() as db:
            cursor = db.execute("DELETE FROM player_links WHERE discord_server_id=? AND discord_user_id=?", (str(server_id), str(user_id)))
            return cursor.rowcount > 0

    def for_server(self, server_id: int) -> list[sqlite3.Row]:
        with self._connect() as db:
            return db.execute("SELECT * FROM player_links WHERE discord_server_id=? ORDER BY created_at", (str(server_id),)).fetchall()

    def all(self) -> list[sqlite3.Row]:
        with self._connect() as db:
            return db.execute("SELECT * FROM player_links ORDER BY discord_server_id, created_at").fetchall()


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
    embed = discord.Embed(title=str(raw.get("title") or "CBGraphs"), description=str(raw.get("description") or ""), url=raw.get("url"), color=int(raw.get("color") or 0xF5B544))
    for field in raw.get("fields") or []:
        embed.add_field(name=str(field.get("name") or "—"), value=str(field.get("value") or "—"), inline=bool(field.get("inline", False)))
    thumbnail = raw.get("thumbnail") or {}
    if thumbnail.get("url"):
        embed.set_thumbnail(url=str(thumbnail["url"]))
    footer = raw.get("footer") or {}
    if footer.get("text"):
        embed.set_footer(text=str(footer["text"]))
    return embed


def player_snapshot(payload: dict[str, Any]) -> tuple[str, str, str]:
    embed = payload.get("embed") or {}
    rank_value = next((str(field.get("value")) for field in embed.get("fields") or [] if field.get("name") == "Rank"), "Unranked")
    rank_name = rank_value.split(" · ", 1)[0]
    return str(embed.get("title") or "Player"), rank_name, rank_value


async def send_error(interaction: discord.Interaction, error: Exception) -> None:
    if isinstance(error, ApiError) and error.status == 429:
        message = "CBGraphs API rate limitine ulaşıldı. Biraz sonra tekrar deneyin."
    elif isinstance(error, ApiError) and error.status == 404:
        message = "İstenen oyuncu veya grup indekslenmiş verilerde bulunamadı."
    elif isinstance(error, ApiError) and error.status in {401, 403}:
        message = "Bot API key geçersiz veya gerekli scope'a sahip değil."
    else:
        log.warning("Discord command failed: %s", error)
        message = "CBGraphs şu anda yanıt veremedi. Birkaç saniye sonra tekrar deneyin."
    await interaction.followup.send(message, ephemeral=True)


async def require_manager(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        await interaction.response.send_message("Bu komut yalnızca bir Discord sunucusunda kullanılabilir.", ephemeral=True)
        return False
    member = interaction.user if isinstance(interaction.user, discord.Member) else None
    if member is None or not member.guild_permissions.manage_guild:
        await interaction.response.send_message("Bu komut için Manage Server yetkisi gerekir.", ephemeral=True)
        return False
    return True


class CBGraphsBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(command_prefix="!", intents=intents)
        self.api = CBGraphsApi()
        self.links = LinkStore(BOT_DB_PATH)

    async def setup_hook(self) -> None:
        await self.api.start()
        if COMMAND_GUILD_ID:
            guild = discord.Object(id=COMMAND_GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Slash commands synced to guild %s", COMMAND_GUILD_ID)
        else:
            await self.tree.sync()
            log.info("Global slash commands synced")
        self.sync_registered_players.start()

    async def close(self) -> None:
        self.sync_registered_players.cancel()
        await self.api.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Logged in as %s", self.user)

    async def apply_snapshot(self, guild: discord.Guild, member: discord.Member, payload: dict[str, Any]) -> None:
        player_name, rank_name, _ = player_snapshot(payload)
        if SYNC_NICKNAMES and guild.me and guild.me.guild_permissions.manage_nicknames:
            try:
                await member.edit(nick=f"{player_name} · {rank_name}"[:32], reason="CBGraphs player sync")
            except discord.HTTPException as error:
                log.warning("Nickname sync failed in guild %s for user %s: %s", guild.id, member.id, error)
        if not SYNC_ROLES or not guild.me or not guild.me.guild_permissions.manage_roles:
            return
        role_name = f"CB • {rank_name}"[:100]
        role = discord.utils.get(guild.roles, name=role_name)
        if role is None:
            try:
                role = await guild.create_role(name=role_name, reason="CBGraphs rank sync")
            except discord.HTTPException as error:
                log.warning("Role creation failed in guild %s: %s", guild.id, error)
                return
        if not role.is_assignable():
            return
        old_roles = [item for item in member.roles if item.name.startswith("CB • ") and item != role and item.is_assignable()]
        try:
            if old_roles:
                await member.remove_roles(*old_roles, reason="CBGraphs rank sync")
            if role not in member.roles:
                await member.add_roles(role, reason="CBGraphs rank sync")
        except discord.HTTPException as error:
            log.warning("Role sync failed in guild %s for user %s: %s", guild.id, member.id, error)

    async def sync_link(self, guild: discord.Guild, link: sqlite3.Row) -> bool:
        try:
            member = guild.get_member(int(link["discord_user_id"])) or await guild.fetch_member(int(link["discord_user_id"]))
            payload = await self.api.get(f"/api/v1/bot/players/{link['cb_server_id']}/{link['character_id']}")
            await self.apply_snapshot(guild, member, payload)
            return True
        except (ApiError, discord.HTTPException, ValueError) as error:
            log.warning("Registered player sync failed in guild %s: %s", guild.id, error)
            return False

    @tasks.loop(seconds=SYNC_INTERVAL_SECONDS)
    async def sync_registered_players(self) -> None:
        for link in self.links.all():
            guild = self.get_guild(int(link["discord_server_id"]))
            if guild is not None:
                await self.sync_link(guild, link)

    @sync_registered_players.before_loop
    async def before_registered_sync(self) -> None:
        await self.wait_until_ready()


bot = CBGraphsBot()


@bot.tree.command(name="register", description="Register a CB account for a member in this Discord server")
@app_commands.describe(user="Discord member", server="Internal CB server ID, for example 3006", character_id="Conqueror's Blade character ID")
async def register(interaction: discord.Interaction, user: discord.Member, server: str, character_id: str) -> None:
    if not await require_manager(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    server, character_id = server.strip(), character_id.strip()
    if not server.isdigit() or not character_id.isdigit():
        await interaction.followup.send("Server ve character ID yalnızca rakamlardan oluşmalı.", ephemeral=True)
        return
    try:
        payload = await bot.api.get(f"/api/v1/bot/players/{server}/{character_id}")
        bot.links.upsert(interaction.guild_id, user.id, server, character_id, interaction.user.id)
        await bot.apply_snapshot(interaction.guild, user, payload)
        player_name, rank_name, _ = player_snapshot(payload)
        await interaction.followup.send(f"{user.mention} → **{player_name}** ({rank_name}) kaydedildi. Bu kayıt yalnızca bu Discord sunucusuna aittir.", ephemeral=True)
    except Exception as error:
        await send_error(interaction, error)


@bot.tree.command(name="unregister", description="Remove a member's CB account registration from this server")
@app_commands.describe(user="Discord member")
async def unregister(interaction: discord.Interaction, user: discord.Member) -> None:
    if not await require_manager(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    removed = bot.links.remove(interaction.guild_id, user.id)
    await interaction.followup.send("Kayıt kaldırıldı." if removed else "Bu sunucuda kayıt bulunamadı.", ephemeral=True)


@bot.tree.command(name="sync", description="Refresh one registered member's nickname and rank role")
@app_commands.describe(user="Discord member")
async def sync(interaction: discord.Interaction, user: discord.Member) -> None:
    if not await require_manager(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    link = next((item for item in bot.links.for_server(interaction.guild_id) if item["discord_user_id"] == str(user.id)), None)
    if link is None:
        await interaction.followup.send("Bu kullanıcı bu Discord sunucusunda kayıtlı değil.", ephemeral=True)
        return
    message = "güncellendi." if await bot.sync_link(interaction.guild, link) else "güncellenemedi; logları kontrol edin."
    await interaction.followup.send(f"{user.mention} {message}", ephemeral=True)


@bot.tree.command(name="sync-all", description="Refresh all registered members in this Discord server")
async def sync_all(interaction: discord.Interaction) -> None:
    if not await require_manager(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    links = bot.links.for_server(interaction.guild_id)
    succeeded = 0
    for link in links:
        if await bot.sync_link(interaction.guild, link):
            succeeded += 1
    await interaction.followup.send(f"{succeeded}/{len(links)} kayıt güncellendi.", ephemeral=True)


@bot.tree.command(name="registrations", description="List registrations belonging to this Discord server")
async def registrations(interaction: discord.Interaction) -> None:
    if not await require_manager(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    links = bot.links.for_server(interaction.guild_id)
    lines = [f"<@{item['discord_user_id']}> → `{item['cb_server_id']}:{item['character_id']}`" for item in links]
    await interaction.followup.send("\n".join(lines) or "Bu Discord sunucusunda kayıt yok.", ephemeral=True)


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
    app_commands.Choice(name="Rank", value="rank"), app_commands.Choice(name="Level", value="level"),
    app_commands.Choice(name="KDA", value="kda"), app_commands.Choice(name="Win rate", value="win_rate"),
    app_commands.Choice(name="Damage", value="damage"), app_commands.Choice(name="Hero kills", value="hero_kills"),
    app_commands.Choice(name="MVP", value="mvp"), app_commands.Choice(name="Sovereigns", value="sovereigns"),
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
