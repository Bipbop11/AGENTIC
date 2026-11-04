#!/usr/bin/env python3
"""proxy.py: the naive agent-traffic proxy for Lab 4.

A reverse proxy that prints every request and response and forwards them
unchanged. Standard library only; nothing to install.

    python3 proxy.py                       # listens on http://localhost:8041
    ANTHROPIC_BASE_URL=http://localhost:8041 claude

Everything Claude Code sends to the API now passes through this program and is
printed to the terminal. The proxy changes nothing: headers go through untouched
(a subscription login needs its bearer token and its `anthropic-beta` header),
and the reply is streamed back byte for byte as it arrives.

Open http://localhost:8041 in a browser for a live dashboard of the decoded
conversation (see SPEC.md). `GET /` and `GET /events` are served locally; every
other path is forwarded.

    python3 proxy.py --port 9000
    python3 proxy.py --upstream https://chatgpt.com/backend-api/codex   # for Codex
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import queue
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

# Headers that describe one hop of the connection, not the message. They are
# never forwarded. `accept-encoding` is dropped too, so the API answers in plain
# text and what we print is readable.
SKIP = {
    "host", "connection", "keep-alive", "transfer-encoding", "te", "trailers",
    "upgrade", "proxy-authorization", "proxy-authenticate", "accept-encoding",
    "content-length",
}

UPSTREAM = "https://api.anthropic.com"

# How many past message lists to remember for turn deduplication.
MAX_SNAPSHOTS = 300


def stamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


# ---------------------------------------------------------------------------
# Dashboard state: decodes /v1/messages traffic into events for the browser.
# ---------------------------------------------------------------------------

def strip_cache(x: object) -> object:
    """Drop `cache_control` markers, which move between requests."""
    if isinstance(x, dict):
        return {k: strip_cache(v) for k, v in x.items() if k != "cache_control"}
    if isinstance(x, list):
        return [strip_cache(v) for v in x]
    return x


def digest(x: object) -> str:
    return hashlib.sha1(json.dumps(x, sort_keys=True).encode()).hexdigest()


def fingerprint(msg: dict) -> tuple:
    """A loose identity for one message, stable across re-sends.

    Thinking blocks are ignored (they may be dropped from history) and text is
    compared on a stripped prefix, so the assistant message we reassembled from
    the response matches the copy Claude Code sends back next time.
    """
    content = msg.get("content")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    keys = []
    for b in content or []:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t in ("thinking", "redacted_thinking"):
            continue
        if t in ("tool_use", "server_tool_use"):
            keys.append((t, b.get("id")))
        elif t == "tool_result":
            keys.append((t, b.get("tool_use_id")))
        elif t == "text":
            keys.append((t, (b.get("text") or "").strip()[:300]))
        else:
            keys.append((t,))
    return (msg.get("role"), tuple(keys))


def parse_sse(raw: bytes) -> dict:
    """Reassemble a streamed Messages API response into a complete message."""
    msg: dict = {}
    blocks: dict[int, dict] = {}
    for line in raw.decode("utf-8", "replace").splitlines():
        if not line.startswith("data:"):
            continue
        try:
            ev = json.loads(line[5:].strip())
        except ValueError:
            continue
        t = ev.get("type")
        idx = ev.get("index", 0)
        if t == "message_start":
            msg = dict(ev.get("message") or {})
            msg["usage"] = dict(msg.get("usage") or {})
        elif t == "content_block_start":
            b = dict(ev.get("content_block") or {})
            if "input" in b:
                b["_json"] = ""
            blocks[idx] = b
        elif t == "content_block_delta":
            d = ev.get("delta") or {}
            b = blocks.setdefault(idx, {"type": "text", "text": ""})
            dt = d.get("type")
            if dt == "text_delta":
                b["text"] = b.get("text", "") + d.get("text", "")
            elif dt == "thinking_delta":
                b["thinking"] = b.get("thinking", "") + d.get("thinking", "")
            elif dt == "input_json_delta":
                b["_json"] = b.get("_json", "") + d.get("partial_json", "")
        elif t == "message_delta":
            msg.update({k: v for k, v in (ev.get("delta") or {}).items() if v is not None})
            msg.setdefault("usage", {}).update(ev.get("usage") or {})
        elif t == "error":
            msg["error"] = ev.get("error")
    content = []
    for i in sorted(blocks):
        b = blocks[i]
        if "_json" in b:
            js = b.pop("_json")
            if js:
                try:
                    b["input"] = json.loads(js)
                except ValueError:
                    b["input"] = js
        content.append(b)
    msg["content"] = content
    return msg


class Traffic:
    """In-memory history of decoded turns, fanned out to SSE subscribers."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.history: list[dict] = []
        self.clients: list[queue.Queue] = []
        self.snapshots: list[tuple[int, list]] = []  # (conversation, fingerprints)
        self.requests = 0
        self.conversations = 0
        self.system_hash: str | None = None
        self.tools_hash: str | None = None

    def publish(self, event: dict) -> None:
        with self.lock:
            self.history.append(event)
            for q in self.clients:
                q.put(event)

    def subscribe(self) -> tuple[queue.Queue, list[dict]]:
        q: queue.Queue = queue.Queue()
        with self.lock:
            self.clients.append(q)
            return q, list(self.history)

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)

    def match(self, fps: list) -> tuple[int | None, int]:
        """Find the conversation this message list continues, and where its new part starts."""
        best_conv, best_len = None, 0
        for conv, snap in reversed(self.snapshots):
            n = len(snap)
            if n > best_len and n <= len(fps) and fps[:n] == snap:
                best_conv, best_len = conv, n
        if best_conv is None:
            # History was edited mid-way: accept a long shared prefix as the same conversation.
            for conv, snap in reversed(self.snapshots):
                n = 0
                while n < min(len(snap), len(fps)) and snap[n] == fps[n]:
                    n += 1
                if n >= 2 and n > best_len:
                    best_conv, best_len = conv, n
        return best_conv, best_len

    def on_request(self, body: bytes) -> dict:
        data = json.loads(body)
        messages = data.get("messages") or []
        fps = [fingerprint(m) for m in messages if isinstance(m, dict)]
        side = not data.get("tools")
        with self.lock:
            self.requests += 1
            ev: dict = {
                "type": "request", "id": self.requests, "time": stamp(),
                "model": data.get("model"), "side": side, "total": len(messages),
            }
            conv = None
            if side:
                ev["blocks"] = strip_cache(messages[-1:])
            else:
                conv, start = self.match(fps)
                if conv is None:
                    self.conversations += 1
                    conv, start = self.conversations, 0
                    ev["new_conv"] = True
                self.snapshots.append((conv, fps))
                del self.snapshots[:-MAX_SNAPSHOTS]
                ev.update(conv=conv, start=start, blocks=strip_cache(messages[start:]))
                system = strip_cache(data.get("system"))
                tools = strip_cache(data.get("tools"))
                h = digest(system)
                if h != self.system_hash:
                    ev["system"] = system
                    ev["system_changed"] = self.system_hash is not None
                    self.system_hash = h
                h = digest(tools)
                if h != self.tools_hash:
                    ev["tools"] = tools
                    ev["tools_changed"] = self.tools_hash is not None
                    self.tools_hash = h
        self.publish(ev)
        return {"id": ev["id"], "conv": conv, "fps": fps, "side": side, "t0": time.monotonic()}

    def on_response(self, ctx: dict, status: int, ctype: str, raw: bytes) -> None:
        ev: dict = {
            "type": "response", "id": ctx["id"], "status": status,
            "ms": int((time.monotonic() - ctx["t0"]) * 1000),
        }
        if status >= 400:
            ev["error"] = raw.decode("utf-8", "replace")[:4000]
        else:
            msg = parse_sse(raw) if "event-stream" in ctype else json.loads(raw or b"{}")
            ev.update(
                model=msg.get("model"), content=msg.get("content") or [],
                stop_reason=msg.get("stop_reason"), usage=msg.get("usage"),
            )
            if msg.get("error"):
                ev["error"] = json.dumps(msg["error"])
            elif not ctx["side"] and ev["content"]:
                fp = fingerprint({"role": "assistant", "content": ev["content"]})
                with self.lock:
                    self.snapshots.append((ctx["conv"], ctx["fps"] + [fp]))
                    del self.snapshots[:-MAX_SNAPSHOTS]
        self.publish(ev)

    def on_failure(self, ctx: dict, error: str) -> None:
        self.publish({"type": "response", "id": ctx["id"], "status": 502, "error": error,
                      "ms": int((time.monotonic() - ctx["t0"]) * 1000)})


