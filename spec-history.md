# Spec History

This file traces how `SPEC.md` for the Proxy Traffic Dashboard changed, and why. It was written by the coding agent (Claude Code) from the working session and the git history of https://github.com/Bipbop11/AGENTIC. Both partners changed the spec: Bipbop11 drove v1–v3, and Artureeez wrote v4 in pull request #1.

| Version | Date | Change | Driven by | Status |
|---|---|---|---|---|
| v0 | 2026-11-03 | Initial spec with five Open Decisions | Lab handout | Given |
| v1 | 2026-11-03 | Open Decisions locked into Decisions | User and agent review | Implemented, commit `9625b59` |
| v2 | 2026-11-04 | **Clear** button and `POST /clear` | User feature request | Implemented, commit `95d395f` |
| v3 | 2026-11-04 | All harness calls shown, plus a Raw view | User feature request (Bipbop11) | Implemented, commit `de44317` |
| v4 | 2026-11-04 | Newest-first order, collapsible cards, **Sessions** sidebar | Partner's pull request #1 (Artureeez) | Implemented |

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

## v4: Newest first, collapsible cards, Sessions sidebar (Artureeez, implemented)

**Author and route:** Artureeez wrote this change on the branch `spec-newest-first-collapsible` and opened it as pull request #1. It had two commits:
- `2b2e2bf` (10:47): newest-first message order and expandable/collapsible cards.
- `24140de` (10:58): the Sessions sidebar.

The branch started from v2 (commit `95d395f`). That was before v3 and before `spec.md` was renamed to `SPEC.md`, so it conflicted with `main` and was merged by hand (see *Merging v4 with v3* below).

**What the spec gained (all browser-only, so `proxy.py` and the event format do not change):**
- **New goals:**
  - **You control the detail:** every card and every request group can be expanded or collapsed.
  - **Find a conversation fast:** a **Sessions** sidebar lists the conversations.
  - The **Follow the conversation** goal now says the newest messages appear at the top.
- **New section, "Message Order: Newest First":**
  - New request groups and other top-level items go at the top of the stream.
  - Inside a request group, the `Request #N` header comes first, then the model's response (or the `Waiting for Claude...` indicator), then the new messages, last message first.
  - The blocks of one message keep their original order, and a late response stays in its own group.
  - Markers are labeled with an up arrow (`▲ New Conversation · #N`, `▲ Cleared at HH:MM:SS`) because they sit below what came after them.
  - Scroll anchoring keeps the card you are reading in place, and **Jump to Latest ↑ (N new)** replaces **Jump to Live ↓**.
  - Replay after a refresh builds the same layout and ends scrolled to the top.
- **New section, "Expanding and Collapsing Cards":**
  - Each card header has a ▸/▾ chevron and works with the keyboard (`Enter`/`Space`, `aria-expanded`).
  - A table defines each card type's collapsed view, expanded view and default. The defaults match the previous view, so nothing changes until you click.
  - A whole `Request #N` group can collapse to one line with a short count of what's inside.
  - New **Expand all** and **Collapse all** buttons. **Tool Details** now sets the state of every tool card.
  - The state is per tab and is never sent to `proxy.py`.
- **New section, "Sessions: Conversation List":**
  - A sidebar (an overlay drawer at phone width) lists the run's conversations newest first, under an **All** entry.
  - Each entry has a title (taken from the first user prompt), a request count, last-activity time, and a live or error dot.
  - Selecting an entry hides everything else instead of rebuilding the stream, and shows new-activity counts for the other conversations.
  - After a Clear, a continuing conversation comes back marked `continued`.
  - The choice is kept in the page address (`#conv=N`).
- **New Decisions:**
  - **6:** reverse the order per message, not per card.
  - **7:** keep the expand/collapse state in the browser only.
  - **8:** sessions reuse the conversation numbers `proxy.py` already assigns.
- **Updated text:** Decisions 1, 2, 4 and 5, Steps to Build, and the Clear section were updated for the newest-first layout and the `▲` markers.

