#!/usr/bin/env python3
"""
OpenRouter API Key Rotation Proxy — stdlib only, streaming-friendly.
Uses ThreadingMixIn so multiple requests are handled concurrently.
Streaming responses are forwarded chunk-by-chunk in real-time.
"""

import json
import time
import os
import sys
import urllib.request
import urllib.error
import http.client
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

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


def load_keys():
    global keys_list
    try:
        with open(KEYS_FILE) as f:
            data = json.load(f)
        keys_list = data.get("keys", [])
        print(f"[keys] Loaded {len(keys_list)} keys", flush=True)
    except FileNotFoundError:
        print(f"[error] {KEYS_FILE} not found", flush=True)
        sys.exit(1)


def load_rotation_state():
    global key_failures, current_index
    try:
        with open(KEY_RETRY_FILE) as f:
            data = json.load(f)
        key_failures = data.get("key_failures", {})
        current_index = data.get("current_index", 0)
        print(f"[state] Restored: current={current_index}", flush=True)
    except (FileNotFoundError, json.JSONDecodeError):
        pass


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
            print(f"  [skip] key {idx+1} locked, {int(unlocked_at - now)}s", flush=True)
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
    print(f"[all locked] next in {remaining}s (key {soonest_idx+1})", flush=True)
    return keys_list[soonest_idx], remaining


def record_failure(key):
    state = key_failures.setdefault(key, {"failures": 0, "unlocked_at": 0})
    state["failures"] += 1
    wait = min(60 * (2 ** (state["failures"] - 1)), 3600)
    state["unlocked_at"] = time.time() + wait
    idx = keys_list.index(key)
    print(f"  [fail] Key {idx+1} #{state['failures']}, unlock {int(wait)}s", flush=True)
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
    if code in (400, 402, 500, 503):
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
            now = time.time()
            available = sum(
                1 for k in keys_list
                if key_failures.get(k, {}).get("unlocked_at", 0) == 0
                   or now >= key_failures.get(k, {}).get("unlocked_at", 0)
            )
            body = json.dumps({
                "status": "ok",
                "current_key": current_index + 1,
                "keys_total": len(keys_list),
                "keys_available": available,
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._route()

    def _route(self):
        if "chat/completions" in self.path:
            path = "/chat/completions"
        else:
            path = "/messages"

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else b""
        # Force non-streaming for Claude Code stability over proxy
        is_stream = b'"stream":true' in body or b'"stream": true' in body

        if is_stream:
            self._stream(path, body)
        else:
            self._normal(path, body)

    # ---- non-streaming ----

    def _normal(self, path, body):
        max_a = len(keys_list) + 1
        for attempt in range(1, max_a + 1):
            key, wait = get_next_key()
            if wait > 0:
                print(f"[wait] {wait}s", flush=True)
                time.sleep(wait)
            idx = keys_list.index(key)
            print(f"[try] Key {idx+1} (normal) #{attempt}", flush=True)
            url = f"{OPENROUTER_URL}{path}"
            try:
                req = _build_req(url, key, body)
                with urllib.request.urlopen(req, timeout=300) as resp:
                    cb = resp.read()
                    self._respond(resp.status, cb, "application/json")
                    print(f"  [ok] Key {idx+1}", flush=True)
                    return
            except urllib.error.HTTPError as e:
                eb = e.read() if hasattr(e, "read") else b""
                if _is_rate_limited(e.code, eb):
                    record_failure(key)
                    continue
                self._respond(e.code, eb, "application/json")
                return
            except Exception as e:
                print(f"  [err] {e}", flush=True)
                record_failure(key)
                continue
        save_rotation_state()
        self._respond(429, json.dumps({"error": "All keys rate limited"}).encode(), "application/json")

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
                print(f"[wait] {wait}s", flush=True)
                time.sleep(wait)
            idx = keys_list.index(key)
            print(f"[try] Key {idx+1} (stream) #{attempt}", flush=True)
            try:
                self._do_stream(path, body, key, idx)
                return
            except _RateLimited:
                record_failure(key)
                continue
            except Exception as e:
                print(f"  [err] {e}", flush=True)
                record_failure(key)
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
        self.send_header("Connection", "close")
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
            print(f"  [client disconnect]", flush=True)
        finally:
            conn.close()

        print(f"  [ok] Key {key_idx+1} (stream)", flush=True)


class _RateLimited(Exception):
    pass


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    load_keys()
    load_rotation_state()
    server = ThreadedHTTPServer((PROXY_HOST, PROXY_PORT), ProxyHandler)
    print(f"[proxy] Starting on http://{PROXY_HOST}:{PROXY_PORT}", flush=True)
    print(f"[proxy] {len(keys_list)} keys from #{current_index+1}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
