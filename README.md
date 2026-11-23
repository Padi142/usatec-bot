# Signal TL;DR and Discord cat bots

A bot that lives in Signal group chats. When someone writes

```
/tldr 100
```

it takes the last 100 messages of that group, sends them to an LLM on
[e-INFRA CZ AI-as-a-Service](https://docs.cerit-sc.cz/en/docs/ai-as-a-service/chat-ai),
and replies with a summary of the conversation and its topics. `/tldr` with no number uses `TLDR_DEFAULT_COUNT` (50).

It also understands:

- `/tldr since HH:MM` — summarize everything since that time (in CET/CEST);
  if the time is later today, it summarizes since that time yesterday,
- `/tldr afk` — summarize everything since your last message,
- `/tldr <slug>`, `/tldr 50 <slug>`, `/tldr afk <slug>` — any form can end with a
  model slug, which is used for that one summary.

All forms summarize at most `TLDR_MAX_COUNT` (500) messages — when there are
more, only the newest ones are used.

`/model` shows the current LLM model and the available ones with their live status.
`/model <slug>` changes the model for everyone and survives restarts; `/model default`
goes back to `LLM_MODEL`.

The bot also reacts to group messages by topic: 🐈 cats, 🐦 birds, 🍺 beer, 🚲 bicycles
and 🛏️ sleep. Most messages get no reaction. A decision model (`REACTION_MODEL`, by default
[`vllm-sr-d3-flash`](https://huggingface.co/vllm-sr/d3-flash)) decides this for every message,
so reactions have a small token cost.

The rules can be changed in a group, or in a direct message to the bot:

- `/react` — show the rules,
- `/react add <emoji> <topic>` — add a rule, or add words to the topic that emoji already has (remove it first to replace the topic),
- `/react remove <emoji>` — remove a rule (with no rules left, the bot stops asking the decision model, but photos are still described for `/tldr`).

Pictures get reactions too, so a cat photo gets 🐈 even without a caption. A vision model
(`VISION_MODEL`, by default `gemma4`) describes up to 4 photos of a message (JPEG, PNG or WebP,
up to 5 MB each), including any readable text in screenshots. That description is saved with the
message for `/tldr` summaries, and the decision model judges it together with the text.
d3-flash reads images itself, but e-INFRA's gateway drops them. Videos, GIFs and files aren't judged.

Stickers get reactions too, judged by the emoji their pack gives them: a 😴 sticker gets 🛏️.
The bot downloads each sticker pack's manifest from Signal's CDN once, to read those emojis,
because signal-cli doesn't pass them on.

Rules apply to all groups and survive restarts. Direct messages only accept `/react`, and only
from someone who has already written in one of the bot's groups; everyone else is ignored. If the
bot is linked to your own account, use Note to Self for this.

`/prompt <slug> <prompt>` sends a prompt to a chat model and replies with its answer. It takes the
same slugs as `/model`, so embedding and decision models are rejected. Each model works on one
request at a time, shared with `/tldr`. Signal doesn't render Markdown, so it shows up as plain text.

`/macka` replies with a random cat picture from [kocicividea.cz](https://kocicividea.cz/).

`/mackagen` generates a new cat with [nano-catana](https://research.kocicividea.cz/), using
the `cat-diffusion-v2` model. `/mackagen a cat wearing a tiny hat` generates one from the prompt
with the `flowmatch-cats-tti` text-to-cat model. `/mackagen 2x2`, `/mackagen 3x3`, `/mackagen 4x4`
and `/mackagen 5x5` make a grid of 4, 9, 16 or 25 cats in one picture (only without a prompt). Generating takes around 20 seconds, and
it needs `CAT_GEN_API_KEY` to be set.

`/feature <request>` saves a feature request as a GitHub issue labeled `feature-request`,
and replies with its link. Open requests get built in batches, see
[`.claude/skills/build-features`](.claude/skills/build-features/SKILL.md). The repo is public,
so the request and the sender's display name are public too. Phone numbers are masked and
GitHub mentions don't notify anyone. Each person can send one request every 5 minutes.
It needs `GITHUB_TOKEN`: a fine-grained token with only **Issues: read and write** on this repo,
from an account with write access to it, or GitHub drops the label.

After a deploy with new commits, the bot posts a short "What's new" summary to the groups in
`ALLOWED_GROUPS`, or the commit titles if the LLM is unavailable. It compares the deployed commit
(`SOURCE_COMMIT`, which Coolify sets) with the one each group last heard about, so a plain restart
posts nothing, and a group that missed a post gets it on the next start. Without `SOURCE_COMMIT`
or with `ALLOWED_GROUPS` empty it announces nothing, so a linked personal account never
broadcasts to every group it's in. Set `ANNOUNCE_UPDATES=false` to turn these announcements off.

`/sticker` as a reply to an image adds it to the sticker pack all groups share, and the bot
replies with the new sticker. Tap it to install the pack. `/sticker 😺` gives the sticker an emoji,
and an image sent with `/sticker` as its caption works too. `/sticker remove 3` removes the third
sticker, and `/sticker` on its own shows the pack's link.

Signal sticker packs can't change, so each edit uploads the whole pack again as a new pack, and
it has to be installed again to get the new stickers. Signal allows about 20 uploads a day. The
REST API can't upload packs, so the bot does it itself, with the account credentials it reads
from signal-cli's data volume. It only knows images sent while it was running, and the ones it
sent itself since its last restart.

## How it works

Signal has no API for fetching chat history, so the bot **saves every group
message it receives** in a SQLite database (in the `bot-data` Docker volume) and
summarizes from there. As a result:

- it can only summarize messages sent **after** it joined the group,
- it has to keep running to keep collecting messages,
- pictures are described by the vision model (including text in screenshots), and those descriptions are saved with the messages and included in what gets summarized,
- deleted messages are removed from the database, and edited ones are updated,
- only the newest `HISTORY_LIMIT` messages per group are kept (default 5000).

It's a good idea to tell the group that a bot is storing their messages and
sending them to e-INFRA CZ: for summarization when someone asks, and each new
message as it arrives, photos included, to describe pictures and pick a reaction. See e-INFRA's
[data privacy notes](https://docs.cerit-sc.cz/en/docs/ai-as-a-service/ai-data-privacy).

```
Signal  <-->  signal-cli-rest-api (docker, json-rpc mode)  <-- websocket/HTTP -->  tldr_bot  -->  llm.ai.e-infra.cz
                                                                                      |
                                                                                   SQLite
```

## Setup

You need Docker (or Podman with `podman compose`) and a phone number for the bot.

There are two ways to connect the bot to Signal:

- **A dedicated number (recommended).** The bot is its own group member and its replies come from "the bot".
- **Linking to your own account** as a secondary device. This is easier, but the bot's replies are sent as *you*. The bot also sees every group you're in, so set `ALLOWED_GROUPS`.

### 1. Configure

```sh
cp .env.example .env
$EDITOR .env          # set SIGNAL_NUMBER and LLM_API_KEY
```

To get an e-INFRA API key, open <https://chat.ai.e-infra.cz> and go to Settings → Account →
API keys. Generate an **API key** there, not the JWT token. You can check that the key and
model work:

```sh
curl https://llm.ai.e-infra.cz/v1/chat/completions \
  -H "Authorization: Bearer $LLM_API_KEY" -H 'Content-Type: application/json' \
  -d '{"model": "deepseek", "messages": [{"role": "user", "content": "Ahoj"}]}'
```

`LLM_MODEL` defaults to the `deepseek` alias. Other aliases include `kimi`, `glm` and `mini`
(see the [model list](https://docs.cerit-sc.cz/en/docs/ai-as-a-service/chat-ai)).
Use aliases rather than exact model names, because e-INFRA replaces the models behind them over time.

The bot can use any other OpenAI-compatible endpoint too: set `LLM_BASE_URL` to
the URL that `/chat/completions` gets appended to.

### 2. Start the Signal API container

```sh
docker compose up -d signal-api
```

The API has no authentication, so it isn't published on any host port. You reach it
on the container's IP instead, which only the host can see:

```sh
SIGNAL_API=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' \
  "$(docker compose ps -q signal-api)"):8080
curl "http://$SIGNAL_API/v1/about"
```

In the steps below, replace `localhost:8080` with that address. On a remote server, use an
SSH tunnel instead: `ssh -L 8080:<container-ip>:8080 you@server`. Then `localhost:8080`
works as written, on your own machine.

### 3a. Register a dedicated number

```sh
# Signal usually requires a captcha: solve https://signalcaptchas.org/registration/generate.html
# then copy the "signalcaptcha://..." link from "Open Signal".
curl -X POST -H 'Content-Type: application/json' \
  -d '{"captcha": "signalcaptcha://..."}' \
  'http://localhost:8080/v1/register/+420123456789'

# Enter the code you got by SMS:
curl -X POST 'http://localhost:8080/v1/register/+420123456789/verify/123456'
```

Then add that number to your group from a phone, the same way you'd add any member.

### 3b. Or link it to your existing account

Open <http://localhost:8080/v1/qrcodelink?device_name=tldr-bot> in a browser.
On your phone, go to Signal → Settings → Linked devices → **+** and scan the code.
The code expires after about a minute, so reload the page if scanning fails.

### 4. Start the bot

```sh
docker compose restart signal-api   # so it picks up the newly registered/linked account
docker compose up -d --build
docker compose logs -f bot
```

### Restricting the bot to certain groups

```sh
curl 'http://localhost:8080/v1/groups/+420123456789'
```

Copy the `id` values (`group.xxxx...`) into `ALLOWED_GROUPS`, separated by commas,
and run `docker compose up -d` again.

## Deploying on Coolify

1. Create the resource from this repo and set **Build Pack** to **Docker Compose**
   (Docker Compose location: `/docker-compose.yml`). Nixpacks won't work: it only
   builds the bot and leaves out the Signal API container.
2. Under **Environment Variables**, set `SIGNAL_NUMBER`, `LLM_API_KEY`, `DISCORD_TOKEN`,
   `CAT_GEN_API_KEY` and `GITHUB_TOKEN`. Optionally set `DISCORD_GUILD_ID` to register Discord commands in one server.
   Don't add a domain; these services use outbound connections.
   Don't add port mappings either. Coolify publishes them on all interfaces
   (it drops a `127.0.0.1:` prefix), which would expose the unauthenticated Signal API.
3. Deploy.
4. To link or register the account, find the Signal API container's IP on the server:

   ```sh
   docker ps --format '{{.Names}}' | grep signal-api
   docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' <container-name>
   ```

   Coolify attaches the container to more than one network, so this can print several IPs.
   Any of them works.

   Then, from your own machine, run `ssh -L 8080:<container-ip>:8080 you@server` and follow
   step 3a or 3b above with `localhost:8080`.
5. Once the account is linked, use **Restart** in Coolify so the Signal API picks up the account.

All three services have healthchecks in `docker-compose.yml`, which Coolify shows as the app's status.
The Signal API is healthy when its `/v1/health` endpoint answers. The bot is healthy while it's
connected to the Signal API, so it shows as unhealthy until the account is linked.
The Discord bot becomes healthy once its gateway connection is ready.

For an existing Signal application, keep `/docker-compose.yml` as the Compose location,
load this updated definition, set `DISCORD_TOKEN` and `CAT_GEN_API_KEY`, and redeploy.
The existing Signal volumes keep the linked account and message history.

## Running without Docker

You still need `signal-cli-rest-api` running somewhere (step 1). Then:

```sh
uv sync
uv run --env-file .env python -m tldr_bot   # with SIGNAL_API_URL=http://localhost:8080 in .env
```

## Discord cat bot

The Discord bot runs in its own container alongside Signal and shares its cat API client. It has native slash commands:

- `/macka` — a random cat picture.
- `/mackagen` — generate a cat, with optional `prompt`, `model`, `layout` and `seed` fields.

Model choices are **Automatic**, **Cat diffusion v2**, **Cat diffusion (original)** and **Text-to-cat**.
Automatic picks text-to-cat when you enter a prompt, otherwise cat diffusion v2. Prompts can have
up to 500 characters. Layout choices are single, 2×2, 3×3, 4×4 and 5×5; grids require a
cat-diffusion model and no prompt. A seed from 0 to 4294967295 lets you repeat a generation;
leave it empty for a random seed. These are all the options exposed by the client API;
steps and guidance use its model defaults.

For example, `/mackagen layout:5x5 seed:42` generates 25 cats in one image, and
`/mackagen prompt:a ginger cat wearing a hat` uses text-to-cat.

1. Create an application in the [Discord Developer Portal](https://discord.com/developers/applications).
   On its **Bot** page, generate a token and set `DISCORD_TOKEN` in `.env`. Set `CAT_GEN_API_KEY` too.
2. Under **OAuth2 → URL Generator**, select the `bot` and `applications.commands` scopes and
   the **View Channels**, **Send Messages** and **Attach Files** permissions. Open the generated URL
   to invite the bot to your server. No privileged intents are needed.
3. Optionally set `DISCORD_GUILD_ID` to your server ID to register commands only there.
   Without it, commands register globally. To copy a server ID, enable **Developer Mode** in
   Discord's advanced settings, then right-click the server and choose **Copy Server ID**.
4. Start the bot:

   ```sh
   uv sync
   uv run --env-file .env python -m tldr_bot.discord_bot
   ```

   Or run both bots with Docker Compose, using the Signal configuration described above:

   ```sh
   docker compose up -d --build
   docker compose logs -f discord-bot
   ```

The bot registers slash commands on startup and uploads generated pictures as attachments.
The cat API must include the 4×4 and 5×5 layout update before those layouts work against it.

### Discord on Coolify

Use the existing Signal application with **Docker Compose** as its build pack,
**Base Directory** `/` and **Docker Compose Location** `/docker-compose.yml`.
Keep the Signal settings and add `DISCORD_TOKEN` and `CAT_GEN_API_KEY`; optionally set
`DISCORD_GUILD_ID` to register commands in one server. Leave domains and port mappings empty:
the bot connects to Discord over an outbound connection.

Deploy the application and check its logs for `Synced 2 Discord slash commands` and the Discord
gateway connection. The Compose healthcheck becomes healthy once Discord is ready, and detects
a lost connection or a stalled event loop. Invite the bot as described above before trying `/mackagen`.

## Tests

```sh
uv run pytest
```

## Configuration

All options are environment variables. They're documented in [`.env.example`](.env.example).