TRAFFIC = Traffic()


def dashboard_hook(fn, *args) -> object:
    """Run a dashboard step; never let it break the proxying."""
    try:
        return fn(*args)
    except Exception as e:  # noqa: BLE001
        print(f"[{stamp()}] (dashboard: {type(e).__name__}: {e})", file=sys.stderr, flush=True)
        return None


class Tap(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # we print our own lines

    def do_POST(self) -> None:
        self.relay()

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            self.serve_dashboard()
        elif path == "/events":
            self.serve_events()
        else:
            self.relay()

    def do_HEAD(self) -> None:
        self.relay()

    def serve_dashboard(self) -> None:
        page = DASHBOARD_HTML.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(page)

    def serve_events(self) -> None:
        q, backlog = TRAFFIC.subscribe()
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.send_event({"type": "reset"})
            for ev in backlog:
                self.send_event(ev)
            while True:
                try:
                    self.send_event(q.get(timeout=15))
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except OSError:
            pass  # the browser went away
        finally:
            TRAFFIC.unsubscribe(q)

    def send_event(self, ev: dict) -> None:
        self.wfile.write(b"data: " + json.dumps(ev).encode() + b"\n\n")
        self.wfile.flush()

    def relay(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""

        # 1. Print what the agent sent.
        print(f"\n{'=' * 78}\n[{stamp()}] >>> {self.command} {self.path}", flush=True)
        for name, value in self.headers.items():
            if name.lower() in ("authorization", "x-api-key"):
                value = value[:16] + "..."
            print(f"    {name}: {value}")
        if body:
            try:
                print(json.dumps(json.loads(body), indent=2, ensure_ascii=False))
            except ValueError:
                print(body.decode("utf-8", "replace"))

        # Hand Messages API calls to the dashboard (count_tokens etc. are skipped).
        ctx = None
        if self.command == "POST" and urlsplit(self.path).path.rstrip("/") == "/v1/messages" and body:
            ctx = dashboard_hook(TRAFFIC.on_request, body)

        # 2. Forward it, headers intact.
        target = urlsplit(UPSTREAM)
        conn_cls = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
        # Connect with a short timeout (a lost SYN must not stall the agent for
        # minutes); the long timeout is for reading a slow stream.
        upstream = conn_cls(target.netloc, timeout=10)
        try:
            upstream.connect()
        except OSError as e:
            print(f"\n[{stamp()}] !!! cannot reach {UPSTREAM}: {e}", flush=True)
            if ctx:
                dashboard_hook(TRAFFIC.on_failure, ctx, f"cannot reach {UPSTREAM}: {e}")
            self.send_error(502, f"proxy: cannot reach {UPSTREAM}: {e}")
            self.close_connection = True
            return
        upstream.sock.settimeout(600)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in SKIP}
        try:
            upstream.request(self.command, target.path.rstrip("/") + self.path, body=body or None, headers=headers)
            reply = upstream.getresponse()
        except Exception as e:
            if ctx:
                dashboard_hook(TRAFFIC.on_failure, ctx, f"{type(e).__name__}: {e}")
            raise

        # 3. Stream the reply back as it arrives, printing it on the way.
        print(f"\n[{stamp()}] <<< {reply.status} {reply.reason}", flush=True)
        self.send_response(reply.status, reply.reason)
        for name, value in reply.getheaders():
            if name.lower() not in SKIP and name.lower() != "content-encoding":
                self.send_header(name, value)
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command == "HEAD":
            self.close_connection = True
            return
        captured = bytearray()
        try:
            while chunk := reply.read1(4096):  # read1: pass on each chunk as it lands, don't wait for 4096 bytes
                if ctx:
                    captured += chunk
                self.wfile.write(chunk)
                self.wfile.flush()
                sys.stdout.write(chunk.decode("utf-8", "replace"))
                sys.stdout.flush()
        finally:
            if ctx:
                dashboard_hook(TRAFFIC.on_response, ctx, reply.status,
                               reply.getheader("content-type", ""), bytes(captured))
        self.close_connection = True
        print(f"\n[{stamp()}] --- done", flush=True)


DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Proxy Traffic</title>
<style>
:root {
  --bg: #f6f7f9; --card: #ffffff; --fg: #1d2430; --muted: #6b7380; --line: #e2e5ea;
  --code: #f1f3f6; --pill: #e9ebef;
  --user: #2f6fed; --model: #17a34a; --tool: #e8710a; --result: #0d9488; --error: #dc2626;
  --shadow: 0 1px 2px rgba(0,0,0,.06);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #111418; --card: #1a1e24; --fg: #e3e7ed; --muted: #8b94a1; --line: #2a3039;
    --code: #232931; --pill: #2a3039;
    --user: #5b8ff5; --model: #34c46a; --tool: #f59338; --result: #2dbfb1; --error: #f05252;
    --shadow: none;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
code, pre, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12.5px; }
header { position: sticky; top: 0; z-index: 10; background: var(--card); border-bottom: 1px solid var(--line); }
.bar { max-width: 960px; margin: 0 auto; padding: 8px 16px; display: flex; flex-wrap: wrap; gap: 8px 14px; align-items: center; }
.bar h1 { font-size: 15px; margin: 0; display: flex; align-items: center; gap: 8px; }
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--error); }
.dot.on { background: var(--model); }
.bar label { display: flex; gap: 4px; align-items: center; color: var(--muted); cursor: pointer; user-select: none; }
.bar input[type=search] { flex: 1; min-width: 140px; padding: 5px 9px; border: 1px solid var(--line); border-radius: 6px;
  background: var(--bg); color: var(--fg); }
