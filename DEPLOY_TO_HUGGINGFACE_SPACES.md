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

This keeps GitHub history unchanged while letting the Space track a deployment-only branch state.
