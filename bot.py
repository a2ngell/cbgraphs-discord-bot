from __future__ import annotations

import asyncio
import logging
import io
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import Any

import aiohttp
import discord
from dotenv import load_dotenv
from discord import app_commands
from discord.ext import commands, tasks

try:
    import cairosvg
except (ImportError, OSError):
    cairosvg = None


load_dotenv()

API_BASE_URL = os.getenv("CBGRAPHS_API_BASE_URL", "https://www.cbgraphs.info").rstrip("/")
API_KEY = os.getenv("CBGRAPHS_API_KEY", "").strip()
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
BOT_DB_PATH = os.getenv("CBGRAPHS_BOT_DB", "cbgraphs-bot.sqlite3")
SYNC_INTERVAL_SECONDS = max(300, int(os.getenv("CBGRAPHS_SYNC_INTERVAL_SECONDS", "900")))
SYNC_NICKNAMES = os.getenv("CBGRAPHS_SYNC_NICKNAMES", "true").lower() in {"1", "true", "yes"}
SYNC_ROLES = os.getenv("CBGRAPHS_SYNC_ROLES", "true").lower() in {"1", "true", "yes"}

SERVER_ALIASES = {
    "EU1": "3006",
    "AM1": "3014",
    "EU4": "3021",
}

RANK_ROLE_COLORS = {
    "fresh_blood": 0x4CAF50,
    "rookie": 0x43A047,
    "contender": 0x2196F3,
    "brawler": 0x1976D2,
    "fighter": 0x7E57C2,
    "killer": 0x5E35B1,
    "gladiator": 0x26A69A,
    "arena_hero": 0xF2C14E,
    "imperial_hero": 0xFFD54F,
    "grand_champion": 0xFF4D5D,
}

RANK_ROLE_ICONS = {
    "fresh_blood": "🟢",
    "rookie": "🔵",
    "contender": "🟣",
    "brawler": "🟠",
    "fighter": "🔴",
    "killer": "⚔️",
    "gladiator": "🛡️",
    "arena_hero": "🏅",
    "imperial_hero": "👑",
    "grand_champion": "🏆",
}

RANK_DISPLAY_NAMES = {key.replace("_", " ").title() for key in RANK_ROLE_COLORS}

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cbgraphs-discord")


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class PlayerLookupError(RuntimeError):
    pass


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
            timeout=aiohttp.ClientTimeout(total=30),
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

    async def get_bytes(self, path: str) -> bytes:
        if self.session is None:
            raise RuntimeError("API client is not ready")
        async with self.session.get(path, headers={"Accept": "image/svg+xml"}) as response:
            body = await response.read()
            if response.status >= 400:
                raise ApiError(response.status, "CBGraphs card request failed.")
            return body

    async def get_png(self, path: str) -> bytes | None:
        # Render the exact SVG returned by CBGraphs. Do not rewrite remote image
        # hrefs here: doing so made Discord's PNG differ from the source card.
        svg = await self.get_bytes(path)
        # CairoSVG does not always select the same fallback font as Chromium for
        # full-width/CJK player names. Noto CJK is used on Linux and Malgun
        # Gothic remains the Windows fallback.
        svg = svg.replace(
            b"font-family=\"Inter,Segoe UI,Arial,sans-serif\"",
            b"font-family=\"Noto Sans CJK JP,Malgun Gothic,Inter,Segoe UI,Arial,sans-serif\"",
        )
        global cairosvg
        if cairosvg is None:
            try:
                import cairosvg as renderer
                cairosvg = renderer
            except (ImportError, OSError) as error:
                log.error("PNG card renderer unavailable: %s", error)
                return None
        try:
            return await asyncio.to_thread(cairosvg.svg2png, bytestring=svg, output_width=1200)
        except Exception as error:
            log.exception("PNG card conversion failed: %s", error)
            return None