button { font: inherit; cursor: pointer; border: 1px solid var(--line); background: var(--bg); color: var(--fg);
  border-radius: 6px; padding: 4px 10px; }
#sysbtn.changed { border-color: var(--tool); color: var(--tool); }
#drawer { display: none; max-width: 960px; margin: 0 auto; padding: 0 16px 12px; max-height: 60vh; overflow: auto; }
#drawer.open { display: block; }
#drawer h3 { font-size: 12px; text-transform: uppercase; letter-spacing: .05em; color: var(--muted); margin: 12px 0 6px; }
#drawer details { border: 1px solid var(--line); border-radius: 6px; margin: 4px 0; background: var(--bg); }
#drawer summary { padding: 4px 8px; cursor: pointer; }
#drawer summary .desc { color: var(--muted); }
#drawer pre { margin: 0; padding: 8px; white-space: pre-wrap; word-break: break-word; border-top: 1px solid var(--line); }
main { max-width: 960px; margin: 0 auto; padding: 12px 16px 120px; }
.empty { color: var(--muted); text-align: center; margin-top: 60px; }
.divider { display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: 12px; margin: 18px 0 8px; }
.divider::before, .divider::after { content: ""; flex: 1; border-top: 1px solid var(--line); }
.divider .chip { cursor: pointer; }
.divider.conv { color: var(--fg); font-weight: 600; font-size: 13px; margin-top: 30px; }
.divider.conv::before, .divider.conv::after { border-top: 2px solid var(--fg); opacity: .5; }
.chip { display: inline-block; font-size: 11px; padding: 0 7px; border-radius: 9px; background: var(--pill); color: var(--muted); }
.chip.warn { background: transparent; border: 1px solid var(--tool); color: var(--tool); }
.card { background: var(--card); border: 1px solid var(--line); border-left: 4px solid var(--muted); border-radius: 8px;
  margin: 8px 0; box-shadow: var(--shadow); overflow: hidden; }
