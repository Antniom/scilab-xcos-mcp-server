---
title: Scilab Xcos MCP Server
emoji: "gear"
colorFrom: orange
colorTo: red
sdk: docker
app_port: 7860
---

# Scilab Xcos MCP Server

A stateless, self-contained Python MCP server for Scilab Xcos engineering.

## Features
- **Dual-Server Design**: MCP stdio server for LLM tools + HTTP polling bridge (port 8000).
- **Automated Initialization**: `init.bat` auto-discovers Scilab and stages data.
- **Connection Telemetry**: Real-time CLI status (CONNECTED/DISCONNECTED) for Scilab connection.
- **Automated Polling**: `launch_scilab.bat` boots Scilab and starts the verification loop.
- **Phased Workflow UI**: review Phase 1, Phase 2, and draft handoff status at `/workflow-ui`.
- **Hosted MCP Endpoint**: Streamable HTTP MCP transport at `/mcp` for remote clients and MCP Apps-capable hosts.
- **Headless Validation Mode**: Docker/Spaces can validate `.xcos` diagrams by launching `scilab-cli` as a subprocess.

## Setup Instructions

### 1. Initialization
Run the initialization script to discover your Scilab installation and stage block data:
```batch
init.bat
```

### 2. Configure Claude Desktop
Add the following to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "scilab-xcos": {
      "command": "c:/Users/anton/Desktop/AI xcos module/.venv/Scripts/python.exe",
      "args": [
        "c:/Users/anton/Desktop/AI xcos module/scilab-xcos-mcp-server/server.py"
      ],
      "env": {
        "PYTHONPATH": "c:/Users/anton/Desktop/AI xcos module/scilab-xcos-mcp-server"
      }
    }
  }
}
```

### 3. Usage
1. Start the MCP server (it starts inside Claude Desktop automatically).
2. Run the Scilab polling launcher to enable verification:
```batch
launch_scilab.bat
```
3. Look for the `[CONNECTED] Scilab Connected` status in the terminal.

## Simple Guides

If you do not want to deal with code, start here:

- [DEPLOY_TO_HUGGINGFACE_SPACES.md](C:\Users\anton\Desktop\AI xcos module\scilab-xcos-mcp-server\DEPLOY_TO_HUGGINGFACE_SPACES.md)
- [USE_LOCALLY.md](C:\Users\anton\Desktop\AI xcos module\scilab-xcos-mcp-server\USE_LOCALLY.md)

Helper launchers:

- `start_local_browser_ui.bat`: open the workflow UI locally in your browser
- `start_spaces_mode_locally.bat`: test the Hugging Face-style headless mode on your own computer
- `launch_scilab.bat`: start the classic Windows Scilab verification bridge

## Available Tools
- `xcos_open_workflow_ui`: open the phased workflow dashboard UI.
- `xcos_create_workflow`, `xcos_list_workflows`, `xcos_get_workflow`: manage approval-gated workflow sessions.
- `xcos_submit_phase`, `xcos_review_phase`: submit and approve/reject Phase 1 and Phase 2 artifacts.
- `get_xcos_block_data`: Get block annotation JSON, reference XML, extra examples, and help text.
- `get_xcos_block_source`: Read Scilab `.sci` macro.
- `search_related_xcos_files`: Search files in `./data/`.
- `verify_xcos_xml`: Send XML to Scilab for simulation validation.
- `xcos_start_draft`, `xcos_add_blocks`, `xcos_add_links`, `xcos_verify_draft`: Incremental draft workflow.
- `xcos_get_draft_xml`: Inspect the accumulated draft XML, with optional pretty-printing and comment stripping.
- `xcos_get_file_path`: Return the saved session file path and latest verified temp file metadata.
- `xcos_get_file_content`: Return the draft/session/verified `.xcos` content as text or base64.
- `xcos_list_sessions`: List active sessions with counts and last-verification status.

## Output Path Configuration

The server writes verification temp files and saved session snapshots to configurable directories:

- `XCOS_TEMP_OUTPUT_DIR`: override the temp verification directory (defaults to `data/temp`)
- `XCOS_SESSION_OUTPUT_DIR`: override the per-session snapshot directory (defaults to `sessions/`)

## Runtime Modes

- `XCOS_SERVER_MODE=both`: run stdio MCP plus the HTTP server.
- `XCOS_SERVER_MODE=http`: run only the hosted HTTP server. This is the expected Hugging Face Spaces mode.
- `XCOS_SERVER_MODE=stdio`: run only stdio MCP.

## Validation Modes

- `XCOS_VALIDATION_MODE=poll`: use the legacy Scilab polling bridge via `/task` and `/result`.
- `XCOS_VALIDATION_MODE=subprocess`: write a temporary `.xcos`, launch `scilab-cli`, import the diagram, and run `scicos_simulate(..., "nw")`.
- If `XCOS_VALIDATION_MODE` is unset, Linux defaults to `subprocess` and Windows defaults to `poll` unless `SCILAB_BIN` is set.

## Hugging Face Spaces

This folder is Docker-ready for a free-tier Docker Space:

- Root app URL: `/workflow-ui`
- MCP endpoint: `/mcp`
- Health check: `/healthz`

The Docker image installs Scilab and starts the server in `http` + `subprocess` mode. This avoids the Windows-only polling launcher and is the recommended hosted path.

## Troubleshooting & Best Practices

### 1. CMSCOPE Parameter Widths
When defining `realParameters` for a `CMSCOPE` block, ensure the width matches the number of channels:
- **1-channel**: `width=5` -> `[t_start, t_end, win_size, ymin, ymax]`
- **2-channel**: `width=7` -> `[t_start, t_end, win_size, ymin1, ymax1, ymin2, ymax2]`

### 2. Graphical Block Validation
- **Block Substitution**: Blocks that use `SCILAB` macros (like `BARXY`) are automatically substituted with a no-op function during validation to prevent hangs or crashes in headless mode. 
- **Warnings**: If substitution occurs, a `[WARN]` message will appear in the validation result to indicate that graphical behavior was skipped.

### 3. Simulation Time for Validation
To ensure rapid feedback, the validator overrides the diagram's `finalIntegrationTime` to **0.1s** during the `verify_xcos_xml` tool call. This is sufficient to catch structural and parameter errors without waiting for a long simulation to complete.
