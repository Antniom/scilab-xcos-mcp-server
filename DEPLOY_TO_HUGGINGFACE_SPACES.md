# Deploy To Hugging Face Spaces

This repository is designed for a Docker Space.

## How It Works

The container downloads the official Scilab `2026.0.1` Linux binary archive during the Docker build.
The repository does not need to vendor the Scilab distribution.

Base image pinning:

- Both Dockerfiles now accept `ARG PYTHON_BASE_IMAGE`.
- Default remains `python:3.11-slim-bookworm`.
- For stronger reproducibility, set `PYTHON_BASE_IMAGE` to a digest-pinned image (for example, `python:3.11-slim-bookworm@sha256:...`) in your Space build configuration.

## Why

- Hugging Face rejects this repository when large bundled binary assets are committed directly.
- Downloading Scilab during image build keeps the Git repository smaller and Space-compatible.
- The server still runs Scilab locally inside the container for subprocess validation.

## Expected Runtime

- `XCOS_SERVER_MODE=http`
- `XCOS_VALIDATION_MODE=subprocess`

Hosted validation defaults:

- `XCOS_SCILAB_SUBPROCESS_TIMEOUT_SECONDS=180`
- `XCOS_POLL_VALIDATION_TIMEOUT_SECONDS=420`
- `XCOS_VALIDATION_JOB_TIMEOUT_SECONDS=720`
- `XCOS_PREFLIGHT_ENABLED=1`
- `XCOS_PREFLIGHT_STRICT=0`
- `XCOS_PREFLIGHT_TIMEOUT_SECONDS=45`
- `XCOS_VALIDATION_WORKER_URL=` optional full-runtime worker Space base URL
- `XCOS_VALIDATION_WORKER_TOKEN=` optional bearer token shared with the worker Space
- `XCOS_VALIDATION_WORKER_REQUEST_RETRY_COUNT=3` transient HTTP retry budget for remote worker create/poll requests
- `XCOS_VALIDATION_WORKER_RETRY_BACKOFF_SECONDS=1.0` base exponential backoff for transient remote worker request failures

Startup preflight notes:

- On startup, the server now performs a Scilab preflight smoke check in subprocess mode.
- The check verifies Scilab binary resolution, Linux headless dependencies (`xvfb-run`, `xauth`), and runs a minimal `loadXcosLibs()` probe.
- Results are exposed in `/healthz` under `startup_preflight` and timeout settings under `runtime_timeouts`.
- Keep `XCOS_PREFLIGHT_STRICT=0` on constrained hosted environments to avoid hard fail on transient startup hiccups.

Validation profiles:

- `full_runtime`
  - structural validation plus full Scilab simulation
  - intended for manual or on-demand verification
  - can be offloaded to a separate validation worker Space when `XCOS_VALIDATION_WORKER_URL` is configured
- `hosted_smoke`
  - structural validation plus Scilab load/import checks only
  - intended for Hugging Face `cpu-basic` deployment smoke tests
  - does not run `scicos_simulate(...)`

## Local Notes

Local Windows development can still use `.scilab_path` or `SCILAB_BIN`.
That path is only for local runs and is not required in Hugging Face Spaces.

## Recommended Deploy Command

Use the clean deployment script instead of pushing the repo directly to the Space:

```powershell
.\tools\deploy_huggingface_clean.ps1
```

What it does:

- creates a temporary git worktree from the current `HEAD`
- builds an orphan deployment branch
- removes tracked binary assets that Hugging Face Spaces rejects
- force-pushes the clean snapshot to `huggingface/main`
- waits 210 seconds for the Space rebuild
- runs a remote MCP smoke test against the deployed Space using `--validation-profile hosted_smoke` and the pendulum fixture in `pendulo_simples_fiel_raw.xcos`

This keeps GitHub history unchanged while letting the Space track a deployment-only branch state.

## Remote Validation