.card.user { border-left-color: var(--user); }
.card.model { border-left-color: var(--model); }
.card.tool { border-left-color: var(--tool); }
.card.result { border-left-color: var(--result); }
.card.result.err, .card.err { border-left-color: var(--error); }
.card.flash { animation: flash 1.2s; }
@keyframes flash { 0% { box-shadow: 0 0 0 3px var(--tool); } 100% { box-shadow: 0 0 0 0 transparent; } }
.head { display: flex; align-items: center; gap: 8px; padding: 7px 12px; min-width: 0; }
.tool > .head, .result > .head { cursor: pointer; }
.badge { font-size: 10.5px; font-weight: 700; letter-spacing: .05em; padding: 1px 7px; border-radius: 4px; color: #fff; flex: none; }
.user .badge { background: var(--user); } .model .badge { background: var(--model); }
.tool .badge { background: var(--tool); } .result .badge, .rslot .badge { background: var(--result); }
.err .badge, .rslot.err .badge { background: var(--error); }
.meta { color: var(--muted); font-size: 12px; margin-left: auto; flex: none; white-space: nowrap; }
.name { font-weight: 600; flex: none; }
.arg { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--muted); min-width: 0; }
.body { padding: 0 12px 10px; }
.body pre, .rslot pre { margin: 0; white-space: pre-wrap; word-break: break-word; }
.usertext { white-space: pre-wrap; word-break: break-word; }
.card.user.quiet { opacity: .75; }
.card.user.quiet .head { display: none; }
.card.user.quiet .body { padding-top: 8px; }
details.reminder { display: inline-block; vertical-align: top; max-width: 100%; margin: 2px 0; }
details.reminder > summary { list-style: none; cursor: pointer; display: inline-block; font-size: 11px; padding: 0 8px;
  border-radius: 9px; background: var(--pill); color: var(--muted); font-family: inherit; }
details.reminder > summary::-webkit-details-marker { display: none; }
details.reminder[open] { display: block; }
details.reminder[open] > div { white-space: pre-wrap; word-break: break-word; color: var(--muted); border-left: 2px solid var(--line);
  padding: 4px 8px; margin: 4px 0; font-size: 12.5px; }
.thinking { border: 1px dashed var(--line); border-radius: 6px; margin: 6px 0; }
.thinking > summary { cursor: pointer; padding: 3px 8px; color: var(--muted); font-style: italic; white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis; }
.thinking > div { padding: 4px 10px 8px; white-space: pre-wrap; color: var(--muted); }
.md > :first-child { margin-top: 0; } .md > :last-child { margin-bottom: 0; }
.md p, .md ul, .md ol, .md pre, .md blockquote, .md table { margin: 6px 0; }
.md h1, .md h2, .md h3, .md h4, .md h5, .md h6 { margin: 10px 0 4px; font-size: 15px; }
.md h1 { font-size: 17px; } .md h2 { font-size: 16px; }
.md code { background: var(--code); padding: 1px 4px; border-radius: 4px; }
.md pre.code { background: var(--code); padding: 8px 10px; border-radius: 6px; overflow-x: auto; white-space: pre; }
.md pre.code code { background: none; padding: 0; }
.md blockquote { border-left: 3px solid var(--line); padding-left: 10px; color: var(--muted); }
.md table { border-collapse: collapse; display: block; overflow-x: auto; }
.md th, .md td { border: 1px solid var(--line); padding: 3px 8px; text-align: left; }
.md a { color: var(--user); }
.input { display: none; padding: 0 12px 10px; }
.input pre { background: var(--code); padding: 8px; border-radius: 6px; }
.card.tool.open .input, body.details .card.tool .input { display: block; }
.rslot { border-top: 1px solid var(--line); padding: 7px 12px 9px; }
.rslot .rhead { display: flex; gap: 8px; align-items: center; margin-bottom: 4px; cursor: pointer; }
.rslot.pending { color: var(--muted); font-size: 12px; font-style: italic; }
.out { background: var(--code); border-left: 3px solid var(--result); border-radius: 4px; padding: 6px 8px; }
.err .out, .rslot.err .out { border-left-color: var(--error); }
.out .full { display: none; }
.out.open .full, body.details .out .full { display: block; }
.out.open .preview, body.details .out .preview { display: none; }
.out .more { color: var(--muted); font-size: 11.5px; cursor: pointer; margin-top: 2px; }
.out.long { cursor: pointer; }
.pairbadge { display: flex; gap: 8px; align-items: center; margin: 4px 0 4px 18px; font-size: 12px; color: var(--muted); cursor: pointer; }
.pairbadge .badge { background: var(--result); }
.pairbadge.err .badge { background: var(--error); }
.side { display: flex; gap: 8px; align-items: center; font-size: 12px; color: var(--muted); margin: 6px 0; padding: 3px 10px;
  border: 1px dashed var(--line); border-radius: 6px; cursor: pointer; overflow: hidden; }
