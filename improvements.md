# Scilab Xcos MCP Server Review

Review date: 2026-03-23

Scope reviewed:
- MCP tool definitions and implementations in `server.py`
- MCP app metadata/resource wiring in `server.py`
- Dashboard UI in `ui/workflow-dashboard.html`
- Local launch flow described in `USE_LOCALLY.md`

## Highest-priority changes

### 1. Fix the Claude-local UI boot path first

This is the biggest practical problem.

- The embedded UI imports `@modelcontextprotocol/ext-apps` from `https://esm.sh` at runtime in [`ui/workflow-dashboard.html`](./ui/workflow-dashboard.html).
- If that import fails, or if `App.connect()` fails, the code silently falls back to "standalone" mode and immediately tries `fetch("/workflow-ui/api/...")`.
- In local Claude stdio mode, that HTTP API usually does not exist, because the server is not running in HTTP mode.
- Result: the fallback path is not a real fallback for Claude local use, and the app can fail without a useful explanation.

What I would change:

- Bundle the UI into a self-contained asset instead of importing from `esm.sh` at runtime.
- Split the `init()` error handling into separate stages:
  - import failure
  - MCP bridge connection failure
  - standalone HTTP fallback failure
- Show the actual error in the UI instead of swallowing it with `catch (_)`.
- Only enter standalone mode if the page is actually being served from the MCP server HTTP app, not just because MCP mode failed.
- Add a visible "MCP bridge unavailable" banner with actionable next steps.

