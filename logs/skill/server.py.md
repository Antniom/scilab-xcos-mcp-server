### 2026-03-23 13:52:41 UTC — Fix
- **Summary:** Added phased workflow UI, hosted MCP transport, and headless Scilab validation mode.
- The MCP server now exposes workflow creation/review tools, a ui:// dashboard resource, a standalone /workflow-ui browser surface, and a Streamable HTTP MCP endpoint at /mcp. Validation now auto-selects subprocess mode on Linux/Spaces and keeps poll mode available for local Windows.
- **Files:** scilab-xcos-mcp-server/server.py, scilab-xcos-mcp-server/ui/workflow-dashboard.html, scilab-xcos-mcp-server/Dockerfile

### 2026-03-23 17:34:53 UTC — Note
- **Summary:** Reviewed MCP server tools and workflow UI; found embedded UI fallback mismatch in Claude-local mode
- The workflow dashboard imports ext-apps from esm.sh at runtime and falls back to standalone HTTP fetches if MCP app init fails. In local Claude stdio mode that fallback path usually does not exist, so the UI can fail without a visible cause. Also noted tool-surface inconsistencies: unimplemented xcos_revert_phase is exposed, draft/phase workflow semantics conflict, and several tools return text or raw arrays instead of consistent structured payloads.
- **Files:** scilab-xcos-mcp-server/server.py, scilab-xcos-mcp-server/ui/workflow-dashboard.html, scilab-xcos-mcp-server/improvements.md

### 2026-03-30 00:00:00 UTC - Fix
- **Summary:** Added MCP 2025-06-18 `build_xcos_diagram` prompt with server-side argument substitution.
- Registered the prompt in `prompts/list` with `title`, required `problem_statement`, and the full gated workflow instructions. `prompts/get` now validates the argument, replaces `{{problem_statement}}` before returning the messages array, and stdio initialization explicitly preserves `capabilities.prompts.listChanged=false`.
- **Files:** scilab-xcos-mcp-server/server.py, scilab-xcos-mcp-server/test_draft_workflow.py

### 2026-03-30 02:02:47 UTC — Note
- **Summary:** Push-on-write enabled: log_writer now commits and pushes to origin + huggingface after every entry

### 2026-04-01 16:06:23 UTC — Note
- **Summary:** Added XML boundary diagnostics for subprocess validation EOF debugging
- run_headless_scilab_validation now returns memory and disk XML hashes, lengths, tail excerpts, python re-parse status, and the generated verification script path.
- The Scilab verification script now prints fileinfo and last-line metadata before importXcosDiagram so premature EOF can be attributed to Python write vs Scilab import.
- **Files:** server.py, ui/app.js

### 2026-04-01 16:24:00 UTC — Note
- **Summary:** Added automatic poll-worker fallback for remote premature EOF failures
- Hosted HTTP mode now serves the real /workflow-ui assets and reports version 1.0.2.
- When subprocess validation fails with premature EOF, run_verification now retries automatically via a long-lived Scilab poll worker using the existing /task and /result bridge.
- The server also starts the poll worker in the HTTP app lifespan so fallback validation is ready on the remote Spaces deployment.
- **Files:** server.py

### 2026-04-01 16:40:34 UTC — Note
- **Summary:** Poll fallback now returns explicit Scilab verdict fields
- The Scilab poll worker now posts scilab_import_passed, scilab_block_validation_passed, scilab_link_validation_passed, scilab_simulation_passed, substitution metadata, and diary_path.
- run_poll_validation now exposes those fields directly and adds a scilab_verdict message so clients do not have to infer that success=true means import+simulate passed.
- **Files:** server.py, data/xcosai_poll_loop.sci

### 2026-04-02 13:10:41 UTC — Note
- **Summary:** Validation output is now public-by-default and cached
- Added a compact public validation payload for verify_xcos_xml and xcos_verify_draft, gated raw internals behind XCOS_DEBUG_TOOL_OUTPUT, cached validation results by XML hash so xcos_get_validation_widget does not re-run validation after a fresh verify, and made get_xcos_block_data load help/extra examples only on demand to reduce token-heavy responses.
- **Files:** scilab-xcos-mcp-server/server.py

### 2026-04-04 00:00:00 UTC â€” Fix
- **Summary:** Embedded topology assets and suppressed false validation failures from GTK locale noise
- Topology SVG generation now embeds block artwork as data URIs via `resolve_block_image()` instead of referencing `/block_images/...` URLs, which prevents broken image icons in app-rendered widgets.
- Scilab subprocess validation now runs with `LC_ALL/LANG/LANGUAGE=C` and treats exit-code-0 runs that contain only informational `XCOSAI_VERIFY_*` lines plus harmless GTK locale warnings as success instead of `VALIDATION_FAILED`.
- Session validation and commit/file-path payloads now include `/api/sessions/{session_id}/diagram.xcos` so clients can present a direct download link for the verified `.xcos` artifact.
- **Files:** server.py, test_widgets.py, test_draft_workflow.py

### 2026-04-04 15:00:00 UTC â€” Note
- **Summary:** Added strict workflow fidelity contracts, persisted draft context, and validation-time fanout normalization
- Workflows now persist `generation_requirements`, derived context lines, and optional `autopilot`; `xcos_create_workflow` rejects unsupported explicitly named blocks before Phase 2 begins.
- Phase 2 submissions now require a fenced JSON manifest and fail closed when required blocks or context vars are omitted without explicit approved omissions.
- Draft sessions now persist top-level context through `xcos_set_context`, automatically inherit required context lines from the linked workflow, and write them into `<Array as="context" scilabClass="String[]">`.
- Validation now rewrites explicit and event fan-out through synthetic `SPLIT_f` and `CLKSPLIT_f` blocks before final checks, then reports the normalization back in validation payloads.
- **Files:** server.py, test_draft_workflow.py, test_workflow_ui.py
