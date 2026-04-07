# OpenRouter Key Rotation Proxy

A lightweight, stdlib-only Python proxy for the OpenRouter API with automatic API key rotation, streaming support, and a built-in browser dashboard.

## Features

- **Key rotation**: Automatically rotates through multiple API keys when one hits rate limits
- **Exponential backoff**: Locked keys are temporarily disabled (60s → 120s → 240s → max 1h)
- **Streaming by default**: Forces `stream: true` internally — avoids free-tier 402 errors
- **Concurrent**: Threaded request handling via `ThreadingMixIn`
- **State persistence**: Rotation state survives restarts
- **Color console output**: Professional timestamped logs with emoji indicators
- **Browser dashboard**: Beautiful dark-mode status page at `http://localhost:8900`
- **Usage field injection**: All responses include `usage` object — prevents client-side crashes
- **Error handling**: Graceful handling of empty bodies, unknown paths, and client disconnects
- **Zero dependencies**: Python standard library only

## Setup

### 1. Add your API keys

Create `keys.json` from the example:

```bash
cp keys.json.example keys.json
```

Edit `keys.json` and add your OpenRouter API keys:

```json
{
  "keys": [
    "sk-or-v1-your-key-1",
    "sk-or-v1-your-key-2"
  ]
}
```

### 2. Start the proxy

Default (stream mode — forces `stream: true` internally):

```bash
python3 proxy.py
```

Mixed mode (client decides):

```bash
python3 proxy.py mixed
```

### 3. Configure your client

Point your `ANTHROPIC_BASE_URL` to the proxy:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://localhost:8900",
    "ANTHROPIC_AUTH_TOKEN": "any-string-works",
    "ANTHROPIC_MODEL": "your-model-here"
  }
}
```

### 4. Run as a systemd service (optional)

```bash
sudo cp openrouter-proxy.service /etc/systemd/system/
sudo systemctl enable --now openrouter-proxy
```

## Configuration

Environment variables:

| Variable | Default | Description |
|---|---|---|
| `PROXY_HOST` | `127.0.0.1` | Host to bind to |
| `PROXY_PORT` | `8900` | Port to listen on |
| `PROXY_KEYS_FILE` | `keys.json` | Path to keys file |
| `PROXY_STATE_FILE` | `./key_rotation.json` | Path to rotation state file |
| `PROXY_ANTHROPIC_MODE` | off (unset) | Set to `1` to add Anthropic-compatible headers |
| `PROXY_SITE_URL` | `http://localhost` | HTTP-Referer header |
| `PROXY_FORCE_STREAM` | `1` (on by default) | Force streaming for all requests |
| `PROXY_SITE_NAME` | `OpenRouter Key Rotation Proxy` | X-Title header |

## How it works

```
Client -> Proxy (localhost:8900) -> OpenRouter API
```

- If a key gets 429/403 (rate limited), proxy auto-switches to the next key
- Failed keys are locked with exponential backoff: 60s → 120s → 240s → ... → max 1hr
- Locked keys are retried after their cooldown period

## Key rotation flow

```
Key 1 -> rate limited -> Key 2 -> rate limited -> Key 3
  ^                                              |
Key 10 <- rate limited <- Key 9 <- ... <- Key 4 -+
```

## Browser dashboard

Open `http://localhost:8900` in your browser to see a real-time dark-mode status page with key availability, countdown timers, and failure counters.

## Console output

Color-coded, timestamped logs:

```
──────────────────────────────────────────────────────────
  ⚡  OpenRouter Key Rotation Proxy
  AxionAura
──────────────────────────────────────────────────────────
14:10:01 ⚡ PROXY  Listening on http://127.0.0.1:8900
14:10:01 ⚡ PROXY  3 keys loaded, starting from key 1
14:10:21 » REQ   Key 1 (stream) attempt #1
14:10:25 ✓ OK    Key 1 responded (stream)
14:10:26 ⚠ WARN  Key 1 rate limited, backing off 1s
```

## Health check

JSON response (machine-readable):

```bash
curl -H "Accept: application/json" http://localhost:8900/health
```

Response:

```json
{
  "status": "ok",
  "current_key": 1,
  "keys_total": 10,
  "keys_available": 8
}
```

## License

MIT
