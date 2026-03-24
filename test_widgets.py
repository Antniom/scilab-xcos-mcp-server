import asyncio
import os
import json
from server import xcos_get_block_catalogue_widget, xcos_get_topology_widget, state, DraftDiagram

async def verify_fixes():
    print("--- Verifying Bug 1 (Catalogue) ---")
    res = await xcos_get_block_catalogue_widget()
    html = json.loads(res[0].text)["html"]
    if "No blocks found." in html:
        print("FAIL: Catalogue still empty.")
    else:
        print("SUCCESS: Catalogue populated.")
        if "CMAT3D" in html:
            print("Verified: CMAT3D found in catalogue.")

    print("\n--- Verifying Bug 2 (Topology Interpolation) ---")
    session_id = "test-session"
    state.drafts[session_id] = DraftDiagram()
    # Add a dummy block
    state.drafts[session_id].add_blocks('<BasicBlock id="101" interfaceFunctionName="GAIN_f"><mxGeometry x="0" y="0" width="40" height="40" as="geometry"/></BasicBlock>')
    
    res = await xcos_get_topology_widget(session_id)
    html = json.loads(res[0].text)["html"]
    
    bugs_found = []
    if "{bid}" in html: bugs_found.append("{bid}")
    if "{bdata['name']}" in html: bugs_found.append("{bdata['name']}")
    if "{err_badge}" in html: bugs_found.append("{err_badge}")
    if "{edges_str}" in html: bugs_found.append("{edges_str}")
    if "{nodes_str}" in html: bugs_found.append("{nodes_str}")
    if "{max_x}" in html: bugs_found.append("{max_x}")
    if "{max_y}" in html: bugs_found.append("{max_y}")
    
    if bugs_found:
        print(f"FAIL: Found uninterpolated tags: {', '.join(bugs_found)}")
    else:
        print("SUCCESS: No uninterpolated tags found in topology widget.")
        if "GAIN_f" in html:
            print("Verified: GAIN_f correctly rendered in topology.")

if __name__ == "__main__":
    asyncio.run(verify_fixes())
