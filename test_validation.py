import json
from lxml import etree
from server import validate_port_sizes, auto_fix_mux_to_scalar

def test_validation():
    xml = """<?xml version="1.0" ?>
<XcosDiagram>
  <mxGraphModel as="model">
    <root>
      <mxCell id="0:1:0"/>
      <mxCell id="0:2:0" parent="0:1:0"/>
      <BasicBlock id="B1" interfaceFunctionName="CONST_f">
        <ExplicitOutputPort id="P1" dataLines="1" dataColumns="1" ordering="1"/>
      </BasicBlock>
      <BasicBlock id="B2" interfaceFunctionName="CANIMXY">
        <ExplicitInputPort id="P2" dataLines="1" dataColumns="1" ordering="1"/>
        <ExplicitInputPort id="P3" dataLines="1" dataColumns="1" ordering="2"/>
      </BasicBlock>
      <BasicLink id="L1" parent="0:2:0">
        <SourcePort reference="P1"/>
        <DestinationPort reference="P2"/>
      </BasicLink>
    </root>
  </mxGraphModel>
</XcosDiagram>
"""
    parser = etree.XMLParser(remove_blank_text=True)
    tree = etree.fromstring(xml.encode('utf-8'), parser)
    
    # 1. Valid case
    errors = validate_port_sizes(tree)
    print(f"Valid case errors: {errors}")
    assert len(errors) == 0

    # 2. Mismatch case
    xml_bad = xml.replace('dataLines="1" dataColumns="1" ordering="1"', 'dataLines="2" dataColumns="1" ordering="1"', 1)
    tree_bad = etree.fromstring(xml_bad.encode('utf-8'), parser)
    errors = validate_port_sizes(tree_bad)
    print(f"Mismatch case errors: {json.dumps(errors, indent=2)}")
    assert len(errors) > 0
    assert any(e['type'] == 'PORT_SIZE_MISMATCH' for e in errors)
    assert any(e['type'] == 'REGISTRY_SIZE_MISMATCH' for e in errors)

def test_autofix():
    xml = """<?xml version="1.0" ?>
<XcosDiagram>
  <mxGraphModel as="model">
    <root>
      <mxCell id="0:1:0"/>
      <mxCell id="0:2:0" parent="0:1:0"/>
      <BasicBlock id="S1" interfaceFunctionName="SineVoltage">
        <ExplicitOutputPort id="P_S1" dataLines="1" dataColumns="1"/>
      </BasicBlock>
      <BasicBlock id="S2" interfaceFunctionName="SineVoltage">
        <ExplicitOutputPort id="P_S2" dataLines="1" dataColumns="1"/>
      </BasicBlock>
      <BasicBlock id="M1" interfaceFunctionName="MUX">
        <ExplicitInputPort id="P_M1_I1" ordering="1"/>
        <ExplicitInputPort id="P_M1_I2" ordering="2"/>
        <ExplicitOutputPort id="P_M1_O" dataLines="2" dataColumns="1"/>
      </BasicBlock>
      <BasicBlock id="D1" interfaceFunctionName="CANIMXY">
        <ExplicitInputPort id="P_D1_I1" ordering="1" dataLines="1" dataColumns="1"/>
        <ExplicitInputPort id="P_D1_I2" ordering="2" dataLines="1" dataColumns="1"/>
      </BasicBlock>
      <BasicLink id="L_S1_M1">
        <SourcePort reference="P_S1"/>
        <DestinationPort reference="P_M1_I1"/>
      </BasicLink>
      <BasicLink id="L_S2_M1">
        <SourcePort reference="P_S2"/>
        <DestinationPort reference="P_M1_I2"/>
      </BasicLink>
      <BasicLink id="L_M1_D1">
        <SourcePort reference="P_M1_O"/>
        <DestinationPort reference="P_D1_I1"/>
      </BasicLink>
    </root>
  </mxGraphModel>
</XcosDiagram>
"""
    parser = etree.XMLParser(remove_blank_text=True)
    tree = etree.fromstring(xml.encode('utf-8'), parser)
    
    fixed = auto_fix_mux_to_scalar(tree)
    print(f"Auto-fixed: {fixed}")
    assert fixed == True
    
    # Check if MUX is gone
    assert not tree.xpath("//BasicBlock[@id='M1']")
    # Check if we have two links to D1
    links_to_d1 = tree.xpath("//BasicLink[DestinationPort[starts-with(@reference, 'P_D1')]]")
    print(f"Links to D1: {len(links_to_d1)}")
    assert len(links_to_d1) == 2
    
    # Check sources
    srcs = [l.xpath("./SourcePort/@reference")[0] for l in links_to_d1]
    assert "P_S1" in srcs
    assert "P_S2" in srcs

if __name__ == "__main__":
    test_validation()
    test_autofix()
    print("All tests passed!")
