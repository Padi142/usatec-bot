# Signal TL;DR bot

A bot that lives in Signal group chats. When someone writes

```
/tldr 100
```

it takes the last 100 messages of that group, sends them to an LLM on
[e-INFRA CZ AI-as-a-Service](https://docs.cerit-sc.cz/en/docs/ai-as-a-service/chat-ai),
and replies with a summary of the conversation and its topics. `/tldr` with no number uses `TLDR_DEFAULT_COUNT` (50).

## How it works

Signal has no API for fetching chat history, so the bot **saves every group
message it receives** in a SQLite database (in the `bot-data` Docker volume) and
summarizes from there. As a result:

- it can only summarize messages sent **after** it joined the group,
- it has to keep running to keep collecting messages,
- deleted messages are removed from the database, and edited ones are updated,
- only the newest `HISTORY_LIMIT` messages per group are kept (default 5000).

It's a good idea to tell the group that a bot is storing their messages
and sending them to e-INFRA CZ for summarization. See e-INFRA's
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
2. Under **Environment Variables**, set `SIGNAL_NUMBER` and `LLM_API_KEY`.
   Don't add a domain; neither service should be reachable from outside.
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

## Running without Docker

You still need `signal-cli-rest-api` running somewhere (step 1). Then:

```sh
uv sync
uv run --env-file .env python -m tldr_bot   # with SIGNAL_API_URL=http://localhost:8080 in .env
```

## Tests

```sh
uv run pytest
```

## Configuration

All options are environment variables. They're documented in [`.env.example`](.env.example).
