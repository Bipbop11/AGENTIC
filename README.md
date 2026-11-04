# AGENTIC: Agent-Traffic Proxy with a Live Dashboard

`proxy.py` sits between Claude Code and the Anthropic Messages API. It forwards every request unchanged, prints the raw traffic to the terminal, and serves a live browser dashboard that shows the conversation as readable cards, along with every other call the harness makes.

It uses only the Python 3 standard library, so there's nothing to install.

## Quick start

```bash
# Terminal 1: start the proxy
python3 proxy.py                      # listens on http://localhost:8041

# Terminal 2: run Claude Code through it
ANTHROPIC_BASE_URL=http://localhost:8041 claude
```

Then open **http://localhost:8041** in a browser.

### Options

```bash
python3 proxy.py --port 9000
python3 proxy.py --upstream https://chatgpt.com/backend-api/codex   # for Codex
```

## What the dashboard shows

| Card | Color | Content |
|---|---|---|
| **USER** | Blue | The prompt. `<system-reminder>` blocks are collapsed into gray pills. |
| **MODEL** | Green | The reply rendered as Markdown, with model, response time, tokens and stop reason. Thinking is a one-line preview you can expand. |
| **TOOL CALL** | Orange | Tool name and main argument (`file_path`, `command`, …). Click to see the full JSON input. |
| **TOOL RESULT** | Teal / red | Nested inside its tool call card. Long outputs show 3 lines and a line count. A small badge in the timeline marks when the result arrived. |

Other features:

- **Newest first:** new request groups appear at the top. Inside a group, the model's reply sits under the `Request #N` header, above the messages that triggered it. If you scroll down to read, new traffic doesn't move what you're reading, and a **Jump to Latest ↑** button counts what arrived.
- **Only new messages:** Claude Code re-sends the whole conversation on every request. Each `Request #N` group shows only what's new.
- **New Conversation markers:** `▲ New Conversation · #N` appears when the message list resets (after `/clear`, a restart, or a new subagent), just below that conversation's first group.
- **Expand and collapse:** every card and every request group has a ▸/▾ toggle (click the header or press Enter/Space). **Expand all** and **Collapse all** act on everything in view. Tool cards start collapsed, and **Tool Details** opens or closes them all.
- **Sessions sidebar:** lists the conversations of the current run. Pick one to see only its messages; the choice is kept in the address (`#conv=N`). Other conversations show a count of new requests.
- **Harness calls:** every other call through the proxy appears in the same stream, numbered in call order:
  - Auxiliary model calls (title generation, quota checks, compaction…) are purple **HARNESS** cards, labeled with the first line of their system prompt.
  - `count_tokens` calls are **COUNT** rows.
  - Any other endpoint is an **HTTP** row.
  - Failed calls get a red border.
- **Raw view:** every call has a **Raw** expander with the request headers (keys redacted), the full request body, and the raw response. The last 200 calls are kept.
- **System & Tools drawer:** the system prompt and tool definitions are collapsed in the top bar and flagged when they change.
- **Filters:** toggle System, Thinking, Tool Details and Harness, or filter cards by text.
- **Clear:** empties the view in every open tab and the stored history. The proxy keeps tracking the conversation, so the next turn shows only messages sent after the Clear, with no New Conversation divider.
- **Waiting indicator:** a "Waiting for Claude…" line with a timer shows while a request is in flight.
- **Survives refresh:** history is kept in memory for the current run and replayed when the page reloads. Restarting the proxy clears it.

## How it works

- `POST /v1/messages` (and any other path) is forwarded upstream with headers intact, and the reply is streamed back byte for byte.
- `GET /` serves the dashboard page, which is embedded in `proxy.py`.
- `GET /events` is a Server-Sent Events stream. It replays the session history, then sends each new request and completed response.
- `GET /raw/<id>` returns a call's raw request and response for the Raw view (404 once it has expired).
- `POST /clear` (the **Clear** button) drops the stored history but keeps the deduplication state, counters and latest system/tools. It is never forwarded upstream.
- Streamed responses are put back together into complete text, thinking and `tool_use` blocks before they are sent to the browser.
- A dashboard error can never break proxying. It only prints a line to stderr.

See [`SPEC.md`](SPEC.md) for the full design and the decisions behind it.

## Testing without an API key

`tests/` contains a fake Messages API, a scripted client and two checks. The client sends a tool call and its result, a side request, and an API error. `test_harness.py` checks that every kind of call appears, the Raw view and its redaction, the 200-call cap, and Clear. `test_clear.py` checks that Clear keeps tracking the conversation, including a request that is in flight during the Clear.

```bash
python3 tests/fake_upstream.py 8942
python3 proxy.py --port 8941 --upstream http://127.0.0.1:8942
python3 tests/client.py        # then open http://localhost:8941
python3 tests/test_harness.py  # prints OK or fails an assert
python3 tests/test_clear.py    # prints OK or fails an assert
```

## Security note

The proxy listens on `127.0.0.1` only. The terminal log and the Raw view truncate `authorization` and `x-api-key` headers, but request and response bodies are printed and shown in full. Don't expose the dashboard to other machines.
