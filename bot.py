"""Hank bot: replies to keywords with custom responses.

Each server manages its own keywords, and auto-replies to specific users, with
the /hank slash commands; those are stored in a SQLite database. keywords.json holds optional global rules that
apply on every server, and edits to it are picked up without a restart.
"""
import json
import logging
import os
import random
import re
import sqlite3
import time
from pathlib import Path

import discord
from discord import app_commands

HERE = Path(__file__).parent
CONFIG_PATH = Path(os.environ.get("HANK_CONFIG", HERE / "keywords.json"))
DATA_DIR = Path(os.environ.get("HANK_DATA_DIR", HERE / "data"))
MATCH_MODES = ("word", "contains", "exact")

log = logging.getLogger("hank")


def compile_pattern(keywords, mode="word"):
    """Build one case-insensitive regex matching any of the keywords."""
    parts = []
    for w in keywords:
        w = re.escape(w.strip())
        if mode == "word":
            # \b fails next to symbols, so use explicit non-word lookarounds
            w = rf"(?<!\w){w}(?!\w)"
        elif mode == "exact":
            w = rf"^\s*{w}\s*$"
        parts.append(w)
    return re.compile("|".join(parts), re.IGNORECASE)


class GlobalRules:
    """Loads keywords.json and reloads it whenever the file changes."""

    def __init__(self, path: Path):
        self.path = path
        self.mtime = None
        self.rules = []

    def _compile(self, rule):
        words = rule["keywords"]
        if isinstance(words, str):
            words = [words]
        responses = rule["response"]
        if isinstance(responses, str):
            responses = [responses]
        return compile_pattern(words, rule.get("match", "word")), responses

    def get(self):
        if not self.path.exists():
            return []
        mtime = self.path.stat().st_mtime
        if mtime != self.mtime:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.rules = [self._compile(r) for r in data.get("rules", [])]
                self.mtime = mtime
                log.info("Loaded %d global rules from %s", len(self.rules), self.path)
            except Exception:
                # Keep the previous rules if the file is mid-edit or broken
                log.exception("Could not load %s, keeping previous rules", self.path)
                self.mtime = mtime
        return self.rules