.side .t { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
.side-full { display: none; font-size: 12.5px; margin: -2px 0 8px; padding: 6px 10px; border: 1px dashed var(--line);
  border-top: none; border-radius: 0 0 6px 6px; white-space: pre-wrap; word-break: break-word; color: var(--muted); }
.side.open + .side-full { display: block; }
.waiting { display: flex; gap: 10px; align-items: center; color: var(--muted); padding: 8px 12px; margin: 8px 0;
  border: 1px dashed var(--model); border-radius: 8px; }
.pulse { width: 8px; height: 8px; border-radius: 50%; background: var(--model); animation: pulse 1s infinite alternate; }
@keyframes pulse { from { opacity: .25; } to { opacity: 1; } }
body.no-thinking .thinking { display: none; }
body.no-system details.reminder, body.no-system .side, body.no-system .side-full, body.no-system .card.user.quiet { display: none !important; }
.hidden { display: none !important; }
#jump { position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%); display: none; z-index: 20;
  background: var(--user); color: #fff; border: none; padding: 8px 16px; border-radius: 20px; box-shadow: 0 2px 8px rgba(0,0,0,.25); }
#jump.show { display: block; }
@media (max-width: 600px) { .meta { display: none; } }
</style>
</head>
<body>
<header>
  <div class="bar">
    <h1><span class="dot" id="dot" title="disconnected"></span>Proxy Traffic</h1>
    <button id="sysbtn" title="System prompt and tool definitions">System &amp; Tools</button>
    <label><input type="checkbox" id="f-system" checked> System</label>
    <label><input type="checkbox" id="f-thinking" checked> Thinking</label>
    <label><input type="checkbox" id="f-details"> Tool Details</label>
    <input type="search" id="q" placeholder="Filter cards…">
  </div>
  <div id="drawer"></div>
</header>
<main id="stream"><p class="empty" id="empty">Waiting for traffic… run <code>ANTHROPIC_BASE_URL=http://localhost:PORT claude</code></p></main>
<button id="jump">Jump to Live ↓</button>
<script>
"use strict";
const $ = (s) => document.querySelector(s);
const stream = $("#stream"), drawer = $("#drawer"), jump = $("#jump");
$("#empty").querySelector("code").textContent = "ANTHROPIC_BASE_URL=" + location.origin + " claude";

let toolCards, pending, sideRows, sys, unseen;
function resetState() {
  toolCards = new Map(); pending = new Map(); sideRows = new Map();
  sys = { system: null, tools: null, changed: false };
  unseen = 0;
}
resetState();

// ---------- helpers ----------
function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function el(tag, cls, html) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html != null) e.innerHTML = html;
  return e;
}
function blocksOf(content) {
  if (typeof content === "string") return [{ type: "text", text: content }];
  return Array.isArray(content) ? content : [];
}
function textOf(content) {
  return blocksOf(content).map((b) => b.type === "text" ? b.text : b.type === "image" ? "[image]" : "").join("\n");
}
function short(s, n) { s = String(s); return s.length > n ? s.slice(0, n) + "…" : s; }
function fmtMs(ms) { return ms == null ? "" : (ms / 1000).toFixed(1) + "s"; }

// Text with <system-reminder> blocks collapsed into pills. Returns HTML.
const REMINDER = /<system-reminder>([\s\S]*?)<\/system-reminder>/g;
function withReminders(text) {
  let out = "", last = 0, m;
  REMINDER.lastIndex = 0;
  while ((m = REMINDER.exec(text))) {
    out += esc(text.slice(last, m.index));
    const inner = m[1].trim();
    out += '<details class="reminder"><summary>system-reminder · ' + inner.length.toLocaleString() +
      ' chars</summary><div>' + esc(inner) + "</div></details>";
    last = REMINDER.lastIndex;
  }
  return out + esc(text.slice(last));
}
function onlyReminders(text) { return /<system-reminder>/.test(text) && text.replace(REMINDER, "").trim() === ""; }

// ---------- tiny markdown ----------
function inline(s) {
  const codes = [];
  s = esc(s).replace(/`([^`]+)`/g, (_, c) => { codes.push(c); return "\u0000" + (codes.length - 1) + "\u0000"; });
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
       .replace(/(^|[^*\w])\*([^*\s][^*]*?)\*(?!\w)/g, "$1<em>$2</em>")
       .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  return s.replace(/\u0000(\d+)\u0000/g, (_, n) => "<code>" + codes[n] + "</code>");
}
function md(src) {
  const lines = String(src).split("\n"), out = [];
  let i = 0;
  const isList = (l) => /^\s*[-*+]\s+/.test(l), isOl = (l) => /^\s*\d+[.)]\s+/.test(l);
  const special = (l) => /^\s*```/.test(l) || /^#{1,6}\s/.test(l) || isList(l) || isOl(l) || /^\s*>/.test(l) || /^\s*\|/.test(l);
  while (i < lines.length) {
    const l = lines[i];
    if (/^\s*```/.test(l)) {
      const buf = []; i++;
      while (i < lines.length && !/^\s*```/.test(lines[i])) buf.push(lines[i++]);
      i++;
      out.push('<pre class="code"><code>' + esc(buf.join("\n")) + "</code></pre>");
    } else if (/^#{1,6}\s/.test(l)) {
      const n = l.match(/^#+/)[0].length;
      out.push("<h" + n + ">" + inline(l.slice(n).trim()) + "</h" + n + ">"); i++;
    } else if (isList(l) || isOl(l)) {
      const ol = isOl(l), test = ol ? isOl : isList, items = [];
      while (i < lines.length && test(lines[i])) items.push(lines[i++].replace(/^\s*(?:[-*+]|\d+[.)])\s+/, ""));
      out.push((ol ? "<ol>" : "<ul>") + items.map((x) => "<li>" + inline(x) + "</li>").join("") + (ol ? "</ol>" : "</ul>"));
    } else if (/^\s*>/.test(l)) {
      const buf = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) buf.push(lines[i++].replace(/^\s*>\s?/, ""));
      out.push("<blockquote>" + buf.map(inline).join("<br>") + "</blockquote>");
    } else if (/^\s*\|/.test(l)) {
      const rows = [];
      while (i < lines.length && /^\s*\|/.test(lines[i])) rows.push(lines[i++]);
      const cells = (r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      if (rows.length > 1 && /^[\s|:-]+$/.test(rows[1])) {
        let h = "<table><tr>" + cells(rows[0]).map((c) => "<th>" + inline(c) + "</th>").join("") + "</tr>";
        for (const r of rows.slice(2)) h += "<tr>" + cells(r).map((c) => "<td>" + inline(c) + "</td>").join("") + "</tr>";
        out.push(h + "</table>");
      } else out.push("<p>" + rows.map(inline).join("<br>") + "</p>");
    } else if (!l.trim()) {
      i++;
    } else {
      const buf = [];
      while (i < lines.length && lines[i].trim() && !special(lines[i])) buf.push(lines[i++]);
      if (!buf.length) buf.push(lines[i++]);
      out.push("<p>" + buf.map(inline).join("<br>") + "</p>");
    }
  }
  return out.join("");
}