async def send_card(interaction: discord.Interaction, *, path: str, url: str, title: str, description: str, color: int) -> None:
    """Send the existing CBGraphs SVG card rendered as a Discord-compatible PNG."""
    body = await bot.api.get_png(path)
    if body is None:
        await interaction.followup.send(f"[Open CBGraphs card]({url})\nCard preview renderer is unavailable on this bot host.")
        return
    embed = discord.Embed(title=title, url=url, description=description, color=color)
    filename = "cbgraphs-card.png"
    embed.set_image(url=f"attachment://{filename}")
    await interaction.followup.send(
        content=f"[Open CBGraphs card]({url})",
        embed=embed,
        file=discord.File(io.BytesIO(body), filename=filename),
    )


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


def display_number(value: Any, decimals: int = 0) -> str:
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{number:,.{decimals}f}" if decimals else f"{number:,.0f}"


def display_percent(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value) * 100:.0f}%"
    except (TypeError, ValueError):
        return str(value)


def display_rank(item: dict[str, Any]) -> str:
    rank = str(item.get("rank_key") or "unranked").replace("_", " ").title()
    rp = item.get("ladder_rank")
    return f"{rank} · {display_number(rp)} RP" if rp is not None else rank


def display_server(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("short_name") or value.get("name") or value.get("server_group_id") or "—")
    return str(value or "—")


def normalize_server_id(value: str) -> str:
    normalized = value.strip().upper().replace("-", "").replace(" ", "")
    return SERVER_ALIASES.get(normalized, normalized)


def rank_role_colour(rank_name: str) -> discord.Colour:
    key = rank_name.strip().casefold().replace(" ", "_")
    return discord.Colour(RANK_ROLE_COLORS.get(key, 0x7B8494))


def rank_role_icon(rank_name: str) -> str | None:
    key = rank_name.strip().casefold().replace(" ", "_")
    return RANK_ROLE_ICONS.get(key)


def can_manage_member(guild: discord.Guild, member: discord.Member) -> bool:
    bot_member = guild.me
    return bool(
        bot_member
        and member.id != guild.owner_id
        and bot_member.top_role > member.top_role
    )


def leader_lines(items: list[dict[str, Any]], metric: str) -> str:
    lines = []
    for index, item in enumerate(items[:3], 1):
        name = item.get("name") or item.get("character_id") or "Unknown"
        if metric == "rank":
            stat = display_rank(item)
        elif metric == "level":
            stat = f"Level {display_number(item.get('level'))}"
        elif metric == "kda":
            stat = f"KDA {display_number(item.get('kda'), 2)}"
        elif metric == "damage":
            stat = f"Avg. damage {display_number(item.get('avg_damage'))}"
        else:
            stat = "—"
        lines.append(f"{index}. **{name}** · {stat}")
    return "\n".join(lines) or "No eligible players yet."


RANK_BADGES = {
    "fresh_blood": "🟢",
    "rookie": "🔵",
    "contender": "🟣",
    "brawler": "🟠",
    "fighter": "🔴",
    "killer": "⚔️",
    "gladiator": "🛡️",
    "arena_hero": "🏅",
    "imperial_hero": "👑",
    "grand_champion": "🔴🏆",
}


def rank_badge(item: dict[str, Any]) -> str:
    key = str(item.get("rank_key") or "").casefold()
    if int(item.get("ladder_stars") or 0) >= 30:
        key = "grand_champion"
    return RANK_BADGES.get(key, "◆")


