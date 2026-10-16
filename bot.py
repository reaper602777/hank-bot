"""Hank bot: replies to keywords with custom responses.

Each server manages its own keywords, ! commands, auto-replies to specific
users, and replies to dice rolls with the /hank slash commands; those are stored in a SQLite database. keywords.json holds optional global rules that
apply on every server, and edits to it are picked up without a restart.
"""
import html
import json
import logging
import os
import random
import re
import sqlite3
import time
from pathlib import Path
from typing import Optional

import discord
from aiohttp import web
from discord import app_commands

HERE = Path(__file__).parent
CONFIG_PATH = Path(os.environ.get("HANK_CONFIG", HERE / "keywords.json"))
DATA_DIR = Path(os.environ.get("HANK_DATA_DIR", HERE / "data"))
MATCH_MODES = ("word", "contains", "exact")
COMMAND_NAME = re.compile(r"^[\w-]{1,32}$")
# "< 6", "<=5", ">= 18", "= 20", "20", "1-5", "1 to 5"
ROLL_CONDITION = re.compile(r"^\s*(?:(<=|>=|<|>|==|=)\s*(-?\d+)|(-?\d+)\s*(?:-|to)\s*(-?\d+)|(-?\d+))\s*$")
# Dice bots usually label the total ("Result: 7", "Total = 7", "= `7`"); otherwise use the last number
ROLL_TOTAL = re.compile(r"(?:total|result|=)\W{0,4}(-?\d+)", re.IGNORECASE)
NUMBER = re.compile(r"-?\d+")
DICE_NOTATION = re.compile(r"\b\d*d\d+\b", re.IGNORECASE)
WEB_DIR = HERE / "web"
# View Channels, Send Messages, Embed Links (previews for links in replies), Read Message History
INVITE_PERMISSIONS = 1024 | 2048 | 16384 | 65536

log = logging.getLogger("hank")


def parse_condition(text):
    """Turn "< 6", "1-5", "20" etc. into a normalized string and a test, or (None, None)."""
    m = ROLL_CONDITION.match(text)
    if not m:
        return None, None
    op, n, low, high, exact = m.groups()
    if op:
        n = int(n)
        op = "=" if op == "==" else op
        tests = {"<": n.__gt__, "<=": n.__ge__, ">": n.__lt__, ">=": n.__le__, "=": n.__eq__}
        return f"{op} {n}", tests[op]
    if low is not None:
        low, high = sorted((int(low), int(high)))
        return f"{low}-{high}", lambda x: low <= x <= high
    n = int(exact)
    return f"= {n}", n.__eq__


def roll_total(text):
    """The rolled total in a dice bot's message, or None if it has no number."""
    text = text.replace("*", "").replace("`", "").replace("_", "")
    text = DICE_NOTATION.sub(" ", text)  # "1d20" is the dice, not the roll
    labelled = ROLL_TOTAL.findall(text)
    if labelled:
        return int(labelled[-1])
    numbers = NUMBER.findall(text)
    return int(numbers[-1]) if numbers else None


def message_text(message):
    """A message's text plus the text of its embeds, where dice bots often put the result."""
    parts = [message.content]
    for e in message.embeds:
        parts += [e.title or "", e.description or ""]
        parts += [f"{f.name}\n{f.value}" for f in e.fields]
    return "\n".join(p for p in parts if p)