After the Hugging Face push, the deploy script waits 210 seconds for the Space rebuild,
then runs:

```powershell
.\.venv\Scripts\python.exe .\tools\remote_hf_smoke_test.py
```

That smoke test:

- connects to the deployed MCP endpoint over streamable HTTP
- creates and approves the 3-phase pendulum workflow
- starts a draft session
- loads the pendulum `.xcos` fixture in chunked block and link batches
- runs `xcos_verify_draft(validation_profile="hosted_smoke")`
- commits the verified phase when hosted-smoke validation succeeds
- fails if structural validation or Scilab import/load fails
- checks that the session file is available when validation succeeds

This avoids the large single-payload `verify_xcos_xml` transport failure seen on the Space by using chunked draft assembly instead.

Useful flags:

```powershell
.\tools\deploy_huggingface_clean.ps1 -SkipRemoteSmokeTest
.\tools\deploy_huggingface_clean.ps1 -SmokeTestMcpUrl "https://<space>.hf.space/mcp"
.\tools\deploy_huggingface_clean.ps1 -SmokeTestFixturePath "C:\path\to\diagram.xcos"
.\tools\deploy_huggingface_clean.ps1 -SmokeTestDelaySeconds 300
.\.venv\Scripts\python.exe .\tools\remote_hf_smoke_test.py --validation-profile hosted_smoke
.\.venv\Scripts\python.exe .\tools\remote_hf_smoke_test.py --validation-profile full_runtime
```

## Optional Validation Worker Space

If you want to isolate `full_runtime` simulation from the MCP server, deploy a second Hugging Face Docker Space using `Dockerfile.validation-worker`.

Recommended MCP Space variables:

```text
XCOS_VALIDATION_WORKER_URL=https://<worker-space>.hf.space
XCOS_VALIDATION_WORKER_TOKEN=<shared-secret>
```

Recommended worker Space variables:

```text
XCOS_SERVER_ROLE=validation_worker
XCOS_VALIDATION_WORKER_TOKEN=<shared-secret>
XCOS_VALIDATION_MODE=subprocess
XCOS_DEBUG_TOOL_OUTPUT=1
```

Worker deployment helper:

```powershell
.\tools\deploy_huggingface_validation_worker.ps1 -Remote huggingface-worker -HealthcheckUrl "https://<worker-space>.hf.space/healthz"
```

With that split:

- MCP `hosted_smoke` stays local and remains the deploy gate
- MCP `full_runtime` calls the worker over HTTP and polls `/jobs/{job_id}`
- the worker runs the same Scilab validation code without recursively offloading again

## Troubleshooting Runbook (worker offload)

Quick triage fields:

- `/healthz` -> `runtime_timeouts`
- validation payload -> `bucket`, `code`
- remote offload result -> `remote_worker.create_retry_count`, `remote_worker.poll_transient_errors`

Common signatures and actions:

- `bucket=runtime_timeout` with `origin` from Scilab runtime paths
  - Increase `XCOS_SCILAB_SUBPROCESS_TIMEOUT_SECONDS` and/or worker job timeout
  - Keep `hosted_smoke` as deploy gate and reserve `full_runtime` for worker

- `origin=validation-worker-remote` and message like "Failed to create remote validation job..."
  - Check worker URL/token first
  - Increase `XCOS_VALIDATION_WORKER_REQUEST_RETRY_COUNT` (for transient network/load spikes)
  - Increase `XCOS_VALIDATION_WORKER_RETRY_BACKOFF_SECONDS` (to reduce retry pressure)

- High `remote_worker.poll_transient_errors`
  - Indicates unstable or overloaded worker/network path
  - Raise backoff and/or reduce worker load
  - Verify worker `/healthz` and Space cold-start behavior

- `bucket=import` on `hosted_smoke`
  - Focus on Scilab load/import compatibility (`loadXcosLibs`, diagram import), not simulation runtime