// ---------- scrolling ----------
function atBottom() { return window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 80; }
function mutate(fn) {
  const stick = atBottom();
  fn();
  if (stick) window.scrollTo(0, document.documentElement.scrollHeight);
  else { unseen++; jump.textContent = "Jump to Live ↓ (" + unseen + " new)"; jump.classList.add("show"); }
}
window.addEventListener("scroll", () => { if (atBottom()) { unseen = 0; jump.classList.remove("show"); } });
jump.onclick = () => window.scrollTo({ top: document.documentElement.scrollHeight, behavior: "smooth" });
function add(node) {
  $("#empty")?.remove();
  stream.appendChild(node);
  applyFilter(node);
  return node;
}

// ---------- filters ----------
const body = document.body;
function bindToggle(id, cls, invert) {
  const box = $(id);
  const apply = () => body.classList.toggle(cls, invert ? !box.checked : box.checked);
  box.onchange = apply; apply();
}
bindToggle("#f-system", "no-system", true);
bindToggle("#f-thinking", "no-thinking", true);
bindToggle("#f-details", "details", false);
const q = $("#q");
function applyFilter(node) {
  const term = q.value.trim().toLowerCase();
  const targets = node ? [node] : stream.children;
  for (const n of targets) {
    if (n.classList.contains("divider") || n.classList.contains("waiting")) continue;
    const hay = ((n.dataset.search || "") + " " + n.textContent).toLowerCase();
    n.classList.toggle("hidden", !!term && !hay.includes(term));
  }
}
q.oninput = () => applyFilter();

// ---------- system & tools drawer ----------
$("#sysbtn").onclick = () => {
  drawer.classList.toggle("open");
  sys.changed = false; updateSysBtn();
};
function updateSysBtn() {
  const b = $("#sysbtn");
  const n = Array.isArray(sys.tools) ? sys.tools.length : 0;
  b.textContent = "System & Tools (" + n + ")" + (sys.changed ? " • changed" : "");
  b.classList.toggle("changed", sys.changed);
}
function renderDrawer() {
  let h = "<h3>System prompt</h3>";
  const parts = sys.system == null ? [] : typeof sys.system === "string" ? [sys.system] : blocksOf(sys.system).map((b) => b.text || "");
  if (!parts.length) h += '<p class="chip">none</p>';
  parts.forEach((p, i) => {
    h += "<details><summary>Block " + (i + 1) + ' <span class="desc">· ' + p.length.toLocaleString() + " chars · " +
      esc(short(p.split("\n")[0], 90)) + "</span></summary><pre>" + esc(p) + "</pre></details>";
  });
  h += "<h3>Tools</h3>";
  for (const t of sys.tools || []) {
    const desc = t.description || "";
    h += '<details><summary><b class="mono">' + esc(t.name || t.type || "?") + '</b> <span class="desc">' +
      esc(short(desc.split("\n")[0], 100)) + "</span></summary><pre>" + esc(desc) + "\n\n" +
      esc(JSON.stringify(t.input_schema || t, null, 2)) + "</pre></details>";
  }
  drawer.innerHTML = h;
}

// ---------- cards ----------
const MAIN_ARGS = ["file_path", "command", "pattern", "path", "url", "query", "notebook_path", "skill", "description", "prompt", "subject"];
function mainArg(input) {
  if (!input || typeof input !== "object") return String(input ?? "");
  for (const k of MAIN_ARGS) if (typeof input[k] === "string") return input[k];
  const v = Object.values(input).find((x) => typeof x === "string");
  return v || "";
}

function userCard(text) {
  const c = el("div", "card user" + (onlyReminders(text) ? " quiet" : ""));
  c.appendChild(el("div", "head", '<span class="badge">USER</span>'));
  c.appendChild(el("div", "body", '<div class="usertext">' + withReminders(text) + "</div>"));
  return c;
}