def roller(message):
    """Who asked the dice bot to roll: the slash command's user, else the first person mentioned."""
    meta = getattr(message, "interaction_metadata", None) or getattr(message, "interaction", None)
    if meta and meta.user:
        return meta.user
    people = [u for u in message.mentions if not u.bot]
    return people[0] if people else None


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
        # Databases from before keyword cooldowns existed lack this column
        if "cooldown" not in [row[1] for row in self.db.execute("PRAGMA table_info(rules)")]:
            self.db.execute("ALTER TABLE rules ADD COLUMN cooldown INTEGER NOT NULL DEFAULT 0")
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
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS commands (
                id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                name TEXT NOT NULL COLLATE NOCASE,
                response TEXT NOT NULL
            )"""
        )
        self.db.execute("CREATE INDEX IF NOT EXISTS commands_guild ON commands (guild_id, name)")
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS roll_rules (
                id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                condition TEXT NOT NULL,
                bot_id INTEGER,
                cooldown INTEGER NOT NULL DEFAULT 0,
                response TEXT NOT NULL
            )"""
        )
        self.db.execute("CREATE INDEX IF NOT EXISTS roll_rules_guild ON roll_rules (guild_id)")
        self.db.commit()
        self.cache = {}
        self.last_user_reply = {}
        self.last_keyword_reply = {}
        self.last_roll_reply = {}
        log.info("Using database %s", db_path)

    def add(self, guild_id, keyword, response, match, cooldown=None):
        """Add a reply. cooldown=None keeps the keyword's current cooldown (0 if new)."""
        keyword = keyword.strip()
        if cooldown is None:
            row = self.db.execute(
                "SELECT cooldown FROM rules WHERE guild_id = ? AND keyword = ? LIMIT 1", (guild_id, keyword)
            ).fetchone()
            cooldown = row[0] if row else 0
        # One match mode and cooldown per keyword: adding again with new ones updates them
        self.db.execute(
            "UPDATE rules SET match = ?, cooldown = ? WHERE guild_id = ? AND keyword = ?",
            (match, cooldown, guild_id, keyword),
        )
        self.db.execute(
            "INSERT INTO rules (guild_id, keyword, match, cooldown, response) VALUES (?, ?, ?, ?, ?)",
            (guild_id, keyword, match, cooldown, response),
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

    def set_cooldown(self, guild_id, keyword, cooldown):
        cur = self.db.execute(
            "UPDATE rules SET cooldown = ? WHERE guild_id = ? AND keyword = ?", (cooldown, guild_id, keyword.strip())
        )
        self.db.commit()
        self.cache.pop(guild_id, None)
        return cur.rowcount

    def count(self, guild_id, keyword):
        return self.db.execute(
            "SELECT COUNT(*) FROM rules WHERE guild_id = ? AND keyword = ?", (guild_id, keyword)
        ).fetchone()[0]

    def list(self, guild_id):
        """Return [(keyword, match, cooldown_minutes, [responses])] in the order keywords were first added."""
        grouped = {}
        for keyword, match, cooldown, response in self.db.execute(
            "SELECT keyword, match, cooldown, response FROM rules WHERE guild_id = ? ORDER BY id",
            (guild_id,),
        ):
            grouped.setdefault(keyword.lower(), [keyword, match, cooldown, []])[3].append(response)
        return [tuple(v) for v in grouped.values()]

    def add_command(self, guild_id, name, response):
        self.db.execute(
            "INSERT INTO commands (guild_id, name, response) VALUES (?, ?, ?)", (guild_id, name, response)
        )
        self.db.commit()
        return self.db.execute(
            "SELECT COUNT(*) FROM commands WHERE guild_id = ? AND name = ?", (guild_id, name)
        ).fetchone()[0]

    def remove_command(self, guild_id, name):
        cur = self.db.execute("DELETE FROM commands WHERE guild_id = ? AND name = ?", (guild_id, name))
        self.db.commit()
        return cur.rowcount

    def list_commands(self, guild_id):
        """Return [(name, [responses])] in the order commands were first added."""
        grouped = {}
        for name, response in self.db.execute(
            "SELECT name, response FROM commands WHERE guild_id = ? ORDER BY id", (guild_id,)
        ):
            grouped.setdefault(name.lower(), [name, []])[1].append(response)
        return [tuple(v) for v in grouped.values()]

    def command_response(self, guild_id, text):
        """A reply if the message starts with one of this server's !commands."""
        if not text.startswith("!"):
            return None
        words = text[1:].split(maxsplit=1)
        if not words:
            return None
        rows = self.db.execute(
            "SELECT response FROM commands WHERE guild_id = ? AND name = ?", (guild_id, words[0])
        ).fetchall()
        return random.choice(rows)[0] if rows else None

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

    def add_roll(self, guild_id, condition, bot_id, response, cooldown):
        # One bot and cooldown per condition: adding again with new ones updates them
        self.db.execute(
            "UPDATE roll_rules SET bot_id = ?, cooldown = ? WHERE guild_id = ? AND condition = ?",
            (bot_id, cooldown, guild_id, condition),
        )
        self.db.execute(
            "INSERT INTO roll_rules (guild_id, condition, bot_id, cooldown, response) VALUES (?, ?, ?, ?, ?)",
            (guild_id, condition, bot_id, cooldown, response),
        )
        self.db.commit()
        return self.db.execute(
            "SELECT COUNT(*) FROM roll_rules WHERE guild_id = ? AND condition = ?", (guild_id, condition)
        ).fetchone()[0]

    def remove_roll(self, guild_id, condition):
        cur = self.db.execute("DELETE FROM roll_rules WHERE guild_id = ? AND condition = ?", (guild_id, condition))
        self.db.commit()
        return cur.rowcount

    def list_rolls(self, guild_id):
        """Return [(condition, bot_id, cooldown_minutes, [responses])] in the order conditions were first added."""
        grouped = {}
        for condition, bot_id, cooldown, response in self.db.execute(
            "SELECT condition, bot_id, cooldown, response FROM roll_rules WHERE guild_id = ? ORDER BY id",
            (guild_id,),
        ):
            grouped.setdefault(condition, [condition, bot_id, cooldown, []])[3].append(response)
        return [tuple(v) for v in grouped.values()]

    def roll_response(self, guild_id, bot_id, total):
        """A reply for the first roll condition this bot's total meets that isn't on cooldown."""
        now = time.monotonic()
        for condition, rule_bot, cooldown, responses in self.list_rolls(guild_id):
            if rule_bot not in (None, bot_id) or not parse_condition(condition)[1](total):
                continue
            key = (guild_id, condition)
            if cooldown and now - self.last_roll_reply.get(key, float("-inf")) < cooldown * 60:
                continue
            self.last_roll_reply[key] = now
            return random.choice(responses)
        return None

    def get(self, guild_id):
        if guild_id not in self.cache:
            self.cache[guild_id] = [
                (kw.lower(), compile_pattern([kw], match), cooldown, responses)
                for kw, match, cooldown, responses in self.list(guild_id)
            ]
        return self.cache[guild_id]

    def keyword_response(self, guild_id, text):
        """A reply for the first matching keyword that isn't on cooldown."""
        now = time.monotonic()
        for kw, pattern, cooldown, responses in self.get(guild_id):
            if not pattern.search(text):
                continue
            key = (guild_id, kw)
            if cooldown and now - self.last_keyword_reply.get(key, float("-inf")) < cooldown * 60:
                continue
            self.last_keyword_reply[key] = now
            return random.choice(responses)
        return None


global_rules = GlobalRules(CONFIG_PATH)
server_rules = ServerRules(DATA_DIR / "hank.db")


def find_response(guild_id, user_id, text):
    """! commands come first, then user auto-replies, server keywords, and keywords.json."""
    if guild_id:
        response = (
            server_rules.command_response(guild_id, text)
            or server_rules.user_response(guild_id, user_id)
            or server_rules.keyword_response(guild_id, text)
        )
        if response:
            return response
    for pattern, responses in global_rules.get():
        if pattern.search(text):
            return random.choice(responses)
    return None


intents = discord.Intents.default()
intents.message_content = True  # must also be enabled in the Developer Portal
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

hank = app_commands.Group(
    name="hank",
    description="Manage Hank's keywords and commands on this server",
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
    cooldown="Minutes to wait before replying to this keyword again (default: no cooldown)",
)
@app_commands.choices(match=MATCH_CHOICES)
async def hank_add(
    interaction: discord.Interaction,
    keyword: app_commands.Range[str, 1, 100],
    response: app_commands.Range[str, 1, 2000],
    match: str = "word",
    cooldown: Optional[app_commands.Range[int, 0, 10080]] = None,
):
    total = server_rules.add(interaction.guild_id, keyword, response, match, cooldown)
    extra = f" It now has {total} replies to pick from." if total > 1 else ""
    wait = f" Hank replies to it at most once every {cooldown} min." if cooldown else ""
    await interaction.response.send_message(f"Added a reply for **{keyword.strip()}**.{extra}{wait}", ephemeral=True)


@hank.command(name="cooldown", description="Set how often Hank can reply to a keyword")
@app_commands.describe(keyword="The keyword to change", minutes="Minutes between replies (0 turns the cooldown off)")
async def hank_cooldown(
    interaction: discord.Interaction, keyword: str, minutes: app_commands.Range[int, 0, 10080]
):
    if not server_rules.set_cooldown(interaction.guild_id, keyword, minutes):
        msg = f"There's no keyword **{keyword}** on this server."
    elif minutes:
        msg = f"Hank will reply to **{keyword}** at most once every {minutes} min."
    else:
        msg = f"Turned off the cooldown for **{keyword}**."
    await interaction.response.send_message(msg, ephemeral=True)


@hank.command(name="remove", description="Stop replying to a keyword (removes all its replies)")
@app_commands.describe(keyword="The keyword to remove")
async def hank_remove(interaction: discord.Interaction, keyword: str):
    removed = server_rules.remove(interaction.guild_id, keyword)
    msg = f"Removed **{keyword}**." if removed else f"There's no keyword **{keyword}** on this server."
    await interaction.response.send_message(msg, ephemeral=True)


@hank_remove.autocomplete("keyword")
@hank_cooldown.autocomplete("keyword")
async def keyword_autocomplete(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=kw, value=kw)
        for kw, _, _, _ in server_rules.list(interaction.guild_id)
        if current.lower() in kw.lower()
    ][:25]


