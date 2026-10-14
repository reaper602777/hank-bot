# Hank bot

A Discord bot that replies to keywords with custom responses.

## 1. Create the bot in Discord

1. Go to https://discord.com/developers/applications and click **New Application**. Name it Hank.
2. Open the **Bot** tab. Click **Reset Token**, copy the token, and keep it secret.
3. On the same tab, under **Privileged Gateway Intents**, turn on **Message Content Intent** and save. Without this the bot can't read messages.
4. Open **OAuth2 → URL Generator**. Tick the `bot` scope, then the permissions **View Channels**, **Send Messages** and **Read Message History**.
5. Open the generated URL, pick your server, and authorize.

## 2. Run it

Needs Python 3.9 or newer.

```bash
pip install -r requirements.txt

# macOS / Linux
export DISCORD_TOKEN="your-token-here"
python bot.py

# Windows (PowerShell)
$env:DISCORD_TOKEN="your-token-here"
python bot.py
```

The bot only answers while this is running. To keep it online 24/7, use Railway (section 4) or your own Ubuntu server (section 5).

## 3. Edit keywords

Everything lives in `keywords.json`. Changes are picked up on the next message, no restart needed.

```json
{
  "rules": [
    { "keywords": ["hello hank", "hi hank"], "response": "Hey {user}!" },
    { "keywords": "good bot", "response": ["Thanks!", "🥹"] },
    { "keywords": "!rules", "match": "exact", "response": "Be kind." },
    { "keywords": "pizza", "match": "contains", "response": "Pizza? 🍕" }
  ]
}
```

- `keywords`: one phrase or a list. Matching ignores upper/lower case.
- `response`: one reply, or a list to pick one at random. `{user}` mentions whoever triggered it.
- `match` (optional):
  - `word` (default): the keyword must appear as whole words ("hank" matches "hi hank!" but not "hankster").
  - `contains`: matches anywhere, even inside other words ("pizza" matches "pizzas").
  - `exact`: the whole message must be the keyword.

Rules are checked top to bottom and only the first match replies. The bot ignores other bots, including itself.

## 4. Run it 24/7 on Railway

1. Sign in at https://railway.com with your GitHub account.
2. Click **New Project → Deploy from GitHub repo** and pick the Hank repo.
3. Open the service's **Variables** tab and add `DISCORD_TOKEN` with your bot token.
4. Railway builds and starts it. `railway.json` tells it to run `python bot.py` and restart it if it ever stops. The **Deployments → Logs** view should show `Logged in as Hank#...`.

No domain or port is needed, since the bot only connects out to Discord.

To change keywords, edit `keywords.json` in the GitHub repo (the pencil icon on github.com works) and commit. Railway redeploys automatically in a minute or so.

## 5. Run it 24/7 on an Ubuntu server (Oracle free tier or any VPS)

On the server:

```bash
sudo apt update && sudo apt install -y python3-venv
sudo useradd --system --create-home --shell /usr/sbin/nologin hank
sudo mkdir -p /opt/hank-bot
# copy bot.py, keywords.json, requirements.txt, hank.service into /opt/hank-bot (scp/sftp)
cd /opt/hank-bot
sudo python3 -m venv venv
sudo venv/bin/pip install -r requirements.txt
echo 'DISCORD_TOKEN=your-token-here' | sudo tee .env
sudo chmod 600 .env && sudo chown -R hank:hank /opt/hank-bot

sudo cp hank.service /etc/systemd/system/
sudo systemctl enable --now hank
```

Useful commands:

- `sudo systemctl status hank` to check it's running
- `journalctl -u hank -f` to watch its log
- `sudo systemctl restart hank` after changing `bot.py`. Changes to `keywords.json` need no restart.

The bot only makes outgoing connections to Discord, so you don't need to open any ports.