function modelCard(blocks, meta) {
  const c = el("div", "card model");
  c.appendChild(el("div", "head", '<span class="badge">MODEL</span>' + (meta ? '<span class="meta">' + meta + "</span>" : "")));
  const b = el("div", "body");
  for (const blk of blocks) {
    if (blk.type === "thinking" || blk.type === "redacted_thinking") {
      const t = blk.thinking || "(redacted)";
      b.insertAdjacentHTML("beforeend", '<details class="thinking"><summary>💭 ' + esc(short(t.replace(/\s+/g, " "), 160)) +
        "</summary><div>" + esc(t) + "</div></details>");
    } else {
      b.appendChild(el("div", "md", md(blk.text || "")));
    }
  }
  c.appendChild(b);
  return c;
}

function toolCard(blk, meta) {
  const c = el("div", "card tool");
  const arg = mainArg(blk.input);
  c.appendChild(el("div", "head", '<span class="badge">TOOL CALL</span><span class="name mono">' + esc(blk.name || "?") +
    '</span><span class="arg mono" title="' + esc(arg) + '">' + esc(short(arg, 300)) + "</span>" +
    '<span class="meta">' + (meta ? meta + " · " : "") + esc(blk.id || "") + "</span>"));
  c.querySelector(".head").onclick = () => c.classList.toggle("open");
  c.appendChild(el("div", "input", "<pre>" + esc(typeof blk.input === "string" ? blk.input : JSON.stringify(blk.input, null, 2)) + "</pre>"));
  const slot = el("div", "rslot pending", "awaiting result…");
  c.appendChild(slot);
  if (blk.id) toolCards.set(blk.id, { card: c, slot, name: blk.name });
  return c;
}

// The result body: 3-line preview with a line count, expanding on click.
function outBox(blk) {
  const text = textOf(blk.content);
  const lines = text.split("\n");
  const long = lines.length > 3 || text.length > 400;
  const box = el("div", "out" + (long ? " long" : ""));
  if (long) {
    box.innerHTML = '<pre class="preview">' + withReminders(short(lines.slice(0, 3).join("\n"), 400)) + "</pre>" +
      '<pre class="full">' + withReminders(text) + "</pre>" +
      '<div class="more">' + lines.length.toLocaleString() + " lines · " + text.length.toLocaleString() + " chars · click to expand</div>";
    box.onclick = (e) => { if (!e.target.closest("summary") && !getSelection().toString()) box.classList.toggle("open"); };
  } else {
    box.innerHTML = "<pre>" + (text ? withReminders(text) : '<span class="chip">empty</span>') + "</pre>";
  }
  return { box, lines: lines.length };
}

function renderResult(blk) {
  const err = !!blk.is_error;
  const target = toolCards.get(blk.tool_use_id);
  const { box, lines } = outBox(blk);
  const head = '<span class="badge">' + (err ? "TOOL ERROR" : "TOOL RESULT") + '</span><span class="meta mono">' + esc(blk.tool_use_id || "") + "</span>";
  if (target) {
    target.slot.className = "rslot" + (err ? " err" : "");
    target.slot.innerHTML = "";
    target.slot.appendChild(el("div", "rhead", head));
    target.slot.appendChild(box);
    if (err) target.card.classList.add("err");
    const pb = el("div", "pairbadge" + (err ? " err" : ""), '<span class="badge">' + (err ? "TOOL ERROR" : "TOOL RESULT") +
      "</span>↳ " + esc(target.name || "") + " · " + lines.toLocaleString() + " line" + (lines === 1 ? "" : "s") +
      ' <span class="mono">' + esc(blk.tool_use_id || "") + "</span>");
    pb.onclick = () => { target.card.scrollIntoView({ behavior: "smooth", block: "center" });
      target.card.classList.remove("flash"); void target.card.offsetWidth; target.card.classList.add("flash"); };
    pb.dataset.search = target.card.textContent;
    return pb;
  }
  const c = el("div", "card result" + (err ? " err" : ""));
  c.appendChild(el("div", "head", head));
  const b = el("div", "body"); b.appendChild(box); c.appendChild(b);
  return c;
}

// Render one message's content blocks into the stream.
function renderMessage(msg, meta) {
  const blocks = blocksOf(msg.content);
  if (msg.role === "assistant") {
    let group = [];
    const flush = () => { if (group.length) { add(modelCard(group, meta)); meta = null; group = []; } };
    for (const b of blocks) {
      if (b.type === "text" || b.type === "thinking" || b.type === "redacted_thinking") group.push(b);
      else if (b.type === "tool_use" || b.type === "server_tool_use") { flush(); add(toolCard(b, meta)); meta = null; }
      else if (b.type && b.type.endsWith("_tool_result")) { flush(); add(renderResult({ tool_use_id: b.tool_use_id, content: JSON.stringify(b.content, null, 2) })); }
    }
    flush();
    if (meta) add(modelCard([], meta));
  } else {
    let texts = [];
    const flush = () => { if (texts.length) { add(userCard(texts.join("\n\n"))); texts = []; } };
    for (const b of blocks) {
      if (b.type === "tool_result") { flush(); add(renderResult(b)); }
      else if (b.type === "text") texts.push(b.text || "");
      else texts.push("[" + (b.type || "block") + "]");
    }
    flush();
  }
}

