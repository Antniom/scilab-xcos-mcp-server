---
title: Scilab Xcos MCP Server
emoji: "⚙️"
colorFrom: gray
colorTo: red
sdk: docker
app_port: 7860
---

# Scilab Xcos MCP Server

Remote-first MCP server for Scilab Xcos engineering on Hugging Face Spaces.

## Endpoints

- Workflow UI: `/workflow-ui`
- MCP endpoint: `/mcp`
- Health check: `/healthz`

## Runtime

The Space runs in hosted mode:

- `XCOS_SERVER_MODE=http`
- `XCOS_VALIDATION_MODE=subprocess`

Validation is performed headlessly by launching Scilab as a subprocess inside the container.

## Deployment

This repository is intended to be deployed as a Docker Space.

See [DEPLOY_TO_HUGGINGFACE_SPACES.md](./DEPLOY_TO_HUGGINGFACE_SPACES.md).

The Docker build downloads the official Scilab `2026.0.1` Linux archive during image build, so the repository does not need to vendor the Scilab distribution.

## Developer Debug Output

Normal users receive compact validation results only.

To expose internal validation diagnostics for developer sessions, set this Space variable:

```text
XCOS_DEBUG_TOOL_OUTPUT=1
```

Then restart the Space.

When enabled, `verify_xcos_xml` and `xcos_verify_draft` include a `debug` object in their payloads.

## Notes

- The repository no longer includes the old local Windows launcher workflow.
- `get_xcos_block_data` is now lightweight by default. Request help and extra examples only when needed.
- Validation widgets reuse cached verification results to avoid duplicate validation calls.
