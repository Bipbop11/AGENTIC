"""Fake Anthropic Messages API for testing proxy.py without a real key.

Streams SSE replies: a thinking block plus a Read tool call on the first turn,
a Markdown text reply once a tool_result arrives (or for tool-less requests),
and a 529 error when the last message contains "fail".

    python3 tests/fake_upstream.py 8942
    python3 proxy.py --port 8941 --upstream http://127.0.0.1:8942
    python3 tests/client.py        # then open http://localhost:8941
"""
import json, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

def sse(events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        msgs = body["messages"]
        if "fail" in json.dumps(msgs[-1]):
            out = b'{"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}'
            self.send_response(529); self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(out))); self.end_headers(); self.wfile.write(out); return
        last = msgs[-1]["content"]
        has_result = isinstance(last, list) and any(b.get("type") == "tool_result" for b in last)
        ev = [{"type": "message_start", "message": {"id": "m1", "model": body["model"], "role": "assistant", "content": [], "usage": {"input_tokens": 50, "output_tokens": 1}}}]
        if has_result or not body.get("tools"):
            ev += [{"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                   {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "The file says **hello**.\n\n- one\n- `two`"}},
                   {"type": "content_block_stop", "index": 0},
                   {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 12}}]
        else:
            ev += [{"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
                   {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "I should read it."}},
                   {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig"}},
                   {"type": "content_block_stop", "index": 0},
                   {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {}}},
                   {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{\"file_pa"}},
                   {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "th\": \"/tmp/a.txt\"}"}},
                   {"type": "content_block_stop", "index": 1},
                   {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 30}}]
        ev.append({"type": "message_stop"})
        out = sse(ev)
        self.send_response(200); self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(out))); self.end_headers(); self.wfile.write(out)

ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
