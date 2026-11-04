"""Send a short scripted conversation through proxy.py on port 8941.

Covers turn dedup (moved cache_control), tool call -> tool result pairing,
a tool-less side request, and an API error. See tests/fake_upstream.py.
"""
import json, http.client

def post(payload):
    c = http.client.HTTPConnection("127.0.0.1", 8941)
    c.request("POST", "/v1/messages?beta=true", body=json.dumps(payload), headers={"content-type": "application/json", "x-api-key": "sk-test-1234567890abcdef"})
    r = c.getresponse()
    return r.status, r.read()

tools = [{"name": "Read", "description": "Reads a file", "input_schema": {"type": "object"}}]
sysp = [{"type": "text", "text": "You are Claude Code.", "cache_control": {"type": "ephemeral"}}]
m1 = {"role": "user", "content": [{"type": "text", "text": "<system-reminder>ctx</system-reminder>"},
                                  {"type": "text", "text": "read /tmp/a.txt", "cache_control": {"type": "ephemeral"}}]}
st, b1 = post({"model": "claude-opus-5-5", "stream": True, "system": sysp, "tools": tools, "messages": [m1]})
print("req1", st, len(b1))
a1 = {"role": "assistant", "content": [{"type": "thinking", "thinking": "I should read it.", "signature": "sig"},
                                       {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"file_path": "/tmp/a.txt"}}]}
m2 = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "1\thello\n2\tworld\n3\t!\n4\tmore"}]}
m1b = json.loads(json.dumps(m1)); del m1b["content"][1]["cache_control"]
print("req2", post({"model": "claude-opus-5-5", "stream": True, "system": sysp, "tools": tools, "messages": [m1b, a1, m2]})[0])
print("side", post({"model": "claude-haiku-5-5", "stream": True, "messages": [{"role": "user", "content": "title this"}]})[0])
print("err", post({"model": "claude-opus-5-5", "stream": True, "system": sysp, "tools": tools, "messages": [{"role": "user", "content": "fail please"}]})[0])
