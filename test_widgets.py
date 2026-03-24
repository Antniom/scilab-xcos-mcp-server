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
    # Add dummy blocks and a link
    state.drafts[session_id].add_blocks('''
    <BasicBlock id="101" interfaceFunctionName="GAIN_f">
      <mxGeometry x="0" y="0" width="40" height="40" as="geometry"/>
      <ExplicitOutputPort id="p1" as="out"/>
    </BasicBlock>
    <BasicBlock id="102" interfaceFunctionName="AFFICH_m">
      <mxGeometry x="200" y="0" width="40" height="40" as="geometry"/>
      <ExplicitInputPort id="p2" as="in"/>
    </BasicBlock>
    ''')
    state.drafts[session_id].add_links('''
    <BasicLink id="103" parent="0">
      <ExplicitOutputPort as="source" reference="p1"/>
      <ExplicitInputPort as="target" reference="p2"/>
    </BasicLink>
    ''')
    
    res = await xcos_get_topology_widget(session_id)
    html = json.loads(res[0].text)["html"]
    
    # ... previous checks ...
    
    if "GAIN_f &rarr; AFFICH_m" in html:
        print("SUCCESS: Link detected and rendered in text.")
    else:
        print("FAIL: Link NOT detected in text.")
        print(f"DEBUG: html snippet: {html[-500:]}")
        
    if '<path d="M' in html and 'stroke="#007bff"' in html:
        print("SUCCESS: SVG edge rendered.")
    else:
        print("FAIL: SVG edge NOT rendered.")

if __name__ == "__main__":
    asyncio.run(verify_fixes())