### Merging v4 with v3

Both versions changed the same parts of the spec, so the merge kept both sides and adjusted Artureeez's sections to fit v3:
- **Conflicts resolved:**
  - **Goal:** kept the newest-first wording from v4 and the **Nothing hidden** goal from v3.
  - **Quick filters:** the v4 text plus the `Harness` toggle from v3.
  - **Clear:** kept v3's "raw bodies are cleared" rule and v4's `▲ Cleared at` marker.
  - **Steps to Build:** v3's harness step stays step 5, v4's steps become 6–8, and one combined test step 9 covers both, plus `tests/test_clear.py` and `tests/test_harness.py`.
  - **Decision 5:** kept v4's `▲ New Conversation · #N` marker. Its sentence about "side requests as compact one-line rows" was replaced with v3's harness-calls sentence, because v3 had already dropped those rows.
- **v4 text updated to fit v3:**
  - Top-level items now include harness calls (**HARNESS** cards, **COUNT** and **HTTP** rows) instead of side-request rows.
  - The expand/collapse table gained a **HARNESS** row (expanded by default). **COUNT** and **HTTP** rows stay one line, with only their **Raw** expander.
  - **Expand all** never opens **Raw** expanders, so it never fetches raw bodies in bulk.
  - Each request group keeps its **Raw** expander right under the `Request #N` header.
  - In the Sessions sidebar, harness calls belong to no conversation and are shown only under **All**. The **Harness** filter applies to the selected conversation like the other filters.
  - The note on browser-only data now says `conv` is sent with every main-conversation request event (`kind: "main"`).
  - The Harness Calls section now says the shared stream is newest first instead of chronological.

**Status:** merged into `main` as spec only (merge commit `0731630`), then implemented.

**Implementation and testing:**
- **Code:** only the page embedded in `proxy.py` changed, as the spec says. The server and the event format are unchanged.
  - Every top-level item is inserted at the top.
  - Each request group holds a response slot above its messages.
  - Manual scroll anchoring measures the element under the top of the viewport before each change and scrolls by how far it moved. The browser's own anchoring is turned off so the two don't fight.
  - Cards are built by one helper that adds the chevron, the keyboard handling, `aria-expanded` and the collapsed one-line summary.
  - The Sessions sidebar hides other conversations' items with a CSS class instead of removing them.
- **Added during implementation:** following a `#conv=N` link inside the open page also selects that conversation (a `hashchange` handler). A fragment-only change doesn't reload the page, so without this only a refresh would apply it. The spec's Sessions section now says so.
- **Testing:**
  - Headless Chrome driven over the DevTools protocol ran 25 in-page checks against the fake upstream, and all passed. They cover newest-first order, marker placement, response-above-messages, sidebar order and titles, card and group toggles (mouse and keyboard), **Expand all**/**Collapse all**, **Tool Details**, the tool-result badge, conversation filtering with new-activity counts, scroll anchoring with **Jump to Latest**, the text filter, and **Expand all** leaving **Raw** closed.
  - Separate checks covered `#conv=N` surviving a reload, a stale `#conv` falling back to **All**, and a conversation continuing after Clear (same number, marked `continued`, titled from the first prompt after the Clear, with the `▲ Cleared at` marker at the bottom).
  - The existing Python tests still pass.

## Process notes

- **Spec before code:** each change was made to the spec first and implemented only afterwards (v1 and v2). v3 followed the same path: spec first (commit `77205be`), then the implementation. v4 was then implemented the same way.
- **Decisions reviewed before implementation:** the user asked to review and lock the Open Decisions before any code was written. The agent recommended an option for each, and the user confirmed them.
- **Recorded in the spec:** behaviour the agent added during implementation, such as the side-request rows in v1 and the Clear edge cases in v2, was written back into the spec so the spec stays the source of truth.
- **Two partners, one spec:** v4 was written in parallel on a branch and reviewed as a pull request. Merging it after v3 needed the conflicts resolved by hand. The merge kept both partners' features and rewrote the places where v4 still described the old side-request rows.
