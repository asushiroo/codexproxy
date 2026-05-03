# codexproxy

Single-port reverse proxy for Codex-compatible upstreams with per-client API key dispatch.

## Features

- Listens on one local port for all downstream clients
- Uses one shared `base_url` and one shared `upstream_api_key` for every client
- Dispatches requests by client API key
- Supports `codexproxy new-client` to append one generated client config
- Auto-generates client names as `client-1`, `client-2`, ...
- Auto-generates unique client API keys
- Defaults each new client to `limit=300` and `count=0`
- Persists counts and limits in a JSON config file
- Supports resetting one client or all clients from the CLI
- Prints the client-facing `base_url` at startup
- Prints one log line per request with the latest per-client count
- Supports `record: true` to capture full downstream/upstream debug records
- Exposes a simple usage page at `/<client_api_key>/usage`

## Quick Start

Create a config file:

```bash
uv run codexproxy init-config \
  --config proxy-config.json \
  --base-url https://your-upstream.example/v1 \
  --upstream-api-key sk-your-real-upstream-key \
  --client-count 2
```

Add one more client later:

```bash
uv run codexproxy --config proxy-config.json new-client
```

If `listen_host` is `0.0.0.0`, set `advertise_host` in the config to your real server IP or domain so startup logs print the exact value your downstream clients should fill.

Run the proxy:

```bash
uv run codexproxy --config proxy-config.json run
```

Open one client's usage page:

```text
http://your-server-host:7001/<client_api_key>/usage
```

Reset one client:

```bash
uv run codexproxy --config proxy-config.json reset --client client-1
```

Reset all clients:

```bash
uv run codexproxy --config proxy-config.json reset --all
```

## Usage Page

Each configured client has a built-in usage page:

```text
/<client_api_key>/usage
```

Example:

```text
http://127.0.0.1:7001/sk-client-xxxxxxxx/usage
```

The page shows:

- client name
- masked `client_api_key`
- shared `base_url`
- remaining usage: `limit - count`
- used usage: `count`
- total limit: `limit`
- usage percent

Notes:

- opening the usage page does **not** increment `count`
- the page reads directly from the current JSON config state
- after `reset --client ...` or `reset --all`, the page reflects the new count immediately

## Auth Behavior

The proxy accepts downstream client credentials from any of these headers:

- `Authorization: Bearer <client_api_key>`
- `api-key: <client_api_key>`
- `x-api-key: <client_api_key>`

For upstream requests, the proxy only replaces the auth headers that already exist on the downstream request:

- downstream has `Authorization` -> upstream keeps `Authorization: Bearer <upstream_api_key>`
- downstream has `api-key` -> upstream keeps `api-key: <upstream_api_key>`
- downstream has `x-api-key` -> upstream keeps `x-api-key: <upstream_api_key>`

It does not add missing auth headers for you.

## Config Format

When `record` is `true`, the proxy captures the full downstream request, the rewritten upstream request, and the upstream response. It prints a terminal-friendly truncated summary (each string field capped at 500 words) and saves the full formatted JSON record under `/tmp/codexproxy-records/`.

```json
{
  "listen_host": "0.0.0.0",
  "advertise_host": "your-server-host-or-ip",
  "listen_port": 7001,
  "base_url": "https://your-upstream.example/v1",
  "upstream_api_key": "sk-your-real-upstream-key",
  "record": false,
  "clients": [
    {
      "name": "client-1",
      "client_api_key": "sk-client-...",
      "limit": 300,
      "count": 0
    },
    {
      "name": "client-2",
      "client_api_key": "sk-client-...",
      "limit": 300,
      "count": 0
    }
  ]
}
```
