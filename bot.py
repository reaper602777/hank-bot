"""Hank bot: replies to keywords with custom responses.

Keywords live in keywords.json next to this file. Edits to that file are
picked up automatically on the next message, no restart needed.
"""
import json
import logging
import os
import random
import re
from pathlib import Path

import discord

CONFIG_PATH = Path(os.environ.get("HANK_CONFIG", Path(__file__).with_name("keywords.json")))

log = logging.getLogger("hank")


class Rules:
    """Loads keywords.json and reloads it whenever the file changes."""

    def __init__(self, path: Path):
        self.path = path
        self.mtime = None
        self.rules = []

    def _compile(self, rule):
        words = rule["keywords"]
        if isinstance(words, str):
            words = [words]
        mode = rule.get("match", "word")
        parts = []
        for w in words:
            w = re.escape(w.strip())
            if mode == "word":
                # \b fails next to symbols, so use explicit non-word lookarounds
                w = rf"(?<!\w){w}(?!\w)"
            elif mode == "exact":
                w = rf"^\s*{w}\s*$"
            parts.append(w)
        pattern = re.compile("|".join(parts), re.IGNORECASE)
        responses = rule["response"]
        if isinstance(responses, str):
            responses = [responses]
        return pattern, responses

    def get(self):
        mtime = self.path.stat().st_mtime
        if mtime != self.mtime:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.rules = [self._compile(r) for r in data["rules"]]
                self.mtime = mtime
                log.info("Loaded %d rules from %s", len(self.rules), self.path)
            except Exception:
                # Keep the previous rules if the file is mid-edit or broken
                log.exception("Could not load %s, keeping previous rules", self.path)
                self.mtime = mtime
        return self.rules

    def match(self, text: str):
        """Return a response for the first rule that matches, or None."""
        for pattern, responses in self.get():
            if pattern.search(text):
                return random.choice(responses)
        return None


rules = Rules(CONFIG_PATH)

intents = discord.Intents.default()
intents.message_content = True  # must also be enabled in the Developer Portal
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    log.info("Logged in as %s", client.user)
    rules.get()


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    response = rules.match(message.content)
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
