# Deploy To Hugging Face Spaces

This repository is designed for a Docker Space.

## How It Works

The container downloads the official Scilab `2026.0.1` Linux binary archive during the Docker build.
The repository does not need to vendor the Scilab distribution.

## Why

- Hugging Face rejects this repository when large bundled binary assets are committed directly.
- Downloading Scilab during image build keeps the Git repository smaller and Space-compatible.
- The server still runs Scilab locally inside the container for subprocess validation.

## Expected Runtime

- `XCOS_SERVER_MODE=http`
- `XCOS_VALIDATION_MODE=subprocess`

Hosted validation defaults:

- `XCOS_SCILAB_SUBPROCESS_TIMEOUT_SECONDS=180`
- `XCOS_POLL_VALIDATION_TIMEOUT_SECONDS=180`
- `XCOS_VALIDATION_JOB_TIMEOUT_SECONDS=240`

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
- runs a remote MCP smoke test against the deployed Space using the pendulum fixture in `pendulo_simples_fiel_raw.xcos`

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
- runs `xcos_verify_draft`
- commits the verified phase when runtime validation succeeds
- fails by default if hosted runtime validation still ends in `SCILAB_RUNTIME_TIMEOUT`
- only allows degraded structural-only success when you explicitly pass `--allow-degraded-runtime`
- checks that the session file is available when validation succeeds

This avoids the large single-payload `verify_xcos_xml` transport failure seen on the Space by using chunked draft assembly instead.

Useful flags:

```powershell
.\tools\deploy_huggingface_clean.ps1 -SkipRemoteSmokeTest
.\tools\deploy_huggingface_clean.ps1 -SmokeTestMcpUrl "https://<space>.hf.space/mcp"
.\tools\deploy_huggingface_clean.ps1 -SmokeTestFixturePath "C:\path\to\diagram.xcos"
.\tools\deploy_huggingface_clean.ps1 -SmokeTestDelaySeconds 300
.\.venv\Scripts\python.exe .\tools\remote_hf_smoke_test.py --allow-degraded-runtime
```
