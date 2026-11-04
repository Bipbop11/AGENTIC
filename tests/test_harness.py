"""Check that every harness call shows up, against a running proxy (port 8941)
and tests/fake_upstream.py.

    python3 tests/fake_upstream.py 8942
    python3 proxy.py --port 8941 --upstream http://127.0.0.1:8942
    python3 tests/test_harness.py
"""
import http.client, json

PORT = 8941
TOOLS = [{"name": "Read", "description": "Reads a file", "input_schema": {"type": "object"}}]

def call(method, path, payload=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", PORT)
    body = json.dumps(payload) if payload is not None else None
    c.request(method, path, body=body, headers={"content-type": "application/json", **(headers or {})})
    r = c.getresponse()
    return r.status, r.read()

def replay():
    """Read the history that a freshly loaded page would get."""
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=1)
    c.request("GET", "/events")
    r = c.getresponse()
    buf = b""
    try:
        while True:
            buf += r.read1(65536)
    except OSError:
        pass
    return [json.loads(l[6:]) for l in buf.decode().splitlines() if l.startswith("data: ")]

def raw(rid):
    status, body = call("GET", f"/raw/{rid}")
    return status, json.loads(body)

call("POST", "/clear")
start = len([e for e in replay() if e["type"] == "request"])

# One of each kind of call.
secret = "sk-ant-secret-0123456789abcdefghij"
assert call("POST", "/v1/messages?beta=true", {"model": "claude-haiku-5-5", "stream": True,
            "system": [{"type": "text", "text": "Generate a short title.\nMore rules."}],
            "messages": [{"role": "user", "content": "title this chat"}]}, {"x-api-key": secret})[0] == 200
assert call("POST", "/v1/messages/count_tokens?beta=true", {"model": "claude-opus-5-5", "tools": TOOLS,
            "messages": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}, {"role": "user", "content": "x"}]})[0] == 200
assert call("GET", "/v1/models")[0] == 200
assert call("GET", "/v1/nope")[0] == 404
assert call("HEAD", "/")[0] == 200
assert call("POST", "/v1/messages", {"model": "claude-opus-5-5", "stream": True, "tools": TOOLS,
            "messages": [{"role": "user", "content": "read /tmp/a.txt"}]})[0] == 200

ev = replay()
reqs = {e["id"]: e for e in ev if e["type"] == "request"}
resps = {e["id"]: e for e in ev if e["type"] == "response"}
kinds = [reqs[i]["kind"] for i in sorted(reqs)]
assert kinds == ["aux", "count", "http", "http", "http", "main"], kinds
ids = sorted(reqs)
assert ids == list(range(ids[0], ids[0] + 6)), "one counter for every call"

aux, count, models, nope, head, main = (reqs[i] for i in ids)
assert aux["label"] == "Generate a short title." and aux["messages"][-1]["content"] == "title this chat", aux
assert "text" in json.dumps(resps[aux["id"]]["content"]), "aux reply reassembled"
assert count["total"] == 3 and resps[count["id"]]["input_tokens"] == 42
assert (models["method"], models["path"]) == ("GET", "/v1/models") and resps[models["id"]]["status"] == 200
assert resps[nope["id"]]["status"] == 404 and "error" in resps[nope["id"]]
assert head["method"] == "HEAD" and resps[head["id"]]["status"] == 200
assert main.get("new_conv") or main.get("conv"), main
assert not any(e.get("new_conv") for e in (aux, count, models, nope, head)), "harness calls never start a conversation"

# Raw view: redacted headers, pretty body, raw SSE response.
status, r = raw(aux["id"])
assert status == 200, r
hdrs = dict(r["request"]["headers"])
assert hdrs["x-api-key"] == secret[:16] + "...", hdrs
assert secret not in json.dumps(r), "secret leaked into raw view"
assert '"model": "claude-haiku-5-5"' in r["request"]["body"]
assert r["response"]["status"] == 200 and "event: message_start" in r["response"]["body"]
assert raw(99999)[0] == 404

# Dashboard routes are not recorded as calls.
assert all(not e["path"].startswith(("/events", "/raw", "/clear")) for e in reqs.values())

# Raw bodies are capped at the most recent 200 calls.
for _ in range(200):
    call("GET", "/v1/models")
assert raw(aux["id"])[0] == 404, "oldest raw body should have expired"
assert raw(main["id"] + 200)[0] == 200

# Clear drops finished raw bodies.
call("POST", "/clear")
assert raw(main["id"] + 200)[0] == 404
print("OK: harness calls, count/http rows, raw view, redaction, cap and clear")
