# Use This Server Locally

Yes, you can still use this as a local MCP server on your own computer.

There are now two local ways to use it:

## Option 1: Local MCP Server for Claude Desktop

Use this if you want Claude Desktop to call the tools directly on your computer.

### One-time setup
1. Install Scilab on Windows.
2. Open this folder:
   `C:\Users\anton\Desktop\AI xcos module\scilab-xcos-mcp-server`
3. Double-click `init.bat`
4. Wait until it says the server is initialized.

### Claude Desktop setup
Put this in your Claude Desktop MCP config:

```json
{
  "mcpServers": {
    "scilab-xcos": {
      "command": "C:/Users/anton/Desktop/AI xcos module/.venv/Scripts/python.exe",
      "args": [
        "C:/Users/anton/Desktop/AI xcos module/scilab-xcos-mcp-server/server.py"
      ],
      "env": {
        "PYTHONPATH": "C:/Users/anton/Desktop/AI xcos module/scilab-xcos-mcp-server",
        "XCOS_SERVER_MODE": "stdio",
        "XCOS_VALIDATION_MODE": "poll"
      }
    }
  }
}
```

### Each time you want verification to work
1. Start Claude Desktop.
2. Double-click `launch_scilab.bat`
3. Leave that Scilab window open while you use the tools.

That is the classic local setup.

## Option 2: Local Browser UI

Use this if you want to open the phased workflow in your browser on your own computer.

### Start it
1. Double-click `start_local_browser_ui.bat`
2. Open:
   `http://127.0.0.1:7860/workflow-ui`

### What this does
- Starts the web server locally
- Keeps the phased workflow UI available in your browser
- Keeps the MCP HTTP endpoint available at:
  `http://127.0.0.1:7860/mcp`

## Which option should you choose?

- Choose **Option 1** if your main goal is using this as a local MCP server inside Claude Desktop.
- Choose **Option 2** if your main goal is opening the workflow UI in a browser.
- You can use both, but Option 1 is the important one for normal local MCP usage.

## Important difference between local and hosted

- **Local Windows mode** usually uses the existing Scilab polling launcher (`launch_scilab.bat`)
- **Hugging Face Spaces mode** uses headless Scilab in the server container and does not use the polling launcher

So yes: local use is still supported.