@hank.command(name="command-add", description="Make a !command. Add the same name again for random replies.")
@app_commands.describe(
    name="Command name, without the ! (letters, numbers, - and _)",
    response="What Hank says when someone types !name. Use {user} to mention them.",
)
async def hank_command_add(
    interaction: discord.Interaction,
    name: app_commands.Range[str, 1, 33],
    response: app_commands.Range[str, 1, 2000],
):
    name = name.strip().removeprefix("!")
    if not COMMAND_NAME.match(name):
        await interaction.response.send_message(
            "Command names can only use letters, numbers, - and _, with no spaces (up to 32).", ephemeral=True
        )
        return
    total = server_rules.add_command(interaction.guild_id, name, response)
    extra = f" It now has {total} replies to pick from." if total > 1 else ""
    await interaction.response.send_message(f"Added **!{name}**.{extra}", ephemeral=True)


@hank.command(name="command-remove", description="Delete a !command and all its replies")
@app_commands.describe(name="The command to remove, without the !")
async def hank_command_remove(interaction: discord.Interaction, name: str):
    name = name.strip().removeprefix("!")
    removed = server_rules.remove_command(interaction.guild_id, name)
    msg = f"Removed **!{name}**." if removed else f"There's no command **!{name}** on this server."
    await interaction.response.send_message(msg, ephemeral=True)