def leaderboard_item_text(item: dict[str, Any], category: str) -> str:
    name = item.get("name") or item.get("character_id") or "Unknown player"
    server = display_server(item.get("server") or item.get("server_group_id"))
    rank = display_rank(item)
    weapon = item.get("primary_weapon") or "Weapon unavailable"
    affiliation = item.get("guild_name") or item.get("nation_name")
    if item.get("affiliation_type") == "cohort":
        affiliation = "Cohort"
    value = item.get("value")
    if category == "rank":
        metric = display_rank(item)
    elif category == "level":
        metric = f"Level {display_number(value)}"
    elif category == "kda":
        metric = f"KDA {display_number(value, 2)}"
    elif category == "win_rate":
        metric = f"Win rate {display_percent(value)}"
    elif category == "damage":
        metric = f"Avg. damage {display_number(value)}"
    elif category == "hero_kills":
        metric = f"Hero kills {display_number(value)}"
    elif category == "mvp":
        metric = f"MVP {display_number(value)}"
    else:
        metric = f"Sovereigns {display_number(value)}"
    movement = item.get("value_change")
    if movement not in (None, 0):
        movement_text = f" · change {'+' if movement > 0 else ''}{display_number(movement)}"
    else:
        movement_text = ""
    extra = " · ".join(part for part in (server, weapon, affiliation) if part)
    return (
        f"{rank_badge(item)} **{name}**\n"
        f"`{metric}{movement_text}` · {extra}\n"
        f"Level {display_number(item.get('level'))} · KDA {display_number(item.get('kda'), 2)} · "
        f"Win rate {display_percent(item.get('win_rate'))}\n"
        f"Hero kills {display_number(item.get('career_hero_kills'))} · MVP {display_number(item.get('career_mvp'))}"
    )


def leaderboard_embed(payload: dict[str, Any]) -> discord.Embed:
    category = str(payload.get("category") or "rank")
    items = payload.get("items") or []
    source = payload.get("embed") or {}
    if not items:
        return make_embed(source)
    category_title = str(source.get("title") or f"CBGraphs · {category.title()}")
    server_label = str((source.get("footer") or {}).get("text") or "All servers")
    color = 0xFF5368 if category == "rank" and any("🏆" in rank_badge(item) for item in items) else 0xF5B544
    embed = discord.Embed(
        title=category_title,
        url=source.get("url") or "https://cbgraphs.info/#leaderboards",
        description=f"Detailed ranking · {server_label}",
        color=color,
    )
    for index, item in enumerate(items, 1):
        embed.add_field(name=f"#{index}", value=leaderboard_item_text(item, category), inline=False)
    if items and (items[0].get("head_icon") or "").startswith(("http://", "https://")):
        embed.set_thumbnail(url=str(items[0]["head_icon"]))
    embed.set_footer(text="Rank icons, player metrics and latest indexed snapshot")
    return embed


async def send_error(interaction: discord.Interaction, error: Exception) -> None:
    if isinstance(error, PlayerLookupError):
        message = str(error)
    elif isinstance(error, ApiError) and error.status == 429:
        message = "The CBGraphs API rate limit was reached. Please try again shortly."
    elif isinstance(error, ApiError) and error.status == 404:
        message = "The requested player or group was not found in the indexed data."
    elif isinstance(error, ApiError) and error.status in {401, 403}:
        message = "The bot API key is invalid or missing the required scope."
    else:
        log.warning("Discord command failed (%s): %r", type(error).__name__, error)
        message = "CBGraphs did not respond. Please try again in a few seconds."
    await interaction.followup.send(message, ephemeral=True)


async def resolve_player_reference(reference: str, server: str) -> tuple[str, str | None]:
    """Resolve a numeric character ID or an exact indexed name on a server."""
    reference = reference.strip()
    if reference.isdigit():
        return reference, None
    search_payload = await bot.api.get("/api/v1/search/players", q=reference, limit=20)
    items = search_payload.get("items") or []
    candidates = [
        item for item in items
        if str(item.get("server_group_id") or "") == server
        and str(item.get("name") or "").casefold() == reference.casefold()
    ]
    if len(candidates) == 1:
        return str(candidates[0]["character_id"]), str(candidates[0].get("name") or reference)
    matches = "\n".join(
        f"• {item.get('name') or item.get('character_id')} · {item.get('server_group_id') or 'unverified'} · `{item.get('character_id')}`"
        for item in items[:8]
    )
    raise PlayerLookupError(
        f"No unique player was found for `{reference}` on server `{server}`.\n"
        f"{matches or 'Player not found; use the exact name or character ID.'}"
    )