// ---------- events ----------
function onRequest(ev) {
  if (ev.side) {
    const last = ev.blocks && ev.blocks[0];
    const row = el("div", "side", '<span class="chip">SIDE</span><span class="mono">#' + ev.id + "</span><span>" + esc(ev.model || "") +
      '</span><span class="t">' + esc(short(last ? textOf(last.content).replace(REMINDER, "").replace(/\s+/g, " ").trim() : "", 200)) +
      '</span><span class="meta st">waiting…</span>');
    const full = el("div", "side-full");
    row.onclick = () => row.classList.toggle("open");
    add(row); add(full);
    sideRows.set(ev.id, { row, full, prompt: last ? textOf(last.content) : "" });
    return;
  }
  if (ev.new_conv) add(el("div", "divider conv", "<span>New Conversation · #" + ev.conv + "</span>"));
  const chips = [];
  if ("system" in ev) { sys.system = ev.system; if (ev.system_changed) { sys.changed = true; chips.push("system changed"); } }
  if ("tools" in ev) { sys.tools = ev.tools; if (ev.tools_changed) { sys.changed = true; chips.push("tools changed"); } }
  if ("system" in ev || "tools" in ev) { renderDrawer(); updateSysBtn(); }
  const n = (ev.blocks || []).length;
  const d = el("div", "divider", "<span>Request #" + ev.id + " · conv " + ev.conv + " · " + esc(ev.time) + " · " + esc(ev.model || "") +
    " · " + (n ? "+" + n + " new message" + (n === 1 ? "" : "s") : "retry, no new messages") + "</span>" +
    chips.map((c) => '<span class="chip warn">' + c + "</span>").join(""));
  d.querySelectorAll(".chip").forEach((c) => c.onclick = () => { drawer.classList.add("open"); sys.changed = false; updateSysBtn(); });
  add(d);
  for (const m of ev.blocks || []) renderMessage(m, null);
  const w = el("div", "waiting", '<span class="pulse"></span><span>Waiting for Claude… <span class="el">0.0s</span></span>');
  w.dataset.t0 = Date.now();
  pending.set(ev.id, w);
  add(w);
}

function onResponse(ev) {
  const w = pending.get(ev.id);
  if (w) { w.remove(); pending.delete(ev.id); }
  const side = sideRows.get(ev.id);
  if (side) {
    const reply = ev.error ? "ERROR " + ev.status + ": " + ev.error : textOf(ev.content);
    side.row.querySelector(".st").textContent = (ev.error ? "error · " : "") + fmtMs(ev.ms);
    side.full.textContent = "PROMPT:\n" + side.prompt.replace(REMINDER, "").trim() + "\n\nREPLY:\n" + reply;
    return;
  }
  if (ev.error) {
    const c = el("div", "card err");
    c.appendChild(el("div", "head", '<span class="badge">API ERROR ' + ev.status + '</span><span class="meta">' + fmtMs(ev.ms) + "</span>"));
    c.appendChild(el("div", "body", "<pre>" + esc(ev.error) + "</pre>"));
    add(c);
    return;
  }
  const u = ev.usage || {};
  const inTok = (u.input_tokens || 0) + (u.cache_read_input_tokens || 0) + (u.cache_creation_input_tokens || 0);
  const meta = [esc(ev.model || ""), fmtMs(ev.ms), inTok ? inTok.toLocaleString() + " in" : "",
    u.output_tokens ? u.output_tokens.toLocaleString() + " out" : "", esc(ev.stop_reason || "")].filter(Boolean).join(" · ");
  renderMessage({ role: "assistant", content: ev.content || [] }, meta);
}

setInterval(() => {
  for (const w of pending.values()) w.querySelector(".el").textContent = ((Date.now() - w.dataset.t0) / 1000).toFixed(1) + "s";
}, 250);

function connect() {
  const es = new EventSource("/events");
  const online = () => { $("#dot").classList.add("on"); $("#dot").title = "connected"; };
  es.onopen = online;
  es.onerror = () => { $("#dot").classList.remove("on"); $("#dot").title = "disconnected — retrying"; };
  es.onmessage = (m) => {
    const ev = JSON.parse(m.data);
    online();
    mutate(() => {
      if (ev.type === "reset") {
        resetState();
        stream.innerHTML = '<p class="empty" id="empty">Waiting for traffic… run <code>ANTHROPIC_BASE_URL=' + esc(location.origin) + " claude</code></p>";
        drawer.innerHTML = ""; updateSysBtn();
      } else if (ev.type === "request") onRequest(ev);
      else if (ev.type === "response") onResponse(ev);
    });
  };
}
updateSysBtn();
connect();
</script>
</body>
</html>
"""


def main() -> None:
    global UPSTREAM
    parser = argparse.ArgumentParser(description="Print and forward an agent's API traffic.")
    parser.add_argument("--port", type=int, default=8041)
    parser.add_argument("--upstream", default=UPSTREAM, help="where to forward (default: the Anthropic API)")
    args = parser.parse_args()
    UPSTREAM = args.upstream.rstrip("/")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Tap)
    print(f"proxy listening on http://localhost:{args.port}  ->  {UPSTREAM}")
    print(f"run the agent with:  ANTHROPIC_BASE_URL=http://localhost:{args.port} claude")
    print(f"live dashboard at:   http://localhost:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
