# Deploy To Hugging Face Spaces

This project is already prepared to run as a **Docker Space**.

## What you need

1. A Hugging Face account
2. A GitHub account
3. This folder: `scilab-xcos-mcp-server`

## Important

- Hugging Face Space, not GitHub Space.
- Use a **Docker Space**.
- The app will open at `/workflow-ui`.
- The remote MCP endpoint will be `/mcp`.

## Easiest path

### 1. Put this folder in its own GitHub repository

Create a new GitHub repo, for example:

- Name: `scilab-xcos-mcp-server`
- Visibility: private or public, your choice

Then upload the contents of this folder into that repo:

- `server.py`
- `Dockerfile`
- `README.md`
- `pyproject.toml`
- `data/`
- `ui/`
- and the rest of the files in this folder

Important:

- Upload the **contents** of `scilab-xcos-mcp-server`
- Do **not** upload the parent folder around it

### 2. Create the Space

In Hugging Face:

1. Click `New Space`
2. Choose a name
3. Choose `Docker` as the SDK
4. Choose free CPU hardware
5. Create the Space

### 3. Connect the Space to GitHub

In the Space settings:

1. Find the GitHub integration / repository sync option
2. Connect the GitHub repo you created
3. Let Hugging Face pull the repo

Because this project already has:

- a `Dockerfile`
- Hugging Face YAML front matter at the top of `README.md`

the Space should start building automatically.

## What happens during build

The Docker image will:

1. install Python
2. install Scilab
3. install the Python package in this folder
4. start the server in hosted mode

Hosted mode means:

- HTTP server enabled
- MCP endpoint enabled
- headless Scilab subprocess validation enabled

## After the Space finishes building

Open these URLs:

- Main UI: `https://YOUR-SPACE.hf.space/workflow-ui`
- Health check: `https://YOUR-SPACE.hf.space/healthz`
- MCP endpoint: `https://YOUR-SPACE.hf.space/mcp`

Expected result:

- `/workflow-ui` shows the workflow dashboard
- `/healthz` returns JSON

## If build fails

Open the Space build logs and look for:

- package install failure
- Scilab install failure
- Python dependency failure

Most likely cause:

- Hugging Face could not install the Debian `scilab` package in that build image

If that happens, send me the build log and I can adapt the Dockerfile.

## How to connect it as a remote MCP server

That depends on the client:

- some clients want a remote MCP URL
- some MCP Apps-capable hosts can use the `/mcp` endpoint directly

If your client asks for the server URL, use:

`https://YOUR-SPACE.hf.space/mcp`

## Very short version

1. Create GitHub repo
2. Upload the contents of this folder
3. Create Hugging Face Docker Space
4. Connect Space to GitHub repo
5. Wait for build
6. Open `/workflow-ui`
7. Use `/mcp` as the remote MCP endpoint
