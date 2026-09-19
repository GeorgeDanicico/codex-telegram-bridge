# Codex Telegram Bridge

A deliberately small, dependency-free Telegram bot that long-polls Telegram and passes ordinary text to the authenticated local `codex` CLI.

It has these basic bot operations:

- `/start`, `/help`, `/status`, `/projects`, `/server`
- `/wiki <prompt>` to ask, explore, or update the LLM wiki
- `/car` for Skoda status, range, a Telegram map pin, and vehicle controls
- `/quick <prompt>` and `/deep <prompt>` for explicit Codex profiles
- `/cancel` to stop the active request for that chat

## Setup

1. Create a Telegram bot with `@BotFather` and copy its token.
2. In [.env](.env), replace `PASTE_TOKEN_FROM_BOTFATHER` with that token.
3. Start the bridge:

   ```bash
   cd /home/george/codex-telegram-bridge
   python3 bot.py
   ```

The bridge calls the already-authenticated local Codex CLI, so it does not need an `OPENAI_API_KEY`.

## Docker

The image includes Python and the Codex CLI, while keeping the Telegram token, Skoda gateway token, and Codex login outside the image. Build it with:

```bash
docker build --build-arg CODEX_VERSION=latest -t codex-telegram-bridge .
```

Run it with the bridge configuration, your existing Codex login, and the directory Codex may work in. The `--user` flag makes files created by Codex retain your host ownership.

```bash
docker run --rm --init \
  --name codex-telegram-bridge \
  --user "$(id -u):$(id -g)" \
  --env-file .env \
  -e CODEX_WORKDIR=/workspace \
  -e PROJECTS_DIR=/home/bridge/Projects \
  -e SKODA_GATEWAY_URL=http://host.docker.internal:8090 \
  --add-host=host.docker.internal:host-gateway \
  -v "$HOME/.codex:/home/bridge/.codex" \
  -v "$HOME/Projects:/home/bridge/Projects" \
  -v "$PWD:/workspace" \
  codex-telegram-bridge
```

This example reaches a Skoda gateway running on the Docker host. If the gateway runs in another container, put both services on the same Docker network and set `SKODA_GATEWAY_URL` to that container's service name instead. Do not run the container alongside the systemd bridge service: Telegram permits only one long-poller per bot token.

### Complete Docker Compose stack

`compose.yaml` builds native images from `/home/george/Projects/skoda-mcp-server` for `api-skoda` and `skoda-mcp`, then starts this bridge image against the gateway service. Prepare the gateway secrets without adding them to Git:

```bash
cp skoda-gateway.env.example skoda-gateway.env
chmod 600 skoda-gateway.env
# Edit skoda-gateway.env with your MySkoda credentials and gateway token.
```

Start the stack (the native-image builds can take several minutes the first time):

```bash
LOCAL_UID="$(id -u)" LOCAL_GID="$(id -g)" docker compose up --build
```

The Compose services publish the API, its health endpoint, and gateway only on loopback ports `8080`, `8888`, and `8090`. The bridge uses the internal `skoda-mcp:8090` address and mounts `~/Projects` at `/home/bridge/Projects`, so `/projects` lists the host project folders. To use a Skoda source checkout elsewhere, set `SKODA_SOURCE_DIR` to its absolute path. To use an alternate gateway credentials file, set `SKODA_GATEWAY_ENV_FILE`.

## Execution and access

`CODEX_UNSAFE_MODE=true` is set in `.env` as requested. Each prompt then runs Codex with `--dangerously-bypass-approvals-and-sandbox`, which gives it unrestricted local execution.

`TELEGRAM_ALLOWED_USER_IDS` is blank by default, so any Telegram account that can message the bot can submit prompts. That combination is equivalent to giving those accounts unrestricted access to this machine through Codex. To limit access to you, put your numeric Telegram ID in `TELEGRAM_ALLOWED_USER_IDS` before starting the bot.

The bot intentionally keeps no chat memory: every Codex request is an independent `codex exec --ephemeral` run. This keeps the initial communication test simple and predictable.

`/wiki <prompt>` runs Codex in `WIKI_WORKDIR`, which defaults to `PROJECTS_DIR/llm-wiki`. Codex discovers that repository's `AGENTS.md`, so ordinary questions use its inquiry workflow while explicit requests to save or remember something use its capture workflow. Wiki requests are stateless; include the relevant question or context in each follow-up.

`/server` reports OS type, sampled CPU usage, RAM usage and available memory, CPU temperature, and available space on `/`. CPU temperature is shown as `unavailable` when the host does not expose a readable thermal sensor, which is common on virtual machines and non-Linux systems.

`/car` uses the authenticated local Skoda HTTP gateway by default. It does not start Codex or ask a model to select tools. The gateway aggregates the configured vehicle's identity, range, battery level, door/window/lock state, and location; when coordinates are available Telegram displays a native map card. The summary includes Refresh, Flash lights, Honk + flash, Lock, and Unlock buttons. Commands that affect the vehicle require a second confirmation tap and are never retried automatically.

For safety, `/car` and all car-control callbacks are disabled unless `TELEGRAM_ALLOWED_USER_IDS` contains at least one numeric Telegram user ID. Keep that allowlist restricted to people who are authorized to view and control the vehicle.

Configure the gateway with `SKODA_TRANSPORT=http`, `SKODA_GATEWAY_URL`, `SKODA_GATEWAY_TOKEN`, `SKODA_REQUEST_TIMEOUT_SECONDS`, and `SKODA_SNAPSHOT_CACHE_SECONDS`. `SKODA_TRANSPORT=mcp` remains as a temporary read/action rollback path while migrating the local gateway; it should not be the normal configuration.

Codex-backed requests use explicit profiles: `/quick` selects `CODEX_FAST_MODEL` and low reasoning, ordinary text and `/wiki` select `CODEX_DEFAULT_MODEL` and medium reasoning, and `/deep` selects `CODEX_DEEP_MODEL` and high reasoning. The runner passes both the model and `model_reasoning_effort`, so a fast model never accidentally inherits a high global reasoning setting.

If the bot previously had a webhook configured, remove the webhook before using long polling:

```bash
curl -X POST "https://api.telegram.org/bot<your-token>/deleteWebhook"
```

## Local services

Copy `.env.example` to `~/.config/codex-telegram-bridge/bridge.env` and put the MySkoda credentials plus the same random gateway token in `~/.config/codex-telegram-bridge/skoda-gateway.env` (mode `0600`). The service templates are in `systemd/`. The local API proxy on port 8080 must run before the gateway on port 8090. After building both Java projects, install the units and run `systemctl --user daemon-reload && systemctl --user enable --now api-skoda.service skoda-gateway.service codex-telegram-bridge.service`.

Do not run `bot.py` manually while the service is active: Telegram permits only one long-poller per bot token.

For a manual development run, use three terminals in this order:

```bash
# Terminal 1: local API proxy
cd /home/george/Projects/skoda-mcp-server/api-skoda
mvn spring-boot:run

# Terminal 2: aggregate gateway
cd /home/george/Projects/skoda-mcp-server/skoda-mcp
set -a
source ~/.config/codex-telegram-bridge/skoda-gateway.env
set +a
mvn spring-boot:run -Dspring-boot.run.profiles=gateway

# Terminal 3: Telegram bridge
cd /home/george/codex-telegram-bridge
python3 bot.py
```

Verify ports `8080` and `8090` before testing `/car`:

```bash
curl http://127.0.0.1:8888/actuator/health
curl http://127.0.0.1:8090/actuator/health
```
