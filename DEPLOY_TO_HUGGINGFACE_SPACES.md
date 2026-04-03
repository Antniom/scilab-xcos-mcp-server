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
