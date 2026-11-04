# Spec History

This file traces how `SPEC.md` (stored in the repo as `spec.md`) for the Proxy Traffic Dashboard changed, and why. It was written by the coding agent (Claude Code) from the working session and the git history of https://github.com/Bipbop11/AGENTIC.

| Version | Date | Change | Driven by | Status |
|---|---|---|---|---|
| v0 | 2026-11-03 | Initial spec with five Open Decisions | Lab handout | Given |
| v1 | 2026-11-03 | Open Decisions locked into Decisions | User and agent review | Implemented, commit `9625b59` |
| v2 | 2026-11-04 | **Clear** button and `POST /clear` | User feature request | Implemented, commit `95d395f` |
| v3 | 2026-11-04 | All harness calls shown, plus a Raw view | User feature request | Implemented |

---

## v0: Initial spec (given)

The starting spec described a live browser dashboard served by `proxy.py` (standard library only):

- **Card styles:** four card styles: **USER**, **MODEL**, **TOOL CALL** and **TOOL RESULT**.
- **Noise control:** a collapsed **System & Tools** drawer.
- **Deduplication:** only newly added messages are shown in each request.
- **Navigation:** smart auto-scroll and quick filters.

It ended with five **Open Decisions** left for the team to settle:

1. Nest each `tool_result` inside its `tool_use` card, or show it as the next chronological card?
2. Stream tokens live, or render each turn when its response completes?
3. Hide `<system-reminder>` blocks behind a badge, or show them inline?
4. Keep history in memory only, or also save it to `traffic.jsonl`?
5. Show multiple conversations in one stream with dividers, or in separate tabs?

## v1: Decisions locked (implemented)

**How it was decided:** the user first pasted a set of choices, then stopped the agent and asked it to review the Open Decisions *before writing any code*. The agent presented each decision with a recommendation and its reason. The recommendations matched the user's earlier choices, and the user accepted all five. The agent then replaced the "Open Decisions" section with "Decisions":

| # | Decision | Reason |
|---|---|---|
| 1 | Nest the `tool_result` inside its `tool_use` card, and keep a small chronological **TOOL RESULT** badge in the stream that scrolls to the card. Results with no matching call become standalone cards. | You read a call and its output together, and the timeline stays clear. |
| 2 | Show a `Waiting for Claude...` indicator with a timer, and render the turn when the response finishes. The proxy reassembles the streamed SSE response into complete blocks. | Claude Code always streams, so reassembly is needed anyway. Rendering on completion avoids half-rendered Markdown. |
| 3 | Collapse `<system-reminder>` blocks into a gray pill, in both user messages and tool results. | They're noise most of the time but easy to open when needed. |
| 4 | Keep history in memory for the current run and replay it on page load, so a refresh keeps the session. No `traffic.jsonl`. | Zero configuration and no files to clean up. |
| 5 | One continuous stream, with a **New Conversation** divider when the `messages` list no longer extends a previous one. | Tabs add UI complexity for little gain in a lab tool. |

**Added during implementation:** Decision 5 also says that tool-less side requests (titles, quota checks) appear as compact one-line rows and don't start a new conversation. Without this, every background call Claude Code makes would have added a spurious **New Conversation** divider.

**Implementation notes that shaped the spec:**
- **Matching a request to its conversation:** compare each request to every recent message list, not just the previous one. This keeps parallel subagents and forked side calls apart.
- **Comparing messages loosely:** ignore `cache_control` markers and thinking blocks, because Claude Code moves the markers every turn.

**Testing:** done with a fake upstream (`tests/fake_upstream.py`) and a scripted client (`tests/client.py`), plus a headless-Chrome screenshot.

## v2: Clear button (implemented)

**User request:** add a **Clear** button that calls `POST /clear`. It should clear the dashboard view and the stored replay events, but keep tracking the current conversation's fingerprints, so that the next request in the same session shows only the messages sent after the Clear.

**What the spec gained:**
- **New section, "Clearing the View":**
  - `POST /clear` is handled locally and never forwarded. It returns `204` and prints `--- dashboard cleared` in the terminal.
  - It clears the replay history and the view in every open tab.
  - It keeps the deduplication snapshots and the request and conversation counters.
- **Updated sections:** Quick filters, Steps to Build and Decision 4.

**Edge cases the agent added to the spec (each one prevents a bug that Clear would otherwise cause):**
- **Keep the latest `system` and `tools`** and send them with the clear event. These are only sent when they change, so otherwise the **System & Tools** drawer would stay empty for the rest of the session.
- **Requests in flight at Clear time** keep their waiting indicator, and their reply still renders.
- **A `tool_result` whose `tool_use` card was cleared** falls back to a standalone card, as Decision 1 already says.

**Implementation and testing:**
- **Code:** commit `95d395f`.
- **New test:** `tests/test_clear.py` covers dedup after Clear, the counters, kept `system`/`tools`, and a request in flight during the Clear. It passes.

## v3: All harness calls (implemented)

**User request:** "change the spec such that the dashboard will include all the harness calls to and from the model".

**What the spec gained:**
- **Intro, Goal and flow:** they now cover *every* call through the proxy. A new goal, **Nothing hidden**, says so.
- **New section, "Harness Calls":** one stream and one `Request #N` counter for all calls. Each kind of call gets its own style:
  - **Main conversation** (`POST /v1/messages` with `tools`): unchanged.
  - **Auxiliary model calls** (any other `POST /v1/messages`): full purple **HARNESS** cards. These replace v1's one-line side rows. Each card is labeled with the first line of its `system` prompt rather than a guessed purpose.
  - **Token counting** (`/v1/messages/count_tokens`): **COUNT** rows. These were previously skipped entirely.
  - **Any other endpoint and method:** gray **HTTP** rows. These were previously invisible.
  - **Failures:** a red border, whatever the kind of call.
- **Raw view on every call:** request headers (with secrets redacted), request body, and the raw response. The browser fetches it on demand from a new local `GET /raw/<id>` route, and raw bodies are kept for the last 200 calls only. This keeps the live event stream small and memory bounded.
- **Scope note:** only traffic sent to `ANTHROPIC_BASE_URL` passes through the proxy. Calls to other hosts, such as telemetry, cannot be shown.
- **Other updates:**
  - A new **Harness** filter toggle.
  - Clear also drops the stored raw bodies.
  - A new step 5 in Steps to Build.
  - The test step now checks for harness calls and a working **Raw** view.
  - Decision 5 now points to the "Harness Calls" section.

**Reverses part of an earlier decision:** v1's "side requests as compact one-line rows" is replaced. Those calls are now shown in full.

**Implementation and testing:**
- **Code:** `proxy.py` records every relayed call with a kind (`main`, `aux`, `count` or `http`) under one counter. It keeps raw requests and responses in a 200-entry store, served by `GET /raw/<id>`.
- **New test:** `tests/test_harness.py` covers each kind of call, the single counter, the Raw view (including secret redaction), the 200-call cap, and Clear dropping raw bodies. It passes, and so do the existing tests.
- **Test fix:** `tests/test_clear.py` was made safe to rerun against the same proxy.

## Process notes

- **Spec before code:** each change was made to the spec first and implemented only afterwards (v1 and v2). v3 followed the same path: spec first (commit `77205be`), then the implementation.
- **Decisions reviewed before implementation:** the user asked to review and lock the Open Decisions before any code was written. The agent recommended an option for each, and the user confirmed them.
- **Recorded in the spec:** behaviour the agent added during implementation, such as the side-request rows in v1 and the Clear edge cases in v2, was written back into the spec so the spec stays the source of truth.