class ServerRules:
    """Per-server keywords stored in SQLite, cached in memory per server."""

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path)
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS rules (
                id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                keyword TEXT NOT NULL COLLATE NOCASE,
                match TEXT NOT NULL DEFAULT 'word',
                response TEXT NOT NULL
            )"""
        )
        self.db.execute("CREATE INDEX IF NOT EXISTS rules_guild ON rules (guild_id)")
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS user_rules (
                id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                cooldown INTEGER NOT NULL DEFAULT 0,
                response TEXT NOT NULL
            )"""
        )
        self.db.execute("CREATE INDEX IF NOT EXISTS user_rules_guild ON user_rules (guild_id, user_id)")
        self.db.commit()
        self.cache = {}
        self.last_user_reply = {}
        log.info("Using database %s", db_path)

    def add(self, guild_id, keyword, response, match):
        keyword = keyword.strip()
        # One match mode per keyword: adding again with a new mode updates it
        self.db.execute(
            "UPDATE rules SET match = ? WHERE guild_id = ? AND keyword = ?",
            (match, guild_id, keyword),
        )
        self.db.execute(
            "INSERT INTO rules (guild_id, keyword, match, response) VALUES (?, ?, ?, ?)",
            (guild_id, keyword, match, response),
        )
        self.db.commit()
        self.cache.pop(guild_id, None)
        return self.count(guild_id, keyword)

    def remove(self, guild_id, keyword):
        cur = self.db.execute(
            "DELETE FROM rules WHERE guild_id = ? AND keyword = ?", (guild_id, keyword.strip())
        )
        self.db.commit()
        self.cache.pop(guild_id, None)
        return cur.rowcount

    def count(self, guild_id, keyword):
        return self.db.execute(
            "SELECT COUNT(*) FROM rules WHERE guild_id = ? AND keyword = ?", (guild_id, keyword)
        ).fetchone()[0]

    def list(self, guild_id):
        """Return [(keyword, match, [responses])] in the order keywords were first added."""
        grouped = {}
        for keyword, match, response in self.db.execute(
            "SELECT keyword, match, response FROM rules WHERE guild_id = ? ORDER BY id",
            (guild_id,),
        ):
            grouped.setdefault(keyword.lower(), [keyword, match, []])[2].append(response)
        return [tuple(v) for v in grouped.values()]

    def add_user(self, guild_id, user_id, response, cooldown):
        # One cooldown per user: adding again with a new cooldown updates it
        self.db.execute(
            "UPDATE user_rules SET cooldown = ? WHERE guild_id = ? AND user_id = ?",
            (cooldown, guild_id, user_id),
        )
        self.db.execute(
            "INSERT INTO user_rules (guild_id, user_id, cooldown, response) VALUES (?, ?, ?, ?)",
            (guild_id, user_id, cooldown, response),
        )
        self.db.commit()
        return self.db.execute(
            "SELECT COUNT(*) FROM user_rules WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        ).fetchone()[0]

    def remove_user(self, guild_id, user_id):
        cur = self.db.execute(
            "DELETE FROM user_rules WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        )
        self.db.commit()
        return cur.rowcount

    def list_users(self, guild_id):
        """Return [(user_id, cooldown_minutes, [responses])]."""
        grouped = {}
        for user_id, cooldown, response in self.db.execute(
            "SELECT user_id, cooldown, response FROM user_rules WHERE guild_id = ? ORDER BY id",
            (guild_id,),
        ):
            grouped.setdefault(user_id, [user_id, cooldown, []])[2].append(response)
        return [tuple(v) for v in grouped.values()]

    def user_response(self, guild_id, user_id):
        """A reply for this user if they have auto-replies and aren't on cooldown."""
        rows = self.db.execute(
            "SELECT cooldown, response FROM user_rules WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchall()
        if not rows:
            return None
        key = (guild_id, user_id)
        now = time.monotonic()
        if now - self.last_user_reply.get(key, float("-inf")) < rows[0][0] * 60:
            return None
        self.last_user_reply[key] = now
        return random.choice(rows)[1]

    def get(self, guild_id):
        if guild_id not in self.cache:
            self.cache[guild_id] = [
                (compile_pattern([kw], match), responses)
                for kw, match, responses in self.list(guild_id)
            ]
        return self.cache[guild_id]


global_rules = GlobalRules(CONFIG_PATH)
server_rules = ServerRules(DATA_DIR / "hank.db")


def find_response(guild_id, user_id, text):
    """User auto-replies come first, then server keywords, then keywords.json."""
    if guild_id:
        response = server_rules.user_response(guild_id, user_id)
        if response:
            return response
    rules = (server_rules.get(guild_id) if guild_id else []) + global_rules.get()
    for pattern, responses in rules:
        if pattern.search(text):
            return random.choice(responses)
    return None


intents = discord.Intents.default()
intents.message_content = True  # must also be enabled in the Developer Portal
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

hank = app_commands.Group(
    name="hank",
    description="Manage Hank's keywords on this server",
    guild_only=True,
    default_permissions=discord.Permissions(manage_guild=True),
)
tree.add_command(hank)

MATCH_CHOICES = [
    app_commands.Choice(name="whole word (default)", value="word"),
    app_commands.Choice(name="anywhere, even inside words", value="contains"),
    app_commands.Choice(name="whole message only", value="exact"),
]


@hank.command(name="add", description="Reply to a keyword. Add the same keyword again for random replies.")
@app_commands.describe(
    keyword="Word or phrase to listen for (case doesn't matter)",
    response="What Hank says. Use {user} to mention the person.",
    match="How the keyword has to appear in a message",
)
@app_commands.choices(match=MATCH_CHOICES)
async def hank_add(
    interaction: discord.Interaction,
    keyword: app_commands.Range[str, 1, 100],
    response: app_commands.Range[str, 1, 2000],
    match: str = "word",
):
    total = server_rules.add(interaction.guild_id, keyword, response, match)
    extra = f" It now has {total} replies to pick from." if total > 1 else ""
    await interaction.response.send_message(f"Added a reply for **{keyword.strip()}**.{extra}", ephemeral=True)


@hank.command(name="remove", description="Stop replying to a keyword (removes all its replies)")
@app_commands.describe(keyword="The keyword to remove")
async def hank_remove(interaction: discord.Interaction, keyword: str):
    removed = server_rules.remove(interaction.guild_id, keyword)
    msg = f"Removed **{keyword}**." if removed else f"There's no keyword **{keyword}** on this server."
    await interaction.response.send_message(msg, ephemeral=True)


@hank_remove.autocomplete("keyword")
async def keyword_autocomplete(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=kw, value=kw)
        for kw, _, _ in server_rules.list(interaction.guild_id)
        if current.lower() in kw.lower()
    ][:25]


@hank.command(name="user-add", description="Reply to every message from a user. Add again for random replies.")
@app_commands.describe(
    user="Who Hank replies to",
    response="What Hank says. Use {user} to mention them.",
    cooldown="Minutes to wait before replying to them again (default 0: every message)",
)
async def hank_user_add(
    interaction: discord.Interaction,
    user: discord.Member,
    response: app_commands.Range[str, 1, 2000],
    cooldown: app_commands.Range[int, 0, 10080] = 0,
):
    if user.bot:
        await interaction.response.send_message("Hank ignores bots, so pick a person.", ephemeral=True)
        return
    total = server_rules.add_user(interaction.guild_id, user.id, response, cooldown)
    extra = f" They now have {total} replies to pick from." if total > 1 else ""
    wait = f", at most once every {cooldown} min" if cooldown else ""
    await interaction.response.send_message(
        f"Hank will reply to {user.mention}{wait}.{extra}",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@hank.command(name="user-remove", description="Stop auto-replying to a user")
@app_commands.describe(user="The user to stop replying to")
async def hank_user_remove(interaction: discord.Interaction, user: discord.User):
    removed = server_rules.remove_user(interaction.guild_id, user.id)
    msg = f"Stopped replying to {user.mention}." if removed else f"Hank wasn't auto-replying to {user.mention}."
    await interaction.response.send_message(msg, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@hank.command(name="list", description="Show this server's keywords and user auto-replies")
async def hank_list(interaction: discord.Interaction):
    rules = server_rules.list(interaction.guild_id)
    users = server_rules.list_users(interaction.guild_id)
    if not rules and not users:
        await interaction.response.send_message(
            "Nothing set yet. Add a keyword with `/hank add` or a user with `/hank user-add`.", ephemeral=True
        )
        return
    lines = []
    for user_id, cooldown, responses in users:
        wait = f" (every {cooldown} min)" if cooldown else ""
        lines.append(f"<@{user_id}>{wait}")
        lines.extend(f"  → {r}" for r in responses)
    for kw, match, responses in rules:
        mode = "" if match == "word" else f" ({match})"
        lines.append(f"**{kw}**{mode}")
        lines.extend(f"  → {r}" for r in responses)
    text = "\n".join(lines)
    if len(text) > 1900:
        text = text[:1900] + "\n…and more"
    await interaction.response.send_message(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@client.event
async def setup_hook():
    await tree.sync()


@client.event
async def on_ready():
    log.info("Logged in as %s, in %d servers", client.user, len(client.guilds))
    global_rules.get()


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    response = find_response(message.guild.id if message.guild else None, message.author.id, message.content)
    if response:
        await message.channel.send(
            response.replace("{user}", message.author.mention),
            allowed_mentions=discord.AllowedMentions(users=True, everyone=False, roles=False),
        )


def main():
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Set the DISCORD_TOKEN environment variable to your bot token.")
    client.run(token, root_logger=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