@hank_command_remove.autocomplete("name")
async def command_autocomplete(interaction: discord.Interaction, current: str):
    current = current.removeprefix("!").lower()
    return [
        app_commands.Choice(name=f"!{name}", value=name)
        for name, _ in server_rules.list_commands(interaction.guild_id)
        if current in name.lower()
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


@hank.command(name="roll-add", description="Reply when a dice bot rolls a number, e.g. < 6. Add again for random replies.")
@app_commands.describe(
    condition="Which totals count: < 6, <= 5, > 15, >= 18, = 20, or a range like 1-5",
    response="What Hank says. Use {user} to mention whoever rolled.",
    bot="Only listen to this dice bot (default: any bot)",
    cooldown="Minutes to wait before replying to this condition again (default: no cooldown)",
)
async def hank_roll_add(
    interaction: discord.Interaction,
    condition: app_commands.Range[str, 1, 20],
    response: app_commands.Range[str, 1, 2000],
    bot: Optional[discord.Member] = None,
    cooldown: app_commands.Range[int, 0, 10080] = 0,
):
    condition, _ = parse_condition(condition)
    if not condition:
        await interaction.response.send_message(
            "I couldn't read that condition. Try `< 6`, `>= 18`, `= 20` or a range like `1-5`.", ephemeral=True
        )
        return
    if bot and not bot.bot:
        await interaction.response.send_message("That's a person. Pick the dice bot, or leave it empty.", ephemeral=True)
        return
    total = server_rules.add_roll(interaction.guild_id, condition, bot.id if bot else None, response, cooldown)
    extra = f" It now has {total} replies to pick from." if total > 1 else ""
    who = bot.mention if bot else "a bot"
    wait = f" at most once every {cooldown} min" if cooldown else ""
    await interaction.response.send_message(
        f"Hank will reply{wait} when {who} rolls **{condition}**.{extra}",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@hank.command(name="roll-remove", description="Stop replying to a dice roll condition")
@app_commands.describe(condition="The condition to remove")
async def hank_roll_remove(interaction: discord.Interaction, condition: str):
    normalized = parse_condition(condition)[0] or condition
    removed = server_rules.remove_roll(interaction.guild_id, normalized)
    msg = f"Removed rolls **{normalized}**." if removed else f"There's no roll condition **{condition}** on this server."
    await interaction.response.send_message(msg, ephemeral=True)


@hank_roll_remove.autocomplete("condition")
async def roll_autocomplete(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=condition, value=condition)
        for condition, _, _, _ in server_rules.list_rolls(interaction.guild_id)
        if current.replace(" ", "") in condition.replace(" ", "")
    ][:25]


@hank.command(name="list", description="Show this server's keywords, !commands, user auto-replies and dice rolls")
async def hank_list(interaction: discord.Interaction):
    rules = server_rules.list(interaction.guild_id)
    users = server_rules.list_users(interaction.guild_id)
    commands = server_rules.list_commands(interaction.guild_id)
    rolls = server_rules.list_rolls(interaction.guild_id)
    if not rules and not users and not commands and not rolls:
        await interaction.response.send_message(
            "Nothing set yet. Add a keyword with `/hank add`, a command with `/hank command-add`, "
            "a user with `/hank user-add`, or a dice roll with `/hank roll-add`.",
            ephemeral=True,
        )
        return
    lines = []
    for name, responses in commands:
        lines.append(f"**!{name}**")
        lines.extend(f"  → {r}" for r in responses)
    for user_id, cooldown, responses in users:
        wait = f" (every {cooldown} min)" if cooldown else ""
        lines.append(f"<@{user_id}>{wait}")
        lines.extend(f"  → {r}" for r in responses)
    for kw, match, cooldown, responses in rules:
        notes = ([] if match == "word" else [match]) + ([f"every {cooldown} min"] if cooldown else [])
        extra = f" ({', '.join(notes)})" if notes else ""
        lines.append(f"**{kw}**{extra}")
        lines.extend(f"  → {r}" for r in responses)
    for condition, bot_id, cooldown, responses in rolls:
        notes = ([f"<@{bot_id}>"] if bot_id else []) + ([f"every {cooldown} min"] if cooldown else [])
        extra = f" ({', '.join(notes)})" if notes else ""
        lines.append(f"**roll {condition}**{extra}")
        lines.extend(f"  → {r}" for r in responses)
    text = "\n".join(lines)
    if len(text) > 1900:
        text = text[:1900] + "\n…and more"
    await interaction.response.send_message(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


def invite_url():
    client_id = os.environ.get("DISCORD_CLIENT_ID") or client.application_id
    return (
        f"https://discord.com/oauth2/authorize?client_id={client_id}"
        f"&permissions={INVITE_PERMISSIONS}&scope=bot+applications.commands"
    )


async def landing_page(request):
    page = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    page = page.replace("__INVITE_URL__", html.escape(invite_url()))
    return web.Response(text=page, content_type="text/html")


async def invite_redirect(request):
    raise web.HTTPFound(invite_url())


async def start_web():
    """Serve the landing page when the host gives us a port (Railway sets PORT)."""
    port = os.environ.get("PORT")
    if not port:
        return
    app = web.Application()
    app.router.add_get("/", landing_page)
    app.router.add_get("/invite", invite_redirect)
    app.router.add_get("/health", lambda request: web.Response(text="ok"))
    app.router.add_get("/hank-avatar.png", lambda request: web.FileResponse(WEB_DIR / "hank-avatar.png"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(port)).start()
    log.info("Landing page on port %s", port)


@client.event
async def setup_hook():
    await start_web()
    await tree.sync()


@client.event
async def on_ready():
    log.info("Logged in as %s, in %d servers", client.user, len(client.guilds))
    global_rules.get()


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        await on_bot_message(message)
        return
    response = find_response(message.guild.id if message.guild else None, message.author.id, message.content)
    if response:
        await message.channel.send(
            response.replace("{user}", message.author.mention),
            allowed_mentions=discord.AllowedMentions(users=True, everyone=False, roles=False),
        )


async def on_bot_message(message: discord.Message):
    """Bots only trigger dice roll replies, so Hank never answers itself or loops with another bot."""
    if message.author == client.user or not message.guild:
        return
    total = roll_total(message_text(message))
    if total is None:
        return
    response = server_rules.roll_response(message.guild.id, message.author.id, total)
    if response:
        person = roller(message)
        await message.channel.send(
            response.replace("{user}", person.mention if person else "").replace("{roll}", str(total)),
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
