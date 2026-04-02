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

