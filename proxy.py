#!/usr/bin/env python3
"""
OpenRouter API Key Rotation Proxy — stdlib only, streaming-friendly.
Uses ThreadingMixIn so multiple requests are handled concurrently.
Streaming responses are forwarded chunk-by-chunk in real-time.
"""

import argparse
import hashlib
import json
import time
import os
import sys
import urllib.request
import urllib.error
import http.client
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from datetime import datetime


# ──────────────────────────────────────────────────────────
# Console Colors & Logging
# ──────────────────────────────────────────────────────────
class C:
    """ANSI color codes for professional console output."""
    BOLD      = "\033[1m"
    DIM       = "\033[2m"
    CYAN      = "\033[96m"
    BLUE      = "\033[94m"
    GREEN     = "\033[92m"
    YELLOW    = "\033[93m"
    RED       = "\033[91m"
    MAGENTA   = "\033[95m"
    WHITE     = "\033[97m"
    GRAY      = "\033[38;5;244m"
    ORANGE    = "\033[38;5;208m"
    BG_BLUE   = "\033[44m"
    RESET     = "\033[0m"


def _ts():
    """Current timestamp as HH:MM:SS."""
    return datetime.now().strftime("%H:%M:%S")


def _prefix(tag, color, emoji):
    return f"{C.DIM}{_ts()}{C.RESET} {color}{C.BOLD}{emoji} {tag}{C.RESET}"


class Log:
    @staticmethod
    def info(msg):
        print(f"{_prefix('INFO', C.CYAN, 'ℹ')}  {C.WHITE}{msg}{C.RESET}", flush=True)

    @staticmethod
    def key(msg, color=C.GREEN):
        print(f"{_prefix('KEYS', C.MAGENTA, '🔑')}  {color}{msg}{C.RESET}", flush=True)

    @staticmethod
    def proxy(msg, color=C.BLUE):
        print(f"{_prefix('PROXY', C.BLUE, '⚡')}  {color}{msg}{C.RESET}", flush=True)

    @staticmethod
    def success(msg):
        print(f"{_prefix('OK  ', C.GREEN, '✓')}  {C.GREEN}{msg}{C.RESET}", flush=True)

    @staticmethod
    def error(msg):
        print(f"{_prefix('ERR ', C.RED, '✗')}  {C.RED}{msg}{C.RESET}", flush=True)

    @staticmethod
    def warn(msg):
        print(f"{_prefix('WARN', C.YELLOW, '⚠')}  {C.ORANGE}{msg}{C.RESET}", flush=True)

    @staticmethod
    def req(msg):
        print(f"{_prefix('REQ ', C.CYAN, '»')}  {C.DIM}{msg}{C.RESET}", flush=True)

    @staticmethod
    def detail(msg):
        print(f"         {C.DIM}{msg}{C.RESET}", flush=True)


def print_banner():
    """Print a professional startup banner."""
    border = f"{C.DIM}{'─' * 58}{C.RESET}"
    print(border, flush=True)
    print(f"{C.BOLD}{C.CYAN}  ⚡  OpenRouter Key Rotation Proxy{C.RESET}", flush=True)
    print(f"{C.DIM}  {C.CYAN}AxionAura{C.RESET}", flush=True)
    print(border, flush=True)

# ---- CLI ARGS ----
parser = argparse.ArgumentParser(description="OpenRouter API Key Rotation Proxy")
parser.add_argument(
    "mode",
    nargs="?",
    default="stream",
    choices=["stream", "mixed"],
    help="stream (default): force streaming for all requests (avoids free-tier 402); mixed: client decides mode",
)
args = parser.parse_args()
force_stream_mode = (args.mode == "stream") or os.environ.get("PROXY_FORCE_STREAM") == "1"

# ---- CONFIG ----
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KEYS_FILE = os.path.join(BASE_DIR, os.environ.get("PROXY_KEYS_FILE", "keys.json"))
KEY_RETRY_FILE = os.environ.get(
    "PROXY_STATE_FILE", os.path.join(BASE_DIR, "key_rotation.json")
)

PROXY_HOST = os.environ.get("PROXY_HOST", "127.0.0.1")
PROXY_PORT = int(os.environ.get("PROXY_PORT", "8900"))
OPENROUTER_URL = "https://openrouter.ai/api/v1"

keys_list = []
key_failures = {}
current_index = 0
start_time = time.time()


