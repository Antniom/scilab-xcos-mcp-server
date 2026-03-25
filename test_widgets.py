import asyncio
import os
import json

# Force structural validation mode for testing
os.environ["XCOS_VALIDATION_MODE"] = "subprocess"

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
    
    # Add dummy blocks and an attribute-based link (like Tarefa 10.xcos)
    state.drafts["attr-session"] = DraftDiagram()
    state.drafts["attr-session"].add_blocks('''
    <BasicBlock id="b1" interfaceFunctionName="CONST_m">
      <mxGeometry x="0" y="0" width="40" height="40" as="geometry"/>
      <ExplicitOutputPort id="p1" as="out"/>
    </BasicBlock>
    <BasicBlock id="b2" interfaceFunctionName="CMSCOPE">
      <mxGeometry x="200" y="0" width="40" height="40" as="geometry"/>
      <ExplicitInputPort id="p2" as="in"/>
    </BasicBlock>
    ''')
    state.drafts["attr-session"].add_links('''
    <ExplicitLink id="l1" source="p1" target="p2" parent="0" style="ExplicitLink" />
    ''')
    
    res = await xcos_get_topology_widget("attr-session")
    html = json.loads(res[0].text)["html"]
    
    if "CONST_m &rarr; CMSCOPE" in html:
        print("SUCCESS: Attribute-based link detected in text.")
    else:
        print("FAIL: Attribute-based link NOT detected in text.")
        
    if '<path d="M' in html:
        print("SUCCESS: Attribute-based SVG edge rendered.")
    else:
        print("FAIL: Attribute-based SVG edge NOT rendered.")

    print("\n--- Verifying Bug 3.2 (Structural Validator) ---")
    from server import verify_xcos_xml
    # Test 1: Missing endpoint ID
    bad_xml = '''
    <xcosDiagram>
      <mxGraphModel><root>
        <BasicBlock id="1" interfaceFunctionName="GAIN_f"/>
        <ExplicitLink id="2" source="1" target="999"/>
      </root></mxGraphModel>
    </xcosDiagram>
    '''
    res = await verify_xcos_xml(bad_xml)
    data = json.loads(res[0].text)
    if not data["success"] and "999" in str(data["errors"]):
        print("SUCCESS: Validator caught missing ID error.")
    else:
        print(f"FAIL: Validator did not catch error. Output: {data}")

    # Test 2: Fan-out error
    fanout_xml = '''
    <xcosDiagram>
      <mxGraphModel><root>
        <BasicBlock id="1" interfaceFunctionName="GAIN_f"><ExplicitOutputPort id="p1"/></BasicBlock>
        <BasicBlock id="2" interfaceFunctionName="SINK"/>
        <BasicBlock id="3" interfaceFunctionName="SINK"/>
        <ExplicitLink id="L1" source="p1" target="2"/>
        <ExplicitLink id="L2" source="p1" target="3"/>
      </root></mxGraphModel>
    </xcosDiagram>
    '''
    res = await verify_xcos_xml(fanout_xml)
    data = json.loads(res[0].text)
    if not data["success"] and "fan-out" in str(data["errors"]):
        print("SUCCESS: Validator caught fan-out error.")
    else:
        print(f"FAIL: Validator did not catch fan-out. Output: {data}")

if __name__ == "__main__":
    asyncio.run(verify_fixes())
