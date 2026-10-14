# Hank bot

A Discord bot that replies to keywords with custom responses. Each server sets its own keywords with `/hank` commands, so one bot can serve several servers.

## 1. Create the bot in Discord

1. Go to https://discord.com/developers/applications and click **New Application**. Name it Hank.
2. Open the **Bot** tab. Click **Reset Token**, copy the token, and keep it secret.
3. On the same tab, under **Privileged Gateway Intents**, turn on **Message Content Intent** and save. Without this the bot can't read messages.
4. Open **OAuth2 → URL Generator**. Tick the `bot` and `applications.commands` scopes, then the permissions **View Channels**, **Send Messages** and **Read Message History**.
5. Open the generated URL, pick your server, and authorize. Use the same URL to add Hank to more servers.

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

## 3. Set keywords

### From Discord (per server)

Anyone with the **Manage Server** permission can use these. Each server's keywords are separate.

- `/hank add keyword:<word> response:<reply> [match:]` adds a reply. Add the same keyword again with another response and Hank picks one at random.
- `/hank remove keyword:<word>` removes a keyword and all its replies.
- `/hank list` shows this server's keywords. Only you see the answer.

Matching ignores upper/lower case, and `{user}` in a reply mentions whoever triggered it. `match` is optional:

- whole word (default): "hank" matches "hi hank!" but not "hankster".
- anywhere: "pizza" matches "pizzas".
- whole message only: the message must be exactly the keyword, which suits commands like `!rules`.

To let other roles use the commands, go to **Server Settings → Integrations → Hank**.

Server keywords are stored in `data/hank.db`, or in `$HANK_DATA_DIR/hank.db` if that variable is set.

### Global keywords (every server)

`keywords.json` holds keywords that apply on every server. It's empty by default. Server keywords are checked first. Changes to the file are picked up without a restart.

```json
{
  "rules": [
    { "keywords": ["hello hank", "hi hank"], "response": "Hey {user}!" },
    { "keywords": "good bot", "response": ["Thanks!", "🥹"] },
    { "keywords": "!rules", "match": "exact", "response": "Be kind." }
  ]
}
```

## 4. Run it 24/7 on Railway

1. Sign in at https://railway.com with your GitHub account.
2. Click **New Project → Deploy from GitHub repo** and pick the Hank repo.
3. Open the service's **Variables** tab and add `DISCORD_TOKEN` with your bot token, plus `HANK_DATA_DIR` set to `/data`.
4. Add a **Volume** to the service (right-click the service, or the **+ New** menu) and set its mount path to `/data`. Without a volume, keywords added with `/hank` are wiped on every redeploy.
5. Railway builds and starts it. `railway.json` tells it to run `python bot.py` and restart it if it ever stops. The **Deployments → Logs** view should show `Logged in as Hank#...`.

No domain or port is needed, since the bot only connects out to Discord.

Code changes pushed to GitHub redeploy automatically. Keywords set with `/hank` survive redeploys because they live on the volume.

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
- `sudo systemctl restart hank` after changing `bot.py`. Server keywords live in `/opt/hank-bot/data`.

The bot only makes outgoing connections to Discord, so you don't need to open any ports.