def load_keys():
    global keys_list
    try:
        with open(KEYS_FILE) as f:
            data = json.load(f)
        keys_list = data.get("keys", [])
        Log.key(f"Loaded {len(keys_list)} key{'s' if len(keys_list) != 1 else ''}")
    except FileNotFoundError:
        Log.error(f"{KEYS_FILE} not found")
        sys.exit(1)


def load_rotation_state():
    global key_failures, current_index
    try:
        with open(KEY_RETRY_FILE) as f:
            data = json.load(f)
        key_failures = data.get("key_failures", {})
        current_index = data.get("current_index", 0)
        Log.info(f"State restored: key index {current_index}")
    except (FileNotFoundError, json.JSONDecodeError):
        Log.info("No prior state found — starting fresh")


def save_rotation_state():
    os.makedirs(os.path.dirname(KEY_RETRY_FILE), exist_ok=True)
    with open(KEY_RETRY_FILE, "w") as f:
        json.dump({"key_failures": key_failures, "current_index": current_index}, f, indent=2)


def get_next_key():
    global current_index
    total = len(keys_list)
    now = time.time()
    for attempt in range(total):
        idx = (current_index + attempt) % total
        key = keys_list[idx]
        unlocked_at = key_failures.get(key, {}).get("unlocked_at", 0)
        if unlocked_at > 0 and now < unlocked_at:
            Log.detail(f"Key {idx+1} locked, {int(unlocked_at - now)}s remaining")
            continue
        current_index = idx
        return key, 0

    # All exhausted — find soonest
    soonest_idx = 0
    soonest = float("inf")
    for idx, key in enumerate(keys_list):
        u = key_failures.get(key, {}).get("unlocked_at", float("inf"))
        if u < soonest:
            soonest = u
            soonest_idx = idx
    current_index = soonest_idx
    remaining = int(soonest - now)
    Log.warn(f"All keys locked — next available in {remaining}s (key {soonest_idx+1})")
    return keys_list[soonest_idx], remaining


def record_failure(key, error_detail=None):
    state = key_failures.setdefault(key, {"failures": 0, "unlocked_at": 0})
    state["failures"] += 1
    wait = min(60 * (2 ** (state["failures"] - 1)), 3600)
    state["unlocked_at"] = time.time() + wait
    idx = keys_list.index(key)
    detail = f" — {C.DIM}{error_detail}{C.RESET}" if error_detail else ""
    Log.error(f"Key {idx+1} failure #{state['failures']}, locked for {int(wait)}s{detail}")
    save_rotation_state()


def record_success(key):
    if key in key_failures and key_failures[key]["failures"] > 0:
        key_failures[key] = {"failures": 0, "unlocked_at": 0}
        save_rotation_state()


def _build_req(url, key, body):
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {key}")
    # OpenRouter recommended headers
    req.add_header("HTTP-Referer", os.environ.get("PROXY_SITE_URL", "http://localhost"))
    req.add_header("X-Title", os.environ.get("PROXY_SITE_NAME", "OpenRouter Key Rotation Proxy"))
    # Anthropic-compatible headers (only when using Anthropic Claude models)
    if os.environ.get("PROXY_ANTHROPIC_MODE") == "1":
        req.add_header("anthropic-version", "2023-06-01")
        req.add_header("anthropic-beta", "messages-2024-04-01")
    return req


