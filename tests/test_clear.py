"""Check POST /clear against a running proxy (port 8941) and tests/fake_upstream.py.

    python3 tests/fake_upstream.py 8942
    python3 proxy.py --port 8941 --upstream http://127.0.0.1:8942
    python3 tests/test_clear.py
"""
import http.client, json, threading, time

PORT = 8941
TOOLS = [{"name": "Read", "description": "Reads a file", "input_schema": {"type": "object"}}]
SYSTEM = [{"type": "text", "text": "You are Claude Code."}]

def post(path, payload=None):
    c = http.client.HTTPConnection("127.0.0.1", PORT)
    body = json.dumps(payload) if payload is not None else None
    c.request("POST", path, body=body, headers={"content-type": "application/json"})
    r = c.getresponse()
    return r.status, r.read()

def ask(messages):
    status, raw = post("/v1/messages", {"model": "claude-opus-5-5", "stream": True, "system": SYSTEM, "tools": TOOLS, "messages": messages})
    assert status == 200, status
    return raw

def replay():
    """Read the history that a freshly loaded page would get."""
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=1)
    c.request("GET", "/events")
    r = c.getresponse()
    events, buf = [], b""
    try:
        while True:
            buf += r.read1(65536)
    except OSError:
        pass
    for line in buf.decode().splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events

def user(text):
    return {"role": "user", "content": text}

# A conversation with a tool call, before the Clear.
m = [user("read /tmp/a.txt")]
ask(m)
m += [{"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"file_path": "/tmp/a.txt"}}]},
      {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "hello"}]}]
ask(m)
m.append({"role": "assistant", "content": [{"type": "text", "text": "The file says **hello**.\n\n- one\n- `two`"}]})
before = replay()
assert sum(e["type"] == "request" for e in before) == 2

# Clear while a slow request is in flight.
slow = threading.Thread(target=ask, args=([user("something slow")],))
slow.start()
time.sleep(0.5)
status, _ = post("/clear")
assert status == 204, status
slow.join()

# The same session continues after the Clear.
m.append(user("now summarise it"))
ask(m)

ev = replay()
types = [e["type"] for e in ev]
assert types[:2] == ["reset", "cleared"], types
cleared = ev[1]
assert cleared["system"] == SYSTEM and cleared["tools"] == TOOLS, "drawer data kept"
stub = ev[2]
assert stub["type"] == "request" and stub.get("in_flight") and stub["blocks"] == [], stub
assert any(e["type"] == "response" and e["id"] == stub["id"] for e in ev), "in-flight reply replayed"
reqs = [e for e in ev if e["type"] == "request" and not e.get("in_flight")]
assert len(reqs) == 1, reqs
r = reqs[0]
assert not r.get("new_conv"), "same conversation, no divider"
assert r["start"] == 4 and r["blocks"] == [user("now summarise it")], r
assert r["id"] == 4, "request counter kept counting"
print("OK: clear keeps dedup state, counters, system/tools and in-flight replies")