async def require_manager(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used inside a Discord server.", ephemeral=True)
        return False
    member = interaction.user if isinstance(interaction.user, discord.Member) else None
    if member is None or not member.guild_permissions.manage_guild:
        await interaction.response.send_message("Manage Server permission is required for this command.", ephemeral=True)
        return False
    return True


class CBGraphsBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(command_prefix="!", intents=intents)
        self.api = CBGraphsApi()
        self.links = LinkStore(BOT_DB_PATH)
        self._guild_commands_synced: set[int] = set()

    async def setup_hook(self) -> None:
        await self.api.start()
        log.info("Runtime: %s | PNG card renderer: lazy-load enabled", sys.executable)
        # Commands are installed per connected guild so they appear immediately.
        # Remove the old global copies once, otherwise Discord shows duplicates.
        global_commands = self.tree.get_commands()
        self.tree.clear_commands(guild=None)
        await self.tree.sync()
        for command in global_commands:
            self.tree.add_command(command)
        log.info("Removed global slash-command copies; guild sync will be automatic")
        self.sync_registered_players.start()

    async def close(self) -> None:
        self.sync_registered_players.cancel()
        await self.api.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Logged in as %s", self.user)
        for guild in self.guilds:
            if guild.id in self._guild_commands_synced:
                continue
            try:
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                self._guild_commands_synced.add(guild.id)
                log.info("Slash commands synced to connected guild %s", guild.id)
            except discord.HTTPException as error:
                log.warning("Guild command sync failed for %s: %s", guild.id, error)

    async def apply_snapshot(self, guild: discord.Guild, member: discord.Member, payload: dict[str, Any]) -> None:
        player_name, rank_name, _ = player_snapshot(payload)
        if SYNC_NICKNAMES and guild.me and guild.me.guild_permissions.manage_nicknames and can_manage_member(guild, member):
            try:
                display_name = member.global_name or member.display_name or member.name
                await member.edit(nick=f"{display_name} | {player_name}"[:32], reason="CBGraphs player sync")
            except discord.HTTPException as error:
                log.warning("Nickname sync failed in guild %s for user %s: %s", guild.id, member.id, error)
        elif SYNC_NICKNAMES and guild.me and guild.me.guild_permissions.manage_nicknames and not can_manage_member(guild, member):
            log.info("Nickname sync skipped in guild %s for user %s: role hierarchy or server owner restriction", guild.id, member.id)
        if not SYNC_ROLES or not guild.me or not guild.me.guild_permissions.manage_roles:
            return
        role_name = f"CB • {rank_name}"[:100]
        role_colour = rank_role_colour(rank_name)
        role_icon = rank_role_icon(rank_name)
        role = discord.utils.get(guild.roles, name=role_name)
        if role is None:
            try:
                role = await guild.create_role(name=role_name, colour=role_colour, reason="CBGraphs rank sync")
            except discord.HTTPException as error:
                log.warning("Role creation failed in guild %s: %s", guild.id, error)
                return
        elif role.colour.value != role_colour.value:
            try:
                await role.edit(colour=role_colour, reason="CBGraphs rank color sync")
            except discord.HTTPException as error:
                log.warning("Role color update failed in guild %s: %s", guild.id, error)
        if role_icon and "ROLE_ICONS" in guild.features:
            try:
                await role.edit(display_icon=role_icon, reason="CBGraphs rank icon sync")
            except discord.HTTPException as error:
                log.warning("Role icon update failed in guild %s: %s", guild.id, error)
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

    async def clear_sync_state(self, guild: discord.Guild, user: discord.Member) -> None:
        """Remove only the nickname/roles created by CBGraphs."""
        if guild.me and guild.me.guild_permissions.manage_roles:
            managed_roles = [
                role for role in user.roles
                if role.name.startswith("CB • ") and role.is_assignable()
            ]
            if managed_roles:
                try:
                    await user.remove_roles(*managed_roles, reason="CBGraphs registration removed")
                except discord.HTTPException as error:
                    log.warning("Role cleanup failed in guild %s for user %s: %s", guild.id, user.id, error)
        if guild.me and guild.me.guild_permissions.manage_nicknames and user.nick and can_manage_member(guild, user):
            display_name = user.global_name or user.name
            bot_managed_nickname = user.nick.startswith(f"{display_name} | ") or user.nick.startswith(f"{user.name} | ") or any(
                user.nick.endswith(f" · {rank}") for rank in RANK_DISPLAY_NAMES
            )
            if bot_managed_nickname:
                try:
                    await user.edit(nick=None, reason="CBGraphs registration removed")
                except discord.HTTPException as error:
                    log.warning("Nickname cleanup failed in guild %s for user %s: %s", guild.id, user.id, error)

    async def sync_link(self, guild: discord.Guild, link: sqlite3.Row) -> bool:
        try:
            member = guild.get_member(int(link["discord_user_id"])) or await guild.fetch_member(int(link["discord_user_id"]))
            payload = await self.api.get(f"/api/v1/bot/players/{link['cb_server_id']}/{link['character_id']}")
            await self.apply_snapshot(guild, member, payload)
            return True
        except Exception as error:
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
@app_commands.describe(user="Discord member", character_id="Character ID or exact player name", server="EU1, AM1, EU4 or numeric server ID")
async def register(interaction: discord.Interaction, user: discord.Member, character_id: str, server: str = "EU1") -> None:
    if not await require_manager(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    server = normalize_server_id(server or "EU1")
    character_ref = character_id.strip()
    if not server.isdigit() or not character_ref:
        await interaction.followup.send("Server must be EU1, AM1, EU4, or a numeric ID; player name/ID cannot be empty.", ephemeral=True)
        return
    try:
        character_ref, _ = await resolve_player_reference(character_ref, server)
        payload = await bot.api.get(f"/api/v1/bot/players/{server}/{character_ref}")
        bot.links.upsert(interaction.guild_id, user.id, server, character_ref, interaction.user.id)
        await bot.apply_snapshot(interaction.guild, user, payload)
        player_name, rank_name, _ = player_snapshot(payload)
        await interaction.followup.send(f"{user.mention} → **{player_name}** ({rank_name}) registered. This registration belongs to this Discord server only.", ephemeral=True)
    except Exception as error:
        await send_error(interaction, error)


@bot.tree.command(name="unregister", description="Remove a member's CB account registration from this server")
@app_commands.describe(user="Discord member")
async def unregister(interaction: discord.Interaction, user: discord.Member) -> None:
    if not await require_manager(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    removed = bot.links.remove(interaction.guild_id, user.id)
    await bot.clear_sync_state(interaction.guild, user)
    await interaction.followup.send("Registration and CBGraphs roles removed." if removed else "No registration found; any CBGraphs roles were still cleaned up.", ephemeral=True)


@bot.tree.command(name="sync", description="Refresh one registered member's nickname and rank role")
@app_commands.describe(user="Discord member")
async def sync(interaction: discord.Interaction, user: discord.Member) -> None:
    if not await require_manager(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    link = next((item for item in bot.links.for_server(interaction.guild_id) if item["discord_user_id"] == str(user.id)), None)
    if link is None:
        await interaction.followup.send("This user is not registered in this Discord server.", ephemeral=True)
        return
    message = "updated." if await bot.sync_link(interaction.guild, link) else "could not be updated; check the bot logs."
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
    await interaction.followup.send(f"{succeeded}/{len(links)} registrations updated.", ephemeral=True)


@bot.tree.command(name="registrations", description="List registrations belonging to this Discord server")
async def registrations(interaction: discord.Interaction) -> None:
    if not await require_manager(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    links = bot.links.for_server(interaction.guild_id)
    lines = [f"<@{item['discord_user_id']}> → `{item['cb_server_id']}:{item['character_id']}`" for item in links]
    await interaction.followup.send("\n".join(lines) or "No registrations in this Discord server.", ephemeral=True)


@bot.tree.command(name="player", description="Show a player profile by exact name or character ID")
@app_commands.describe(character_id="Exact player name or character ID", server="EU1, AM1, EU4 or numeric server ID; defaults to EU1", format="Output format: detailed or card")
@app_commands.choices(format=[
    app_commands.Choice(name="Detailed", value="detailed"),
    app_commands.Choice(name="Card", value="card"),
])
async def player(interaction: discord.Interaction, character_id: str, server: str = "EU1", format: app_commands.Choice[str] | None = None) -> None:
    await interaction.response.defer()
    try:
        selected_server = normalize_server_id(server or "EU1")
        if not selected_server.isdigit():
            raise PlayerLookupError("Server must be EU1, AM1, EU4, or a numeric server ID.")
        resolved_id, _ = await resolve_player_reference(character_id, selected_server)
        payload = await bot.api.get(f"/api/v1/bot/players/{selected_server}/{resolved_id}")
        output_format = format.value if format else "detailed"
        if output_format == "card":
            card_url = f"{API_BASE_URL}/cards/player/{selected_server}/{resolved_id}.svg"
            await send_card(
                interaction,
                path=f"/cards/player/{selected_server}/{resolved_id}.svg",
                url=card_url,
                title="CBGraphs · Player card",
                description=f"{character_id.strip()} · {display_server(selected_server)}",
                color=0xFF5368,
            )
            return
        await interaction.followup.send(embed=make_embed(payload.get("embed") or {}))
    except Exception as error:
        await send_error(interaction, error)


@bot.tree.command(name="leaderboard", description="Show a CBGraphs leaderboard")
@app_commands.describe(category="Leaderboard metric", server="EU1, AM1, EU4, numeric server ID, or all", limit="Number of entries, 1–10", format="Output format: detailed or card")
@app_commands.choices(category=[
    app_commands.Choice(name="Rank", value="rank"), app_commands.Choice(name="Level", value="level"),
    app_commands.Choice(name="KDA", value="kda"), app_commands.Choice(name="Win rate", value="win_rate"),
    app_commands.Choice(name="Damage", value="damage"), app_commands.Choice(name="Hero kills", value="hero_kills"),
    app_commands.Choice(name="MVP", value="mvp"), app_commands.Choice(name="Sovereigns", value="sovereigns"),
], format=[
    app_commands.Choice(name="Detailed", value="detailed"),
    app_commands.Choice(name="Card", value="card"),
])
async def leaderboard(interaction: discord.Interaction, category: app_commands.Choice[str], server: str = "all", limit: int = 10, format: app_commands.Choice[str] | None = None) -> None:
    await interaction.response.defer()
    try:
        selected_server = server.strip() or "all"
        if selected_server.casefold() not in {"all", "allservers", "all_servers"}:
            selected_server = normalize_server_id(selected_server)
        payload = await bot.api.get(
            "/api/v1/leaderboards",
            category=category.value,
            server=selected_server,
            limit=max(1, min(limit, 10)),
        )
        output_format = format.value if format else "detailed"
        if output_format == "card":
            card_url = f"{API_BASE_URL}/cards/leaderboard/{category.value}/{selected_server}.svg"
            await send_card(
                interaction,
                path=f"/cards/leaderboard/{category.value}/{selected_server}.svg",
                url=card_url,
                title=f"CBGraphs · {category.name} card",
                description=f"Top 5 · {selected_server} · latest indexed snapshots",
                color=0xFF5368 if category.value == "rank" else 0xF5B544,
            )
            return
        payload["category"] = category.value
        payload["embed"] = {
            "title": f"CBGraphs · {category.name}",
            "url": "https://cbgraphs.info/#leaderboards",
            "footer": {"text": f"{selected_server} · latest indexed snapshots"},
        }
        await interaction.followup.send(embed=leaderboard_embed(payload))
    except Exception as error:
        await send_error(interaction, error)


@bot.tree.command(name="search", description="Search indexed CBGraphs players by name or ID")
@app_commands.describe(query="Player name, ID, or ID#EU1", format="Output format: detailed or card; card requires one exact result")
@app_commands.choices(format=[
    app_commands.Choice(name="Detailed", value="detailed"),
    app_commands.Choice(name="Card", value="card"),
])
async def search(interaction: discord.Interaction, query: str, format: app_commands.Choice[str] | None = None) -> None:
    await interaction.response.defer()
    try:
        payload = await bot.api.get("/api/v1/search/players", q=query.strip(), limit=8)
        items = payload.get("items") or []
        if not items:
            await interaction.followup.send("No players found.", ephemeral=True)
            return
        output_format = format.value if format else "detailed"
        if output_format == "card" and len(items) == 1:
            item = items[0]
            selected_server = str(item.get("server_group_id") or "")
            character_id = str(item.get("character_id") or "")
            if selected_server.isdigit() and character_id.isdigit():
                player_payload = await bot.api.get(f"/api/v1/bot/players/{selected_server}/{character_id}")
                card_url = f"{API_BASE_URL}/cards/player/{selected_server}/{character_id}.svg"
                await send_card(
                    interaction,
                    path=f"/cards/player/{selected_server}/{character_id}.svg",
                    url=card_url,
                    title="CBGraphs · Player card",
                    description=f"{item.get('name') or character_id} · {display_server(selected_server)}",
                    color=0xFF5368,
                )
                return
        elif output_format == "card":
            await interaction.followup.send("Card format needs one exact player result. Use `/player` with the player name or character ID.", ephemeral=True)
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


@bot.tree.command(name="clan", description="Show an indexed House or Alliance")
@app_commands.describe(name="Exact House or Alliance name", group_type="house or alliance", server="EU1, AM1, EU4, numeric server ID, or all")
@app_commands.choices(group_type=[app_commands.Choice(name="House", value="house"), app_commands.Choice(name="Alliance", value="alliance")])
async def clan(interaction: discord.Interaction, name: str, group_type: app_commands.Choice[str], server: str = "all") -> None:
    await interaction.response.defer()
    try:
        payload = await bot.api.get(
            "/api/v1/groups/members",
            type=group_type.value,
            name=name.strip(),
            server=(server.strip() if server.strip().casefold() in {"all", "allservers", "all_servers"} else normalize_server_id(server.strip() or "all")),
            sort="rank",
            page=1,
            page_size=1,
        )
        if payload.get("ambiguous"):
            choices = payload.get("server_choices") or []
            choice_text = "\n".join(
                f"{display_server(item.get('server'))} · {display_number(item.get('member_count'))} members"
                for item in choices
            )
            await interaction.followup.send(
                f"This group exists on multiple servers. Specify a server ID:\n{choice_text}",
                ephemeral=True,
            )
            return
        if not payload.get("found", True):
            await interaction.followup.send("This group was not found in the indexed data.", ephemeral=True)
            return

        summary = payload.get("summary") or {}
        server_label = display_server(payload.get("server_info") or payload.get("server"))
        embed = discord.Embed(
            title=f"CBGraphs · {name}",
            description=f"{group_type.name} · {server_label}\nIndexed community overview and verified player observations.",
            color=0xF5B544,
        )
        embed.add_field(name="Known members", value=display_number(summary.get("member_count") or payload.get("member_count")), inline=True)
        embed.add_field(name="Grand Champions", value=display_number(summary.get("grand_champions")), inline=True)
        embed.add_field(name="Average level", value=f"{display_number(summary.get('average_level'), 1)} · median {display_number(summary.get('median_level'), 1)}", inline=True)
        embed.add_field(name="Average KDA", value=f"{display_number(summary.get('average_kda'), 2)} · median {display_number(summary.get('median_kda'), 2)}", inline=True)
        embed.add_field(name="Win rate", value=f"{display_percent(summary.get('average_win_rate'))} · median {display_percent(summary.get('median_win_rate'))}", inline=True)
        embed.add_field(name="Average damage", value=f"{display_number(summary.get('average_damage_per_match'))} · median {display_number(summary.get('median_damage_per_match'))}", inline=True)
        embed.add_field(name="Career damage", value=display_number(summary.get("career_damage")), inline=True)
        embed.add_field(name="Career hero kills", value=display_number(summary.get("career_hero_kills")), inline=True)
        embed.add_field(name="Career unit kills", value=display_number(summary.get("career_unit_kills")), inline=True)
        embed.add_field(name="Career MVP", value=display_number(summary.get("career_mvp")), inline=True)
        embed.add_field(name="Recent observations", value=display_number(summary.get("recent_match_samples_sum")), inline=True)

        leaders = payload.get("leaders") or {}
        for key, label in (("rank", "Best rank"), ("kda", "Best KDA"), ("damage", "Highest damage"), ("level", "Highest level")):
            embed.add_field(name=label, value=leader_lines(leaders.get(key) or [], key), inline=False)

        activity = payload.get("activity") or {}
        activity_lines = []
        for days in (7, 30, 90):
            period = activity.get(str(days)) or {}
            activity_lines.append(
                f"**{days}d:** {display_number(period.get('active_players'))} active players · "
                f"{display_number(period.get('unique_matches'))} matches · "
                f"{display_percent(period.get('observed_player_win_rate'))} observed win rate"
            )
        embed.add_field(name="Observed activity", value="\n".join(activity_lines) or "—", inline=False)

        events = payload.get("membership_events") or {}
        joins = events.get("joins_or_first_observed") or []
        departures = events.get("departures") or events.get("leaves") or []
        if joins:
            embed.add_field(name="Latest joins", value="\n".join(f"• {item.get('player_name') or item.get('character_id')}" for item in joins[:5]), inline=True)
        if departures:
            embed.add_field(name="Latest departures", value="\n".join(f"• {item.get('player_name') or item.get('character_id')}" for item in departures[:5]), inline=True)
        embed.set_footer(text="Based on indexed official player and match observations.")
        await interaction.followup.send(embed=embed)
    except Exception as error:
        await send_error(interaction, error)


@bot.tree.command(name="status", description="Check CBGraphs API health")
async def status(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    try:
        started = time.perf_counter()
        payload = await bot.api.get("/api/v1/status")
        api_latency = round((time.perf_counter() - started) * 1000)
        discord_latency = round(bot.latency * 1000) if bot.latency >= 0 else 0
        interval_minutes = max(1, SYNC_INTERVAL_SECONDS // 60)
        embed = discord.Embed(title="CBGraphs API status", description="All connected services are responding.", color=0x75E0B1)
        embed.add_field(name="API", value="Online", inline=True)
        embed.add_field(name="API latency", value=f"{api_latency} ms", inline=True)
        embed.add_field(name="Discord latency", value=f"{discord_latency} ms", inline=True)
        embed.add_field(name="Connected servers", value=str(len(bot.guilds)), inline=True)
        embed.add_field(name="Player sync", value=f"Every {interval_minutes} min", inline=True)
        embed.add_field(name="Commands", value="Ready", inline=True)
        embed.set_footer(text="CBGraphs bot operational status")
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as error:
        await send_error(interaction, error)


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("DISCORD_BOT_TOKEN is required")
    if not API_KEY:
        raise SystemExit("CBGRAPHS_API_KEY is required")
    bot.run(DISCORD_TOKEN, log_handler=None)