def _is_rate_limited(code, body_bytes):
    if code == 429:
        return True
    if code == 403:
        txt = body_bytes.decode(errors="replace").lower()
        return any(k in txt for k in ["exceeded", "limit", "quota", "rate", "daily"])
    # Only check 402 (payment required) and 503 (unavailable) as rate limit
    if code in (402, 503):
        try:
            data = json.loads(body_bytes)
            msg = (data.get("error", {}).get("message", "")).lower()
            return any(k in msg for k in ["rate", "limit", "quota", "exceeded", "daily", "too_many"])
        except Exception:
            pass
    return False


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        # Suppress default verbose log
        pass

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass  # Client closed connection before/during request — ignore

    def finish(self):
        # Suppress I/O error when connection was closed (streaming with Connection: close)
        try:
            super().finish()
        except (ValueError, BrokenPipeError, ConnectionResetError, OSError):
            pass  # Already closed — nothing to do

    def do_POST(self):
        self._route()

    def do_GET(self):
        if self.path in ("/", "/health"):
            # Check if client wants HTML (browser)
            accept = self.headers.get("Accept", "")
            if "text/html" in accept or self.path == "/":
                html = self._render_status_page()
                body = html.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                body = json.dumps({
                    "status": "ok",
                    "current_key": current_index + 1,
                    "keys_total": len(keys_list),
                    "keys_available": sum(
                        1 for k in keys_list
                        if key_failures.get(k, {}).get("unlocked_at", 0) == 0
                           or time.time() >= key_failures.get(k, {}).get("unlocked_at", 0)
                    ),
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            return
        self._route()

    def _render_status_page(self):
        """Beautiful HTML dashboard for browser status view."""
        now = time.time()
        total = len(keys_list)
        available = sum(
            1 for k in keys_list
            if key_failures.get(k, {}).get("unlocked_at", 0) == 0
               or now >= key_failures.get(k, {}).get("unlocked_at", 0)
        )
        uptime = time.time() - start_time

        status_pill = f'<span class="pill ok">● {available}/{total} Available</span>'

        rows = ""
        for i, key in enumerate(keys_list):
            state = key_failures.get(key, {})
            fail_count = state.get("failures", 0)
            unlocked_at = state.get("unlocked_at", 0)

            if fail_count == 0 and unlocked_at == 0:
                status = '<span class="status ok">Active</span>'
                remaining = "—"
            elif now >= unlocked_at:
                status = '<span class="status ok">Recovered</span>'
                remaining = "—"
                fail_count = 0  # reset display
            else:
                secs = int(unlocked_at - now)
                status = f'<span class="status locked">Locked · {secs}s</span>'
                remaining = f"⏱ {secs}s"

            is_current = ' class="current"' if i == current_index else ""
            key_short = f"...{key[-6:]}" if len(key) > 10 else key.replace(" ", "")
            rows += f"""<tr{is_current}>
                <td class="key-num">{'▶' if i == current_index else ' '}{i+1}</td>
                <td class="key-val">{key_short}</td>
                <td>{status}</td>
                <td class="fail">{fail_count}</td>
                <td class="rem">{remaining}</td>
            </tr>\n"""

        mode_text = 'stream' if force_stream_mode else 'mixed'
        mode_label = f'<span class="mode">{mode_text} mode</span>'
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OpenRouter Proxy — AxionAura</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    background: #0d1117; color: #c9d1d9;
    font-family: 'Segoe UI', -apple-system, BlinkMacSystemFont, sans-serif;
    padding: 2rem; min-height: 100vh;
  }}
  .container {{ max-width: 720px; margin: 0 auto; }}
  h1 {{ color: #58a6ff; font-size: 1.5rem; margin-bottom: .25rem; }}
  .subtitle {{ color: #484f58; font-size: .85rem; margin-bottom: 1.5rem; }}
  .meta {{ display: flex; gap: 1rem; flex-wrap: wrap; margin-bottom: 1.5rem; }}
  .pill {{ padding: .3rem .7rem; border-radius: 999px; font-size: .8rem; font-weight: 600; }}
  .pill.ok {{ background: #23863633; color: #3fb950; }}
  .pill.host {{ background: #1f6feb33; color: #58a6ff; }}
  .pill.mode {{ background: #d2992233; color: #d29922; }}
  .uptime {{ color: #484f58; font-size: .8rem; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{ text-align: left; padding: .5rem .75rem; color: #484f58; font-size: .75rem; text-transform: uppercase; letter-spacing: .05em; border-bottom: 1px solid #21262d; }}
  td {{ padding: .6rem .75rem; border-bottom: 1px solid #161b22; font-size: .85rem; }}
  tr.current {{ background: #1f6feb11; }}
  tr:hover {{ background: #ffffff08; }}
  tr.current:hover {{ background: #1f6feb22; }}
  .key-num {{ font-weight: 700; color: #58a6ff; width: 3rem; }}
  .key-val {{ font-family: 'SF Mono', 'Fira Code', monospace; font-size: .8rem; color: #8b949e; }}
  .status {{ padding: .15rem .5rem; border-radius: 999px; font-size: .75rem; font-weight: 500; }}
  .status.ok {{ background: #23863633; color: #3fb950; }}
  .status.locked {{ background: #da363333; color: #f85149; }}
  .fail {{ color: #f85149; text-align: center; }}
  .rem {{ color: #d29922; }}
  .footer {{ margin-top: 2rem; text-align: center; color: #30363d; font-size: .75rem; }}
  .footer a {{ color: #58a6ff; text-decoration: none; }}
  td.current-indicator {{ width: 4px; border-left: 3px solid #58a6ff; }}
  @keyframes pulse {{ 0%,100% {{ opacity: 1 }} 50% {{ opacity: .5 }} }}
  .pill.ok {{ animation: pulse 3s ease-in-out infinite; }}
</style>
</head>
<body>
<div class="container">
  <h1>⚡ OpenRouter Key Rotation Proxy</h1>
  <p class="subtitle">AxionAura</p>
  <div class="meta">
    {status_pill}
    <span class="pill host">🌐 http://{PROXY_HOST}:{PROXY_PORT}</span>
    <span class="pill mode">🔄 {mode_text} mode</span>
  </div>
  <table>
    <thead><tr><th>#</th><th>Key</th><th>Status</th><th>Fails</th><th>Unlocks</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
  <p class="footer">Built by <a href="https://github.com/AxionAura">AxionAura</a></p>
</div>
</body>
</html>"""

    def _route(self):
        if "chat/completions" in self.path:
            path = "/chat/completions"
        elif "messages" in self.path:
            path = "/messages"
        else:
            # Unknown path — not an API request
            body = json.dumps({"error": "Not found", "path": self.path}).encode()
            self._respond(404, body, "application/json")
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else b""
        if not body:
            self._respond(400, json.dumps({"error": "Empty request body"}).encode(), "application/json")
            return
        # Force streaming when PROXY_FORCE_STREAM is set or --stream mode
        is_stream = force_stream_mode or b'"stream":true' in body or b'"stream": true' in body

        if is_stream:
            self._stream(path, body)
        else:
            self._normal(path, body)

    @staticmethod
    def _inject_usage(body_bytes):
        """Ensure response always has usage.input_tokens so clients don't crash."""
        try:
            data = json.loads(body_bytes)
            if "usage" not in data:
                data["usage"] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
            return json.dumps(data).encode()
        except json.JSONDecodeError:
            return body_bytes

    # ---- non-streaming ----

    def _normal(self, path, body):
        max_a = len(keys_list) + 1
        for attempt in range(1, max_a + 1):
            key, wait = get_next_key()
            if wait > 0:
                Log.warn(f"Waiting {wait}s for key unlock...")
                time.sleep(wait)
            idx = keys_list.index(key)
            Log.req(f"Key {idx+1} (normal) attempt #{attempt}")
            try:
                status, resp_body = self._do_non_stream(path, body, key, idx)
                resp_body = ProxyHandler._inject_usage(resp_body)
                self._respond(status, resp_body, "application/json")
                if status == 200:
                    record_success(key)
                return
            except _RateLimited:
                Log.warn(f"Key {idx+1} rate limited, backing off 1s")
                time.sleep(1)
                continue
            except ConnectionResetError:
                Log.info(f"Client disconnected during normal request #{attempt}")
                return
            except Exception as e:
                Log.error(f"Normal request #{attempt}: {e}")
                record_failure(key, str(e)[:100])
                continue
        save_rotation_state()
        self._respond(429, json.dumps({"error": "All keys rate limited"}).encode(), "application/json")

    def _do_non_stream(self, path, body, key, key_idx):
        """Send request with stream:true internally, parse SSE, return full JSON response.
        This avoids HTTP 402 on free-tier OpenRouter accounts."""
        # Inject stream flag into body
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON body: {e}")
        data["stream"] = True
        stream_body = json.dumps(data).encode()

        url_path = f"/api/v1{path}"
        content_length = str(len(stream_body))
        headers_dict = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "HTTP-Referer": os.environ.get("PROXY_SITE_URL", "http://localhost"),
            "X-Title": os.environ.get("PROXY_SITE_NAME", "OpenRouter Key Rotation Proxy"),
            "Content-Length": content_length,
        }

        conn = http.client.HTTPSConnection("openrouter.ai", timeout=300)
        conn.request("POST", url_path, body=stream_body, headers=headers_dict)
        resp = conn.getresponse()

        # Check for immediate error (rate limit, auth, etc.)
        if resp.status == 429:
            resp.read()
            conn.close()
            raise _RateLimited(resp.status)

        # Collect all response data including first chunk
        buf = b""
        while True:
            chunk_bytes = resp.read(4096)
            if not chunk_bytes:
                break
            buf += chunk_bytes
        conn.close()

        if resp.status != 200:
            return resp.status, buf

        # Parse SSE to accumulate content
        full_text = ""
        finish_reason = ""
        usage = None
        model_name = ""

        # Parse SSE lines from buffer
        for line in buf.decode(errors="replace").split("\n"):
            line = line.strip()
            if line.startswith("data: "):
                payload = line[6:].strip()
                if payload == "[DONE]":
                    finish_reason = "stop"
                    continue
                try:
                    chunk = json.loads(payload)
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        full_text += content
                    model_name = chunk.get("model", model_name)
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    fr = chunk.get("choices", [{}])[0].get("finish_reason")
                    if fr:
                        finish_reason = fr
                except (json.JSONDecodeError, IndexError, KeyError):
                    pass

        # Build normal OpenAI-style response
        resp_id = hashlib.md5(f"{key}_{int(time.time()*1000)}".encode()).hexdigest()[:16]
        result = {
            "id": f"chatcmpl-{resp_id}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model_name or "unknown",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": full_text},
                    "finish_reason": finish_reason or "stop",
                }
            ],
        }
        if usage:
            result["usage"] = usage
        else:
            # Some providers don't send usage in SSE — provide defaults
            # so clients like Claude Code don't crash on missing fields
            result["usage"] = {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
            }

        Log.success(f"Key {key_idx+1} responded (normal via stream)")
        return 200, json.dumps(result).encode()

    def _respond(self, code, body, ct):
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---- streaming ----

    def _stream(self, path, body):
        max_a = len(keys_list) + 1
        for attempt in range(1, max_a + 1):
            key, wait = get_next_key()
            if wait > 0:
                Log.warn(f"Waiting {wait}s for key unlock...")
                time.sleep(wait)
            idx = keys_list.index(key)
            Log.req(f"Key {idx+1} (stream) attempt #{attempt}")
            try:
                self._do_stream(path, body, key, idx)
                return
            except _RateLimited:
                Log.warn(f"Key {idx+1} rate limited, backing off 1s")
                time.sleep(1)
                continue
            except Exception as e:
                Log.error(f"Stream #{attempt}: {e}")
                record_failure(key, str(e)[:100])
                continue
        save_rotation_state()
        self._respond(429, json.dumps({"error": "All keys rate limited"}).encode(), "application/json")

    def _do_stream(self, path, body, key, key_idx):
        url_path = f"/api/v1{path}"
        content_length = str(len(body))
        headers_dict = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "HTTP-Referer": os.environ.get("PROXY_SITE_URL", "http://localhost"),
            "X-Title": os.environ.get("PROXY_SITE_NAME", "OpenRouter Key Rotation Proxy"),
            "Content-Length": content_length,
        }
        if os.environ.get("PROXY_ANTHROPIC_MODE") == "1":
            headers_dict["anthropic-version"] = "2023-06-01"
            headers_dict["anthropic-beta"] = "messages-2024-04-01"

        conn = http.client.HTTPSConnection("openrouter.ai", timeout=300)
        conn.request("POST", url_path, body=body, headers=headers_dict)
        resp = conn.getresponse()

        # Read first chunk to check for rate limit (OpenRouter sends JSON error on first event)
        first_chunk = resp.read(2048)
        if resp.status == 429 or (first_chunk and _is_rate_limited(resp.status, first_chunk)):
            resp.read()
            conn.close()
            raise _RateLimited

        # Send headers to client (once)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("X-Key-Index", str(key_idx + 1))
        self.end_headers()

        # Stream to client
        try:
            if first_chunk:
                self.wfile.write(first_chunk)
                self.wfile.flush()
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            Log.info("Client disconnected mid-stream")
        finally:
            conn.close()

        self.close_connection = True
        Log.success(f"Key {key_idx+1} responded (stream)")
        record_success(key)


class _RateLimited(Exception):
    pass


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    load_keys()
    load_rotation_state()
    server = None
    try:
        server = ThreadedHTTPServer((PROXY_HOST, PROXY_PORT), ProxyHandler)
        print_banner()
        mode_label = "stream (forced)" if force_stream_mode else "mixed (client decides)"
        Log.proxy(f"Listening on {C.BOLD}{C.CYAN}http://{PROXY_HOST}:{PROXY_PORT}{C.RESET}")
        Log.proxy(f"{len(keys_list)} keys loaded, starting from key {C.BOLD}{current_index+1}{C.RESET}")
        Log.proxy(f"Mode: {C.YELLOW}{mode_label}{C.RESET}")
        Log.proxy(f"Env: config from {C.DIM}.env{C.RESET} or {C.DIM}systemd{C.RESET}")
        print(f"{C.DIM}{'─' * 58}{C.RESET}", flush=True)
        Log.info("Ready to accept connections\n")
        server.serve_forever()
    except KeyboardInterrupt:
        if server:
            print(f"\n{C.DIM}{'─' * 58}{C.RESET}", flush=True)
            Log.info(f"{C.BOLD}Shutting down gracefully...{C.RESET}")
            server.shutdown()
