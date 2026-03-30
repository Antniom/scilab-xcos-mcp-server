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