Relevant code:
- [`server.py:1542`](./server.py#L1542)
- [`server.py:1600`](./server.py#L1600)
- [`server.py:1626`](./server.py#L1626)
- [`ui/workflow-dashboard.html:381`](./ui/workflow-dashboard.html#L381)
- [`ui/workflow-dashboard.html:474`](./ui/workflow-dashboard.html#L474)

### 2. Stop exposing unimplemented or misleading tools

Right now the server advertises a tool that is explicitly not implemented:

- `xcos_revert_phase` is listed in [`server.py:1798`](./server.py#L1798)
- The implementation always returns an error in [`server.py:1126`](./server.py#L1126)

That should be one of:

- hidden until implemented
- marked experimental in naming/docs
- fully implemented with real phase ownership tracking

I would remove it from `list_tools()` for now.

### 3. Make tool outputs consistent and structured

The tool surface is currently mixed:

- some tools return structured content through `handle_call_tool`
- some return JSON-as-text only
- some return plain text
- `xcos_list_sessions` returns a bare JSON array rather than an object wrapper

This inconsistency makes client code harder to write and easier to break.

What I would change:

- Give every tool an `outputSchema`
- Return structured dicts from the underlying tool functions, not JSON strings
- Use plain text only for human-readable summaries, not as the primary transport
- Wrap list outputs as objects like `{ "sessions": [...] }` instead of returning a top-level array

Relevant code:
- [`server.py:549`](./server.py#L549)
- [`server.py:553`](./server.py#L553)
- [`server.py:1091`](./server.py#L1091)
- [`server.py:1626`](./server.py#L1626)
- [`server.py:1808`](./server.py#L1808)

### 4. Tighten the draft/phase workflow contract

The draft workflow currently has conflicting rules:

- `xcos_plan_phases` says "call this first", but it auto-creates a draft if the session does not exist
- `xcos_add_blocks` and `xcos_add_links` bypass phase planning entirely
- `xcos_commit_phase` only accepts `blocks_xml`, not links
- phase order is not enforced
- `xcos_revert_phase` cannot work because phase-to-change ownership is not stored

What I would change:

- Decide whether the server supports:
  - freeform draft editing, or
  - strict phased draft editing
- If phased editing wins:
  - require an existing session for `xcos_plan_phases`
  - enforce phase order
  - store per-phase block/link deltas
  - make commit/revert operate on those deltas
- If freeform editing wins:
  - remove `xcos_plan_phases`, `xcos_commit_phase`, and `xcos_revert_phase`

Relevant code:
- [`server.py:1139`](./server.py#L1139)
- [`server.py:1151`](./server.py#L1151)
- [`server.py:1203`](./server.py#L1203)
- [`server.py:1217`](./server.py#L1217)
- [`server.py:1126`](./server.py#L1126)

## Tool-by-tool review

### `xcos_open_workflow_ui`

Status: concept is good, but the value is limited by the current UI boot fragility.

Changes:
- Keep the tool.
- Add `workflow_count` to the payload for lightweight clients.
- Consider returning the currently selected workflow or last active workflow id.
- Most importantly, fix the UI app loading path.

### `xcos_create_workflow`

Status: good foundation.

Changes:
- Return a stable top-level `workflow_id` in addition to the nested workflow object.
- Add basic limits or truncation guidance for very large `problem_statement` inputs.
- Consider duplicate detection if the same problem statement is submitted repeatedly.

### `xcos_list_workflows`

Status: generally fine.

Changes:
- Add pagination if these sessions can accumulate over time.
- Return counts or summary metadata explicitly instead of making the client derive everything.

### `xcos_get_workflow`

Status: generally fine.

Changes:
- Add a clear schema for `phases` and `last_verified`.
- Consider a `not_found` structured error instead of text-based `"Error: ..."` flow.

### `xcos_submit_phase`

Status: okay for Phase 1 and Phase 2, but blurry for Phase 3.

Problems:
- The tool treats Phase 3 like a text submission step, while the real implementation workflow is draft/session based.

Changes:
- Either restrict this tool to Phase 1 and Phase 2 only, or define exactly what Phase 3 submission means.
- Return a structured transition summary such as previous status, new status, and next allowed action.

### `xcos_review_phase`

Status: solid basic behavior.

Changes:
- Return `next_phase` explicitly.
- Consider requiring non-empty feedback on `request_changes`.

### `get_xcos_block_data`

Status: useful data source, but the current output shape is hard to consume.

Problems:
- Returns one large concatenated text blob.
- Can become extremely large.
- Uses headings in text instead of typed fields.
- The description promises annotation JSON, reference XML, and help, but the result is not structured as those separate artifacts.

Changes:
- Return structured fields such as:
  - `info_json`
  - `reference_xml`
  - `extra_examples`
  - `help_sections`
  - `missing_sections`
- Add size limits or truncation flags.
- Add a `found` boolean and explicit file paths.

Relevant code:
- [`server.py:818`](./server.py#L818)

### `get_xcos_block_source`

Status: useful, but too primitive.

Problems:
- Recursive filesystem scan on every call.
- Plain text error reporting.

Changes:
- Build and reuse a source index at startup.
- Return `{ found, path, source }`.
- Add optional `include_path` or `include_metadata`.

Relevant code:
- [`server.py:808`](./server.py#L808)

### `search_related_xcos_files`

Status: currently weaker than the name suggests.

Problems:
- It only searches filenames, not file contents.
- It returns raw paths only.
- No ranking, no metadata, no match reason.

Changes:
- Decide whether this is filename search, content search, or both.
- Return structured matches with:
  - `path`
  - `match_type`
  - `match_text`
- Add an optional `mode` argument like `filename`, `content`, or `all`.

Relevant code:
- [`server.py:882`](./server.py#L882)

### `verify_xcos_xml`

Status: important tool, but the contract is underspecified.

Problems:
- The description says it sends XML to an open Scilab instance, but subprocess mode does something different.
- No `outputSchema`.
- Validation mode differences are hidden in behavior instead of made explicit in the contract.

Changes:
- Update the description to reflect both poll and subprocess validators.
- Add a full `outputSchema`.
- Always return:
  - `success`
  - `origin`
  - `validator_mode`
  - `warnings`
  - `file_path`
  - `error`
  - `hint`

Relevant code:
- [`server.py:894`](./server.py#L894)
- [`server.py:969`](./server.py#L969)
- [`server.py:1718`](./server.py#L1718)

### `xcos_start_draft`

Status: useful, but it needs stronger lifecycle rules.

Problems:
- A workflow can start another draft even if it already has one.
- The old draft mapping is not cleaned before replacing `draft_session_id`.
- No output schema.

Changes:
- Refuse to start a second draft unless the caller explicitly asks to replace the old one.
- Return the previous draft id if replacement happens.
- Add `outputSchema`.

Relevant code:
- [`server.py:1032`](./server.py#L1032)

### `xcos_add_blocks`

Status: too weak for a core workflow tool.

Problems:
- No XML fragment validation before storing.
- Text-only success response.
- No counts, ids, or parsed feedback returned.

Changes:
- Parse the fragment and validate that only supported block node types are present.
- Return structured data such as `block_count_added`, `session_id`, and maybe extracted block ids.
- Add `outputSchema`.

Relevant code:
- [`server.py:1139`](./server.py#L1139)

### `xcos_add_links`

Status: same issues as `xcos_add_blocks`.

Changes:
- Validate link fragment structure.
- Return structured counts and ids.
- Add `outputSchema`.

Relevant code:
- [`server.py:1151`](./server.py#L1151)

### `xcos_verify_draft`

Status: one of the better tools.

Changes:
- Add `outputSchema`.
- Include block/link counts in the returned payload.
- Store warnings in workflow phase metadata, not just the raw result.

Relevant code:
- [`server.py:1163`](./server.py#L1163)

### `xcos_plan_phases`

Status: the current behavior is misleading.

Problems:
- The description says to call it after a draft exists, but it silently creates a draft if the session id is missing.

Changes:
- Require that `session_id` already exists.
- Validate that `phases` is non-empty and contains unique labels.
- Return the current active phase explicitly.

Relevant code:
- [`server.py:1203`](./server.py#L1203)

### `xcos_commit_phase`

Status: underpowered and internally inconsistent.

Problems:
- Accepts only `blocks_xml`.
- Does not enforce commit order.
- Does not persist ownership data needed for revert.
- The name suggests a full phase commit, but it only appends blocks and writes a snapshot.

Changes:
- Replace `blocks_xml` with a commit model that can include both blocks and links.
- Enforce sequential phase completion.
- Return the committed phase and the next required phase.

Relevant code:
- [`server.py:1217`](./server.py#L1217)

### `xcos_get_draft_xml`

Status: useful, but one flag is misleading.

Problems:
- `validate=True` does not perform semantic validation; it only parses XML and optionally pretty-prints it.

Changes:
- Rename `validate` to something like `parse_check`, or make it call real validation.
- Add `outputSchema`.

Relevant code:
- [`server.py:1065`](./server.py#L1065)

### `xcos_get_file_path`

Status: useful, but not truly read-only.

Problems:
- It writes a fresh session snapshot as a side effect.

Changes:
- Split into:
  - `xcos_snapshot_session`
  - `xcos_get_file_path`
- Or add a `write_snapshot` boolean defaulting to `false`.

Relevant code:
- [`server.py:1251`](./server.py#L1251)

### `xcos_get_file_content`

Status: useful export tool, but also not truly read-only.

Problems:
- `source="session"` writes a new snapshot before reading it.
- Large content can create very heavy tool payloads.

Changes:
- Avoid implicit writes in read APIs.
- Add size metadata and an optional `max_bytes`.
- Add `outputSchema`.

Relevant code:
- [`server.py:1274`](./server.py#L1274)

### `xcos_list_sessions`

Status: helpful, but structurally inconsistent.

Problems:
- Returns a top-level array instead of `{ "sessions": [...] }`.
- No `outputSchema`.
- No sort or pagination controls.

Changes:
- Return `{ "sessions": [...] }`.
- Add `outputSchema`.
- Consider sorting by `created_at` descending.

Relevant code:
- [`server.py:1091`](./server.py#L1091)

### `xcos_revert_phase`

Status: should not be public yet.

Changes:
- Remove from `list_tools()` until implemented, or finish the implementation with per-phase ownership tracking.

Relevant code:
- [`server.py:1126`](./server.py#L1126)
- [`server.py:1798`](./server.py#L1798)

## UI review

### What looks good

- The UI is self-contained in one HTML file, which makes resource serving simple.
- The visual direction is clear and readable.
- The workflow state rendering is straightforward and easy to follow.

### What is incorrect or fragile

#### 1. The embedded app silently hides the real failure

In [`ui/workflow-dashboard.html:489`](./ui/workflow-dashboard.html#L489), the code catches all app startup failures and discards the error.

That makes debugging much harder, especially for Claude-local embedding failures.

Change:
- Surface the exact exception in a visible error box.

#### 2. The "standalone" fallback is not valid inside local Claude stdio mode

The fallback path uses relative HTTP calls such as:

- [`ui/workflow-dashboard.html:402`](./ui/workflow-dashboard.html#L402)
- [`ui/workflow-dashboard.html:417`](./ui/workflow-dashboard.html#L417)

That works only when the page is served from the Starlette HTTP server.
It is not a safe generic fallback for an embedded MCP app running from a stdio-connected local server.

Change:
- Detect whether the app is actually running from the standalone HTTP server before using those endpoints.

#### 3. The app depends on a remote module at runtime

[`ui/workflow-dashboard.html:476`](./ui/workflow-dashboard.html#L476) imports from `https://esm.sh/...`.

That is fragile for:

- offline/local development
- CSP differences across hosts
- host sandbox networking differences
- reproducibility

Change:
- bundle the dependency and serve local static output

#### 4. Phase 3 is not actually represented in the UI

The dashboard can start a draft session in [`ui/workflow-dashboard.html:310`](./ui/workflow-dashboard.html#L310), but there are no controls for:

- `xcos_plan_phases`
- `xcos_add_blocks`
- `xcos_add_links`
- `xcos_verify_draft`
- `xcos_get_file_path`
- `xcos_get_file_content`

So the UI only really covers workflow approval, not the implementation workflow it claims to expose.

Change:
- Either keep the UI intentionally Phase-1/2-only, or add a real Phase 3 panel with draft controls and verification results.

#### 5. No loading/disabled state during async actions

Buttons remain active during network/tool calls, which can create duplicate submissions.

Change:
- add per-action loading state
- disable buttons while requests are in flight

#### 6. No auto-refresh or workflow polling in standalone mode

The browser UI only refreshes when the user clicks refresh or triggers an action.

Change:
- poll periodically in standalone mode, or add manual "last updated" status so stale data is obvious

## Server-level cleanup I would also do

- Replace string-based `"Error: ..."` transport with structured errors or MCP error results.
- Add output schemas for every tool.
- Add small integration tests that assert each tool's success payload matches its schema.
- Add a dedicated test for the Claude-local embedded UI boot path.
- Separate browser-only concerns from pure MCP tool logic so the server is easier to reason about.

## Most likely reason the UI showed nothing in Claude local

Based on the current code, the most likely failure chain is:

1. The iframe tries to import `@modelcontextprotocol/ext-apps` from `https://esm.sh`.
2. That import or `App.connect()` fails in the host environment.
3. The code silently falls back to standalone mode.
4. Standalone mode tries relative HTTP API calls that do not exist in stdio-only Claude local usage.
5. The app ends up with no meaningful recovery path and no visible root-cause message.

That is the first thing I would fix before changing the visual design.
