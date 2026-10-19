# Hank bot

A Discord bot that replies to keywords with custom responses. Each server sets its own keywords with `/hank` commands, so one bot can serve several servers.

## 1. Create the bot in Discord

1. Go to https://discord.com/developers/applications and click **New Application**. Name it Hank.
2. Open the **Bot** tab. Click **Reset Token**, copy the token, and keep it secret.
3. On the same tab, under **Privileged Gateway Intents**, turn on **Message Content Intent** and save. Without this the bot can't read messages.
4. Open **OAuth2 → URL Generator**. Tick the `bot` and `applications.commands` scopes, then the permissions **View Channels**, **Send Messages**, **Embed Links** and **Read Message History**. Embed Links lets links in Hank's replies, like a YouTube video or Google Sheet, show a preview.
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

- `/hank add keyword:<word> response:<reply> [match:] [cooldown:]` adds a reply. Add the same keyword again with another response and Hank picks one at random. `cooldown` is optional: the number of minutes Hank waits before replying to that keyword again on this server.
- `/hank cooldown keyword:<word> minutes:<n>` changes a keyword's cooldown. `0` turns it off.
- `/hank remove keyword:<word>` removes a keyword and all its replies.
- `/hank command-add name:<name> response:<reply>` makes a `!name` command. When a message starts with `!name`, Hank replies. Add the same name again with another response and Hank picks one at random.
- `/hank command-remove name:<name>` deletes a `!` command.
- `/hank user-add user:@someone response:<reply> [cooldown:]` makes Hank reply to every message that person posts. `cooldown` is optional: the number of minutes to wait before replying to them again. Add the same user again with another response and Hank picks one at random.
- `/hank user-remove user:@someone` stops replying to that person.
- `/hank bot-add response:<reply> [bot:] [contains:] [number:] [number_after:] [cooldown:]` makes Hank reply to other bots' posts. Narrow it down with any mix of:
  - `bot`: only that bot (default: any bot).
  - `contains`: only posts containing that text.
  - `number`: only when the post's number matches `< 6`, `<= 5`, `> 15`, `>= 18`, `= 20` (or just `20`), or a range like `1-5`.
  - `number_after`: which number to read, e.g. `Total` reads the number after "Total:". By default Hank uses the number after `Total`, `Result` or `=`, otherwise the last number, and ignores dice notation like `1d20`.

  In the reply, `{user}` mentions who the post is for (whoever ran the bot's command, or the first person it mentions) and `{number}` is the number Hank read. Add the same rule again for random replies. Example for a dice bot: `/hank bot-add bot:@Dice number:< 6 number_after:Total response:Ouch {user}, only a {number}`.
- `/hank bot-remove rule:<rule>` stops one of those (pick it from the list).
- `/hank ignore-add user:@someone` makes Hank never reply to that person on this server: no `!` commands, auto-replies or keywords. Pick a bot to ignore its posts too, and bot posts made for an ignored person (like their dice rolls) are skipped as well.
- `/hank ignore-remove user:@someone` lets Hank reply to them again.
- `/hank list` shows this server's `!` commands, keywords, user and bot auto-replies, and who's ignored. Only you see the answer.

`!` commands come first, then user auto-replies, then keywords. While a user auto-reply or keyword is on cooldown, the message can still trigger the next thing in line. Hank sends at most one reply per message. Posts from other bots only ever trigger `/hank bot-add` replies, and Hank never replies to itself.

Matching ignores upper/lower case, and `{user}` in a reply mentions whoever triggered it. `match` is optional:

- whole word (default): "hank" matches "hi hank!" but not "hankster".
- anywhere: "pizza" matches "pizzas".
- whole message only: the message must be exactly the keyword.

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

### Landing page (optional)

When Railway gives the service a port, Hank also serves a small landing page from `web/` with what Hank does, the command list and an **Add to Server** button. `/invite` redirects straight to the invite link.

1. Open the service's **Settings → Networking** and click **Generate Domain** to get a free `*.up.railway.app` address. Railway sets the `PORT` variable for you.
2. For your own domain, click **Custom Domain** in the same place, enter it (for example `hank.example.com`), and add the CNAME (and any TXT verification) record Railway shows at your DNS provider. HTTPS is set up automatically once DNS resolves.

The invite button uses the bot's application ID automatically. Set `DISCORD_CLIENT_ID` only if you want it to point at a different application. Without a port (locally, or on the Ubuntu server) no web server starts.

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
