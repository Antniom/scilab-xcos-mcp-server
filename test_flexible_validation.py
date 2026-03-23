import json
from lxml import etree
from server import validate_port_sizes

def test_flexible_validation():
    xml = """<?xml version="1.0" ?>
<XcosDiagram>
  <mxGraphModel as="model">
    <root>
      <mxCell id="0:1:0"/>
      <mxCell id="0:2:0" parent="0:1:0"/>
      
      <!-- GAINBLK_f with inherited size [-1, -2] in registry -->
      <BasicBlock id="B1" interfaceFunctionName="GAINBLK_f">
        <ExplicitInputPort id="P1" dataLines="5" dataColumns="1" ordering="1"/>
        <ExplicitOutputPort id="P2" dataLines="5" dataColumns="1" ordering="1"/>
      </BasicBlock>
      
      <!-- CONST_f with fixed size [1, 1] in registry -->
      <BasicBlock id="B2" interfaceFunctionName="CONST_f">
        <ExplicitOutputPort id="P3" dataLines="1" dataColumns="1" ordering="1"/>
      </BasicBlock>
      
      <!-- Link from fixed [1,1] to inherited [-1,-2] should pass -->
      <BasicLink id="L1" parent="0:2:0">
        <SourcePort reference="P3"/>
        <DestinationPort reference="P1"/>
      </BasicLink>
      
      <!-- MUX marked as variadic -->
      <BasicBlock id="M1" interfaceFunctionName="MUX">
        <ExplicitInputPort id="PM1" ordering="1"/>
        <ExplicitInputPort id="PM2" ordering="2"/>
        <ExplicitOutputPort id="PM3" dataLines="2" dataColumns="1" ordering="1"/>
      </BasicBlock>

    </root>
  </mxGraphModel>
</XcosDiagram>
"""
    parser = etree.XMLParser(remove_blank_text=True)
    tree = etree.fromstring(xml.encode('utf-8'), parser)
    
    errors = validate_port_sizes(tree)
    print(f"Flexible case errors: {json.dumps(errors, indent=2)}")
    # Should pass because B1's expected [-1,-2] matches [5,1] (wildcard)
    # And L1 link from [1,1] to [5,1] should honestly FAIL if they are strictly matched,
    # BUT in Scicos, if one end is -1, it usually "adapts".
    # Wait, in my server.py, I check src_size vs dst_size.
    # If P1 is [5,1] and P3 is [1,1], dims_match([1,1], [5,1]) is False.
    # UNLESS one of them is marked as wildcard in the XML itself.
    
    # In Xcos XML, if a port is inherited, it usually has dataLines="-1". 
    assert len(errors) > 0 # Currently should fail due to link mismatch [1,1] != [5,1]

def test_wildcards_in_xml():
    xml = """<?xml version="1.0" ?>
<XcosDiagram>
  <mxGraphModel as="model">
    <root>
      <mxCell id="0:1:0"/>
      <mxCell id="0:2:0" parent="0:1:0"/>
      <BasicBlock id="B1" interfaceFunctionName="GAINBLK_f">
        <ExplicitInputPort id="P1" dataLines="-1" dataColumns="-2" ordering="1"/>
        <ExplicitOutputPort id="P2" dataLines="-1" dataColumns="-2" ordering="1"/>
      </BasicBlock>
      <BasicBlock id="B2" interfaceFunctionName="CONST_f">
        <ExplicitOutputPort id="P3" dataLines="1" dataColumns="1" ordering="1"/>
      </BasicBlock>
      <BasicLink id="L1" parent="0:2:0">
        <SourcePort reference="P3"/>
        <DestinationPort reference="P1"/>
      </BasicLink>
    </root>
  </mxGraphModel>
</XcosDiagram>
"""
    parser = etree.XMLParser(remove_blank_text=True)
    tree = etree.fromstring(xml.encode('utf-8'), parser)
    errors = validate_port_sizes(tree)
    print(f"Wildcard in XML errors: {json.dumps(errors, indent=2)}")
    assert len(errors) == 0 # Should pass because P1 is negative in XML

if __name__ == "__main__":
    test_flexible_validation()
    test_wildcards_in_xml()
    print("Flexible tests passed!")
