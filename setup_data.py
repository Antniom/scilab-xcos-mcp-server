import os
import shutil
import sys
import argparse

def setup_data(scilab_base, workspace_base, target_data):
    """
    scilab_base: path to scilab root folder (e.g. scilab-2026.0.1/scilab-2026.0.1/scilab)
    workspace_base: path to the AI xcos module root
    target_data: target directory (./data)
    """
    
    # Define mapping (Source -> Target)
    mappings = {
        # Reference blocks
        os.path.join(workspace_base, "Reference blocks"): os.path.join(target_data, "reference_blocks"),
        # Block info (JSON)
        os.path.join(workspace_base, "xcosgen", "server", "blocks"): os.path.join(target_data, "blocks"),
        # Macros
        os.path.join(scilab_base, "modules", "scicos_blocks", "macros"): os.path.join(target_data, "macros"),
        # Help
        os.path.join(scilab_base, "modules", "xcos", "help", "en_US", "palettes"): os.path.join(target_data, "help"),
    }

    # Added: xcosai_poll_loop.sci copy for launch script
    poll_loop_src = os.path.join(workspace_base, "XcosAICompiler", "macros", "xcosai_poll_loop.sci")
    poll_loop_dest = os.path.join(target_data, "xcosai_poll_loop.sci")

    for src, dest in mappings.items():
        if os.path.exists(src):
            print(f"Staging {src} -> {dest}")
            if os.path.exists(dest):
                shutil.rmtree(dest)
            shutil.copytree(src, dest)
        else:
            print(f"WARNING: Source not found: {src}")

    if os.path.exists(poll_loop_src):
        print(f"Staging poll loop -> {poll_loop_dest}")
        shutil.copy2(poll_loop_src, poll_loop_dest)
    else:
        print(f"WARNING: Poll loop source not found: {poll_loop_src}")

    print("Data staging complete.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stage data for Scilab Xcos MCP Server")
    parser.add_argument("--scilab", required=True, help="Path to Scilab source root")
    parser.add_argument("--workspace", required=True, help="Path to workspace root")
    parser.add_argument("--target", default="data", help="Target data directory")
    
    args = parser.parse_args()
    setup_data(args.scilab, args.workspace, args.target)
