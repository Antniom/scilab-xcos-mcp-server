import os
import json
import glob

DATA_DIR = r"c:\Users\anton\Desktop\AI xcos module\scilab-xcos-mcp-server\data"
BLOCKS_DIR = os.path.join(DATA_DIR, "blocks")
MACROS_DIR = os.path.join(DATA_DIR, "macros")

def update_source_file(json_path):
    with open(json_path, 'r', encoding='utf-8') as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            print(f"Failed to parse {json_path}")
            return

    if "sourceFile" in data:
        old_path = data["sourceFile"]
        # Standardize path separators
        old_path = old_path.replace("\\", "/")
        
        # We want to find the file in our local MACROS_DIR
        filename = os.path.basename(old_path)
        
        # Search for the file in MACROS_DIR
        found = False
        for root, dirs, files in os.walk(MACROS_DIR):
            if filename in files:
                rel_path = os.path.relpath(os.path.join(root, filename), os.path.dirname(DATA_DIR))
                data["sourceFile"] = rel_path.replace("\\", "/")
                found = True
                break
        
        if not found:
            print(f"Could not find local macro for {filename} (referenced in {json_path})")
            # If not found, we'll keep it as is or mark it
            return

    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)

def main():
    json_files = glob.glob(os.path.join(BLOCKS_DIR, "*.json"))
    for jf in json_files:
        update_source_file(jf)
    print(f"Updated {len(json_files)} block metadata files.")

if __name__ == "__main__":
    main()
