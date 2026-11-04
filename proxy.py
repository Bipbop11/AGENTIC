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
conversation and every other call the harness makes (see SPEC.md). `GET /`,
`GET /events`, `GET /raw/<id>` and `POST /clear` are served locally; every other
path is forwarded.

    python3 proxy.py --port 9000
    python3 proxy.py --upstream https://chatgpt.com/backend-api/codex   # for Codex
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import queue
import re
import sys
import threading
import time
from collections import OrderedDict
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
# How many calls keep their raw request/response for the Raw view.
MAX_RAW = 200


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


def redact(headers: object) -> list[tuple[str, str]]:
    """Header pairs with secrets shortened exactly as in the terminal log."""
    out = []
    for name, value in headers.items() if hasattr(headers, "items") else headers:
        if name.lower() in ("authorization", "x-api-key"):
            value = value[:16] + "..."
        out.append((name, value))
    return out


def body_text(body: bytes) -> str:
    """A body for the Raw view: pretty JSON when it parses, plain text otherwise."""
    if not body:
        return ""
    try:
        return json.dumps(json.loads(body), indent=2, ensure_ascii=False)
    except ValueError:
        return body.decode("utf-8", "replace")


def system_label(system: object) -> str:
    """First non-empty line of a system prompt, used to label harness calls."""
    if isinstance(system, list):
        system = "\n".join(b.get("text", "") for b in system if isinstance(b, dict))
    for line in str(system or "").splitlines():
        if line.strip():
            return line.strip()[:160]
    return "(no system prompt)"


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
    """In-memory history of every proxied call, fanned out to SSE subscribers."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.history: list[dict] = []
        self.clients: list[queue.Queue] = []
        self.snapshots: list[tuple[int, list]] = []  # (conversation, fingerprints)
        self.requests = 0
        self.conversations = 0
        self.system_hash: str | None = None
        self.tools_hash: str | None = None
        self.system: object = None  # latest values, re-sent after a Clear
        self.tools: object = None
        self.in_flight: dict[int, dict] = {}  # request id -> request event
        self.raw: OrderedDict[int, dict] = OrderedDict()  # request id -> raw request/response

    def broadcast(self, event: dict) -> None:
        """Store and fan out one event. Caller holds the lock."""
        self.history.append(event)
        for q in self.clients:
            q.put(event)

    def publish(self, event: dict) -> None:
        with self.lock:
            if event["type"] == "request":
                self.in_flight[event["id"]] = event
            elif event["type"] == "response":
                self.in_flight.pop(event["id"], None)
            self.broadcast(event)

    def clear(self) -> None:
        """Drop the replay history but keep the dedup snapshots, counters and system/tools."""
        with self.lock:
            self.history = []
            for rid in [rid for rid in self.raw if rid not in self.in_flight]:
                del self.raw[rid]
            self.broadcast({"type": "cleared", "time": stamp(), "system": self.system, "tools": self.tools})
            for ev in self.in_flight.values():
                # Keep the waiting indicator; the messages were already shown before the Clear.
                keep = ("type", "id", "time", "model", "kind", "conv", "label", "method", "path", "req_bytes")
                stub = {k: ev[k] for k in keep if k in ev}
                self.broadcast(dict(stub, blocks=[], in_flight=True))

    def subscribe(self) -> tuple[queue.Queue, list[dict]]:
        q: queue.Queue = queue.Queue()
        with self.lock:
            self.clients.append(q)
            return q, list(self.history)

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)

    def get_raw(self, rid: int) -> dict | None:
        with self.lock:
            return self.raw.get(rid)

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

    def on_request(self, method: str, path: str, headers: list[tuple[str, str]], body: bytes) -> dict:
        route = urlsplit(path).path.rstrip("/")
        data = None
        if method == "POST" and route in ("/v1/messages", "/v1/messages/count_tokens"):
            try:
                data = json.loads(body)
            except ValueError:
                pass
        if not isinstance(data, dict):
            kind = "http"
        elif route == "/v1/messages/count_tokens":
            kind = "count"
        else:
            kind = "main" if data.get("tools") else "aux"
        messages = (data or {}).get("messages") or []
        fps = [fingerprint(m) for m in messages if isinstance(m, dict)] if kind == "main" else []
        with self.lock:
            self.requests += 1
            rid = self.requests
            self.raw[rid] = {"request": {"method": method, "path": path, "headers": headers, "body": body_text(body)}}
            while len(self.raw) > MAX_RAW:
                self.raw.popitem(last=False)
            ev: dict = {"type": "request", "id": rid, "time": stamp(), "kind": kind,
                        "method": method, "path": path, "req_bytes": len(body)}
            conv = None
            if data is not None:
                ev.update(model=data.get("model"), total=len(messages))
            if kind == "aux":
                system = strip_cache(data.get("system"))
                ev.update(label=system_label(system), system=system, messages=strip_cache(messages))
            elif kind == "main":
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
                self.system, self.tools = system, tools
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
        return {"id": rid, "kind": kind, "conv": conv, "fps": fps, "t0": time.monotonic()}

    def store_raw_response(self, rid: int, response: dict) -> None:
        with self.lock:
            if rid in self.raw:
                self.raw[rid]["response"] = response

    def on_response(self, ctx: dict, status: int, reason: str, headers: list[tuple[str, str]], raw: bytes) -> None:
        self.store_raw_response(ctx["id"], {"status": status, "reason": reason, "headers": headers, "body": body_text(raw)})
        ctype = next((v for k, v in headers if k.lower() == "content-type"), "")
        ev: dict = {
            "type": "response", "id": ctx["id"], "status": status, "resp_bytes": len(raw),
            "ms": int((time.monotonic() - ctx["t0"]) * 1000),
        }
        if status >= 400:
            ev["error"] = raw.decode("utf-8", "replace")[:4000]
        elif ctx["kind"] == "count":
            try:
                ev["input_tokens"] = json.loads(raw).get("input_tokens")
            except (ValueError, AttributeError):
                ev["error"] = "proxy: could not decode the response body"
        elif ctx["kind"] in ("main", "aux"):
            try:
                msg = parse_sse(raw) if "event-stream" in ctype else json.loads(raw or b"{}")
            except ValueError:
                msg = {"error": "proxy: could not decode the response body"}
            ev.update(
                model=msg.get("model"), content=msg.get("content") or [],
                stop_reason=msg.get("stop_reason"), usage=msg.get("usage"),
            )
            if msg.get("error"):
                ev["error"] = json.dumps(msg["error"])
            elif ctx["kind"] == "main" and ev["content"]:
                fp = fingerprint({"role": "assistant", "content": ev["content"]})
                with self.lock:
                    self.snapshots.append((ctx["conv"], ctx["fps"] + [fp]))
                    del self.snapshots[:-MAX_SNAPSHOTS]
        self.publish(ev)

    def on_failure(self, ctx: dict, error: str) -> None:
        self.store_raw_response(ctx["id"], {"status": 502, "reason": "Bad Gateway", "headers": [], "body": error})
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
        if urlsplit(self.path).path == "/clear":
            self.serve_clear()
        else:
            self.relay()

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            self.serve_dashboard()
        elif path == "/events":
            self.serve_events()
        elif re.fullmatch(r"/raw/\d+", path):
            self.serve_raw(int(path.rsplit("/", 1)[1]))
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

    def serve_raw(self, rid: int) -> None:
        raw = TRAFFIC.get_raw(rid)
        data = json.dumps(raw if raw else {"expired": True}).encode()
        self.send_response(200 if raw else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def serve_clear(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        if length:
            self.rfile.read(length)
        TRAFFIC.clear()
        print(f"\n[{stamp()}] --- dashboard cleared", flush=True)
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

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
        for name, value in redact(self.headers):
            print(f"    {name}: {value}")
        if body:
            try:
                print(json.dumps(json.loads(body), indent=2, ensure_ascii=False))
            except ValueError:
                print(body.decode("utf-8", "replace"))

        # Hand every call to the dashboard.
        ctx = dashboard_hook(TRAFFIC.on_request, self.command, self.path, redact(self.headers), body)

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
            if ctx:
                dashboard_hook(TRAFFIC.on_response, ctx, reply.status, reply.reason, reply.getheaders(), b"")
            self.close_connection = True
            return
        captured = bytearray()
        try:
            while chunk := reply.read1(4096):  # read1: pass on each chunk as it lands, don't wait for 4096 bytes
                captured += chunk
                self.wfile.write(chunk)
                self.wfile.flush()
                sys.stdout.write(chunk.decode("utf-8", "replace"))
                sys.stdout.flush()
        finally:
            if ctx:
                dashboard_hook(TRAFFIC.on_response, ctx, reply.status, reply.reason,
                               reply.getheaders(), bytes(captured))
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
  --user: #2f6fed; --model: #17a34a; --tool: #e8710a; --result: #0d9488; --error: #dc2626; --harness: #7c3aed;
  --shadow: 0 1px 2px rgba(0,0,0,.06);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #111418; --card: #1a1e24; --fg: #e3e7ed; --muted: #8b94a1; --line: #2a3039;
    --code: #232931; --pill: #2a3039;
    --user: #5b8ff5; --model: #34c46a; --tool: #f59338; --result: #2dbfb1; --error: #f05252; --harness: #a78bfa;
    --shadow: none;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
code, pre, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12.5px; }
header { position: sticky; top: 0; z-index: 10; background: var(--card); border-bottom: 1px solid var(--line); }
.bar { max-width: 1240px; margin: 0 auto; padding: 8px 16px; display: flex; flex-wrap: wrap; gap: 8px 14px; align-items: center; }
.bar h1 { font-size: 15px; margin: 0; display: flex; align-items: center; gap: 8px; }
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--error); }
.dot.on { background: var(--model); }
.bar label { display: flex; gap: 4px; align-items: center; color: var(--muted); cursor: pointer; user-select: none; }
.bar input[type=search] { flex: 1; min-width: 140px; padding: 5px 9px; border: 1px solid var(--line); border-radius: 6px;
  background: var(--bg); color: var(--fg); }
button { font: inherit; cursor: pointer; border: 1px solid var(--line); background: var(--bg); color: var(--fg);
  border-radius: 6px; padding: 4px 10px; }
#sysbtn.changed { border-color: var(--tool); color: var(--tool); }
#drawer { display: none; max-width: 1240px; margin: 0 auto; padding: 0 16px 12px; max-height: 60vh; overflow: auto; }
#drawer.open { display: block; }
#drawer h3 { font-size: 12px; text-transform: uppercase; letter-spacing: .05em; color: var(--muted); margin: 12px 0 6px; }
#drawer details { border: 1px solid var(--line); border-radius: 6px; margin: 4px 0; background: var(--bg); }
#drawer summary { padding: 4px 8px; cursor: pointer; }
#drawer summary .desc { color: var(--muted); }
#drawer pre { margin: 0; padding: 8px; white-space: pre-wrap; word-break: break-word; border-top: 1px solid var(--line); }
body { overflow-anchor: none; }
#layout { max-width: 1240px; margin: 0 auto; padding: 0 16px; display: flex; gap: 16px; align-items: flex-start; }
#col { flex: 1; min-width: 0; max-width: 960px; }
main { padding: 12px 0 24px; }
footer { padding: 0 0 100px; color: var(--muted); font-size: 12px; text-align: center; }
#sessions { width: 250px; flex: none; position: sticky; top: var(--hh, 50px); max-height: calc(100vh - var(--hh, 50px));
  overflow: auto; padding: 12px 0; }
body.no-sessions #sessions { display: none; }
.shead { font-size: 12px; text-transform: uppercase; letter-spacing: .05em; color: var(--muted); margin: 0 0 6px 4px; }
#slist { list-style: none; margin: 0; padding: 0; }
.entry { padding: 6px 8px; border-radius: 6px; cursor: pointer; border: 1px solid transparent; margin-bottom: 2px; }
.entry:hover { background: var(--card); }
.entry.active { background: var(--card); border-color: var(--line); box-shadow: inset 3px 0 0 var(--user); }
.etop { display: flex; align-items: center; gap: 6px; min-width: 0; }
.etitle { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; flex: 1; }
.emeta { color: var(--muted); font-size: 11.5px; margin-top: 1px; }
.sdot { width: 7px; height: 7px; border-radius: 50%; background: var(--line); flex: none; }
.entry.busy .sdot { background: var(--model); animation: pulse 1s infinite alternate; }
.entry.err .sdot { background: var(--error); }
.chip.new { background: var(--user); color: #fff; }
@media (max-width: 800px) {
  #sessions { display: none; position: fixed; left: 0; top: var(--hh, 50px); bottom: 0; max-height: none; width: min(320px, 85vw);
    z-index: 30; background: var(--bg); border-right: 1px solid var(--line); padding: 12px; box-shadow: 0 4px 16px rgba(0,0,0,.25); }
  body.sessions-open #sessions { display: block; }
}
.empty { color: var(--muted); text-align: center; margin-top: 60px; }
.marker { display: flex; align-items: center; gap: 8px; color: var(--fg); font-weight: 600; font-size: 13px; margin: 14px 0 26px; }
.marker::before, .marker::after { content: ""; flex: 1; border-top: 2px solid var(--fg); opacity: .5; }
.ghead { display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: 12px; margin: 18px 0 4px; cursor: pointer;
  border-radius: 4px; }
.ghead::after { content: ""; flex: 1; border-top: 1px solid var(--line); min-width: 20px; }
.ghead .chip { cursor: pointer; }
.ghead:focus-visible, .head:focus-visible { outline: 2px solid var(--user); outline-offset: 2px; }
.gsum { color: var(--fg); }
.group:not(.collapsed) .gsum { display: none; }
.group.collapsed > .gbody, .group.collapsed > .rawline { display: none; }
.chev { flex: none; width: 14px; color: var(--muted); font-size: 15px; line-height: 1; text-align: center; }
.chev::before { content: "▾"; }
.collapsed > .head > .chev::before, .group.collapsed > .ghead > .chev::before { content: "▸"; }
.nomore > .head { cursor: default; }
.nomore > .head > .chev { opacity: .3; }
.sum { padding: 0 12px 8px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.card:not(.collapsed) > .sum { display: none; }
.card.collapsed > .body { display: none; }
.card.result.collapsed > .body { display: block; }
.rstat { font-size: 11.5px; color: var(--muted); flex: none; }
.rstat.bad { color: var(--error); }
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
.head { display: flex; align-items: center; gap: 8px; padding: 7px 12px; min-width: 0; cursor: pointer; }
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
.card.tool:not(.collapsed) > .input { display: block; }
.rslot { border-top: 1px solid var(--line); padding: 7px 12px 9px; }
.rslot .rhead { display: flex; gap: 8px; align-items: center; margin-bottom: 4px; cursor: pointer; }
.rslot.pending { color: var(--muted); font-size: 12px; font-style: italic; }
.out { background: var(--code); border-left: 3px solid var(--result); border-radius: 4px; padding: 6px 8px; }
.err .out, .rslot.err .out { border-left-color: var(--error); }
.out .full { display: none; }
.card:not(.collapsed) .out .full { display: block; }
.card:not(.collapsed) .out .preview, .card:not(.collapsed) .out .more { display: none; }
.out .more { color: var(--muted); font-size: 11.5px; cursor: pointer; margin-top: 2px; }
.out.long { cursor: pointer; }
.pairbadge { display: flex; gap: 8px; align-items: center; margin: 4px 0 4px 18px; font-size: 12px; color: var(--muted); cursor: pointer; }
.pairbadge .badge { background: var(--result); }
.pairbadge.err .badge { background: var(--error); }
.card.harness { border-left-color: var(--harness); }
.harness .badge { background: var(--harness); }
.card.harness.err { border-left-color: var(--error); }
.card.harness.err > .head .badge { background: var(--error); }
.harness .head .arg { color: var(--fg); }
.harness .st { display: flex; align-items: center; gap: 6px; }
.hprompt { margin-bottom: 6px; }
.hreply:empty { display: none; }
.hmodel { border-left: 3px solid var(--model); padding: 2px 0 2px 10px; margin: 6px 0; }
.hmodel .mbadge { background: var(--model); display: inline-block; margin-bottom: 4px; }
.herr { color: var(--error); white-space: pre-wrap; word-break: break-word; margin: 6px 0; }
details.fold, details.raw { margin: 4px 0; }
details.fold > summary, details.raw > summary { cursor: pointer; color: var(--muted); font-size: 12px; }
details.fold > pre, .rawbody pre { background: var(--code); padding: 8px; border-radius: 6px; max-height: 420px; overflow: auto;
  white-space: pre-wrap; word-break: break-word; margin: 4px 0; }
.rawbody h4 { margin: 8px 0 2px; font-size: 11px; text-transform: uppercase; letter-spacing: .05em; color: var(--muted); }
.rawline { margin: 0 0 2px; }
.rawline details.raw > summary { text-align: right; }
.hrow { background: var(--card); border: 1px solid var(--line); border-left: 4px solid var(--muted); border-radius: 6px;
  margin: 6px 0; padding: 3px 10px; font-size: 12px; color: var(--muted); }
.hrow .line { display: flex; gap: 8px; align-items: center; min-width: 0; }
.hrow .t { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
.hrow.err { border-left-color: var(--error); }
.hrow.err .chip.hk { background: var(--error); color: #fff; }
.waiting { display: flex; gap: 10px; align-items: center; color: var(--muted); padding: 8px 12px; margin: 8px 0;
  border: 1px dashed var(--model); border-radius: 8px; }
.pulse { width: 8px; height: 8px; border-radius: 50%; background: var(--model); animation: pulse 1s infinite alternate; }
@keyframes pulse { from { opacity: .25; } to { opacity: 1; } }
body.no-thinking .thinking { display: none; }
body.no-system details.reminder, body.no-system .card.user.quiet { display: none !important; }
body.no-harness .harness { display: none !important; }
.shide, .fhide { display: none !important; }
#jump { position: fixed; top: calc(var(--hh, 50px) + 10px); left: 50%; transform: translateX(-50%); display: none; z-index: 20;
  background: var(--user); color: #fff; border: none; padding: 8px 16px; border-radius: 20px; box-shadow: 0 2px 8px rgba(0,0,0,.25); }
#jump.show { display: block; }
@media (max-width: 600px) { .meta { display: none; } }
</style>
</head>
<body>
<header>
  <div class="bar">
    <h1><span class="dot" id="dot" title="disconnected"></span>Proxy Traffic</h1>
    <button id="sessbtn" title="Show or hide the Sessions sidebar">Sessions</button>
    <button id="sysbtn" title="System prompt and tool definitions">System &amp; Tools</button>
    <label><input type="checkbox" id="f-system" checked> System</label>
    <label><input type="checkbox" id="f-thinking" checked> Thinking</label>
    <label><input type="checkbox" id="f-details"> Tool Details</label>
    <label><input type="checkbox" id="f-harness" checked> Harness</label>
    <button id="expandall" title="Expand every card and request group in view">Expand all</button>
    <button id="collapseall" title="Collapse every card and request group in view">Collapse all</button>
    <button id="clearbtn" title="Clear the view (the proxy keeps tracking the conversation)">Clear</button>
    <input type="search" id="q" placeholder="Filter cards…">
  </div>
  <div id="drawer"></div>
</header>
<div id="layout">
  <aside id="sessions" aria-label="Sessions">
    <div class="shead">Sessions</div>
    <ul id="slist"></ul>
  </aside>
  <div id="col">
    <main id="stream"><p class="empty" id="empty">Waiting for traffic… run <code>ANTHROPIC_BASE_URL=http://localhost:PORT claude</code></p></main>
    <footer>Shows every call sent to <code>ANTHROPIC_BASE_URL</code>. Calls Claude Code makes to other hosts (for example telemetry) don't pass through this proxy and aren't shown.</footer>
  </div>
</div>
<button id="jump">Jump to Latest ↑</button>
<script>
"use strict";
const $ = (s) => document.querySelector(s);
const stream = $("#stream"), drawer = $("#drawer"), jump = $("#jump");
$("#empty").querySelector("code").textContent = "ANTHROPIC_BASE_URL=" + location.origin + " claude";

let toolCards, pending, harness, groups, sys, unseen;
function resetState() {
  toolCards = new Map(); pending = new Map(); harness = new Map(); groups = new Map();
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

// ---------- layout & scrolling (newest first) ----------
const header = $("header");
function setHeaderVar() { document.documentElement.style.setProperty("--hh", header.offsetHeight + "px"); }
new ResizeObserver(setHeaderVar).observe(header);
setHeaderVar();
function atTop() { return window.scrollY <= 80; }
function isShown(n) { return n.isConnected && n.getClientRects().length > 0; }
let added = [];  // nodes inserted by the current event, for the Jump to Latest count

// The element under the top edge of the viewport, used to keep the reading position.
function anchorEl() {
  const r = stream.getBoundingClientRect(), x = r.left + r.width / 2;
  for (let y = header.offsetHeight + 4; y < window.innerHeight; y += 12) {
    const a = document.elementFromPoint(x, y);
    if (a && a !== stream && stream.contains(a)) return a;
  }
  return null;
}
// Run fn without moving what the reader is looking at (scroll anchoring).
function withAnchor(fn) {
  if (atTop()) { fn(); return; }
  const a = anchorEl(), before = a ? a.getBoundingClientRect().top : 0;
  fn();
  if (a && isShown(a)) window.scrollBy(0, a.getBoundingClientRect().top - before);
}
function mutate(fn, force) {
  added = [];
  const top = force || atTop();
  if (top) fn(); else withAnchor(fn);
  if (!top) {
    const n = added.filter(isShown).length;
    if (n) { unseen += n; jump.textContent = "Jump to Latest ↑ (" + unseen + " new)"; jump.classList.add("show"); }
  }
}
function hideJump() { unseen = 0; jump.classList.remove("show"); }
window.addEventListener("scroll", () => { if (atTop()) hideJump(); });
jump.onclick = () => window.scrollTo({ top: 0, behavior: "smooth" });
// Insert a top-level item (request group, harness call, marker) at the top of the stream.
function addTop(node) {
  $("#empty")?.remove();
  stream.prepend(node);
  applySession(node);
  added.push(node);
  return node;
}
function addInto(parent, node) { parent.appendChild(node); added.push(node); return node; }
function flash(n) { n.classList.remove("flash"); void n.offsetWidth; n.classList.add("flash"); }

// ---------- filters ----------
const body = document.body;
function bindToggle(id, cls, invert) {
  const box = $(id);
  const apply = () => withAnchor(() => body.classList.toggle(cls, invert ? !box.checked : box.checked));
  box.onchange = apply; apply();
}
bindToggle("#f-system", "no-system", true);
bindToggle("#f-thinking", "no-thinking", true);
bindToggle("#f-harness", "no-harness", true);
const details = $("#f-details");
// Tool Details sets the state of every tool card in view; new ones arrive in that state.
details.onchange = () => withAnchor(() => {
  for (const c of stream.querySelectorAll(".card.tool, .card.result")) if (!c.closest(".shide")) setCard(c, details.checked);
});
const q = $("#q");
function applyFilter() {
  const term = q.value.trim().toLowerCase();
  for (const n of stream.querySelectorAll(".card, .hrow, .pairbadge")) {
    const hay = ((n.dataset.search || "") + " " + n.textContent).toLowerCase();
    n.classList.toggle("fhide", !!term && !hay.includes(term));
  }
  for (const g of stream.querySelectorAll(".group")) {
    const hit = !term || g.querySelector(".ghead").textContent.toLowerCase().includes(term) ||
      g.querySelector(".gbody .card:not(.fhide), .gbody .pairbadge:not(.fhide)");
    g.classList.toggle("fhide", !hit);
  }
}
q.oninput = () => withAnchor(applyFilter);

// ---------- expand / collapse ----------
function setCard(c, open) {
  if (c.classList.contains("nomore")) open = true;
  c.classList.toggle("collapsed", !open);
  c.querySelector(":scope > .head")?.setAttribute("aria-expanded", String(open));
}
function setGroup(g, open) {
  g.classList.toggle("collapsed", !open);
  g.querySelector(".ghead").setAttribute("aria-expanded", String(open));
  if (!open) g.querySelector(".gsum").textContent = groupSummary(g);
}
function groupSummary(g) {
  const gb = g.querySelector(".gbody"), count = (sel) => gb.querySelectorAll(sel).length, parts = [];
  const put = (n, one, many) => { if (n) parts.push(n === 1 ? one : n + " " + many); };
  put(count(".card.user:not(.quiet)"), "USER", "USERS");
  put(count(".card.model"), "MODEL", "MODELS");
  put(count(".card.tool"), "TOOL CALL", "TOOL CALLS");
  put(count(".card.result") + count(".pairbadge"), "TOOL RESULT", "TOOL RESULTS");
  put(count(".card.apierr"), "API ERROR", "API ERRORS");
  if (count(".waiting")) parts.push("waiting…");
  return parts.join(" · ");
}
// Click (except on links, chips, details or while selecting) or Enter/Space toggles.
function bindHead(h, toggle) {
  h.tabIndex = 0;
  h.setAttribute("role", "button");
  h.onclick = (e) => {
    if (e.target.closest("a, .chip, summary, details, button") || String(getSelection())) return;
    withAnchor(toggle);
  };
  h.onkeydown = (e) => {
    if ((e.key === "Enter" || e.key === " ") && e.target === h) { e.preventDefault(); withAnchor(toggle); }
  };
}
// A card with a chevron header. opts: sum (collapsed one-liner), open (default state), more (false: nothing to expand).
function makeCard(cls, headHTML, opts) {
  const c = el("div", "card " + cls + (opts.more === false ? " nomore" : ""));
  const h = el("div", "head", '<span class="chev" aria-hidden="true"></span>' + headHTML);
  c.appendChild(h);
  if (opts.sum != null) c.appendChild(el("div", "sum", esc(opts.sum)));
  bindHead(h, () => setCard(c, c.classList.contains("collapsed")));
  setCard(c, opts.open !== false);
  return c;
}
function setAll(open) {
  withAnchor(() => {
    for (const c of stream.querySelectorAll(".card")) if (!c.closest(".shide")) setCard(c, open);
    for (const g of stream.querySelectorAll(".group:not(.shide)")) setGroup(g, open);
  });
}
$("#expandall").onclick = () => setAll(true);
$("#collapseall").onclick = () => setAll(false);

// ---------- sessions sidebar ----------
const slist = $("#slist");
let convs, selected = null, wantConv = null;
function resetSessions() {
  convs = new Map(); selected = null;
  slist.innerHTML = "";
  const all = el("li", "entry all active", '<div class="etop"><b>All</b></div><div class="emeta">every conversation and harness call</div>');
  all.onclick = () => select(null);
  slist.appendChild(all);
}
function getConv(ev) {
  let c = convs.get(ev.conv);
  if (!c) {
    c = { n: ev.conv, title: "", start: ev.time, last: ev.time, requests: 0, inflight: new Set(), err: false,
          newCount: 0, continued: !ev.new_conv, el: el("li", "entry") };
    c.el.dataset.conv = c.n;
    c.el.onclick = () => select(c.n);
    // Sorted by start, newest first; conversation numbers grow with start time.
    const before = [...slist.children].find((li) => li.dataset.conv && +li.dataset.conv < c.n) || null;
    slist.insertBefore(c.el, before);
    convs.set(c.n, c);
    if (wantConv === c.n) { wantConv = null; select(c.n, true); }
  }
  return c;
}
function renderEntry(c) {
  c.el.className = "entry" + (selected === c.n ? " active" : "") + (c.inflight.size ? " busy" : "") + (c.err ? " err" : "");
  c.el.innerHTML = '<div class="etop"><span class="sdot"></span><b>#' + c.n + '</b><span class="etitle">' +
    esc(c.title || "(no prompt yet)") + "</span>" + (c.newCount ? '<span class="chip new">+' + c.newCount + "</span>" : "") + "</div>" +
    '<div class="emeta">' + esc(c.start) + " · " + c.requests + " request" + (c.requests === 1 ? "" : "s") +
    " · last " + esc(c.last) + (c.continued ? " · continued" : "") + "</div>";
}
function applySession(n) {
  if (n.classList) n.classList.toggle("shide", selected != null && n.dataset.conv !== String(selected));
}
function select(conv, quiet) {
  selected = conv;
  if (conv != null && convs.get(conv)) convs.get(conv).newCount = 0;
  slist.querySelector(".all").classList.toggle("active", conv == null);
  for (const c of convs.values()) renderEntry(c);
  for (const n of stream.children) applySession(n);
  history.replaceState(null, "", conv == null ? location.pathname + location.search : "#conv=" + conv);
  body.classList.remove("sessions-open");
  if (!quiet) window.scrollTo(0, 0);
  hideJump();
}
function titleFrom(blocks) {
  for (const m of blocks || []) {
    if (m.role !== "user") continue;
    const t = textOf(m.content).replace(REMINDER, "").trim();
    if (t) return short(t.split("\n")[0], 60);
  }
  return "";
}
// Following a #conv=N link inside the page selects that conversation too.
window.addEventListener("hashchange", () => {
  const m = location.hash.match(/conv=(\d+)/), n = m ? +m[1] : null;
  if (n == null || convs.has(n)) select(n);
});
$("#sessbtn").onclick = () => {
  if (matchMedia("(max-width: 800px)").matches) body.classList.toggle("sessions-open");
  else body.classList.toggle("no-sessions");
};

// ---------- clear & system/tools drawer ----------
$("#clearbtn").onclick = () => {
  fetch("/clear", { method: "POST" }).catch(() => {});
};
function onCleared(ev) {
  resetState(); resetSessions(); wantConv = null;
  history.replaceState(null, "", location.pathname + location.search);
  sys.system = ev.system; sys.tools = ev.tools;
  stream.innerHTML = "";
  hideJump();
  addTop(el("div", "marker cleared", "<span>▲ Cleared at " + esc(ev.time) + "</span>"));
  renderDrawer(); updateSysBtn();
}
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
function firstLine(text) { return String(text || "").replace(REMINDER, "").trim().split("\n")[0]; }
const longText = (t) => t.includes("\n") || t.length > 120;

function userCard(text) {
  const clean = text.replace(REMINDER, "").trim(), quiet = onlyReminders(text);
  const c = makeCard("user" + (quiet ? " quiet" : ""), '<span class="badge">USER</span>', {
    sum: short(firstLine(text), 120) + " · " + clean.length.toLocaleString() + " chars",
    open: true, more: !quiet && (longText(clean) || /<system-reminder>/.test(text)) });
  c.appendChild(el("div", "body", '<div class="usertext">' + withReminders(text) + "</div>"));
  return c;
}

function modelCard(blocks, meta) {
  const text = blocks.filter((b) => b.type === "text").map((b) => b.text || "").join("\n").trim();
  const c = makeCard("model", '<span class="badge">MODEL</span>' + (meta ? '<span class="meta">' + meta + "</span>" : ""), {
    sum: short(firstLine(text) || (blocks.length ? "(thinking only)" : ""), 160),
    open: true, more: blocks.some((b) => b.type !== "text") || longText(text) });
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

function errorCard(ev) {
  const c = makeCard("apierr err", '<span class="badge">API ERROR ' + ev.status + '</span><span class="meta">' + fmtMs(ev.ms) + "</span>", {
    sum: short(firstLine(ev.error), 160), open: true, more: longText(String(ev.error || "")) });
  c.appendChild(el("div", "body", "<pre>" + esc(ev.error) + "</pre>"));
  return c;
}

function toolCard(blk, meta) {
  const arg = mainArg(blk.input);
  const c = makeCard("tool", '<span class="badge">TOOL CALL</span><span class="name mono">' + esc(blk.name || "?") +
    '</span><span class="arg mono" title="' + esc(arg) + '">' + esc(short(arg, 300)) + "</span>" +
    '<span class="rstat">awaiting result</span>' +
    '<span class="meta">' + (meta ? meta + " · " : "") + esc(blk.id || "") + "</span>", { open: details.checked });
  c.appendChild(el("div", "input", "<pre>" + esc(typeof blk.input === "string" ? blk.input : JSON.stringify(blk.input, null, 2)) + "</pre>"));
  const slot = el("div", "rslot pending", "awaiting result…");
  c.appendChild(slot);
  if (blk.id) toolCards.set(blk.id, { card: c, slot, name: blk.name, rstat: c.querySelector(".rstat") });
  return c;
}

// The result body: a 3-line preview with a line count; the full text shows when its card is expanded.
function outBox(blk) {
  const text = textOf(blk.content);
  const lines = text.split("\n");
  const long = lines.length > 3 || text.length > 400;
  const box = el("div", "out" + (long ? " long" : ""));
  if (long) {
    box.innerHTML = '<pre class="preview">' + withReminders(short(lines.slice(0, 3).join("\n"), 400)) + "</pre>" +
      '<pre class="full">' + withReminders(text) + "</pre>" +
      '<div class="more">' + lines.length.toLocaleString() + " lines · " + text.length.toLocaleString() + " chars · click to expand</div>";
    box.onclick = (e) => {
      if (e.target.closest("summary, details") || String(getSelection())) return;
      const c = box.closest(".card");
      if (c) withAnchor(() => setCard(c, c.classList.contains("collapsed")));
    };
  } else {
    box.innerHTML = "<pre>" + (text ? withReminders(text) : '<span class="chip">empty</span>') + "</pre>";
  }
  return { box, lines: lines.length, long };
}

function renderResult(blk) {
  const err = !!blk.is_error;
  const target = toolCards.get(blk.tool_use_id);
  const { box, lines, long } = outBox(blk);
  const head = '<span class="badge">' + (err ? "TOOL ERROR" : "TOOL RESULT") + '</span><span class="meta mono">' + esc(blk.tool_use_id || "") + "</span>";
  const count = lines.toLocaleString() + " line" + (lines === 1 ? "" : "s");
  if (target) {
    target.slot.className = "rslot" + (err ? " err" : "");
    target.slot.innerHTML = "";
    target.slot.appendChild(el("div", "rhead", head));
    target.slot.appendChild(box);
    target.rstat.textContent = err ? "✗ error" : "✓ " + count;
    target.rstat.classList.toggle("bad", err);
    if (err) target.card.classList.add("err");
    const pb = el("div", "pairbadge" + (err ? " err" : ""), '<span class="badge">' + (err ? "TOOL ERROR" : "TOOL RESULT") +
      "</span>↓ " + esc(target.name || "") + " · " + count + ' <span class="mono">' + esc(blk.tool_use_id || "") + "</span>");
    pb.onclick = () => {
      const g = target.card.closest(".group");
      if (g) setGroup(g, true);
      setCard(target.card, true);
      target.card.scrollIntoView({ behavior: "smooth", block: "center" });
      flash(target.card);
    };
    pb.dataset.search = target.card.textContent;
    return pb;
  }
  const c = makeCard("result" + (err ? " err" : ""), head, { open: details.checked, more: long });
  const b = el("div", "body"); b.appendChild(box); c.appendChild(b);
  return c;
}

// One message's content blocks as cards, in their original order.
function renderMessage(msg, meta) {
  const nodes = [], blocks = blocksOf(msg.content);
  if (msg.role === "assistant") {
    let group = [];
    const flush = () => { if (group.length) { nodes.push(modelCard(group, meta)); meta = null; group = []; } };
    for (const b of blocks) {
      if (b.type === "text" || b.type === "thinking" || b.type === "redacted_thinking") group.push(b);
      else if (b.type === "tool_use" || b.type === "server_tool_use") { flush(); nodes.push(toolCard(b, meta)); meta = null; }
      else if (b.type && b.type.endsWith("_tool_result")) { flush(); nodes.push(renderResult({ tool_use_id: b.tool_use_id, content: JSON.stringify(b.content, null, 2) })); }
    }
    flush();
    if (meta) nodes.push(modelCard([], meta));
  } else {
    let texts = [];
    const flush = () => { if (texts.length) { nodes.push(userCard(texts.join("\n\n"))); texts = []; } };
    for (const b of blocks) {
      if (b.type === "tool_result") { flush(); nodes.push(renderResult(b)); }
      else if (b.type === "text") texts.push(b.text || "");
      else texts.push("[" + (b.type || "block") + "]");
    }
    flush();
  }
  return nodes;
}

// ---------- raw view ----------
function fmtHeaders(h) { return (h || []).map(([k, v]) => k + ": " + v).join("\n"); }
function fmtBytes(n) { return n == null ? "" : n < 1024 ? n + " B" : (n / 1024).toFixed(1) + " KB"; }
async function loadRaw(id, box) {
  box.textContent = "loading…";
  try {
    const r = await fetch("/raw/" + id);
    if (!r.ok) { box.innerHTML = '<span class="chip">raw body expired</span>'; return; }
    const raw = await r.json(), rq = raw.request, rs = raw.response;
    let h = "<h4>Request</h4><pre>" + esc(rq.method + " " + rq.path + "\n" + fmtHeaders(rq.headers)) + "</pre>";
    if (rq.body) h += "<pre>" + esc(rq.body) + "</pre>";
    h += "<h4>Response</h4>";
    if (rs) {
      h += "<pre>" + esc(rs.status + " " + (rs.reason || "") + "\n" + fmtHeaders(rs.headers)) + "</pre>";
      if (rs.body) h += "<pre>" + esc(rs.body) + "</pre>";
    } else h += '<span class="chip">in flight…</span>';
    box.innerHTML = h;
  } catch (e) { box.textContent = "could not load: " + e; }
}
function rawBox(id, label) {
  const d = el("details", "raw", "<summary>" + esc(label || "Raw") + '</summary><div class="rawbody"></div>');
  d.addEventListener("toggle", () => { if (d.open) loadRaw(id, d.querySelector(".rawbody")); });
  return d;
}
function fold(label, text) {
  return el("details", "fold", "<summary>" + esc(label) + "</summary><pre>" + withReminders(text) + "</pre>");
}
function systemText(system) {
  return typeof system === "string" ? system : blocksOf(system).map((b) => b.text || "").join("\n\n");
}
function messageText(m) {
  return blocksOf(m.content).map((b) =>
    b.type === "text" ? b.text || "" :
    b.type === "thinking" ? "[thinking] " + (b.thinking || "") :
    b.type === "tool_use" ? "[tool_use " + (b.name || "") + "] " + JSON.stringify(b.input) :
    b.type === "tool_result" ? "[tool_result " + (b.tool_use_id || "") + "] " + textOf(b.content) :
    "[" + (b.type || "block") + "]").join("\n");
}
function modelBlocks(content) {
  const box = el("div", "hmodel", '<span class="badge mbadge">MODEL</span>');
  for (const b of content || []) {
    if (b.type === "thinking" || b.type === "redacted_thinking") {
      const t = b.thinking || "(redacted)";
      box.insertAdjacentHTML("beforeend", '<details class="thinking"><summary>💭 ' + esc(short(t.replace(/\s+/g, " "), 160)) +
        "</summary><div>" + esc(t) + "</div></details>");
    } else if (b.type === "text") box.appendChild(el("div", "md", md(b.text || "")));
    else box.appendChild(el("pre", "mono", esc("[" + b.type + (b.name ? " " + b.name : "") + "] " + JSON.stringify(b.input ?? ""))));
  }
  return box;
}

// ---------- harness calls ----------
function onHarnessRequest(ev) {
  if (ev.kind === "aux") {
    const c = makeCard("harness", '<span class="badge">HARNESS</span><span class="name mono">#' + ev.id + '</span><span class="arg" title="' +
      esc(ev.label || "") + '">' + esc(ev.label || "") + '</span><span class="meta st"><span class="pulse"></span> ' + esc(ev.model || "") + " · waiting…</span>",
      { open: true });
    const b = el("div", "body");
    const msgs = ev.messages || [];
    const last = [...msgs].reverse().find((m) => m.role === "user");
    if (last) b.appendChild(el("div", "usertext hprompt", withReminders(messageText(last))));
    else if (ev.in_flight) b.appendChild(el("span", "chip", "in flight at Clear"));
    const reply = el("div", "hreply");
    b.appendChild(reply);
    if (ev.system != null) b.appendChild(fold("System prompt", systemText(ev.system)));
    if (msgs.length) b.appendChild(fold("All " + msgs.length + " message" + (msgs.length === 1 ? "" : "s"),
      msgs.map((m) => "── " + m.role + " ──\n" + messageText(m)).join("\n\n")));
    b.appendChild(rawBox(ev.id));
    c.appendChild(b);
    addTop(c);
    harness.set(ev.id, { kind: "aux", node: c, reply });
    return;
  }
  const r = el("div", "hrow harness");
  const what = ev.kind === "count"
    ? '<span class="chip hk">COUNT</span><span class="mono">#' + ev.id + "</span><span>" + esc(ev.model || "") + '</span><span class="t">' +
      (ev.in_flight ? "in flight at Clear" : (ev.total || 0) + " message" + (ev.total === 1 ? "" : "s")) + "</span>"
    : '<span class="chip hk">HTTP</span><span class="mono">#' + ev.id + '</span><span class="t mono" title="' + esc(ev.path) + '">' +
      esc(ev.method + " " + ev.path) + "</span>";
  r.appendChild(el("div", "line", what + '<span class="meta st">…</span>'));
  r.appendChild(rawBox(ev.id));
  addTop(r);
  harness.set(ev.id, { kind: ev.kind, node: r, req_bytes: ev.req_bytes });
}

function onHarnessResponse(h, ev) {
  const st = h.node.querySelector(".st");
  if (ev.error || ev.status >= 400) h.node.classList.add("err");
  if (h.kind === "aux") {
    const u = ev.usage || {};
    const inTok = (u.input_tokens || 0) + (u.cache_read_input_tokens || 0) + (u.cache_creation_input_tokens || 0);
    st.textContent = [ev.model || "", fmtMs(ev.ms), inTok ? inTok.toLocaleString() + " in" : "",
      u.output_tokens ? u.output_tokens.toLocaleString() + " out" : "", ev.stop_reason || ""].filter(Boolean).join(" · ");
    if (ev.error) h.reply.appendChild(el("pre", "herr", esc("ERROR " + ev.status + ": " + ev.error)));
    else h.reply.appendChild(modelBlocks(ev.content));
  } else if (h.kind === "count") {
    st.textContent = ev.error ? "error " + ev.status + " · " + fmtMs(ev.ms)
      : "→ " + (ev.input_tokens ?? "?").toLocaleString() + " tokens · " + fmtMs(ev.ms);
    if (ev.error) h.node.title = ev.error;
  } else {
    st.textContent = ev.status + " · " + fmtMs(ev.ms) + " · ↑" + fmtBytes(h.req_bytes) + " ↓" + fmtBytes(ev.resp_bytes);
    if (ev.error) h.node.title = ev.error;
  }
}

// ---------- main conversation ----------
function onRequest(ev) {
  if (ev.kind !== "main") { onHarnessRequest(ev); return; }
  const conv = getConv(ev);
  conv.requests++; conv.last = ev.time; conv.inflight.add(ev.id);
  if (!conv.title) conv.title = titleFrom(ev.blocks);
  if (selected != null && selected !== ev.conv) conv.newCount++;
  renderEntry(conv);
  if (ev.new_conv) {
    const m = el("div", "marker conv", "<span>▲ New Conversation · #" + ev.conv + "</span>");
    m.dataset.conv = ev.conv;
    addTop(m);
  }
  const chips = [];
  if ("system" in ev) { sys.system = ev.system; if (ev.system_changed) { sys.changed = true; chips.push("system changed"); } }
  if ("tools" in ev) { sys.tools = ev.tools; if (ev.tools_changed) { sys.changed = true; chips.push("tools changed"); } }
  if ("system" in ev || "tools" in ev) { renderDrawer(); updateSysBtn(); }
  const n = (ev.blocks || []).length;
  const g = el("div", "group");
  g.dataset.conv = ev.conv;
  const h = el("div", "ghead", '<span class="chev" aria-hidden="true"></span><span class="gtitle">Request #' + ev.id + " · conv " + ev.conv +
    " · " + esc(ev.time) + " · " + esc(ev.model || "") + " · " +
    (ev.in_flight ? "in flight at Clear" : n ? "+" + n + " new message" + (n === 1 ? "" : "s") : "retry, no new messages") + "</span>" +
    chips.map((c) => '<span class="chip warn">' + c + "</span>").join("") + '<span class="gsum"></span>');
  h.querySelectorAll(".chip").forEach((c) => c.onclick = () => { drawer.classList.add("open"); sys.changed = false; updateSysBtn(); });
  bindHead(h, () => setGroup(g, g.classList.contains("collapsed")));
  h.setAttribute("aria-expanded", "true");
  const rl = el("div", "rawline");
  rl.appendChild(rawBox(ev.id, "Raw #" + ev.id));
  const gb = el("div", "gbody"), resp = el("div", "resp"), msgs = el("div", "msgs");
  gb.append(resp, msgs);
  g.append(h, rl, gb);
  // Render in sent order (so tool results find their calls), then show the last message first.
  const perMessage = (ev.blocks || []).map((m) => renderMessage(m, null));
  for (const nodes of perMessage.reverse()) msgs.append(...nodes);
  const w = el("div", "waiting", '<span class="pulse"></span><span>Waiting for Claude… <span class="el">0.0s</span></span>');
  w.dataset.t0 = Date.now();
  resp.appendChild(w);
  pending.set(ev.id, w);
  groups.set(ev.id, g);
  addTop(g);
}

function onResponse(ev) {
  const h = harness.get(ev.id);
  if (h) { onHarnessResponse(h, ev); return; }
  const w = pending.get(ev.id);
  if (w) { w.remove(); pending.delete(ev.id); }
  const g = groups.get(ev.id);
  if (!g) return;
  const conv = convs.get(+g.dataset.conv);
  if (conv) { conv.inflight.delete(ev.id); conv.err = !!ev.error; renderEntry(conv); }
  let nodes;
  if (ev.error) nodes = [errorCard(ev)];
  else {
    const u = ev.usage || {};
    const inTok = (u.input_tokens || 0) + (u.cache_read_input_tokens || 0) + (u.cache_creation_input_tokens || 0);
    const meta = [esc(ev.model || ""), fmtMs(ev.ms), inTok ? inTok.toLocaleString() + " in" : "",
      u.output_tokens ? u.output_tokens.toLocaleString() + " out" : "", esc(ev.stop_reason || "")].filter(Boolean).join(" · ");
    nodes = renderMessage({ role: "assistant", content: ev.content || [] }, meta);
  }
  const resp = g.querySelector(".resp");
  for (const node of nodes) addInto(resp, node);
  // A late response lands in its own (older) group; flash it so it gets noticed.
  if (g !== stream.querySelector(".group")) nodes.forEach(flash);
  if (g.classList.contains("collapsed")) g.querySelector(".gsum").textContent = groupSummary(g);
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
        resetState(); resetSessions();
        const want = location.hash.match(/conv=(\d+)/);
        wantConv = want ? +want[1] : null;
        // If the conversation in the address never shows up in the replay, fall back to All.
        setTimeout(() => {
          if (wantConv != null && !convs.has(wantConv)) history.replaceState(null, "", location.pathname + location.search);
          wantConv = null;
        }, 2000);
        stream.innerHTML = '<p class="empty" id="empty">Waiting for traffic… run <code>ANTHROPIC_BASE_URL=' + esc(location.origin) + " claude</code></p>";
        drawer.innerHTML = ""; updateSysBtn();
        window.scrollTo(0, 0);
      } else if (ev.type === "cleared") onCleared(ev);
      else if (ev.type === "request") onRequest(ev);
      else if (ev.type === "response") onResponse(ev);
      if (q.value.trim()) applyFilter();
    }, ev.type === "reset" || ev.type === "cleared");
  };
}
resetSessions();
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
