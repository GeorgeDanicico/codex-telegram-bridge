# Codex Telegram Bridge

This is a small, dependency-free Telegram bot that long-polls Telegram and sends prompts to the authenticated local Codex CLI.

Supported commands:

- `/start`, `/help`, `/status`, `/projects`, `/server`
- `/wiki <prompt>` for questions or updates in the configured workspace
- `/car` for vehicle status, text-only location details, and confirmed controls
- `/quick <prompt>` and `/deep <prompt>` for explicit Codex profiles
- `/cancel` to stop the active request for a chat

Ordinary text is sent to Codex as a prompt too.

## Configuration

Create the private environment file and fill in the bot token:

```bash
cp .env.example .env
chmod 600 .env
```

The process environment takes precedence over values in `.env`. The important settings are:

- `TELEGRAM_BOT_TOKEN`: token from `@BotFather`.
- `TELEGRAM_ALLOWED_USER_IDS`: optional comma-separated allowlist. Leave it blank to allow every Telegram user who can reach the bot.
- `CODEX_WORKDIR`: directory passed to Codex for normal prompts.
- `PROJECTS_DIR`: directory listed by `/projects`.
- `WIKI_WORKDIR`: directory passed to Codex for `/wiki`.
- `CODEX_BIN`, `CODEX_MODEL`, `CODEX_TIMEOUT_SECONDS`, and `CODEX_UNSAFE_MODE`.
- `CODEX_FAST_*`, `CODEX_DEFAULT_*`, and `CODEX_DEEP_*` for profile-specific model and reasoning settings.
- `SKODA_GATEWAY_URL`, `SKODA_GATEWAY_TOKEN`, `SKODA_REQUEST_TIMEOUT_SECONDS`, `SKODA_SNAPSHOT_CACHE_SECONDS`, and `SKODA_ACTION_COOLDOWN_SECONDS` for `/car`.
- The JVM gateway reads `SKODA_EMAIL`, `SKODA_PASSWORD`, `SKODA_VIN`, and `SKODA_SPIN` from `/home/r3k1Nu/projects/skoda-mcp-server/skoda-mcp/.env.local` in Compose. Those values stay out of the Telegram bridge container.

The checked-in example uses paths inside the container: `/workspace` for this checkout and `/projects` for the mounted parent directory. Use paths that exist in the environment where the bridge runs. Both the bridge and the JVM gateway use host networking because the existing `api-skoda` deployment publishes its application API on host loopback port `8090`.

## Docker Compose

The Compose service reads the application settings from `.env`; it does not replace them with hard-coded application values. It mounts:

- the checkout at `/workspace`;
- the checkout's parent directory, read-only, at `/projects`;
  - the host Codex login directory at `/home/bridge/.codex`.

Start it with the host user's UID and GID so Codex can use the mounted login directory and keep generated files owned by you:

```bash
LOCAL_UID="$(id -u)" LOCAL_GID="$(id -g)" docker compose up --build -d
docker compose logs -f codex-telegram-bridge
```

Stop it with:

```bash
docker compose down
```

Set `CODEX_AUTH_DIR` or `PROJECTS_HOST_DIR` in the Compose environment when the host directories differ from `$HOME/.codex` and the checkout's parent directory. These are host-side mount sources; `CODEX_WORKDIR`, `PROJECTS_DIR`, and `WIKI_WORKDIR` remain the paths visible inside the container.

The image does not contain `.env` or Codex credentials. Compose passes the values at runtime through `env_file`, and the Codex login is supplied through the mounted host directory. Log in to Codex on the host before starting the service.

The `/car` command calls the sibling JVM `skoda-mcp` gateway, which uses the `skoda-api-client` OpenAPI-generated Java client and its token service. It sends vehicle status, range, address, and latitude/longitude as text; it never sends a Telegram map message. Flash, honk-and-flash, lock, and unlock require a second confirmation tap. The gateway owns the MySkoda credentials and S-PIN.

### Skoda gateway setup

`skoda-api-client` is a Maven library, so the Compose service builds and runs the sibling `skoda-mcp` Spring Boot gateway in a JVM container. Before starting the stack, fill the real account values in:

```text
/home/r3k1Nu/projects/skoda-mcp-server/skoda-mcp/.env.local
```

That file must contain non-placeholder values for `SKODA_EMAIL`, `SKODA_PASSWORD`, `SKODA_VIN`, and the four-digit `SKODA_SPIN`. Set the same non-empty `SKODA_GATEWAY_TOKEN` in this bridge's `/home/r3k1Nu/projects/codex-telegram-bridge/.env`; Compose passes that value to both the bot and the gateway. Keep the existing `api-skoda` container running on `127.0.0.1:8090`.

Start or redeploy the bot and JVM gateway from this directory with:

```bash
LOCAL_UID="$(id -u)" LOCAL_GID="$(id -g)" docker compose up --build -d
```

The gateway listens on `127.0.0.1:8091`. In Telegram, send `/car`, then use Refresh for a text snapshot or confirm one of the Flash lights, Honk + flash, Lock, or Unlock actions. No map is sent; location is rendered as address and coordinates when available.

## Docker without Compose

```bash
docker build --build-arg CODEX_VERSION=latest -t codex-telegram-bridge .
docker run --rm --init \
  --name codex-telegram-bridge \
  --user "$(id -u):$(id -g)" \
  --env-file .env \
  -e HOME=/home/bridge \
  -e CODEX_HOME=/home/bridge/.codex \
  -v "$HOME/.codex:/home/bridge/.codex" \
  -v "$PWD/..:/projects:ro" \
  -v "$PWD:/workspace" \
  codex-telegram-bridge
```

Do not run another long-polling instance with the same bot token at the same time. If the bot previously used a webhook, remove it before starting polling:

```bash
curl -X POST "https://api.telegram.org/bot<your-token>/deleteWebhook"
```

## Safety

`CODEX_UNSAFE_MODE=true` grants Codex unrestricted execution inside the container. Keep `TELEGRAM_ALLOWED_USER_IDS` restricted when the bot is exposed to other Telegram users. The container has access to the mounted workspace and Codex login directory, so treat the bot token and environment file as secrets.

## Checks

Run the unit tests locally with:

```bash
python3 -m unittest -v
```
