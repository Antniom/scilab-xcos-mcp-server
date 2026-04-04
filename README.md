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

Hosted timeout defaults:

- subprocess validation: `180s`
- poll fallback validation: `420s`
- async validation jobs: `720s`

Optional overrides:

```text
XCOS_SCILAB_SUBPROCESS_TIMEOUT_SECONDS=180
XCOS_POLL_VALIDATION_TIMEOUT_SECONDS=420
XCOS_VALIDATION_JOB_TIMEOUT_SECONDS=720
```

## Deployment

This repository is intended to be deployed as a Docker Space.

See [DEPLOY_TO_HUGGINGFACE_SPACES.md](./DEPLOY_TO_HUGGINGFACE_SPACES.md).

The Docker build downloads the official Scilab `2026.0.1` Linux archive during image build, so the repository does not need to vendor the Scilab distribution.

## Cross-Platform MCP App Metadata

The server now publishes MCP App metadata intended to work in both Claude and ChatGPT from the same `/mcp` endpoint:

- Widget tools return compact `structuredContent` plus full widget payloads in `_meta.widget`
- Widget tools advertise both `_meta.ui.resourceUri` and `openai/outputTemplate`
- The workflow UI resource publishes `_meta.ui.csp` and `_meta.ui.domain`

Optional environment variables for marketplace deployment:

```text
XCOS_PUBLIC_BASE_URL=https://your-host.example
XCOS_PUBLIC_MCP_URL=https://your-host.example/mcp
XCOS_UI_RESOURCE_DOMAINS=https://esm.sh
XCOS_UI_CONNECT_DOMAINS=
XCOS_UI_FRAME_DOMAINS=
XCOS_UI_BASE_URI_DOMAINS=
```

Notes:

- `XCOS_PUBLIC_MCP_URL` takes precedence when computing the Claude app domain hash
- If unset, the server derives the public MCP URL from `XCOS_PUBLIC_BASE_URL` plus `/mcp`
- `XCOS_UI_RESOURCE_DOMAINS` defaults to `https://esm.sh` because the UI bridge client is currently loaded from that origin

## Developer Debug Output

Normal users receive compact validation results only.

To expose internal validation diagnostics for developer sessions, set this Space variable:

```text
XCOS_DEBUG_TOOL_OUTPUT=1
```

Then restart the Space.

When enabled, `verify_xcos_xml` and `xcos_verify_draft` include a `debug` object in their payloads.

## Validation Profiles

Draft validation now supports two explicit profiles:

- `full_runtime`
  - default for `xcos_start_validation` and `xcos_verify_draft`
  - runs structural validation plus full Scilab simulation
  - may use the long-lived poll-worker fallback on eligible runtime failures
- `hosted_smoke`
  - intended for Hugging Face `cpu-basic` deploy checks
  - runs structural validation plus Scilab load/import checks
  - does not call `scicos_simulate(...)`
  - does not use the poll-worker fallback

Successful validation payloads include `validation_profile` so callers can tell whether a result came from deploy-safe import validation or full simulation.

## Remote Smoke Test

`tools/remote_hf_smoke_test.py` now defaults to:

- `--validation-profile hosted_smoke`
- strict success only; there is no degraded-timeout success mode
- a client-side wait budget of `900s`

Use `--validation-profile full_runtime` only for manual diagnostics when you explicitly want the live Space to attempt full simulation.

## Notes

- The repository no longer includes the old local Windows launcher workflow.
- `get_xcos_block_data` is now lightweight by default. Request help and extra examples only when needed.
- Validation widgets reuse cached verification results to avoid duplicate validation calls.
